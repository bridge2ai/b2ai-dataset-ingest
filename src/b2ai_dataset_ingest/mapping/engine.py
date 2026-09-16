"""Apply a YAML mapping config to source rows to produce IR fragments.

The engine is the reusable core shared across datasets: given a table's mapping config it
turns one source row into IR objects. Two table shapes are supported in v1:

- **Demographics** (``produces: Individual`` with a ``columns`` block) -> a dict of
  :class:`~b2ai_dataset_ingest.model.core.Individual` field values, via
  :meth:`MappingEngine.individual_fields`.
- **Questionnaire** (``items`` and/or ``score``) -> a list of
  :class:`~b2ai_dataset_ingest.model.core.MeasurementObservation`, via
  :meth:`MappingEngine.measurements`.

Ordinal answers become ``Quantity(value=<int>, unit=UCUM {score})`` (per the confirmed
plan). The integer for an answer is resolved encoding-agnostically: an answer cell may hold
the choice *label* ("Several days"), the choice *code* ("almostNever"), or a numeric code
("1" / "3.0"), and :meth:`MappingEngine.ordinal_value` accepts all three. The resolution
index is built from the companion ReproSchema data dict's per-item ``choices`` when
available, falling back to the config's ``ordinal_scale`` (used by fixtures that ship no
data dict). Diagnosis is basename-driven and handled by the reader, not here.

Logging is PHI-safe: warnings identify the *table.column* that failed to resolve, never the
raw cell value (which on real data may be participant-identifying). Aggregate counts are
recorded on the optional :class:`~b2ai_dataset_ingest.reporting.IngestReport`.
"""

from __future__ import annotations

import logging
from typing import Any

from b2ai_dataset_ingest.mapping.loaders import is_placeholder
from b2ai_dataset_ingest.model import (
    MeasurementObservation,
    OntologyTerm,
    Quantity,
    TimePoint,
)
from b2ai_dataset_ingest.reporting import IngestReport

logger = logging.getLogger(__name__)

#: UCUM unit for unitless questionnaire scores. ``Quantity.unit`` is REQUIRED by the
#: phenopacket schema, so every numeric measurement carries this even when "unitless".
SCORE_UNIT = OntologyTerm(id="UCUM:{score}", label="score")


def _as_int(value: Any) -> int | None:
    """Parse an integer from ``"3"`` / ``"3.0"`` / ``3``; None if not an integer value."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else None


class MappingEngine:
    def __init__(self, mapping: dict[str, Any]) -> None:
        self.mapping = mapping

    # -- generic entry point (used by tests / callers that don't know the table shape) --
    def apply(
        self, row: dict[str, Any], data_dict: dict[str, Any] | None = None
    ) -> list[Any]:
        """Translate one row into IR fragments, dispatching on the mapping's shape."""
        if "items" in self.mapping or "score" in self.mapping:
            return self.measurements(row, data_dict)
        if self.mapping.get("produces") == "Individual" or "columns" in self.mapping:
            return [self.individual_fields(row)]
        return []

    # -- demographics -> Individual field values --
    def individual_fields(
        self, row: dict[str, Any], report: IngestReport | None = None
    ) -> dict[str, Any]:
        """Return Individual field values (e.g. ``sex``, ``age_iso8601``) for one row.

        ``Individual.id`` is set by the reader from ``participant_id``, not here. Columns
        whose value is blank, or that recode/transform to nothing, are omitted.
        """
        table = self.mapping.get("table", "?")
        fields: dict[str, Any] = {}
        for column, spec in (self.mapping.get("columns") or {}).items():
            raw = (row.get(column) or "").strip()
            if not raw:
                continue
            target = spec.get("target", "")
            if not target.startswith("Individual."):
                continue
            field = target.split(".", 1)[1]
            value = self._recode(raw, spec, table=table, column=column, report=report)
            if value is not None:
                fields[field] = value
        return fields

    def _recode(
        self,
        raw: str,
        spec: dict[str, Any],
        table: str = "?",
        column: str = "?",
        report: IngestReport | None = None,
    ) -> Any:
        """Apply a column spec's ``value_map`` then ``transform`` to a raw cell value."""
        value: Any = raw
        value_map = spec.get("value_map")
        if value_map:
            if raw in value_map:
                value = value_map[raw]
            else:
                # tolerate case / code-vs-label differences from the source vocabulary
                lowered = {str(k).lower(): v for k, v in value_map.items()}
                if raw.lower() in lowered:
                    value = lowered[raw.lower()]
                else:
                    # PHI-safe: name the column, never the (potentially identifying) value.
                    logger.warning("%s: no value_map entry for column %s; dropping", table, column)
                    if report is not None:
                        report.note_value_map_miss(table, column)
                    return None
        transform = spec.get("transform")
        if transform:
            value = self._transform(transform, value)
        return value

    @staticmethod
    def _transform(name: str, value: Any) -> Any:
        if name == "years_to_iso8601":
            return MappingEngine.years_to_iso8601(value)
        logger.warning("unknown transform %r; passing value through", name)
        return value

    @staticmethod
    def years_to_iso8601(value: Any) -> str | None:
        """``"63"`` / ``"54.0"`` -> ``"P63Y"`` / ``"P54Y"``; None if not a number."""
        try:
            years = int(float(str(value).strip()))
        except (TypeError, ValueError):
            # PHI-safe: never interpolate the raw age cell (a free-text HIPAA quasi-identifier).
            logger.warning("age value is not numeric; leaving age unset")
            return None
        return f"P{years}Y"

    # -- questionnaire -> Measurements --
    def measurements(
        self,
        row: dict[str, Any],
        data_dict: dict[str, Any] | None = None,
        time: TimePoint | None = None,
        report: IngestReport | None = None,
    ) -> list[MeasurementObservation]:
        """Return per-item ordinal Measurements plus a precomputed total, if configured."""
        out: list[MeasurementObservation] = []
        table = self.mapping.get("table", "?")

        for column, assay in (self.mapping.get("items") or {}).items():
            raw = (row.get(column) or "").strip()
            if not raw:
                continue
            # A placeholder assay must never reach an emitter: `OntologyClass(id="TODO")`
            # serializes into the packet and MetaData then has no Resource to declare for
            # it. The diagnosis path already skips placeholders; this is the same rule for
            # questionnaire/measure items.
            if is_placeholder(assay):
                logger.warning(
                    "%s: item %s has an unresolved ontology term; skipping", table, column
                )
                if report is not None:
                    report.note_placeholder_skipped(table, column)
                continue
            ordinal = self.ordinal_value(column, raw, data_dict)
            if ordinal is None:
                # PHI-safe: identify the column, never the raw answer value.
                logger.warning(
                    "questionnaire %s: unresolved answer for column %s; skipping item",
                    table,
                    column,
                )
                if report is not None:
                    report.note_unresolved_item(table, column)
                continue
            out.append(
                MeasurementObservation(
                    assay=OntologyTerm(**assay),
                    value_quantity=Quantity(value=float(ordinal), unit=SCORE_UNIT),
                    time=time,
                )
            )

        score = self.mapping.get("score")
        if isinstance(score, dict) and is_placeholder(score.get("assay")):
            logger.warning("%s: score assay is unresolved; skipping total", table)
            if report is not None:
                report.note_placeholder_skipped(table, "score")
        elif isinstance(score, dict):
            column = score.get("source_column")
            raw = (row.get(column) or "").strip() if column else ""
            if raw:
                total = self._as_number(raw)
                if total is not None:
                    unit = OntologyTerm(**score["unit"]) if score.get("unit") else SCORE_UNIT
                    out.append(
                        MeasurementObservation(
                            assay=OntologyTerm(**score["assay"]),
                            value_quantity=Quantity(value=total, unit=unit),
                            time=time,
                        )
                    )
                else:
                    logger.warning(
                        "questionnaire %s: total in column %s is not numeric; skipping",
                        table,
                        column,
                    )
                    if report is not None:
                        report.note_total_unparsed(table, str(column))

        if report is not None:
            report.measurements_emitted += len(out)
        return out

    def ordinal_value(
        self, column: str, answer: str, data_dict: dict[str, Any] | None
    ) -> int | None:
        """Map an answer to its ordinal integer, agnostic to how the cell is encoded.

        Public so the conditional-mapping apply path
        (:func:`b2ai_dataset_ingest.mapping.hpo_rules.derive_features`) and the preflight
        validator can resolve an answer to the same score the emitted Measurement uses.

        The cell may carry the choice *label* ("Several days"), the choice *code*
        ("almostNever"), or a numeric code ("1" / "3.0"). Resolution order:

        1. the per-item ``choices`` index from the companion data dict (keyed by label,
           by code, and by score — see :meth:`_choices_map`), including a numeric-normalized
           retry ("3.0" -> "3") for float-formatted numeric codes;
        2. the config ``ordinal_scale`` (label-keyed) fallback; and
        3. for items that declare *no* choice set (e.g. numeric-entry), a bare numeric answer.

        Returns ``None`` only when the answer resolves against nothing — the caller then
        skips the item and records it on the report.
        """
        choice_map = self._choices_map(column, data_dict)
        if choice_map:
            if answer in choice_map:
                return choice_map[answer]
            numeric = _as_int(answer)
            if numeric is not None and str(numeric) in choice_map:
                return choice_map[str(numeric)]
        fallback = self.mapping.get("ordinal_scale") or {}
        if answer in fallback:
            return fallback[answer]
        # No choice set to validate against -> accept a bare numeric answer as its own value.
        if not choice_map:
            numeric = _as_int(answer)
            if numeric is not None:
                return numeric
        return None

    @staticmethod
    def _choices_map(
        column: str, data_dict: dict[str, Any] | None
    ) -> dict[str, int] | None:
        """Build an answer -> ordinal-score index from a data-dict element's ``choices``.

        The returned map is keyed by every way a cell might spell a choice, all pointing at
        the same ordinal score: the choice *label* ("Several days"), the stringified choice
        *code* ("1" or "almostNever"), and the stringified *score*. The score itself is the
        integer ``value`` when the choice carries one, else the choice's positional index —
        so string-coded ordinal scales (e.g. VHI-10's "never"/"almostNever") resolve instead
        of being silently dropped. Label keys take precedence on the rare key collision.
        """
        element = (data_dict or {}).get(column)
        if not isinstance(element, dict):
            return None
        choices = element.get("choices")
        if not choices:
            return None
        mapping: dict[str, int] = {}
        for index, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue
            value = choice.get("value")
            # bool is an int subclass but never a valid ordinal code -> treat as non-numeric.
            if isinstance(value, bool):
                score: int | None = index
            elif isinstance(value, int):
                score = value
            else:
                score = _as_int(value)
                if score is None:
                    score = index  # non-numeric code (e.g. "almostNever") -> positional
            # code keys first, so an explicit label can override on collision
            if value is not None:
                mapping.setdefault(str(value), score)
            mapping.setdefault(str(score), score)
            name = choice.get("name")
            if isinstance(name, dict):
                name = name.get("en")
            if name is not None:
                mapping[name] = score
        return mapping or None

    @staticmethod
    def _as_number(raw: str) -> float | None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

"""Bridge2AI AI-READI reader: OMOP CDM CSVs -> canonical Participants.

The AI-READI release is a CDS-v0.1.1 tree whose clinical payload is OMOP CDM v5.4:

    participants.tsv                       -> Individual (age) + cohort provenance
    clinical_data/person.csv               -> Individual (sex, when a release ships it)
    clinical_data/visit_occurrence.csv     -> the visit -> time index
    clinical_data/condition_occurrence.csv -> DiseaseObservation (MONDO)
    clinical_data/measurement.csv          -> MeasurementObservation (assay + unit + range)

Where this diverges from :mod:`sources.voice.reader`, and why:

- **Long, not wide.** A row is one observation named by ``<domain>_source_value``. The item
  key is the REDCap variable, never ``*_concept_id`` — one concept backs several items
  (``3004249`` is both ``bp1_sysbp_vsorres`` and ``bp2_sysbp_vsorres``). See
  :mod:`b2ai_dataset_ingest.mapping.omop`.
- **Streaming, not materializing.** Voice reads a table with ``list(csv.DictReader(...))``,
  which is fine for its small wide tables. Measured on the 117 MB synthetic AI-READI
  ``measurement.csv`` (767 814 rows): materializing peaks at **1206 MB** RSS, streaming at
  **18 MB**. Every table here is consumed row by row.
- **Real dates.** Voice had only session labels; AI-READI has timestamps. The default is
  still to emit an *age*, not a date — see ``time_precision`` in ``participants.yaml``:
  day-precision dates are HIPAA identifiers and the licence extends to derived output.
- **No disease onset.** ``condition_start_date`` is the date the medical-history form was
  filled in (it equals the participant's assessment date, not any onset), so emitting it as
  ``Disease.onset`` would assert a false natural history. Suppressed by config.

Logging is PHI-safe on the same terms as the voice reader: warnings name a *table.item*,
never a cell value, a ``person_id``, or a date.
"""

from __future__ import annotations

import csv
import logging
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from b2ai_dataset_ingest.mapping.engine import MappingEngine
from b2ai_dataset_ingest.mapping.hpo_rules import (
    ConditionalRule,
    derive_features,
    load_conditional_rules,
)
from b2ai_dataset_ingest.mapping.loaders import is_placeholder, load_mapping, validate_mapping
from b2ai_dataset_ingest.mapping.omop import (
    AgeAnchor,
    LongCell,
    OmopTableSpec,
    as_number,
    is_blank,
    item_key,
    to_rfc3339,
)
from b2ai_dataset_ingest.model import (
    DiseaseObservation,
    Individual,
    MeasurementObservation,
    OntologyTerm,
    Participant,
    PhenotypicFeatureObservation,
    ProcedureContext,
    Quantity,
    ReferenceRange,
    TimePoint,
)
from b2ai_dataset_ingest.reporting import IngestReport
from b2ai_dataset_ingest.sources.base import Source

logger = logging.getLogger(__name__)

CLINICAL_DIR = "clinical_data"


class _Accumulator:
    """Collects the observations for one participant across all OMOP tables."""

    def __init__(self, person_id: str) -> None:
        self.person_id = person_id
        self.individual: Individual | None = None
        self.cohort: dict[str, str] = {}
        self.diseases: list[DiseaseObservation] = []
        self.measurements: list[MeasurementObservation] = []
        self.phenotypic_features: list[PhenotypicFeatureObservation] = []
        self._disease_ids: set[str] = set()

    def add_disease(self, disease: DiseaseObservation) -> None:
        if disease.term.id not in self._disease_ids:
            self._disease_ids.add(disease.term.id)
            self.diseases.append(disease)


class AireadiSource(Source):
    dataset_id = "bridge2ai-aireadi"

    def __init__(
        self, root: Path, config_dir: Path, mappings: Iterable[Path] | None = None
    ) -> None:
        super().__init__(root, config_dir)
        #: Aggregate, PHI-safe counts for the most recent :meth:`read`. The CLI prints it.
        self.report = IngestReport()
        self._dataset_cfg: dict[str, Any] | None = None
        #: SSSOM files carrying value-gated rules; None -> the shipped aireadi sets.
        self._mappings = list(mappings) if mappings is not None else None
        self._hpo_rules: dict[str, dict[str, list[ConditionalRule]]] | None = None

    # -- config --------------------------------------------------------------------
    @property
    def dataset_config(self) -> dict[str, Any]:
        if self._dataset_cfg is None:
            path = self.config_dir / "dataset.yaml"
            self._dataset_cfg = load_mapping(path) if path.exists() else {}
        return self._dataset_cfg

    def _config(self, name: str) -> dict[str, Any] | None:
        path = self.config_dir / name
        if not path.exists():
            return None
        mapping = load_mapping(path)
        for warning in validate_mapping(mapping):
            logger.warning("%s: %s", name, warning)
        return mapping

    def _clinical(self, table: str) -> Path:
        return self.root / CLINICAL_DIR / f"{table}.csv"

    # -- entry point ---------------------------------------------------------------
    def read(self) -> Iterable[Participant]:
        self.report = IngestReport()
        participants: OrderedDict[str, _Accumulator] = OrderedDict()

        def accumulator(person_id: str) -> _Accumulator:
            if person_id not in participants:
                participants[person_id] = _Accumulator(person_id)
            return participants[person_id]

        visits = self._read_visits()
        anchors = self._read_participants(accumulator)
        self._read_person(accumulator)
        self._read_conditions(accumulator, visits, anchors)
        self._read_measurements(accumulator, visits, anchors)
        self._read_observations(accumulator, visits, anchors)

        self.report.participants = len(participants)
        for person_id in sorted(participants):
            acc = participants[person_id]
            individual = acc.individual or Individual(id=person_id)
            self.report.diseases_emitted += len(acc.diseases)
            yield Participant(
                individual=individual,
                diseases=acc.diseases,
                measurements=acc.measurements,
                phenotypic_features=acc.phenotypic_features,
                source_dataset=self.dataset_id,
                cohort=acc.cohort,
            )

    # -- time ----------------------------------------------------------------------
    def _read_visits(self) -> dict[str, str]:
        """``visit_occurrence_id -> RFC3339 start``. Absent table -> no index, not an error."""
        path = self._clinical("visit_occurrence")
        index: dict[str, str] = {}
        rows = _stream_csv(path)
        if rows is None:
            self.report.tables_missing.append("visit_occurrence")
            return index
        for row in rows:
            visit_id = (row.get("visit_occurrence_id") or "").strip()
            stamp = to_rfc3339(row.get("visit_start_datetime") or row.get("visit_start_date") or "")
            if visit_id and stamp:
                index[visit_id] = stamp
        self.report.tables_read.append("visit_occurrence")
        return index

    def _timepoint(
        self, cell: LongCell, visits: dict[str, str], anchor: AgeAnchor | None
    ) -> TimePoint | None:
        """Build the TimePoint for one long row, at the configured precision.

        ``time_precision: age`` (the default) emits only an age — no date leaves the machine.
        ``date``/``datetime`` are opt-in and normalize to RFC3339-Z, because protobuf
        silently rejects every other spelling.

        The age is **derived per row** from the participant's anchor and the row's own date,
        not reused from the cohort table. OMOP records a date on every row while a cohort
        table records one age at one reference date; reusing that single value gives every
        observation the same ``Age``, which is indistinguishable in the output as soon as a
        participant has observations on more than one date. The date is used only for the
        arithmetic — it is not emitted unless the precision says so.
        """
        precision = (self.dataset_config.get("time_precision") or "age").lower()
        visit_id = cell.visit_id
        resolved = visits.get(visit_id) if not is_blank(visit_id, ("", " ", "0")) else None
        if resolved is None:
            self.report.rows_without_visit += 1
        session = f"visit-{visit_id}" if resolved else (f"date-{cell.date}" if cell.date else "")
        observed_on = resolved or cell.date
        age_iso = anchor.age_at(observed_on) if anchor is not None else None
        stamp = None
        if precision in ("date", "datetime"):
            stamp = resolved or to_rfc3339(cell.date)
        if not session and not stamp and not age_iso:
            return None
        return TimePoint(session_id=session, timestamp=stamp, age_iso8601=age_iso)

    # -- participants.tsv -> Individual + cohort ------------------------------------
    def _read_participants(self, accumulator: Any) -> dict[str, AgeAnchor]:
        """Populate Individual from the one wide table; return ``person_id -> AgeAnchor``.

        The anchor pairs the cohort table's age with the date it was measured on, so every
        observation can carry its own derived age (see :meth:`_timepoint`). A release that
        ships no anchor date still yields ages — the anchor then behaves as the single
        constant it used to be.
        """
        mapping = self._config("participants.yaml")
        anchors: dict[str, AgeAnchor] = {}
        if mapping is None:
            return anchors
        path = self.root / mapping.get("file", "participants.tsv")
        rows = _stream_csv(path, delimiter=mapping.get("delimiter", "\t"))
        if rows is None:
            self.report.tables_missing.append("participants")
            return anchors
        self.report.tables_read.append("participants")
        engine = MappingEngine(mapping)
        id_column = mapping.get("id_column", "person_id")
        age_column = mapping.get("age_column", "age")
        anchor_column = mapping.get("anchor_date_column", "")
        cohort_columns = list((mapping.get("cohort") or {}).get("columns") or [])
        group_spec = mapping.get("study_group") or {}
        for row in rows:
            person_id = (row.get(id_column) or "").strip()
            if not person_id:
                continue
            acc = accumulator(person_id)
            fields = engine.individual_fields(row, report=self.report)
            acc.individual = Individual(id=person_id, **fields)
            # The anchor date is optional; without it the age is simply a constant.
            anchor = AgeAnchor.build(row.get(age_column), row.get(anchor_column) or "")
            if anchor is not None:
                anchors[person_id] = anchor
            acc.cohort = {c: (row.get(c) or "").strip() for c in cohort_columns if row.get(c)}
            measurement = _study_group_measurement(row, group_spec, anchor)
            if measurement is not None:
                acc.measurements.append(measurement)
                self.report.measurements_emitted += 1
        return anchors

    # -- person.csv -> Individual.sex ------------------------------------------------
    def _read_person(self, accumulator: Any) -> None:
        mapping = self._config("person.yaml")
        if mapping is None:
            return
        rows = _stream_csv(self._clinical("person"), delimiter=mapping.get("delimiter", ","))
        if rows is None:
            self.report.tables_missing.append("person")
            return
        self.report.tables_read.append("person")
        engine = MappingEngine(mapping)
        id_column = mapping.get("id_column", "person_id")
        redacted: dict[str, int] = {}
        for row in rows:
            person_id = (row.get(id_column) or "").strip()
            if not person_id:
                continue
            for column in mapping.get("columns") or {}:
                if is_blank(row.get(column, ""), ("", " ", "0")):
                    redacted[column] = redacted.get(column, 0) + 1
            fields = engine.individual_fields(row, report=self.report)
            if not fields:
                continue
            acc = accumulator(person_id)
            base = acc.individual.model_dump() if acc.individual else {"id": person_id}
            base.update(fields)
            acc.individual = Individual(**base)
        # A release that redacts sex must say so: 100 UNKNOWN_SEX subjects otherwise look
        # like a clean run. Counts only — never a value.
        for column, count in redacted.items():
            self.report.note_field_redacted("person", column, count)

    # -- condition_occurrence -> Disease ---------------------------------------------
    def _read_conditions(
        self, accumulator: Any, visits: dict[str, str], anchors: dict[str, AgeAnchor]
    ) -> None:
        mapping = self._config("conditions.yaml")
        if mapping is None:
            return
        spec = OmopTableSpec(mapping)
        rows = _stream_csv(self._clinical(spec.table), delimiter=mapping.get("delimiter", ","))
        if rows is None:
            self.report.tables_missing.append(spec.table)
            return
        self.report.tables_read.append(spec.table)
        conditions = mapping.get("conditions") or {}
        emit_onset = bool(mapping.get("emit_onset", False))
        for row in rows:
            cell = spec.cell(row)
            person_id = (row.get(spec.id_column) or "").strip()
            if cell is None or not person_id:
                continue
            term_spec = conditions.get(cell.item)
            if term_spec is None:
                self.report.note_item_unmapped(spec.table, cell.item)
                continue
            if is_placeholder(term_spec):
                # A configured-but-unresolved condition is a different failure from an
                # item nobody has looked at: it is on someone's curation list.
                self.report.note_placeholder_skipped(spec.table, cell.item)
                continue
            onset = (
                self._timepoint(cell, visits, anchors.get(person_id)) if emit_onset else None
            )
            accumulator(person_id).add_disease(
                DiseaseObservation(term=OntologyTerm(**term_spec), onset=onset)
            )

    # -- observation -> Measurement (+ value-gated PhenotypicFeature) ------------------
    def _conditional_rules(self) -> dict[str, dict[str, list[ConditionalRule]]]:
        """Value-gated rules for this dataset, indexed ``table -> item -> [rules]``.

        Scoped to ``aireadi``: the index key is a bare table name, so an unscoped load would
        let another dataset's gated rules fire on a table that happens to share its name.
        """
        if self._hpo_rules is None:
            self._hpo_rules = load_conditional_rules(self._mappings, dataset="aireadi")
        return self._hpo_rules

    def _read_observations(
        self, accumulator: Any, visits: dict[str, str], anchors: dict[str, AgeAnchor]
    ) -> None:
        """Ingest the configured observation items, and derive gated HPO features.

        Two things happen in one pass, and the split is what keeps memory flat. Measurements
        are emitted **per row** as the table streams. The value-gated HPO derivation needs a
        whole *row* of a participant's answers at once, so it buffers — but only the handful
        of items that actually carry a rule, never the ~355 the table holds. On a full
        release that is the difference between a few thousand buffered strings and a couple
        of million.
        """
        configs = sorted((self.config_dir / "observation").glob("*.yaml"))
        if not configs:
            return
        items: dict[str, dict[str, Any]] = {}
        dropped: dict[str, str] = {}
        table = "observation"
        base: dict[str, Any] = {}
        for path in configs:
            mapping = self._config(f"observation/{path.name}") or {}
            base = base or mapping
            table = mapping.get("table", table)
            items.update(mapping.get("measures") or {})
            dropped.update(_drop_prefixes(mapping))
        spec = OmopTableSpec({**base, "table": table})
        rows = _stream_csv(self._clinical(table), delimiter=base.get("delimiter", ","))
        if rows is None:
            self.report.tables_missing.append(table)
            return
        self.report.tables_read.append(table)

        table_rules = self._conditional_rules().get(table, {})
        gated = set(table_rules)
        answers: dict[str, dict[str, str]] = {}
        when: dict[str, TimePoint | None] = {}

        for row in rows:
            cell = spec.cell(row)
            person_id = (row.get(spec.id_column) or "").strip()
            if cell is None or not person_id:
                continue
            if cell.item in gated and cell.value and not cell.sentinel:
                # The sentinel screen is NOT redundant with the one on the measurement path,
                # and leaving it out is a real defect rather than a tidiness issue. A refusal
                # code reaches the condition evaluator as an ordinary number, and an
                # open-ended gate fires on it: `>=1` matches an answer of 777, and so does
                # `>=2`. Returning None for the ordinal does not help either, because
                # `conditions._match_scalar` falls back to the raw cell. So "declined to
                # answer" would silently assert the phenotype.
                #
                # This is one of two defences; the other is that every shipped `when_value`
                # is bounded (`in {1,2,3}`, or `>=n & <=m`) rather than open-ended, so a
                # sentinel that slipped past here still could not fire a gate.
                answers.setdefault(person_id, {})[cell.item] = cell.value
                when.setdefault(person_id, self._timepoint(cell, visits, anchors.get(person_id)))
            if cell.item not in items:
                # Dropped by policy is a different fact from nobody-looked-at-it, and the
                # report must be able to tell them apart.
                reason = _drop_reason(cell.item, dropped)
                if reason is None:
                    self.report.note_item_unmapped(table, cell.item)
                else:
                    self.report.note_item_dropped(table, cell.item)
                continue
            observation = self._measurement(
                cell, items, {}, table, visits, anchors.get(person_id), row
            )
            if observation is not None:
                accumulator(person_id).measurements.append(observation)
                self.report.measurements_emitted += 1

        if not table_rules:
            return
        # `as_row`'s whole reason for existing: the derivation is dataset-agnostic and runs
        # over a plain {item: value} dict, so the OMOP path reuses it with no changes.
        resolve = _ordinal_of
        for person_id, row_answers in answers.items():
            features = derive_features(
                row_answers, table_rules, resolve, when.get(person_id), self.report
            )
            accumulator(person_id).phenotypic_features.extend(features)

    # -- measurement -> Measurement ---------------------------------------------------
    def _read_measurements(
        self, accumulator: Any, visits: dict[str, str], anchors: dict[str, AgeAnchor]
    ) -> None:
        configs = sorted((self.config_dir / "measurement").glob("*.yaml"))
        if not configs:
            return
        units = (self._config("units.yaml") or {}).get("units") or {}
        measures: dict[str, dict[str, Any]] = {}
        table = "measurement"
        base: dict[str, Any] = {}
        for path in configs:
            mapping = self._config(f"measurement/{path.name}") or {}
            base = base or mapping
            table = mapping.get("table", table)
            # A file-level `reference_ranges` applies to every item it declares. Reference
            # intervals are a property of the *family* -- a lab result has one, a cognitive
            # subscore's 0-3 bound is a scoring range and not a reference interval -- so the
            # opt-in belongs at the file that groups the family, not on each item. An item
            # may still override. Stamping it on here keeps the emit path item-only.
            default_ranges = bool(mapping.get("reference_ranges", False))
            for item, item_spec in (mapping.get("measures") or {}).items():
                if isinstance(item_spec, dict):
                    item_spec.setdefault("reference_range", default_ranges)
                measures[item] = item_spec
            units.update(mapping.get("units") or {})
        spec = OmopTableSpec({**base, "table": table})
        rows = _stream_csv(self._clinical(table), delimiter=base.get("delimiter", ","))
        if rows is None:
            self.report.tables_missing.append(table)
            return
        self.report.tables_read.append(table)
        for row in rows:
            cell = spec.cell(row)
            person_id = (row.get(spec.id_column) or "").strip()
            if cell is None or not person_id:
                continue
            observation = self._measurement(
                cell, measures, units, table, visits, anchors.get(person_id), row
            )
            if observation is not None:
                accumulator(person_id).measurements.append(observation)
                self.report.measurements_emitted += 1

    def _measurement(
        self,
        cell: LongCell,
        measures: dict[str, dict[str, Any]],
        units: dict[Any, dict[str, str]],
        table: str,
        visits: dict[str, str],
        anchor: AgeAnchor | None,
        row: dict[str, str],
    ) -> MeasurementObservation | None:
        """One long row -> one Measurement, or None with the reason counted."""
        spec = measures.get(cell.item)
        if spec is None:
            self.report.note_item_unmapped(table, cell.item)
            return None
        if is_placeholder(spec.get("assay")):
            self.report.note_placeholder_skipped(table, cell.item)
            return None
        if cell.sentinel:
            # 555/777/999 are REDCap refusal codes, not values.
            self.report.note_sentinel_answer(table, cell.item)
            return None
        if cell.censored:
            # A bounded result ("< 36") has no GA4GH representation; reporting the bound as
            # a measured value would fabricate a reading.
            self.report.note_value_censored(table, cell.item)
            return None
        value = as_number(cell.value)
        if value is None:
            return None
        unit_term = _unit_term(spec, units, row, table, cell, self.report)
        if unit_term is None:
            return None
        quantity = Quantity(
            value=value,
            unit=unit_term,
            reference_range=self._reference_range(spec, cell),
        )
        return MeasurementObservation(
            assay=OntologyTerm(**spec["assay"]),
            value_quantity=quantity,
            time=self._timepoint(cell, visits, anchor),
            description=spec.get("description"),
            procedure=_procedure(spec),
        )

    def _reference_range(self, spec: dict[str, Any], cell: LongCell) -> ReferenceRange | None:
        """Per-row normal interval, only when the source gives BOTH bounds.

        Opt-in per item because non-lab rows reuse these columns for an item's *scoring*
        range (MoCA naming is 0-3), which is not a reference interval.
        """
        if not spec.get("reference_range"):
            return None
        low, high = as_number(cell.range_low), as_number(cell.range_high)
        if low is None or high is None:
            if low is not None or high is not None:
                # proto3 doubles have no presence, so a half-filled range reads back as
                # "the normal range is 39 to 0".
                self.report.reference_ranges_one_sided += 1
            return None
        return ReferenceRange(low=low, high=high)


def _unit_term(
    spec: dict[str, Any],
    units: dict[Any, dict[str, str]],
    row: dict[str, str],
    table: str,
    cell: LongCell,
    report: IngestReport,
) -> OntologyTerm | None:
    """The measurement's unit. Config wins; the data column is only a fallback.

    ``Quantity.unit`` is required by the phenopacket schema, and AI-READI leaves
    ``unit_source_value`` blank on three quarters of rows (units live inside the truncated
    label instead), so a data-first rule would drop most measurements.
    """
    declared = spec.get("unit")
    if isinstance(declared, dict) and not is_placeholder(declared):
        return OntologyTerm(**declared)
    concept = (row.get("unit_concept_id") or "").strip()
    mapped = units.get(concept) or units.get(_as_int_key(concept))
    if isinstance(mapped, dict):
        return OntologyTerm(**mapped)
    report.note_unit_unmapped(table, concept or "none")
    logger.warning("%s: item %s has no unit mapping; skipping value", table, cell.item)
    return None


def _as_int_key(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _procedure(spec: dict[str, Any]) -> ProcedureContext | None:
    """Laterality carrier, declared per item rather than read from the qualifier column.

    Six autorefraction items are per-eye by name and carry no qualifier at all, while the
    same qualifier column on a medical-history row holds a condition name — so the column is
    not a usable laterality source. Config declares the side; the column is a cross-check.
    """
    code = spec.get("procedure_code")
    if not isinstance(code, dict) or is_placeholder(code):
        return None
    body_site = spec.get("body_site")
    return ProcedureContext(
        code=OntologyTerm(**code),
        body_site=OntologyTerm(**body_site)
        if isinstance(body_site, dict) and not is_placeholder(body_site)
        else None,
    )


def _study_group_measurement(
    row: dict[str, str], spec: dict[str, Any], anchor: AgeAnchor | None
) -> MeasurementObservation | None:
    """The study arm as a categorical Measurement — never a Disease.

    AI-READI's ``study_group`` is a recruitment stratum and it disagrees with the
    participant's own condition table, so asserting it as a diagnosis would state something
    the clinical data does not support.
    """
    assay = spec.get("assay")
    if not isinstance(assay, dict) or is_placeholder(assay):
        return None
    column = spec.get("source_column", "study_group")
    raw = (row.get(column) or "").strip()
    term = (spec.get("value_terms") or {}).get(raw)
    if not isinstance(term, dict) or is_placeholder(term):
        return None
    return MeasurementObservation(
        assay=OntologyTerm(**assay),
        value_term=OntologyTerm(**term),
        time=TimePoint(session_id="enrollment", age_iso8601=anchor.iso) if anchor else None,
    )


def _stream_csv(path: Path, delimiter: str = ",") -> Iterator[dict[str, str]] | None:
    """Yield rows one at a time, or None when the file is absent.

    Streaming rather than ``list(...)`` is load-bearing, not tidiness: materializing the
    117 MB synthetic ``measurement.csv`` costs 1206 MB of RSS against 18 MB streamed.
    The unnamed pandas index column and the R ``X`` column are left in the dict and simply
    never looked up, so a release carrying one, two or neither all parse.
    """
    if not path.exists():
        return None

    def rows() -> Iterator[dict[str, str]]:
        with open(path, newline="", encoding="utf-8") as fh:
            yield from csv.DictReader(fh, delimiter=delimiter)

    return rows()


__all__ = ["AireadiSource", "item_key"]


def _drop_prefixes(mapping: dict[str, Any]) -> dict[str, str]:
    """``drop_rules`` as ``pattern -> reason``.

    Naming the families a config deliberately ignores is what lets the report distinguish
    "dropped by policy" from "nobody has looked at this yet". Silence cannot tell them apart,
    and on a table with hundreds of items that difference is the whole signal.
    """
    return {
        str(pattern): str(reason)
        for pattern, reason in (mapping.get("drop_rules") or {}).items()
    }


def _drop_reason(item: str, dropped: dict[str, str]) -> str | None:
    """The policy reason this item is ignored, or None if no rule covers it.

    Patterns are shell globs, because REDCap families are named at both ends: an instrument
    is a *prefix* (``rtsm_*`` retinal imaging) while the administrative fields it carries are
    a *suffix* (``*cmpdat`` completion date, ``*startts`` start timestamp). A prefix-only
    match would force one rule per instrument for what is really one policy.
    """
    for pattern, reason in dropped.items():
        if item == pattern or fnmatch(item, pattern):
            return reason
    return None


def _ordinal_of(_item: str, raw: str) -> int | None:
    """Resolve an answer to its ordinal score, for the value-gated rule evaluator.

    Unlike Voice, no per-item ``choices`` lookup is needed: OMOP stores the resolved ordinal
    in ``value_as_number`` directly. Reverse-scored items are stored already reversed by the
    source (a positively-worded CES-D-10 item codes "rarely" as 3), so the stored integer is
    directionally consistent across an instrument and a ``>=`` cut-point means the same thing
    on every item of it.
    """
    number = as_number(raw)
    return None if number is None else int(number)

"""Value-gated B2AI -> HPO rules: load conditional SSSOM mappings, derive PhenotypicFeatures.

This is the *apply path* for ADR-0002. A B2AI -> HPO SSSOM row whose ``when_value`` column is
non-empty is a **conditional** mapping: the HPO ``PhenotypicFeature`` is asserted for a
participant only when their answer to the source item satisfies the condition. Rows with an
empty ``when_value`` stay inert semantic mappings and are ignored here (they change no output).

Two steps:

- :func:`load_conditional_rules` reads the SSSOM files with the column-preserving
  :func:`~b2ai_dataset_ingest.mapping.sssom_io.parse_sssom` (``sssom-py`` would drop
  ``when_value``) and returns the conditional rules indexed ``table -> column -> [rules]``.
- :func:`derive_features` evaluates those rules against one answered row and emits
  :class:`~b2ai_dataset_ingest.model.core.PhenotypicFeatureObservation`\\s. Each derived feature
  carries provenance: a human-readable description and a GA4GH ``Evidence`` with an ECO
  self-report code and an ``ExternalReference`` to the source item — so a questionnaire-derived
  phenotype is never mistaken for a clinician-observed finding. Only **presence** is derived:
  a questionnaire's lowest answer denies the symptom within the instrument's recall window,
  not the phenotype (see docs/mapping-conventions.md).

The loader is **tolerant**, mirroring the rest of the reader: a malformed ``when_value`` or a
non-HPO object is logged (by subject — mapping metadata, never PHI) and skipped, because
``b2ai-ingest validate-mappings`` is the enforcing gate in CI.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from b2ai_dataset_ingest.mapping.conditions import (
    Answer,
    ConditionParseError,
    ValueCondition,
    parse_condition,
)
from b2ai_dataset_ingest.mapping.sssom_io import default_mapping_files, parse_sssom
from b2ai_dataset_ingest.model import (
    Evidence,
    ExternalReference,
    OntologyTerm,
    PhenotypicFeatureObservation,
    TimePoint,
)
from b2ai_dataset_ingest.ontology.curie_map import expand
from b2ai_dataset_ingest.reporting import IngestReport

logger = logging.getLogger(__name__)

#: ECO term for "self-reported patient statement evidence used in automatic assertion". Stamped
#: on every derived feature so self-report-derived phenotypes stay distinguishable from observed
#: findings (verified real + non-obsolete via OLS4/oaklib; ADR-0002 decision 4).
SELF_REPORT_EVIDENCE = OntologyTerm(
    id="ECO:0006160",
    label="self-reported patient statement evidence used in automatic assertion",
)


@dataclass(frozen=True)
class ConditionalRule:
    """One value-gated B2AI -> HPO mapping row (a non-empty ``when_value``)."""

    subject_id: str  # b2ai:<table>.<column>
    table: str
    column: str
    object_id: str  # HP:XXXXXXX
    object_label: str
    predicate_id: str
    condition: ValueCondition
    when_value: str  # raw expression, kept for provenance
    subject_label: str = ""
    confidence: str = ""


# --------------------------------------------------------------------------- loading


def load_conditional_rules(
    paths: Iterable[Path] | None = None,
    dataset: str | None = None,
) -> dict[str, dict[str, list[ConditionalRule]]]:
    """Load value-gated rules from SSSOM files, indexed ``table -> column -> [rules]``.

    ``paths`` defaults to the shipped ``mappings/`` sets, narrowed to ``dataset`` when given.
    Callers should pass ``dataset``: the index key is the bare table name, so loading every
    dataset's sets at once would let one dataset's rules fire on another's same-named table.
    Rows with an empty ``when_value`` (inert semantic mappings) are skipped; malformed rows
    are logged by subject and skipped.
    """
    files = list(paths) if paths is not None else default_mapping_files(dataset=dataset)
    index: dict[str, dict[str, list[ConditionalRule]]] = {}
    for path in files:
        _, rows = parse_sssom(path)
        for row in rows:
            rule = _rule_from_row(row, path.name)
            if rule is None:
                continue
            index.setdefault(rule.table, {}).setdefault(rule.column, []).append(rule)
    return index


def _rule_from_row(row: dict[str, str], fname: str) -> ConditionalRule | None:
    when_value = (row.get("when_value") or "").strip()
    if not when_value:
        return None  # inert semantic mapping — carries no condition
    subject_id = (row.get("subject_id") or "").strip()
    object_id = (row.get("object_id") or "").strip()
    if not subject_id.startswith("b2ai:") or "." not in subject_id.split(":", 1)[1]:
        logger.warning("%s: skipping conditional rule with malformed subject %r", fname, subject_id)
        return None
    if not object_id.startswith("HP:"):
        logger.warning(
            "%s: conditional rule %s targets non-HPO object %r; only HPO features are derived",
            fname,
            subject_id,
            object_id,
        )
        return None
    try:
        condition = parse_condition(when_value)
    except ConditionParseError as exc:
        logger.warning(
            "%s: skipping %s — unparseable when_value %r (%s)", fname, subject_id, when_value, exc
        )
        return None
    table, column = subject_id.split(":", 1)[1].split(".", 1)
    return ConditionalRule(
        subject_id=subject_id,
        table=table,
        column=column,
        object_id=object_id,
        object_label=(row.get("object_label") or "").strip(),
        predicate_id=(row.get("predicate_id") or "").strip(),
        condition=condition,
        when_value=when_value,
        subject_label=(row.get("subject_label") or "").strip(),
        confidence=(row.get("confidence") or "").strip(),
    )


# ------------------------------------------------------------------------ derivation


def derive_features(
    row: dict[str, str],
    table_rules: dict[str, list[ConditionalRule]],
    resolve_ordinal: Callable[[str, str], int | None],
    time: TimePoint | None = None,
    report: IngestReport | None = None,
) -> list[PhenotypicFeatureObservation]:
    """Emit HPO ``PhenotypicFeature``s for the rules that match this row's answered cells.

    ``table_rules`` is the ``column -> [rules]`` map for this table (from
    :func:`load_conditional_rules`). ``resolve_ordinal(column, raw)`` returns the ordinal score
    the reader resolved for a cell (``None`` if unresolved), used for numeric conditions; string
    conditions read the raw cell. A blank cell asserts nothing.
    """
    out: list[PhenotypicFeatureObservation] = []
    for column, rules in table_rules.items():
        raw = (row.get(column) or "").strip()
        if not raw:
            continue
        answer = Answer(raw=raw, ordinal=resolve_ordinal(column, raw))
        for rule in rules:
            if rule.condition.matches(answer):
                out.append(_feature_from_rule(rule, time))
    if report is not None:
        report.features_derived += len(out)
    return out


def _feature_from_rule(
    rule: ConditionalRule, time: TimePoint | None
) -> PhenotypicFeatureObservation:
    return PhenotypicFeatureObservation(
        type=OntologyTerm(id=rule.object_id, label=rule.object_label or None),
        onset=time,
        description=_describe(rule),
        evidence=[
            Evidence(
                evidence_code=SELF_REPORT_EVIDENCE,
                reference=ExternalReference(
                    id=rule.subject_id,
                    reference=expand(rule.subject_id),
                    description=rule.subject_label or None,
                ),
            )
        ],
    )


def _describe(rule: ConditionalRule) -> str:
    """Human-readable provenance: source item, label, predicate, when_value, confidence."""
    source = f"{rule.subject_id}" + (f" ({rule.subject_label})" if rule.subject_label else "")
    target = f"{rule.object_id}" + (f" {rule.object_label}" if rule.object_label else "")
    confidence = f", confidence {rule.confidence}" if rule.confidence else ""
    return (
        f"Derived present from self-reported item {source} "
        f"[{rule.predicate_id} {target}] when answer {rule.when_value}{confidence}."
    )

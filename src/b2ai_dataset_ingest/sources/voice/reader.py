"""Bridge2AI-Voice reader: phenotype TSVs -> canonical Participants.

The voice ``phenotype/`` tree is a set of TSVs keyed by ``participant_id`` +
``session_id``, each with a companion ReproSchema JSON data dictionary. In scope for v1:

    demographics/   -> Individual
    diagnosis/      -> DiseaseObservation (per-condition file basename -> MONDO)
    questionnaire/  -> MeasurementObservation (per-item ordinals + precomputed totals), and
                       PhenotypicFeatureObservation for items with a value-gated B2AI -> HPO
                       ``when_value`` mapping (see mapping/hpo_rules.py, ADR-0002)

Audio/derived-feature tables (``task/``) are referenced, not ingested.

The data is messy in ways the reader must tolerate:

- rows are **not** unique per ``(participant_id, session)`` — a participant can have several
  rows for the same session; these are merged "last non-empty wins" *within a session*;
- the session key column is **not** uniform across the real dataset. The synthetic tree
  carries a bare ``session_id``; the real b2aiprep release instead uses per-instrument names
  (``gad_7_session_id``, ``phq_9_session_id``, ``vhi_session_id``, ...). Each questionnaire
  config declares its ``session_columns`` (a candidate list, first present wins); when none
  is present the reader refuses to merge (rows become independent timepoints) and records it,
  rather than silently collapsing every visit into one ``(pid, "")`` group;
- companion data dicts come in two envelope shapes (flat real / nested synthetic), handled
  by :func:`~b2ai_dataset_ingest.mapping.loaders.load_data_dict`; and
- tables are wide and sparse — only mapped columns are read, missing ones are ignored.
"""

from __future__ import annotations

import csv
import functools
import logging
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from b2ai_dataset_ingest.mapping.engine import MappingEngine
from b2ai_dataset_ingest.mapping.hpo_rules import (
    ConditionalRule,
    derive_features,
    load_conditional_rules,
)
from b2ai_dataset_ingest.mapping.loaders import (
    is_placeholder,
    load_data_dict,
    load_mapping,
    validate_mapping,
)
from b2ai_dataset_ingest.model import (
    DiseaseObservation,
    Individual,
    MeasurementObservation,
    OntologyTerm,
    Participant,
    PhenotypicFeatureObservation,
    TimePoint,
)
from b2ai_dataset_ingest.reporting import IngestReport
from b2ai_dataset_ingest.sources.base import Source

logger = logging.getLogger(__name__)

# Default session-key candidates when a config omits ``session_columns``. The bare
# ``session_id`` is the synthetic/fixture convention; real configs prepend it with the
# instrument-specific name so both datasets resolve.
DEFAULT_SESSION_COLUMNS = ["session_id"]

# Recognized session labels -> the NCIT visit term used as the TimeElement fallback.
# (There are no real dates/ages in the data, so the session label is the richest signal.)
# Unrecognized session ids — e.g. real opaque ids or stray UUIDs — leave the timepoint's
# ontology_class unset (the id still distinguishes the visit).
SESSION_TERMS: dict[str, OntologyTerm] = {
    "ses-baseline": OntologyTerm(id="NCIT:C25213", label="Baseline"),
    "ses-followup": OntologyTerm(id="NCIT:C16033", label="Follow-Up"),
}


class _Accumulator:
    """Collects the observations for one participant across all tables."""

    def __init__(self, participant_id: str) -> None:
        self.participant_id = participant_id
        self.individual: Individual | None = None
        self.diseases: list[DiseaseObservation] = []
        self.measurements: list[MeasurementObservation] = []
        self.phenotypic_features: list[PhenotypicFeatureObservation] = []
        self._disease_ids: set[str] = set()

    def add_disease(self, disease: DiseaseObservation) -> None:
        if disease.term.id not in self._disease_ids:
            self._disease_ids.add(disease.term.id)
            self.diseases.append(disease)


class VoiceSource(Source):
    dataset_id = "bridge2ai-voice"

    def __init__(
        self, root: Path, config_dir: Path, mappings: Iterable[Path] | None = None
    ) -> None:
        super().__init__(root, config_dir)
        #: Aggregate, PHI-safe counts for the most recent :meth:`read`. The CLI prints it.
        self.report = IngestReport()
        #: SSSOM files carrying B2AI -> HPO ``when_value`` rules; None -> the shipped defaults.
        self._mappings = list(mappings) if mappings is not None else None
        self._hpo_rules: dict[str, dict[str, list[ConditionalRule]]] | None = None

    def _conditional_rules(self) -> dict[str, dict[str, list[ConditionalRule]]]:
        """Value-gated B2AI -> HPO rules, loaded once and indexed ``table -> column -> [rules]``."""
        if self._hpo_rules is None:
            # Scoped to this dataset's sets: rules are indexed by bare table name, so an
            # unscoped load would let another dataset's gated rules fire on a voice table
            # that happens to share its name.
            self._hpo_rules = load_conditional_rules(self._mappings, dataset="voice")
        return self._hpo_rules

    def read(self) -> Iterable[Participant]:
        self.report = IngestReport()
        participants: OrderedDict[str, _Accumulator] = OrderedDict()

        def accumulator(participant_id: str) -> _Accumulator:
            if participant_id not in participants:
                participants[participant_id] = _Accumulator(participant_id)
            return participants[participant_id]

        self._read_demographics(accumulator)
        self._read_diagnoses(accumulator)
        self._read_questionnaires(accumulator)

        self.report.participants = len(participants)
        for participant_id in sorted(participants):
            acc = participants[participant_id]
            individual = acc.individual or Individual(id=participant_id)
            self.report.diseases_emitted += len(acc.diseases)
            yield Participant(
                individual=individual,
                diseases=acc.diseases,
                measurements=acc.measurements,
                phenotypic_features=acc.phenotypic_features,
                source_dataset=self.dataset_id,
            )

    # -- demographics -> Individual --
    def _read_demographics(self, accumulator: Any) -> None:
        path = self.config_dir / "demographics.yaml"
        if not path.exists():
            return
        mapping = load_mapping(path)
        self._warn(mapping, "demographics.yaml")
        engine = MappingEngine(mapping)
        table = mapping.get("table", "demographics")
        tsv = self.root / "demographics" / f"{table}.tsv"
        rows = self._read_tsv(tsv)
        if not rows:
            self.report.tables_missing.append(f"demographics/{table}")
            return
        self.report.tables_read.append(table)
        for participant_id, prows in _group_by(rows, _participant_key).items():
            merged = _merge_rows(prows, self.report)
            fields = engine.individual_fields(merged, report=self.report)
            accumulator(participant_id).individual = Individual(id=participant_id, **fields)

    # -- diagnosis -> Disease (one per resolved condition file the participant is in) --
    def _read_diagnoses(self, accumulator: Any) -> None:
        path = self.config_dir / "diagnosis.yaml"
        diag_dir = self.root / "diagnosis"
        if not path.exists() or not diag_dir.is_dir():
            return
        mapping = load_mapping(path)
        self._warn(mapping, "diagnosis.yaml")
        conditions = mapping.get("conditions") or {}
        session_candidates = _session_columns(mapping)
        for tsv in sorted(diag_dir.glob("*.tsv")):
            basename = tsv.stem
            term_spec = conditions.get(basename)
            # A resolved, non-control condition contributes a Disease. `control` and
            # unresolved (placeholder) conditions contribute the *participant* to the
            # universe but no Disease — so a participant whose only appearance is a control
            # or not-yet-coded diagnosis still gets an (id-only) phenopacket rather than
            # being silently dropped.
            if basename == "control":
                term = None
            elif term_spec is None or is_placeholder(term_spec):
                logger.info("diagnosis: %s has no resolved MONDO term; no Disease", basename)
                term = None
            else:
                term = OntologyTerm(**term_spec)
            rows = self._read_tsv(tsv)
            if not rows:
                continue
            # Diagnosis is enrollment-level in the real dataset (no session column); onset is
            # only populated when a configured session column is actually present.
            session_col = _resolve_session_column(rows[0], session_candidates)
            for participant_id, prows in _group_by(rows, _participant_key).items():
                acc = accumulator(participant_id)
                if term is not None:
                    session_id = prows[0].get(session_col, "") if session_col else ""
                    onset = _timepoint(session_id) if session_id else None
                    acc.add_disease(DiseaseObservation(term=term, onset=onset))

    # -- questionnaires -> Measurements (per (participant, session) timepoint) --
    def _read_questionnaires(self, accumulator: Any) -> None:
        """Discovery is *data-dir driven*: every ``questionnaire/*.tsv`` is considered, and a
        table with no mapping config is recorded as unmapped (not silently invisible). This is
        symmetric with diagnosis and surfaces real tables the configs don't yet cover.
        """
        data_dir = self.root / "questionnaire"
        if not data_dir.is_dir():
            return
        configs = self._questionnaire_configs()
        for tsv in sorted(data_dir.glob("*.tsv")):
            table = tsv.stem
            entry = configs.get(table)
            if entry is None:
                logger.info("questionnaire: %s has no mapping config; not ingested", table)
                self.report.tables_unmapped.append(table)
                continue
            cfg_path, mapping = entry
            self._warn(mapping, f"questionnaire/{cfg_path.name}")
            engine = MappingEngine(mapping)
            rows = self._read_tsv(tsv)
            if not rows:
                continue
            self.report.tables_read.append(table)
            data_dict = load_data_dict(tsv.with_suffix(".json"))
            self._ingest_questionnaire_rows(table, mapping, engine, rows, data_dict, accumulator)

    def _ingest_questionnaire_rows(
        self,
        table: str,
        mapping: dict[str, Any],
        engine: MappingEngine,
        rows: list[dict[str, str]],
        data_dict: dict[str, Any],
        accumulator: Any,
    ) -> None:
        session_col = _resolve_session_column(rows[0], _session_columns(mapping))
        if session_col is None:
            # Fail loud, not silent: with no resolvable session key we must NOT merge (that
            # would blend distinct visits). Emit each row as its own timepoint and flag it.
            logger.error(
                "questionnaire %s: none of session_columns %s present; emitting rows "
                "un-merged (no session key)",
                table,
                _session_columns(mapping),
            )
            self.report.tables_without_session.append(table)
            groups: OrderedDict[tuple[str, object], list[dict[str, str]]] = OrderedDict(
                ((row.get("participant_id", ""), index), [row]) for index, row in enumerate(rows)
            )
        else:
            groups = _group_by(
                rows, lambda r: (r.get("participant_id", ""), r.get(session_col, ""))
            )
        # Value-gated B2AI -> HPO rules for this table's columns (empty if none authored). The
        # resolver reuses the engine's ordinal resolution so a condition (`>=1`) is evaluated
        # against the same score the emitted Measurement carries.
        table_rules = self._conditional_rules().get(table, {})
        resolve_ordinal = functools.partial(engine.ordinal_value, data_dict=data_dict)
        for (participant_id, _key), prows in groups.items():
            session_id = prows[0].get(session_col, "") if session_col else ""
            merged = _merge_rows(prows, self.report)
            time = _timepoint(session_id) if session_id else None
            acc = accumulator(participant_id)
            acc.measurements.extend(
                engine.measurements(merged, data_dict, time, report=self.report)
            )
            if table_rules:
                acc.phenotypic_features.extend(
                    derive_features(merged, table_rules, resolve_ordinal, time, self.report)
                )

    def _questionnaire_configs(self) -> dict[str, tuple[Path, dict[str, Any]]]:
        """Map each questionnaire config's ``table`` name to ``(path, mapping)``."""
        config_dir = self.config_dir / "questionnaire"
        configs: dict[str, tuple[Path, dict[str, Any]]] = {}
        if not config_dir.is_dir():
            return configs
        for cfg_path in sorted(config_dir.glob("*.yaml")):
            mapping = load_mapping(cfg_path)
            configs[mapping.get("table", cfg_path.stem)] = (cfg_path, mapping)
        return configs

    # -- IO helpers --
    @staticmethod
    def _read_tsv(path: Path) -> list[dict[str, str]]:
        _, rows = _read_tsv_header_and_rows(path)
        return rows

    @staticmethod
    def _warn(mapping: dict[str, Any], source: str) -> None:
        for warning in validate_mapping(mapping):
            logger.warning("%s: %s", source, warning)


def _read_tsv_header_and_rows(path: Path) -> tuple[list[str] | None, list[dict[str, str]]]:
    """Read a TSV into (header, rows). ``header`` is None only when the file is absent, so a
    present-but-empty table is distinguishable from a missing one (the header still lists its
    columns). ``rows`` is a list of column->value dicts.
    """
    if not path.exists():
        return None, []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        rows = list(reader)
        header = list(reader.fieldnames) if reader.fieldnames else []
    return header, rows


def _participant_key(row: dict[str, str]) -> str:
    return row.get("participant_id", "")


def _session_columns(mapping: dict[str, Any]) -> list[str]:
    """The ordered session-key candidates for a table's config (first present in the header
    wins). Accepts a scalar or list ``session_columns``; defaults to the bare ``session_id``.
    """
    cols = mapping.get("session_columns", DEFAULT_SESSION_COLUMNS)
    if isinstance(cols, str):
        return [cols]
    return list(cols) if cols else list(DEFAULT_SESSION_COLUMNS)


def _resolve_session_column(sample_row: dict[str, str], candidates: Iterable[str]) -> str | None:
    """Return the first candidate present as a column in the header, or None if none are."""
    for candidate in candidates:
        if candidate in sample_row:
            return candidate
    return None


def _group_by(rows: Iterable[dict[str, str]], key: Any) -> OrderedDict[Any, list[dict]]:
    grouped: OrderedDict[Any, list[dict]] = OrderedDict()
    for row in rows:
        grouped.setdefault(key(row), []).append(row)
    return grouped


def _merge_rows(rows: list[dict[str, str]], report: IngestReport | None = None) -> dict[str, str]:
    """Collapse rows sharing a key into one, taking the last non-empty value per column.

    Safe only for rows that genuinely share a timepoint; the caller guarantees that by
    grouping on a resolved session column first. A merge of >1 row is recorded on the report.
    """
    if len(rows) == 1:
        return rows[0]
    if report is not None:
        report.rows_merged += len(rows) - 1
    merged: dict[str, str] = {}
    for row in rows:
        for column, value in row.items():
            if value and value.strip():
                merged[column] = value
    return merged


def _timepoint(session_id: str) -> TimePoint:
    """Build a TimePoint for a session id, attaching an NCIT term for known labels."""
    term = SESSION_TERMS.get(session_id)
    if term is None:
        # PHI-safe: don't log the raw id (real session ids can be opaque UUIDs = re-id vectors).
        logger.debug("unrecognized session id; leaving TimeElement unset")
    return TimePoint(session_id=session_id, ontology_class=term)

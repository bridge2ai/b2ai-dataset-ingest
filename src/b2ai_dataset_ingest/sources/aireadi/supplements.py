"""The protected AI-READI supplements: demographics and current medications.

Two wide tables that ship *outside* the OMOP ``clinical_data/`` tree, under the controlled
tier of the Data License Agreement, and that the public release redacts:

    demographics  -> Individual.sex (sex assigned at birth), Individual.gender (identity)
    medications   -> TreatmentObservation, one per reported medication

They arrive as spreadsheets with the REDCap export shape (``studyid`` plus the form's
variables; medications is a repeat instrument, one row per medication). Both are optional
inputs: a release without them emits exactly what v1 emitted.

What is deliberately NOT read, and why, lives in the two configs under
``config/aireadi/supplements/``: race, ethnicity, ancestry and every free-text ``*ot``
column are Data with no phenopacket slot (and no model may read them, §3.C), and
``cmname`` -- the free-text medication name -- is never consulted. A medication row with no
RxNorm code is counted and dropped, not guessed at.

Logging is PHI-safe on the reader's terms: counts and column names, never a value.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from b2ai_dataset_ingest.mapping.loaders import is_placeholder
from b2ai_dataset_ingest.mapping.omop import AgeAnchor, as_number, is_blank
from b2ai_dataset_ingest.model import (
    Individual,
    OntologyTerm,
    Quantity,
    TimePoint,
    TreatmentObservation,
)
from b2ai_dataset_ingest.reporting import IngestReport

logger = logging.getLogger(__name__)

#: REDCap "prefer not to say" / "other" / "not sure" codes on a coded column. They are an
#: answer, not a value, and recode to nothing.
DECLINED = frozenset({"777", "888", "999", "777.0", "888.0", "999.0"})

#: What a supplement file may be. The config globs carry no extension, so a column list or
#: a README sitting beside the real sheet (``columns_from_*.txt``) is never picked up.
SUPPORTED_SUFFIXES = frozenset({".csv", ".tsv", ".xlsx", ".xlsm"})


# -- locating and reading a supplement -----------------------------------------------
def resolve_supplement(
    root: Path, mapping: dict[str, Any], override_dir: Path | None
) -> Path | None:
    """The on-disk file for a supplement config, or None if absent.

    ``file`` in the config is a glob relative to ``root`` (the real deliveries are named
    with spaces and wave suffixes, so a pattern beats a fixed name). ``override_dir``
    replaces the directory part, for a delivery staged outside the release tree.
    """
    pattern = str(mapping.get("file") or "")
    if not pattern:
        return None
    if override_dir is not None:
        pattern = str(Path(override_dir) / Path(pattern).name)
        base = Path("/") if Path(pattern).is_absolute() else Path(".")
        matches = sorted(base.glob(pattern.lstrip("/"))) if "*" in pattern else [Path(pattern)]
    else:
        matches = sorted(root.glob(pattern))
    matches = [m for m in matches if m.is_file() and m.suffix.lower() in SUPPORTED_SUFFIXES]
    if len(matches) > 1:
        logger.warning(
            "supplement pattern %r matches %d files; using the first", pattern, len(matches)
        )
    return matches[0] if matches else None


def stream_table(path: Path) -> Iterator[dict[str, str]]:
    """Yield rows of a CSV / TSV / XLSX supplement as ``{column: cell}``.

    XLSX needs ``openpyxl`` (the ``supplements`` extra). The first sheet is read, the first
    row is the header, and every cell is stringified so the two spellings of a code
    (``6`` and ``6.0``) land on the same config key.
    """
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        delimiter = "\t" if suffix == ".tsv" else ","
        with open(path, newline="", encoding="utf-8-sig") as fh:
            yield from csv.DictReader(fh, delimiter=delimiter)
        return
    if suffix in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                f"{path.name} is a spreadsheet; install the 'supplements' extra "
                "(uv sync --extra supplements) or export it to CSV"
            ) from exc
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = workbook.worksheets[0]
            rows = sheet.iter_rows(values_only=True)
            header = [str(c).strip() if c is not None else "" for c in next(rows, ())]
            for raw in rows:
                yield {
                    header[i]: _cell(raw[i] if i < len(raw) else None)
                    for i in range(len(header))
                    if header[i]
                }
        finally:
            workbook.close()
        return
    raise RuntimeError(f"unsupported supplement format {path.suffix!r} ({path.name})")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _code(value: str) -> str:
    """A coded answer, normalised so ``"6.0"`` and ``"6"`` are the same key."""
    raw = (value or "").strip()
    number = as_number(raw)
    return str(int(number)) if number is not None and float(number).is_integer() else raw


# -- demographics -> Individual ----------------------------------------------------------
def read_demographics(
    path: Path,
    mapping: dict[str, Any],
    accumulator: Callable[[str], Any],
    report: IngestReport,
) -> None:
    """Populate ``Individual.sex`` / ``Individual.gender`` from the demographics supplement.

    Each configured column names a target and either a ``value_map`` (sex, to the
    phenopacket enum) or ``value_terms`` (gender, to an ``OntologyClass``). A declined
    answer (777/888) recodes to nothing and is counted per column; an unmapped code is a
    ``value_map`` miss. The supplement wins over ``person.csv``, which is redacted.
    """
    table = mapping.get("table", "demographics")
    id_column = mapping.get("id_column", "studyid")
    columns: dict[str, dict[str, Any]] = mapping.get("columns") or {}
    report.tables_read.append(table)
    declined: dict[str, int] = {}
    for row in stream_table(path):
        person_id = (row.get(id_column) or "").strip()
        if not person_id:
            continue
        fields: dict[str, Any] = {}
        for column, spec in columns.items():
            raw = (row.get(column) or "").strip()
            if not raw:
                continue
            target = str(spec.get("target", ""))
            if not target.startswith("Individual."):
                continue
            field = target.split(".", 1)[1]
            code = _code(raw)
            if code in DECLINED:
                declined[column] = declined.get(column, 0) + 1
                continue
            value = _recode(code, raw, spec)
            if value is _MISS:
                logger.warning("%s: no mapping for column %s; dropping", table, column)
                report.note_value_map_miss(table, column)
                continue
            if value is not None:
                fields[field] = value
        if not fields:
            continue
        acc = accumulator(person_id)
        base = acc.individual.model_dump() if acc.individual else {"id": person_id}
        base.update(fields)
        acc.individual = Individual(**base)
    for column, n in declined.items():
        report.note_field_redacted(table, column, n)


_MISS = object()


def _recode(code: str, raw: str, spec: dict[str, Any]) -> Any:
    """Apply ``value_terms`` (-> OntologyTerm) or ``value_map`` (-> scalar) to one cell."""
    terms = spec.get("value_terms")
    if isinstance(terms, dict):
        term = terms.get(code, terms.get(raw))
        if term is None:
            return _MISS
        if isinstance(term, dict) and not is_placeholder(term):
            return OntologyTerm(**term)
        return None  # an explicit null, or a placeholder: leave the field unset
    value_map = spec.get("value_map")
    if isinstance(value_map, dict):
        lowered = {str(k).lower(): v for k, v in value_map.items()}
        for candidate in (code, raw, raw.lower()):
            if candidate in value_map:
                return value_map[candidate]
            if candidate.lower() in lowered:
                return lowered[candidate.lower()]
        return _MISS
    return raw


# -- medications -> TreatmentObservation --------------------------------------------------
def read_medications(
    path: Path,
    mapping: dict[str, Any],
    accumulator: Callable[[str], Any],
    report: IngestReport,
    anchors: dict[str, AgeAnchor],
) -> None:
    """One ``TreatmentObservation`` per medication row that carries a drug code.

    The agent is the source's RxNorm code, used as given (no re-mapping to ingredient).
    Route, frequency and dose unit are closed code lists in the REDCap dictionary and are
    recoded through config; an unknown code is counted as unmapped and the field is left
    unset rather than guessed. The dose amount is free text; a non-numeric one (``"one
    half"``) is counted and the treatment keeps its agent. ``cmname`` is never read.
    """
    table = mapping.get("table", "medications")
    id_column = mapping.get("id_column", "studyid")
    agent_spec = mapping.get("agent") or {}
    code_column = agent_spec.get("code_column", "rxnorm_code")
    label_column = agent_spec.get("label_column", "rxnorm_term")
    prefix = agent_spec.get("prefix", "RxNorm")
    route_spec = mapping.get("route") or {}
    frequency_spec = mapping.get("frequency") or {}
    dose_spec = mapping.get("dose") or {}
    report.tables_read.append(table)

    for row in stream_table(path):
        person_id = (row.get(id_column) or "").strip()
        if not person_id:
            continue
        code = _code(row.get(code_column) or "")
        if is_blank(code):
            # No drug code: the only other identification is free text, which is not read.
            report.note_item_unmapped(table, code_column)
            continue
        label = (row.get(label_column) or "").strip() or None
        route = _coded_term(row, route_spec, table, report)
        frequency = _coded_term(row, frequency_spec, table, report)
        dose = _dose(row, dose_spec, table, report)
        anchor = anchors.get(person_id)
        accumulator(person_id).treatments.append(
            TreatmentObservation(
                agent=OntologyTerm(id=f"{prefix}:{code}", label=label),
                route=route,
                dose=dose,
                frequency=frequency,
                time=TimePoint(session_id="enrollment", age_iso8601=anchor.iso) if anchor else None,
                description=mapping.get("description"),
            )
        )
        report.treatments_emitted += 1


def _coded_term(
    row: dict[str, str], spec: dict[str, Any], table: str, report: IngestReport
) -> OntologyTerm | None:
    column = spec.get("column")
    if not column:
        return None
    raw = (row.get(column) or "").strip()
    if not raw:
        return None
    code = _code(raw)
    if code in DECLINED:
        return None  # "Other" / unknown: the free-text companion column is not read
    terms = spec.get("value_terms") or {}
    if code not in terms:
        report.note_item_unmapped(table, f"{column}={code}")
        return None
    term = terms[code]
    if term is None:
        return None  # an explicit null: "unknown", recoded to nothing on purpose
    if not isinstance(term, dict) or is_placeholder(term):
        report.note_placeholder_skipped(table, f"{column}={code}")
        return None
    return OntologyTerm(**term)


def _dose(
    row: dict[str, str], spec: dict[str, Any], table: str, report: IngestReport
) -> Quantity | None:
    amount_column = spec.get("amount_column")
    unit_column = spec.get("unit_column")
    if not amount_column or not unit_column:
        return None
    raw_amount = (row.get(amount_column) or "").strip()
    if not raw_amount:
        return None
    amount = as_number(raw_amount)
    if amount is None:
        # Free text ("one half", "1/2"): a fabricated number is worse than no dose.
        report.note_total_unparsed(table, amount_column)
        return None
    unit_code = _code(row.get(unit_column) or "")
    unit = (spec.get("units") or {}).get(unit_code)
    if not isinstance(unit, dict) or is_placeholder(unit):
        # Quantity.unit is required by the schema; a dose in no unit is dropped.
        report.note_unit_unmapped(table, unit_code or "none")
        return None
    return Quantity(value=amount, unit=OntologyTerm(**unit))


__all__ = ["read_demographics", "read_medications", "resolve_supplement", "stream_table"]

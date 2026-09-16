"""Preflight contract-validation for the AI-READI OMOP layout.

The counterpart to :mod:`sources.voice.validate`, and it exists for the same reason: the
reader degrades rather than crashes, which is right for a batch ingest and hides shape
drift. For an OMOP release the drift that matters is different, so the checks are too:

- a required table or column is missing;
- an item key the config maps is absent from the release (a config naming a variable the
  data does not ship emits nothing, silently);
- an item key in the data has no config entry (present but not ingested);
- a field is *fully redacted* — a release may ship ``gender_concept_id = 0`` on every row,
  and a run of all-``UNKNOWN_SEX`` subjects must not look like success;
- a declared unit disagrees with the unit the data carries where the data carries one.

**Why an item-key inventory is not PHI.** OMOP is EAV, so what Voice keeps in a *header*
(``nervous_anxious``) AI-READI keeps in a *cell* (``observation_source_value``). The set of
distinct ``(item key)`` values in a long table is definitionally the column list of the
equivalent wide table — it is schema, not participant data. Refusing to inventory it would
mean the validator could check nothing at all, which is the worse privacy outcome.

Everything reported is a variable name, a column name, or a count. Never reported: a
``person_id``, a ``value_as_*`` cell, a ``range_*`` bound, or a date. Per-item counts below
:data:`SMALL_CELL` print as ``<5``: a rare condition can have a single-digit count, and a
count of 1 plus outside knowledge is a small-cell disclosure.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from b2ai_dataset_ingest.mapping.loaders import is_placeholder, load_mapping
from b2ai_dataset_ingest.mapping.omop import is_blank, item_key

#: Counts at or below this print as "<5" rather than the exact number.
SMALL_CELL = 5

#: Columns a finding may name. Anything outside this is a value, not a vocabulary.
REPORTABLE_COLUMNS = frozenset(
    {
        "person_id",  # named as a *column*, never as a value
        "condition_source_value",
        "measurement_source_value",
        "observation_source_value",
        "procedure_source_value",
        "gender_concept_id",
        "race_concept_id",
        "ethnicity_concept_id",
        "unit_concept_id",
        "unit_source_value",
        "qualifier_concept_id",
        "visit_occurrence_id",
    }
)


@dataclass
class Finding:
    level: str  # "error" | "warning" | "info"
    table: str
    message: str


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)
    tables_checked: list[str] = field(default_factory=list)

    def error(self, table: str, message: str) -> None:
        self.findings.append(Finding("error", table, message))

    def warning(self, table: str, message: str) -> None:
        self.findings.append(Finding("warning", table, message))

    def info(self, table: str, message: str) -> None:
        self.findings.append(Finding("info", table, message))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    def render(self) -> str:
        lines = [f"Validated {len(self.tables_checked)} table(s)."]
        markers = {"error": "ERROR", "warning": "warn ", "info": "info "}
        for finding in self.findings:
            lines.append(f"  [{markers[finding.level]}] {finding.table}: {finding.message}")
        lines.append(
            f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)."
            if self.findings
            else "No issues found."
        )
        return "\n".join(lines)


def count(n: int) -> str:
    """Render a count, suppressing small cells."""
    return f"<{SMALL_CELL}" if 0 < n < SMALL_CELL else str(n)


def validate_aireadi(
    root: Path, config_dir: Path, strict_coverage: bool = False
) -> ValidationReport:
    """Validate the on-disk AI-READI layout at ``root`` against configs in ``config_dir``.

    ``strict_coverage`` asserts that this release is *complete*, promoting two findings from
    warning to error: a configured item the release does not ship, and a whole table the
    release does not ship. Both are off by default because completeness is a property of the
    release rather than a contract violation — a small fixture legitimately exercises a
    handful of items, and the VUMC synthetic release ships two of the six tables and still
    ingests correctly. Turn it on against a full release, where a config naming a variable or
    a table the data does not have silently emits nothing.

    What is an error *regardless*: a table that is present but malformed — a missing
    ``person_id``, a missing key column, an unreadable header. Those are contract violations
    at any coverage level.
    """
    report = ValidationReport()
    _validate_participants(root, config_dir, report, strict_coverage)
    _validate_person(root, config_dir, report, strict_coverage)
    _validate_visits(root, report, strict_coverage)
    _validate_long_table(
        root, config_dir, report, "conditions.yaml", "conditions", strict_coverage
    )
    _validate_measurements(root, config_dir, report, strict_coverage)
    _check_root_looks_like_a_release(root, report)
    return report


def _check_root_looks_like_a_release(root: Path, report: ValidationReport) -> None:
    """Finding *nothing* is a wrong root far more often than an empty release.

    Every per-table check reports its own table as absent, so pointing at the wrong
    directory produces five identical "not present in this release" lines and no hint that
    the release is fine and the path is not. Distinguishing the two costs one check and
    saves the reader guessing.

    The specific trap this exists for: an AI-READI download nests everything under a
    ``dataset/`` wrapper, so the root a user naturally passes is one level above the one the
    reader wants.

    ``clinical_data/`` sitting directly under the root is what tells the two apart. If it is
    there, the root is right and the release really is empty — which is a legitimate state
    the reader degrades through, so saying "wrong root" would be a lie. Only its absence,
    with nothing found, means the caller is almost certainly one directory too high.
    """
    if report.tables_checked or (root / "clinical_data").is_dir():
        return
    report.error(
        "-",
        f"no tables found under {root.name or root}/ — this usually means the root is wrong "
        f"rather than that the release is empty. The reader expects participants.tsv and "
        f"clinical_data/ directly beneath it; an AI-READI download nests those under a "
        f"dataset/ wrapper, so try the directory that contains clinical_data/.",
    )
    for candidate in ("dataset", "data"):
        nested = root / candidate / "clinical_data"
        if nested.is_dir():
            report.info("-", f"found clinical_data/ at {root.name}/{candidate}/ — try that root")


# -- per-table checks --------------------------------------------------------------
def _validate_participants(
    root: Path, config_dir: Path, report: ValidationReport, strict_coverage: bool = False
) -> None:
    mapping = _load(config_dir / "participants.yaml")
    if mapping is None:
        return
    path = root / mapping.get("file", "participants.tsv")
    header, rows = _read(path, mapping.get("delimiter", "\t"))
    if header is None:
        _note_missing_table(report, "participants", path.name, strict_coverage)
        return
    report.tables_checked.append("participants")
    for column in list(mapping.get("columns") or {}) + [mapping.get("id_column", "person_id")]:
        if column not in header:
            report.error("participants", f"mapped column absent from header: {column}")
    report.info("participants", f"{count(len(rows))} participant row(s)")


def _validate_person(
    root: Path, config_dir: Path, report: ValidationReport, strict_coverage: bool = False
) -> None:
    mapping = _load(config_dir / "person.yaml")
    if mapping is None:
        return
    path = root / "clinical_data" / "person.csv"
    header, rows = _read(path, ",")
    if header is None:
        _note_missing_table(report, "person", "person.csv", strict_coverage)
        return
    report.tables_checked.append("person")
    # A fully-redacted mapped column is the finding that stops a run of all-UNKNOWN_SEX
    # subjects from reading as a clean run.
    for column in mapping.get("columns") or {}:
        if column not in header:
            report.error("person", f"mapped column absent from header: {column}")
            continue
        filled = sum(1 for r in rows if not is_blank(r.get(column, ""), ("", " ", "0")))
        if rows and filled == 0:
            report.warning(
                "person",
                f"{column} is fully redacted in this release ({len(rows)}/{len(rows)} rows "
                f"blank or 0); Individual.sex will be unset",
            )


def _validate_visits(
    root: Path, report: ValidationReport, strict_coverage: bool = False
) -> None:
    header, rows = _read(root / "clinical_data" / "visit_occurrence.csv", ",")
    if header is None:
        _note_missing_table(report, "visit_occurrence", "visit_occurrence.csv", strict_coverage)
        return
    report.tables_checked.append("visit_occurrence")
    for column in ("visit_occurrence_id", "person_id"):
        if column not in header:
            report.error("visit_occurrence", f"required column absent from header: {column}")
    report.info("visit_occurrence", f"{count(len(rows))} visit row(s)")


def _validate_long_table(
    root: Path,
    config_dir: Path,
    report: ValidationReport,
    config_name: str,
    block: str,
    strict_coverage: bool = False,
) -> None:
    mapping = _load(config_dir / config_name)
    if mapping is None:
        return
    table = mapping.get("table", "?")
    header, rows = _read(root / "clinical_data" / f"{table}.csv", mapping.get("delimiter", ","))
    if header is None:
        _note_missing_table(report, table, f"{table}.csv", strict_coverage)
        return
    report.tables_checked.append(table)
    key_column = mapping.get("key_column", f"{table}_source_value")
    for column in (key_column, mapping.get("id_column", "person_id")):
        if column not in header:
            report.error(table, f"required column absent from header: {column}")
            return
    configured = mapping.get(block) or {}
    _compare_items(rows, key_column, configured, table, report, strict_coverage)


def _validate_measurements(
    root: Path, config_dir: Path, report: ValidationReport, strict_coverage: bool = False
) -> None:
    configs = sorted((config_dir / "measurement").glob("*.yaml"))
    if not configs:
        return
    measures: dict[str, Any] = {}
    for path in configs:
        measures.update((_load(path) or {}).get("measures") or {})
    header, rows = _read(root / "clinical_data" / "measurement.csv", ",")
    if header is None:
        _note_missing_table(report, "measurement", "measurement.csv", strict_coverage)
        return
    report.tables_checked.append("measurement")
    if "measurement_source_value" not in header:
        report.error("measurement", "required column absent from header: measurement_source_value")
        return
    _compare_items(
        rows, "measurement_source_value", measures, "measurement", report, strict_coverage
    )
    _check_declared_units(rows, measures, report)
    _check_laterality(rows, measures, report)


# -- shared checks -----------------------------------------------------------------
def _note_missing_table(
    report: ValidationReport, table: str, filename: str, strict_coverage: bool
) -> None:
    """An absent table is a completeness finding, not a contract violation.

    The reader degrades to ``tables_missing`` and still emits, which is the right behaviour
    for a partial release — the VUMC synthetic set ships two of the six tables and ingests
    correctly. So this warns by default and errors only under ``--strict-coverage``, which is
    where the caller is asserting the release is complete. Handling every absent table the
    same way is the point: participants/person/visit previously disagreed with each other.
    """
    note = report.error if strict_coverage else report.warning
    note(table, f"table not present in this release ({filename})")



def _compare_items(
    rows: list[dict[str, str]],
    key_column: str,
    configured: dict[str, Any],
    table: str,
    report: ValidationReport,
    strict_coverage: bool = False,
) -> None:
    """Set-difference the item keys on disk against the item keys the config maps."""
    on_disk = Counter(item_key(r.get(key_column, "")) for r in rows)
    on_disk.pop("", None)
    unmapped = sorted(set(on_disk) - set(configured))
    if unmapped:
        report.warning(
            table,
            f"{len(unmapped)} item(s) on disk have no config entry (not ingested): "
            + ", ".join(unmapped[:12])
            + (" ..." if len(unmapped) > 12 else ""),
        )
    missing = sorted(set(configured) - set(on_disk))
    if missing:
        # A config naming a variable the release does not ship emits nothing, silently —
        # worth an error against a full release, but expected for a fixture or a partial one.
        note = report.error if strict_coverage else report.warning
        note(
            table,
            f"{len(missing)} configured item(s) absent from this release: "
            + ", ".join(missing[:12])
            + (" ..." if len(missing) > 12 else ""),
        )
    placeholders = sorted(
        name for name, spec in configured.items() if is_placeholder(_term_of(spec))
    )
    if placeholders:
        report.warning(
            table,
            f"{len(placeholders)} configured item(s) unresolved (no output): "
            + ", ".join(placeholders),
        )


def _term_of(spec: Any) -> Any:
    """A conditions entry IS the term; a measures entry carries it under ``assay``."""
    return spec.get("assay", spec) if isinstance(spec, dict) and "assay" in spec else spec


def _check_declared_units(
    rows: list[dict[str, str]], measures: dict[str, Any], report: ValidationReport
) -> None:
    """Where the data carries a unit string, it must agree with the config's declared one."""
    seen: dict[str, set[str]] = {}
    for row in rows:
        key = item_key(row.get("measurement_source_value", ""))
        source_unit = (row.get("unit_source_value") or "").strip()
        if key in measures and source_unit and source_unit != "N/A":
            seen.setdefault(key, set()).add(source_unit)
    for key, units in sorted(seen.items()):
        declared = ((measures[key] or {}).get("unit") or {}).get("label")
        mismatched = {u for u in units if u != declared}
        if declared and mismatched:
            report.warning(
                "measurement",
                f"{key}: config declares unit {declared!r} but the data carries "
                f"{sorted(mismatched)!r}",
            )


def _check_laterality(
    rows: list[dict[str, str]], measures: dict[str, Any], report: ValidationReport
) -> None:
    """Cross-check the config's declared body_site against the qualifier column.

    The column is only a cross-check: six autorefraction items are per-eye by name and carry
    no qualifier at all, so its absence is expected and is reported as info, not a warning.
    """
    qualifiers: dict[str, set[str]] = {}
    for row in rows:
        key = item_key(row.get("measurement_source_value", ""))
        qualifier = (row.get("qualifier_source_value") or "").strip()
        if key in measures and qualifier:
            qualifiers.setdefault(key, set()).add(qualifier)
    sided = {k for k, spec in measures.items() if (spec or {}).get("body_site")}
    silent = sorted(k for k in sided if k not in qualifiers)
    if silent:
        report.info(
            "measurement",
            f"{len(silent)} item(s) declare a body_site the data does not qualify "
            f"(expected: per-eye by name): " + ", ".join(silent[:8]),
        )
    for key in sorted(sided & set(qualifiers)):
        declared = (measures[key]["body_site"] or {}).get("label", "")
        observed = {q.lower() for q in qualifiers[key]}
        if declared and declared.lower() not in observed:
            report.warning(
                "measurement",
                f"{key}: config declares body_site {declared!r} but the data qualifies "
                f"{sorted(qualifiers[key])!r}",
            )


# -- IO ------------------------------------------------------------------------------
def _load(path: Path) -> dict[str, Any] | None:
    return load_mapping(path) if path.exists() else None


def _read(path: Path, delimiter: str) -> tuple[list[str] | None, list[dict[str, str]]]:
    """Read a table into (header, rows). ``header`` is None only when the file is absent."""
    if not path.exists():
        return None, []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter=delimiter)
        rows = list(reader)
        header = list(reader.fieldnames) if reader.fieldnames else []
    return header, rows

"""`validate-aireadi` preflight semantics.

The distinction these tests pin is the one that decides whether the tool is usable at all:

- **Completeness** — a table or a configured item the release does not ship — is a property
  of the *release*, not a contract violation. The reader degrades to `tables_missing` and
  still emits, which is right for a partial release. So it warns by default and errors only
  under `--strict-coverage`, where the caller is asserting the release is complete.
- **Malformedness** — a table that is present but missing its `person_id` or key column — is
  a contract violation at any coverage level, and errors either way.

Getting that split wrong in either direction is costly: too strict and the tool cannot
preflight a partial release at all (which is what every source available in CI is); too lax
and a config naming a variable no release ships emits nothing, silently.
"""

from pathlib import Path

import pytest

from b2ai_dataset_ingest.sources.aireadi.validate import SMALL_CELL, count, validate_aireadi

CONFIG_DIR = Path(__file__).parents[1] / "config" / "aireadi"
FIXTURE = Path(__file__).parent / "data" / "aireadi"


# ---------- the strictness split
def test_partial_release_is_clean_by_default():
    """The fixture ships a handful of items on purpose; that must not be an error."""
    report = validate_aireadi(FIXTURE, CONFIG_DIR)
    assert report.errors == [], report.render()


def test_same_release_fails_under_strict_coverage():
    """Same input, asserting completeness -- now the unshipped items are errors."""
    report = validate_aireadi(FIXTURE, CONFIG_DIR, strict_coverage=True)
    assert report.errors, "expected configured-but-absent items to be errors"
    assert any("absent from this release" in f.message for f in report.errors)


def test_a_missing_table_warns_by_default_and_errors_under_strict(tmp_path):
    """An absent table is a completeness finding, and all five tables must agree.

    They did not always: participants errored while person and visit only warned, which made
    the tool exit non-zero on any release missing the cohort table -- including every source
    available in CI.
    """
    (tmp_path / "clinical_data").mkdir()

    lenient = validate_aireadi(tmp_path, CONFIG_DIR)
    assert lenient.errors == [], lenient.render()
    absent = {f.table for f in lenient.warnings if "table not present" in f.message}
    assert {"participants", "person", "visit_occurrence", "condition_occurrence",
            "measurement"} <= absent

    strict = validate_aireadi(tmp_path, CONFIG_DIR, strict_coverage=True)
    assert {f.table for f in strict.errors} >= absent


# ---------- malformedness is an error at any coverage level
@pytest.mark.parametrize("strict", [False, True])
def test_a_present_but_keyless_table_is_always_an_error(tmp_path, strict):
    clinical = tmp_path / "clinical_data"
    clinical.mkdir()
    # Present, parseable, and missing the column the reader groups on.
    (clinical / "condition_occurrence.csv").write_text(
        "condition_occurrence_id,condition_source_value\n1,\"mhoccur_hbp, High blood pressure\"\n"
    )
    report = validate_aireadi(tmp_path, CONFIG_DIR, strict_coverage=strict)
    assert any(
        f.table == "condition_occurrence" and "person_id" in f.message for f in report.errors
    ), report.render()


def test_a_redacted_column_is_reported_not_silently_accepted():
    """A run of all-UNKNOWN_SEX subjects must not look like a clean run."""
    report = validate_aireadi(FIXTURE, CONFIG_DIR)
    assert any(
        f.table == "person" and "fully redacted" in f.message for f in report.warnings
    ), report.render()


def test_unmapped_items_on_disk_are_surfaced():
    """A variable the data has and the config does not is 'present but not ingested'."""
    report = validate_aireadi(FIXTURE, CONFIG_DIR)
    messages = " ".join(f.message for f in report.findings)
    assert "mhoccur_nonesuch" in messages  # no config entry at all
    assert "moca_total_score" in messages  # deferred family


# ---------- PHI safety
def test_small_cells_are_suppressed():
    """A rare condition plus outside knowledge is a disclosure, so counts under 5 blur."""
    assert count(1) == "<5"
    assert count(SMALL_CELL - 1) == "<5"
    assert count(SMALL_CELL) == str(SMALL_CELL)
    assert count(0) == "0"


def test_no_finding_names_a_value_column():
    """Findings may name vocabulary and counts, never a value.

    `value_as_concept_id` is the subtle one -- it looks structural but *is* an answer.
    """
    forbidden = (
        "value_as_number",
        "value_as_string",
        "value_as_concept_id",
        "value_source_value",
        "range_low",
        "range_high",
    )
    report = validate_aireadi(FIXTURE, CONFIG_DIR, strict_coverage=True)
    for finding in report.findings:
        for column in forbidden:
            assert column not in finding.message, f"{column} leaked into: {finding.message}"


# ---------- a wrong root is not an empty release
def test_a_wrong_root_says_so_instead_of_reporting_five_absent_tables(tmp_path):
    """Finding nothing is far more often a wrong path than an empty release.

    Every per-table check reports its own table absent, so pointing one directory too high
    produces five identical "not present in this release" lines and no hint that the release
    is fine and the path is not. Observed costing a round trip against a real download, which
    nests everything under a `dataset/` wrapper.
    """
    (tmp_path / "dataset" / "clinical_data").mkdir(parents=True)
    report = validate_aireadi(tmp_path, CONFIG_DIR)

    root_findings = [f for f in report.findings if f.table == "-"]
    assert root_findings, "expected a finding about the root itself"
    assert any("root is wrong" in f.message for f in root_findings)
    # ...and it should point at where the tables actually are.
    assert any("dataset/" in f.message and "try that root" in f.message for f in root_findings)


def test_a_genuinely_partial_release_does_not_trigger_the_wrong_root_hint():
    """The hint must fire on *nothing found*, not on a release that ships some tables.

    The fixture ships all five, so this also guards against the check misfiring on a healthy
    root — which would make every clean run carry a spurious error.
    """
    report = validate_aireadi(FIXTURE, CONFIG_DIR)
    assert not [f for f in report.findings if f.table == "-"]
    assert report.tables_checked


def test_an_empty_release_with_clinical_data_present_is_not_called_a_wrong_root(tmp_path):
    """clinical_data/ at the root is what tells "empty release" from "wrong path" apart.

    If it is there the root is right and the release is genuinely empty — a state the reader
    degrades through — so claiming the path is wrong would be a lie. Only its absence, with
    nothing found, means the caller is a directory too high.
    """
    (tmp_path / "clinical_data").mkdir()
    report = validate_aireadi(tmp_path, CONFIG_DIR)
    assert not [f for f in report.findings if f.table == "-"]
    assert report.errors == [], report.render()

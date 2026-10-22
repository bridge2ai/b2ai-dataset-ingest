"""`validate-aireadi` on a full release: the DQD report and the modality datatypes.

Two things the mini release never exercised:

- **The release's own quality report.** ``clinical_data/dqd_omop.json`` is the OHDSI Data
  Quality Dashboard output. A failed check there is a known source-side issue, and surfacing
  it up front is what stops it being chased later as an ingest bug.
- **The non-clinical datatypes.** A full release ships eight datatype directories, each with
  a ``manifest.tsv``. The flagship delivery received 2026-10-22 had three of them missing
  (retinal_oct, retinal_octa, retinal_flio) and nothing noticed. ``--modalities`` makes an
  absent datatype a finding, and counts what is there from the manifests without walking
  the tree.
"""

import json
from pathlib import Path

from b2ai_dataset_ingest.sources.aireadi.validate import (
    MODALITY_DATATYPES,
    validate_aireadi,
)

CONFIG_DIR = Path(__file__).parents[1] / "config" / "aireadi"
FIXTURE = Path(__file__).parent / "data" / "aireadi"


def _findings(report, table):
    return [f for f in report.findings if f.table == table]


# ---------- the DQD report
def test_dqd_results_are_summarised_as_counts_and_check_names():
    report = validate_aireadi(FIXTURE, CONFIG_DIR)
    dqd = _findings(report, "dqd")
    assert dqd, report.render()
    messages = " ".join(f.message for f in dqd)
    assert "4 check(s), 2 failed" in messages
    assert "DataQualityDashboard 2.6.0" in messages
    # Failed checks are named by table and check -- DQD vocabulary, never a row.
    assert "person=1" in messages and "measurement=1" in messages
    assert "PERSON.GENDER_CONCEPT_ID:isRequired" in messages
    # Advisory: a failed source-side check is a warning, never an error.
    assert not [f for f in dqd if f.level == "error"]


def test_dqd_findings_never_carry_a_violated_row_count_or_note():
    """The violated-row count on a sparse table can be a small cell; the note is free text."""
    report = validate_aireadi(FIXTURE, CONFIG_DIR)
    for finding in _findings(report, "dqd"):
        assert "numViolatedRows" not in finding.message
        assert "redacted in the published release" not in finding.message


def test_a_missing_dqd_file_is_silent_and_a_malformed_one_only_warns(tmp_path):
    clinical = tmp_path / "clinical_data"
    clinical.mkdir()
    assert not _findings(validate_aireadi(tmp_path, CONFIG_DIR), "dqd")

    (clinical / "dqd_omop.json").write_text("{not json")
    report = validate_aireadi(tmp_path, CONFIG_DIR)
    assert [f.level for f in _findings(report, "dqd")] == ["warning"]

    (clinical / "dqd_omop.json").write_text(json.dumps({"Overview": {}}))
    report = validate_aireadi(tmp_path, CONFIG_DIR)
    assert any("no CheckResults" in f.message for f in _findings(report, "dqd"))


def test_dqd_counts_fall_back_to_the_rows_when_overview_is_absent(tmp_path):
    clinical = tmp_path / "clinical_data"
    clinical.mkdir()
    (clinical / "dqd_omop.json").write_text(
        json.dumps(
            {
                "CheckResults": [
                    {"checkName": "a", "cdmTableName": "PERSON", "failed": 1},
                    {"checkName": "b", "cdmTableName": "PERSON", "failed": 0},
                    {"checkName": "c", "cdmTableName": "OBSERVATION", "failed": "1"},
                ]
            }
        )
    )
    report = validate_aireadi(tmp_path, CONFIG_DIR)
    messages = " ".join(f.message for f in _findings(report, "dqd"))
    assert "3 check(s), 2 failed" in messages


# ---------- the modality datatypes
def test_modalities_are_not_checked_unless_asked():
    report = validate_aireadi(FIXTURE, CONFIG_DIR)
    assert not any(f.table in MODALITY_DATATYPES for f in report.findings)


def test_present_datatypes_are_counted_from_their_manifests():
    report = validate_aireadi(FIXTURE, CONFIG_DIR, modalities=True)
    ecg = " ".join(f.message for f in _findings(report, "cardiac_ecg"))
    # 4 manifest rows over 3 participants, two file paths each -> 8 paths; counts under 5 blur.
    assert "<5 manifest row(s), <5 participant(s), 8 file path(s)" in ecg
    assert "philips_tc30=8" in ecg
    env = " ".join(f.message for f in _findings(report, "environment"))
    assert "<5 manifest row(s), <5 participant(s), <5 file path(s)" in env
    assert "leelab_anura" in env
    assert any("modalities present: cardiac_ecg, environment" in f.message for f in report.findings)


def test_absent_datatypes_warn_by_default_and_error_under_strict():
    lenient = validate_aireadi(FIXTURE, CONFIG_DIR, modalities=True)
    absent = {f.table for f in lenient.warnings if "datatype directory not present" in f.message}
    assert absent == set(MODALITY_DATATYPES) - {"cardiac_ecg", "environment"}
    assert lenient.errors == [], lenient.render()

    strict = validate_aireadi(FIXTURE, CONFIG_DIR, strict_coverage=True, modalities=True)
    assert absent <= {f.table for f in strict.errors}


def test_the_participants_flag_is_cross_checked_against_the_manifest():
    """participants.tsv flags all three for cardiac_ecg; the manifest covers three. Agreement
    is silent. Flag a fourth and the disagreement is a warning, as a count, never an id."""
    report = validate_aireadi(FIXTURE, CONFIG_DIR, modalities=True)
    assert not any("participants.tsv flags" in f.message for f in _findings(report, "cardiac_ecg"))


def test_a_datatype_directory_without_a_manifest_is_an_error(tmp_path):
    (tmp_path / "clinical_data").mkdir()
    (tmp_path / "retinal_oct").mkdir()
    report = validate_aireadi(tmp_path, CONFIG_DIR, modalities=True)
    assert any(
        f.table == "retinal_oct" and f.level == "error" and "no manifest.tsv" in f.message
        for f in report.findings
    ), report.render()


def test_a_manifest_without_a_participant_column_is_an_error(tmp_path):
    (tmp_path / "clinical_data").mkdir()
    (tmp_path / "retinal_oct").mkdir()
    (tmp_path / "retinal_oct" / "manifest.tsv").write_text("filepath\tdevice\n/a/b\tx\n")
    report = validate_aireadi(tmp_path, CONFIG_DIR, modalities=True)
    assert any(
        f.table == "retinal_oct" and f.level == "error" and "participant_id" in f.message
        for f in report.findings
    ), report.render()


def test_modality_findings_name_no_participant_id():
    """The fixture ids are 9000xx; none may appear in a finding."""
    report = validate_aireadi(FIXTURE, CONFIG_DIR, modalities=True)
    for finding in report.findings:
        assert "9000" not in finding.message, finding.message

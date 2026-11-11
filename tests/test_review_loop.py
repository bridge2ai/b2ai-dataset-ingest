"""The Sheet-to-SSSOM review loop and the OMOP2OBO candidate importer.

The reviewer edits a sheet; the TSV stays the truth. What these tests pin:

- the review sheet carries every SSSOM column plus the review columns, keyed by a stable
  row key, so sorting or filtering the sheet changes nothing on pull;
- accept / change / reject each do exactly one thing, unreviewed rows are untouched, an
  unknown decision word is an error, and a rejected row is kept aside with its reason;
- OMOP2OBO proposals are labelled agree / differs / new against what the repo ships and
  never land in the apply path.
"""

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sheet = _load("sssom_sheet")

HEADER = [
    "subject_id", "subject_label", "predicate_id", "predicate_modifier", "object_id",
    "object_label", "mapping_justification", "confidence", "comment", "when_value",
    "when_subject", "evidence_code",
]


def _row(subject, obj, label, when="", modifier="", subj_cond=""):
    return {
        "subject_id": subject, "subject_label": "x", "predicate_id": "skos:relatedMatch",
        "predicate_modifier": modifier, "object_id": obj, "object_label": label,
        "mapping_justification": "semapv:ManualMappingCuration", "confidence": "0.8",
        "comment": "original", "when_value": when, "when_subject": subj_cond,
        "evidence_code": "ECO:0007307",
    }


ROWS = [
    _row("b2ai:measurement.a", "HP:0000001", "A", ">1"),
    _row("b2ai:measurement.a", "HP:0000001", "A", "<=1", modifier="Not"),
    _row("b2ai:measurement.b", "HP:0000002", "B", ">2"),
    _row("b2ai:measurement.b", "HP:0000002", "B", ">3", subj_cond='sex == "MALE"'),
]


def test_review_rows_carry_every_column_and_a_stable_key():
    columns, values = sheet.to_review_rows(ROWS, HEADER)
    assert columns == HEADER + sheet.REVIEW_COLUMNS
    assert len(values) == 4
    keys = [v[columns.index("row_key")] for v in values]
    assert len(set(keys)) == 4  # the stratum and the modifier are part of identity
    assert keys[3].endswith('sex == "MALE"')


def test_merge_applies_each_decision_and_leaves_the_rest_alone():
    columns, values = sheet.to_review_rows(ROWS, HEADER)
    review = [dict(zip(columns, v, strict=True)) for v in values]
    review[0]["decision"] = "accept"
    review[0]["reviewer_comment"] = "fine"
    review[1]["decision"] = "reject"
    review[1]["reviewer_comment"] = "no absent pole here"
    review[2]["decision"] = "change"
    review[2]["new_object_id"] = "HP:0000009"
    review[2]["new_object_label"] = "Nine"
    review[2]["new_when_value"] = ">20"
    review = list(reversed(review))  # a sorted sheet must not matter
    kept, rejected, tally = sheet.merge(ROWS, review, "orcid:0000-0000-0000-0000", "2026-10-22")
    assert tally == {"accept": 1, "reject": 1, "change": 1, "unreviewed": 1}
    assert len(kept) == 3 and len(rejected) == 1
    accepted = kept[0]
    assert accepted["reviewer_id"] == "orcid:0000-0000-0000-0000"
    assert accepted["comment"] == "original REVIEW 2026-10-22: fine"
    changed = kept[1]
    assert (changed["object_id"], changed["object_label"], changed["when_value"]) == (
        "HP:0000009", "Nine", ">20"
    )
    untouched = kept[2]
    assert untouched == ROWS[3]  # no reviewer_id, no date
    assert "REJECTED 2026-10-22" in rejected[0]["comment"]
    assert rejected[0]["predicate_modifier"] == "Not"


def test_unknown_decisions_and_changes_without_a_term_are_errors():
    columns, values = sheet.to_review_rows(ROWS, HEADER)
    review = [dict(zip(columns, v, strict=True)) for v in values]
    review[0]["decision"] = "maybe"
    with pytest.raises(ValueError):
        sheet.merge(ROWS, review, "x", "2026-10-22")
    review[0]["decision"] = "change"
    with pytest.raises(ValueError):
        sheet.merge(ROWS, review, "x", "2026-10-22")


def test_pull_dry_run_round_trips_a_shipped_set_through_csv(tmp_path):
    """Push the devices set to CSV, accept one row, pull without --write: validator clean,
    nothing written."""
    import shutil

    source = REPO / "mappings" / "b2ai-aireadi-devices.sssom.tsv"
    work = tmp_path / source.name
    shutil.copy(source, work)
    csv_path = tmp_path / "review.csv"
    sheet.push(work, str(csv_path))
    import csv as _csv

    with open(csv_path, newline="") as fh:
        rows = list(_csv.DictReader(fh))
    rows[0]["decision"] = "accept"
    rows[0]["reviewer_comment"] = "ok"
    with open(csv_path, "w", newline="") as fh:
        writer = _csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    before = work.read_text()
    assert sheet.pull(work, str(csv_path), "github:reviewer", write=False, rejected_dir=None) == 0
    assert work.read_text() == before
    assert sheet.pull(work, str(csv_path), "github:reviewer", write=True, rejected_dir=None) == 0
    after = work.read_text()
    assert after != before and "github:reviewer" in after
    assert "reviewer_id" in after.splitlines()[-1] or "reviewer_id" in _header(after)


def _header(text: str) -> str:
    return next(ln for ln in text.splitlines() if ln and not ln.startswith("#"))


# ---------- OMOP2OBO
openpyxl = pytest.importorskip("openpyxl")
omop2obo = _load("import_omop2obo")


def _workbook(path: Path, sheet_name: str, header: list[str], rows: list[list]):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)


def test_condition_candidates_are_labelled_against_what_we_ship(tmp_path):
    shipped = omop2obo.shipped_conditions()
    assert shipped["mhoccur_hbp"] == ("316866", "MONDO:0005044")
    path = tmp_path / "conditions.xlsx"
    _workbook(
        path,
        "OMOP2OBO_Mondo_Mapping_Results",
        ["CONCEPT_ID", "CONCEPT_NAME", "ONTOLOGY_URI", "ONTOLOGY_LABEL", "MAPPING_CATEGORY"],
        [
            [316866, "Hypertensive disorder", "MONDO_0005044", "hypertensive disorder",
             "Automatic One-to-One Concept"],
            [4159131, "Dyslipidemia", "MONDO_0002525", "inherited lipid metabolism disorder",
             "Automatic One-to-One Concept"],
            [201826, "Type 2 diabetes mellitus", "MONDO_0005148 | MONDO_0007452",
             "type 2 diabetes mellitus | MODY1", "Automatic One-to-Many Concept"],
            [999999999, "Not ours", "MONDO_0000001", "disease", "Manual One-to-One Concept"],
        ],
    )
    rows = omop2obo.condition_candidates(path)
    by = {(r["subject_id"], r["object_id"]): r for r in rows}
    assert by[("b2ai:condition_occurrence.mhoccur_hbp", "MONDO:0005044")]["status"] == "agree"
    assert by[("b2ai:condition_occurrence.mhoccur_clsh", "MONDO:0002525")]["status"] == "differs"
    assert by[("b2ai:condition_occurrence.mhterm_dm2", "MONDO:0007452")]["status"] == "differs"
    assert by[("b2ai:condition_occurrence.mhterm_dm2", "MONDO:0005148")]["confidence"] == "0.4"
    assert not any("999999999" in r["comment"] for r in rows)
    assert all(r["mapping_justification"] == "semapv:MappingChaining" for r in rows)


def test_measurement_candidates_skip_normal_rows_and_mark_poles(tmp_path):
    path = tmp_path / "measurements.xlsx"
    _workbook(
        path,
        "OMOP2OBO_HPO_Mapping_Results",
        ["CONCEPT_CODE", "CONCEPT_NAME", "RESULT_TYPE", "ONTOLOGY_URI", "ONTOLOGY_LABEL",
         "MAPPING_CATEGORY"],
        [
            ["2160-0", "Creatinine", "High", "HP_0003259", "elevated serum creatinine",
             "Manual One-to-One Concept"],
            ["2160-0", "Creatinine", "Low", "HP_0012101", "decreased serum creatinine",
             "Manual One-to-One Concept"],
            ["2160-0", "Creatinine", "Normal", "HP_0012100", "abnormal creatinine",
             "Manual One-to-One Concept"],
            ["1742-6", "ALT", "Low", "HP_0003199", "decreased muscle mass",
             "Manual One-to-One Concept"],
        ],
    )
    rows = omop2obo.measurement_candidates(path)
    assert {r["omop2obo_result_type"] for r in rows} == {"High", "Low"}
    creat = {r["omop2obo_result_type"]: r for r in rows if "creatinine" in r["subject_id"]}
    assert creat["High"]["status"] == "agree" and creat["Low"]["status"] == "agree"
    alt = [r for r in rows if "alt_got" in r["subject_id"]][0]
    assert alt["status"] == "new" and alt["object_id"] == "HP:0003199"
    assert "when_value" not in alt  # the gate is the curator's to set


def test_candidates_live_outside_the_apply_path():
    from b2ai_dataset_ingest.mapping.sssom_io import default_mapping_files

    assert not any("candidates" in str(p) for p in default_mapping_files(REPO))
    assert omop2obo.OUT.name == "candidates"

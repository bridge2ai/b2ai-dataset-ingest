"""The observation.csv families added in stream 2: falls, vision function, substance use; and
the MoCA total from measurement.csv.

Each is proven on the fixture: the Measurement it emits, the gate it fires (or deliberately
does not), and that the refusal code never reaches a gate.
"""

from pathlib import Path

import pytest

from b2ai_dataset_ingest.sources.aireadi import AireadiSource

CONFIG_DIR = Path(__file__).parents[1] / "config" / "aireadi"
FIXTURE = Path(__file__).parent / "data" / "aireadi"


@pytest.fixture
def source():
    src = AireadiSource(FIXTURE, CONFIG_DIR)
    return src, {p.individual.id: p for p in src.read()}


def _by_assay(participant, assay_id):
    return [m for m in participant.measurements if m.assay.id == assay_id]


def _features(participant, term):
    return [(f.excluded, f.description or "") for f in participant.phenotypic_features
            if f.type.id == term]


# ---------- falls
def test_falls_emit_measurements_and_gate_presence(source):
    _, participants = source
    p = participants["900002"]
    assert _by_assay(p, "LOINC:52552-7")[0].value_quantity.value == 1.0
    assert _by_assay(p, "LOINC:75856-5")[0].value_quantity.value == 3.0
    assert [e for e, _ in _features(p, "HP:0002527")] == [False]  # Falls present
    assert [e for e, _ in _features(p, "HP:0002359")] == [False]  # three falls -> Frequent falls
    assert "self-reported item" in _features(p, "HP:0002359")[0][1]


def test_a_declined_falls_answer_is_screened_before_any_gate(source):
    src, participants = source
    p = participants["900003"]
    assert not _by_assay(p, "LOINC:52552-7")
    assert not _features(p, "HP:0002527")
    assert src.report.sentinel_answers["observation.mhoccur_fall"] == 1


# ---------- vision function
def test_vision_difficulty_gates_on_moderate_or_worse(source):
    _, participants = source
    p = participants["900001"]
    assert _by_assay(p, "b2ai:observation.via1")[0].value_quantity.value == 4.0
    assert _by_assay(p, "LOINC:66082-9")[0].value_quantity.value == 2.0
    assert [e for e, _ in _features(p, "HP:0000505")] == [False]  # via1 = 4 -> Visual impairment
    assert _features(p, "HP:0001123") == []  # via3 = 2 is 'a little': no field defect


def test_the_access_barrier_item_is_dropped_by_policy_not_overlooked(source):
    src, _ = source
    assert src.report.items_dropped["observation.via5"] == 1
    assert "observation.via5" not in src.report.items_unmapped


# ---------- substance use
def test_smoking_items_emit_measurements_and_no_phenotype(source):
    _, participants = source
    p = participants["900001"]
    assert _by_assay(p, "LOINC:63581-3")[0].value_quantity.value == 1.0
    assert _by_assay(p, "LOINC:64234-8")[0].value_quantity.value == 0.0
    assert not [f for f in p.phenotypic_features if "susmk" in (f.description or "")]


# ---------- MoCA
def test_moca_total_is_a_measurement_without_a_reference_range(source):
    _, participants = source
    p = participants["900003"]
    total = _by_assay(p, "LOINC:72172-0")
    assert len(total) == 1 and total[0].value_quantity.value == 27.0
    assert total[0].value_quantity.reference_range is None  # 0-30 is a scoring range
    assert _features(p, "HP:0100543") == []  # 27 >= 26: nothing asserted, nothing excluded


def test_moca_below_cutoff_asserts_cognitive_impairment_present_only(tmp_path):
    import shutil

    root = tmp_path / "release"
    shutil.copytree(FIXTURE, root)
    path = root / "clinical_data" / "measurement.csv"
    before = path.read_text()
    text = before.replace(
        ',27.0,0,0,0.0,30.0,804,"moca_total_score"', ',22.0,0,0,0.0,30.0,804,"moca_total_score"'
    )
    assert text != before
    path.write_text(text)
    participants = {p.individual.id: p for p in AireadiSource(root, CONFIG_DIR).read()}
    assert [e for e, _ in _features(participants["900003"], "HP:0100543")] == [False]


# ---------- the LOINC tranche
def test_the_loinc_tranche_replaced_project_local_lab_ids():
    text = (CONFIG_DIR / "measurement" / "labs.yaml").read_text()
    for item, code in (
        ("import_protein_total", "2885-2"),
        ("import_c_peptide", "1986-9"),
        ("import_urine_albumin", "1754-1"),
        ("lbscat_rbc", "789-8"),
    ):
        assert f'LOINC:{code}' in text and f'b2ai:measurement.{item}' not in text
    eyes = (CONFIG_DIR / "measurement" / "ophthalmic.yaml").read_text()
    assert "LOINC:28687-2" in eyes and "b2ai:measurement.viaodsph" not in eyes

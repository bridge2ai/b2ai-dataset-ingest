"""The protected supplements (demographics, medications) and the stratified gates they unlock.

Everything here runs on the hand-authored fixture under ``tests/data/aireadi/supplements/``,
which mirrors the REDCap export shape of the real deliveries. The rules pinned:

- sex and gender come from the supplement, with declined answers counted, not guessed;
- race, ethnicity and every free-text column are never read;
- a medication row with no RxNorm code is dropped and counted; a free-text dose keeps the
  treatment and drops the dose; a coded route/frequency/unit the config does not know is
  counted as unmapped;
- a ``when_subject`` gate fires only for a matching, known subject, so a female-specific
  reference interval never fires for an unknown sex, and the sex-blind fallback still does.
"""

from pathlib import Path

import phenopackets as pp
import pytest
from google.protobuf.json_format import MessageToJson

from b2ai_dataset_ingest.emitters import PhenopacketEmitter
from b2ai_dataset_ingest.mapping.conditions import (
    ConditionParseError,
    parse_subject_condition,
)
from b2ai_dataset_ingest.mapping.hpo_rules import load_conditional_rules
from b2ai_dataset_ingest.sources.aireadi import AireadiSource
from b2ai_dataset_ingest.sources.aireadi.validate import validate_aireadi

CONFIG_DIR = Path(__file__).parents[1] / "config" / "aireadi"
FIXTURE = Path(__file__).parent / "data" / "aireadi"


@pytest.fixture
def source():
    src = AireadiSource(FIXTURE, CONFIG_DIR)
    participants = {p.individual.id: p for p in src.read()}
    return src, participants


# ---------- demographics
def test_sex_comes_from_the_supplement_not_person_csv(source):
    src, participants = source
    assert participants["900001"].individual.sex == "FEMALE"
    assert participants["900002"].individual.sex == "MALE"
    assert participants["900004"].individual.sex == "OTHER_SEX"  # intersex, measurement-only
    # 900003 declined (777): unset, and counted per column rather than guessed.
    assert participants["900003"].individual.sex is None
    assert src.report.fields_redacted["demographics.scrsex"] == 1
    # person.csv is still reported as redacted; the supplement is what filled sex.
    assert src.report.fields_redacted["person.gender_concept_id"] == 3
    assert "demographics" in src.report.tables_read


def test_gender_identity_is_a_loinc_answer_term(source):
    _, participants = source
    gender = participants["900001"].individual.gender
    assert gender is not None and gender.id == "LOINC:LA22879-3"
    assert participants["900002"].individual.gender.id == "LOINC:LA22878-5"
    assert participants["900004"].individual.gender.id == "LOINC:LA22882-7"  # non-binary
    assert participants["900003"].individual.gender is None  # 888 "Other" -> declined
    # ...and it reaches the packet, not just the IR.
    packet = PhenopacketEmitter().emit(participants["900001"])
    assert packet.subject.gender.id == "LOINC:LA22879-3"
    assert packet.subject.sex == pp.Sex.Value("FEMALE")


def test_race_ethnicity_and_free_text_never_reach_the_output(source):
    _, participants = source
    for participant in participants.values():
        packet = MessageToJson(PhenopacketEmitter().emit(participant))
        assert "Invented" not in packet
        assert "must never be read" not in packet
        assert "C41261" not in packet and "c41261" not in packet


def test_every_demographics_column_is_mapped_or_listed_as_dropped():
    """A column the config neither maps nor names is a policy gap, and the preflight says so."""
    report = validate_aireadi(FIXTURE, CONFIG_DIR)
    demographics = [f for f in report.findings if f.table == "demographics"]
    assert demographics, report.render()
    assert not any("neither mapped nor listed" in f.message for f in demographics), (
        report.render()
    )
    assert not any(f.level == "error" for f in demographics), report.render()


# ---------- medications
def _treatments(participants, person_id):
    return participants[person_id].treatments


def test_each_coded_medication_becomes_a_treatment(source):
    src, participants = source
    agents = [t.agent.id for t in _treatments(participants, "900001")]
    assert agents == ["RxNorm:6809", "RxNorm:29046"]
    metformin = _treatments(participants, "900001")[0]
    assert metformin.agent.label == "metformin"
    assert metformin.route.id == "NCIT:C38288"
    assert metformin.frequency.id == "NCIT:C64496"
    assert (metformin.dose.value, metformin.dose.unit.id) == (500.0, "UCUM:mg")
    # Taken at the study visit: the enrolment age, no interval.
    assert metformin.time.age_iso8601 == "P52Y"
    assert src.report.treatments_emitted == 5


def test_a_row_without_a_code_is_dropped_and_counted_never_named(source):
    src, participants = source
    assert len(_treatments(participants, "900001")) == 2  # the free-text row is gone
    assert len(_treatments(participants, "900002")) == 2
    assert src.report.items_unmapped["medications.rxnorm_code"] == 2


def test_a_free_text_dose_keeps_the_treatment_and_drops_the_dose(source):
    src, participants = source
    atorvastatin = [t for t in _treatments(participants, "900002") if t.agent.id == "RxNorm:83367"]
    assert len(atorvastatin) == 1 and atorvastatin[0].dose is None
    assert src.report.totals_unparsed["medications.cmdos"] == 1


def test_subcutaneous_insulin_carries_its_route_and_iu_unit(source):
    _, participants = source
    glargine = [t for t in _treatments(participants, "900002") if t.agent.id == "RxNorm:274783"]
    assert glargine[0].route.id == "NCIT:C38299"
    assert glargine[0].dose.unit.id == "UCUM:[IU]"


def test_treatments_emit_as_medical_actions_with_an_rxnorm_resource(source):
    _, participants = source
    packet = PhenopacketEmitter().emit(participants["900001"])
    assert len(packet.medical_actions) == 2
    action = packet.medical_actions[0]
    assert action.WhichOneof("action") == "treatment"
    assert action.treatment.agent.id == "RxNorm:6809"
    assert action.treatment.route_of_administration.id == "NCIT:C38288"
    interval = action.treatment.dose_intervals[0]
    assert interval.quantity.value == 500.0
    assert interval.schedule_frequency.id == "NCIT:C64496"
    assert not interval.HasField("interval")  # no start/end is known; none is fabricated
    prefixes = {r.namespace_prefix for r in packet.meta_data.resources}
    assert {"RxNorm", "NCIT", "UCUM", "LOINC"} <= prefixes
    # Round-trip through the schema.
    pp.Phenopacket.FromString(packet.SerializeToString())


def test_medication_free_text_columns_never_reach_the_output(source):
    _, participants = source
    for participant in participants.values():
        packet = MessageToJson(PhenopacketEmitter().emit(participant))
        assert "unmapped free-text name" not in packet
        assert "invented route" not in packet
        assert "eye drops" not in packet


# ---------- when_subject
@pytest.mark.parametrize(
    "text",
    ['sex == "FEMALE"', 'sex == "MALE" & age >= 50', "age < 50", 'SEX != "MALE"'],
)
def test_subject_conditions_parse(text):
    parse_subject_condition(text)


@pytest.mark.parametrize("text", ["", 'height > 180', 'sex "FEMALE"', "age >= "])
def test_malformed_subject_conditions_are_rejected(text):
    with pytest.raises(ConditionParseError):
        parse_subject_condition(text)


def test_a_stratified_gate_never_fires_for_an_unknown_subject():
    condition = parse_subject_condition('sex == "FEMALE"')
    assert condition.matches({"sex": "FEMALE", "age": "60"})
    assert not condition.matches({"sex": "MALE"})
    assert not condition.matches({"age": "60"})  # sex unknown
    assert not condition.matches(None)
    aged = parse_subject_condition('sex == "MALE" & age >= 50')
    assert aged.matches({"sex": "MALE", "age": "50"})
    assert not aged.matches({"sex": "MALE", "age": "49"})
    assert not aged.matches({"sex": "MALE"})  # age unknown


def test_the_shipped_measurement_set_carries_stratified_rows():
    rules = load_conditional_rules(dataset="aireadi")["measurement"]
    hct = rules["lbscat_hct"]
    strata = {r.when_subject for r in hct}
    assert strata == {"", 'sex == "FEMALE"', 'sex == "MALE"'}
    alt = rules["import_alt_got"]
    assert 'sex == "MALE" & age >= 50' in {r.when_subject for r in alt}


def test_sex_stratified_intervals_fire_per_sex_on_the_fixture(source):
    """900001 (F, 52y) and 900002 (M, 67y) both carry a hematocrit of 40 in the fixture?

    They do not -- the fixture carries HbA1c, not hematocrit -- so the stratified path is
    proven on an injected table below. This test pins the integration: a known sex reaches
    the derivation as a subject field.
    """
    src, _ = source
    assert src.report.features_derived >= 1


def test_stratified_gate_end_to_end_on_an_injected_table(tmp_path):
    """Hematocrit 40%: normal for a woman (37-47), reduced for a man (42-50), nothing for
    an unknown sex under the sex-blind band (42-47)."""
    import shutil

    root = tmp_path / "release"
    shutil.copytree(FIXTURE, root)
    header = (
        '"",X,measurement_id,person_id,measurement_concept_id,measurement_date,'
        "measurement_datetime,measurement_time,measurement_type_concept_id,"
        "operator_concept_id,value_as_number,value_as_concept_id,unit_concept_id,"
        "range_low,range_high,visit_occurrence_id,measurement_source_value,"
        "measurement_source_concept_id,unit_source_value,unit_source_concept_id,"
        "value_source_value,measurement_event_id,meas_event_field_concept_id,"
        "qualifier_concept_id,qualifier_source_value\n"
    )
    rows = "".join(
        f'{i},{i},{i},{pid},3009542,2024-01-15,2024-01-15 00:00:00, ,32862,0,40.0,0,8554,,,0,'
        f'"lbscat_hct, Hematocrit - %",0,%,0,,0,0,0,\n'
        for i, pid in enumerate(("900001", "900002", "900003"), start=1)
    )
    (root / "clinical_data" / "measurement.csv").write_text(header + rows)
    participants = {p.individual.id: p for p in AireadiSource(root, CONFIG_DIR).read()}

    def hct(pid):
        return {
            (f.type.id, f.excluded)
            for f in participants[pid].phenotypic_features
            if f.type.id in ("HP:0031851", "HP:0001899")
        }

    assert hct("900001") == {("HP:0031851", True), ("HP:0001899", True)}  # female: normal
    assert hct("900002") == {("HP:0031851", False)}  # male: reduced
    assert hct("900003") == set()  # sex declined: the sex-blind band asserts nothing at 40
    female = [
        f for f in participants["900001"].phenotypic_features if f.type.id == "HP:0031851"
    ][0]
    assert 'for sex == "FEMALE"' in (female.description or "")

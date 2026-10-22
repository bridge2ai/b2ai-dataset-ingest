"""The non-clinical datatypes: file links, ECG intervals, CGM and wearable summaries.

Pinned here:

- the clinical output is byte-identical unless ``files`` / ``device_summaries`` are asked
  for, so nothing a v1 consumer reads changes by default;
- a linked file is a ``File`` with release-relative URI and attributes, never an embedded
  payload, and ``folder`` mode collapses to one link per device directory with a count;
- ECG intervals come straight from the manifest, one set per recording, and gate through
  the same value-gated rules as any lab (with the QTc gate stratified by sex);
- a CGM series becomes the LOINC-defined range metrics, GMI, mean and CV over the window,
  and a non-numeric clamp (``"Low"``) is counted and skipped, never coerced;
- the packet-size rule holds: no sample from a series ever appears in the packet.
"""

import json
from pathlib import Path

import pytest
from google.protobuf.json_format import MessageToJson

from b2ai_dataset_ingest.emitters import PhenopacketEmitter
from b2ai_dataset_ingest.sources.aireadi import AireadiSource

CONFIG_DIR = Path(__file__).parents[1] / "config" / "aireadi"
FIXTURE = Path(__file__).parent / "data" / "aireadi"


def _read(**kwargs):
    src = AireadiSource(FIXTURE, CONFIG_DIR, **kwargs)
    return src, {p.individual.id: p for p in src.read()}


def _by_assay(participant, assay_id):
    return [m for m in participant.measurements if m.assay.id == assay_id]


# ---------- default output is unchanged
def test_nothing_is_linked_or_summarised_unless_asked():
    src, participants = _read()
    assert all(not p.files for p in participants.values())
    assert not _by_assay(participants["900001"], "LOINC:8636-3")
    assert src.report.files_linked == 0
    assert "cardiac_ecg" not in src.report.tables_read


def test_an_unknown_files_mode_is_refused():
    with pytest.raises(ValueError):
        AireadiSource(FIXTURE, CONFIG_DIR, files="all")


# ---------- file links
def test_per_file_links_carry_release_relative_uris_and_attributes():
    src, participants = _read(files="file")
    files = participants["900001"].files
    retinal = [f for f in files if f.attributes["datatype"] == "retinal_photography"]
    assert len(retinal) == 3
    first = retinal[0]
    assert first.uri.startswith("retinal_photography/cfp/icare_eidon/900001/")
    assert first.attributes["modality"] == "cfp"
    assert first.attributes["device"] == "icare_eidon"
    assert first.attributes["fileFormat"] == "DICOM"
    assert first.attributes["laterality"] == "R"
    assert first.attributes["protocol"] == "eidon_mosaic_cfp"
    ecg = [f for f in files if f.attributes["datatype"] == "cardiac_ecg"]
    assert {f.attributes["fileFormat"] for f in ecg} == {"WFDB"} and len(ecg) == 2
    assert any(f.attributes["datatype"] == "wearable_blood_glucose" for f in files)
    assert any(f.attributes["datatype"] == "environment" for f in files)
    # retinal 3 + ecg 2 + cgm 1 + wearable 2 + environment 1
    assert len(files) == 9
    assert src.report.files_linked >= 9
    # 900002 has two ECG recordings -> four WFDB files.
    ecg_2 = [f for f in participants["900002"].files if f.attributes["datatype"] == "cardiac_ecg"]
    assert len(ecg_2) == 4


def test_folder_mode_collapses_to_one_link_per_device_directory():
    _, participants = _read(files="folder")
    files = participants["900001"].files
    retinal = sorted(f.uri for f in files if f.attributes["datatype"] == "retinal_photography")
    assert retinal == [
        "retinal_photography/cfp/icare_eidon/900001/",
        "retinal_photography/ir/heidelberg_spectralis/900001/",
    ]
    cfp = [f for f in files if f.uri == "retinal_photography/cfp/icare_eidon/900001/"][0]
    assert cfp.attributes["fileCount"] == "2"
    assert "laterality" not in cfp.attributes  # per-file attributes do not survive the collapse


def test_links_emit_as_phenopacket_files_with_identifiers_and_a_rebased_uri():
    _, participants = _read(files="file")
    packet = PhenopacketEmitter(file_uri_base="https://example.org/aireadi/v3").emit(
        participants["900001"]
    )
    assert len(packet.files) == 9
    first = packet.files[0]
    assert first.uri.startswith("https://example.org/aireadi/v3/")
    assert first.individual_to_file_identifiers["900001"] == "900001"
    assert first.file_attributes["datatype"]
    # ...and relative by default.
    plain = PhenopacketEmitter().emit(participants["900001"])
    assert not plain.files[0].uri.startswith("http")


# ---------- ECG intervals
def test_ecg_intervals_come_from_the_manifest_one_set_per_recording():
    src, participants = _read(device_summaries=True)
    qtc = _by_assay(participants["900002"], "LOINC:8636-3")
    assert sorted(m.value_quantity.value for m in qtc) == [401.0, 432.0]
    assert {m.description for m in qtc} == {
        "12-lead ECG recording 900002_ecg_bbbb0002",
        "12-lead ECG recording 900002_ecg_bbbb0003",
    }
    assert qtc[0].value_quantity.unit.id == "UCUM:ms"
    assert qtc[0].time is not None and qtc[0].time.age_iso8601 == "P67Y"
    # A blank PR cell emits nothing for that interval and nothing else is lost.
    assert not _by_assay(participants["900003"], "LOINC:8625-6")
    assert _by_assay(participants["900003"], "LOINC:8867-4")
    assert "cardiac_ecg" in src.report.tables_read


def test_ecg_gates_fire_per_recording_and_stratify_qtc_by_sex():
    _, participants = _read(device_summaries=True)

    def features(pid, term):
        return [(f.excluded, f.description) for f in participants[pid].phenotypic_features
                if f.type.id == term]

    # 900002 (male): PR 224 -> prolonged PR present; PR 190 -> absent, different recording,
    # same enrolment age -> presence wins after reconciliation.
    pr = features("900002", "HP:0012248")
    assert [excluded for excluded, _ in pr] == [False]
    # QTc 432 and 401 for a man: both within the male normal (<= 450) -> absent only.
    qtc = features("900002", "HP:0001657")
    assert [excluded for excluded, _ in qtc] == [True]
    assert 'for sex == "MALE"' in qtc[0][1]
    # 900001 (female): QTc 415 -> within normal for women; rate 64 -> normal.
    assert [e for e, _ in features("900001", "HP:0001657")] == [True]
    assert [e for e, _ in features("900001", "HP:0001662")] == [True]
    # 900002 rate 58 (bradycardia) then 61 (normal): presence wins.
    assert [e for e, _ in features("900002", "HP:0001662")] == [False]


# ---------- CGM summaries
@pytest.fixture
def short_window_config(tmp_path):
    """The shipped floor is one day of readings; the fixture series is an hour."""
    import shutil

    import yaml

    config = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, config)
    path = config / "modalities" / "wearable_blood_glucose.yaml"
    mapping = yaml.safe_load(path.read_text())
    mapping["series"]["min_readings"] = 10
    path.write_text(yaml.safe_dump(mapping))
    return config


def test_a_series_shorter_than_the_floor_is_counted_not_summarised():
    src, participants = _read(device_summaries=True)
    assert not _by_assay(participants["900001"], "LOINC:97506-0")
    assert src.report.items_unresolved["wearable_blood_glucose.cgm:too_few_readings"] == 1


def test_cgm_summaries_follow_the_loinc_range_definitions(short_window_config):
    src = AireadiSource(FIXTURE, short_window_config, device_summaries=True)
    participants = {p.individual.id: p for p in src.read()}
    p = participants["900001"]

    def value(assay_id):
        return _by_assay(p, assay_id)[0].value_quantity.value

    # 11 numeric readings: 60,100,120,140,160,180,200,220,260,300,150; "Low" skipped.
    assert value("b2ai:wearable_blood_glucose.cgm_mean") == pytest.approx(171.82, abs=0.01)
    assert value("LOINC:97510-2") == pytest.approx(54.55, abs=0.01)  # 70-180: 6 of 11
    assert value("LOINC:104641-6") == pytest.approx(9.09, abs=0.01)  # 54-69: the 60
    assert value("LOINC:104642-4") == 0.0  # < 54: none
    assert value("LOINC:104640-8") == pytest.approx(18.18, abs=0.01)  # 181-250: 200, 220
    assert value("LOINC:104639-0") == pytest.approx(18.18, abs=0.01)  # > 250: 260, 300
    gmi = _by_assay(p, "LOINC:97506-0")[0]
    assert gmi.value_quantity.value == pytest.approx(3.31 + 0.02392 * 171.82, abs=0.01)
    assert "11 readings over 1 day(s)" in gmi.description
    assert gmi.time is not None and gmi.time.age_iso8601 == "P52Y"
    # The clamp was counted, not coerced.
    assert src.report.sentinel_answers["wearable_blood_glucose.cgm"] == 1
    # GMI 7.42 >= 7.0 -> hyperglycemia present (pending review, confidence 0.7).
    assert any(f.type.id == "HP:0003074" and not f.excluded for f in p.phenotypic_features)


def test_a_missing_series_file_is_counted_not_fatal():
    src, participants = _read(device_summaries=True)
    assert not _by_assay(participants["900002"], "LOINC:97506-0")
    assert src.report.items_unmapped["wearable_blood_glucose.glucose_filepath:missing"] == 1


# ---------- wearable summaries
def test_wearable_heart_rate_and_spo2_are_window_statistics_without_gates():
    _, participants = _read(device_summaries=True)
    p = participants["900001"]
    hr = _by_assay(p, "b2ai:wearable_activity_monitor.hr_mean")[0]
    assert hr.value_quantity.value == pytest.approx(74.5, abs=0.01)
    assert _by_assay(p, "b2ai:wearable_activity_monitor.hr_min")[0].value_quantity.value == 55.0
    assert "8 readings over 2 day(s)" in hr.description
    assert _by_assay(p, "b2ai:wearable_activity_monitor.spo2_min")[0].value_quantity.value == 94.0
    # No wearable-derived phenotype, by policy.
    assert not [f for f in p.phenotypic_features if "wearable_activity" in (f.description or "")]
    # 900003 has a manifest row but no file on disk: counted, nothing emitted.
    assert not _by_assay(participants["900003"], "b2ai:wearable_activity_monitor.hr_mean")


# ---------- the size rule
def test_no_series_sample_reaches_the_packet():
    _, participants = _read(files="file", device_summaries=True)
    packet = json.loads(MessageToJson(PhenopacketEmitter().emit(participants["900001"])))
    # The CGM file holds a reading of 260 and a timestamp of 08:40; neither may appear.
    text = json.dumps(packet)
    assert "2024-01-16T08:40" not in text
    assert "transmitter" not in text
    assert len(packet["measurements"]) < 60


def test_fixture_payload_files_carry_only_invented_ids():
    for path in FIXTURE.rglob("*.json"):
        if path.name == "dqd_omop.json":
            continue
        payload = json.loads(path.read_text())
        assert payload["header"]["patient_id"].startswith("AIREADI-9000")

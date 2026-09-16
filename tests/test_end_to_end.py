"""End-to-end: reader -> emitter -> canonical JSON, with round-trip + MetaData checks.

The multisession fixture always runs. The real-synthetic-data slice runs only when the
public synthetic tables have been fetched (``scripts/fetch_synthetic_data.sh``).
"""

import json
from pathlib import Path

import phenopackets as pp
import pytest
from google.protobuf.json_format import Parse

from b2ai_dataset_ingest.emitters import PhenopacketEmitter
from b2ai_dataset_ingest.sources.voice import VoiceSource

CONFIG_DIR = Path(__file__).parents[1] / "config" / "voice"
SYNTHETIC = (
    Path(__file__).parents[1]
    / "data"
    / "synthetic"
    / "voice_dgp"
    / "b2ai-voice-synthetic-phenotype"
    / "output"
    / "phenotype"
)


def _prefixes_used(pkt: pp.Phenopacket) -> set[str]:
    """Every CURIE prefix referenced by an OntologyClass in the packet."""
    used: set[str] = set()

    def note(ontology_class) -> None:
        if ontology_class and ontology_class.id:
            used.add(ontology_class.id.split(":", 1)[0])

    note(pkt.subject.taxonomy)
    for disease in pkt.diseases:
        note(disease.term)
        if disease.HasField("onset") and disease.onset.HasField("ontology_class"):
            note(disease.onset.ontology_class)
    for measurement in pkt.measurements:
        note(measurement.assay)
        if measurement.value.HasField("quantity"):
            note(measurement.value.quantity.unit)
        if measurement.HasField("time_observed") and measurement.time_observed.HasField(
            "ontology_class"
        ):
            note(measurement.time_observed.ontology_class)
    return {prefix for prefix in used if prefix}


def test_multisession_end_to_end_roundtrip(multisession_dir: Path, tmp_path: Path):
    participants = list(VoiceSource(root=multisession_dir, config_dir=CONFIG_DIR).read())
    written = PhenopacketEmitter().write_all(participants, tmp_path)
    assert written == 1

    out = tmp_path / "ms-0001.json"
    assert out.exists()

    # Round-trips through the canonical protobuf JSON parser.
    parsed = Parse(out.read_text(), pp.Phenopacket())
    assert parsed.subject.id == "ms-0001"

    # MetaData declares a Resource for every ontology prefix used.
    declared = {resource.namespace_prefix for resource in parsed.meta_data.resources}
    assert _prefixes_used(parsed) <= declared
    # Concretely: PHQ-9 (LOINC) + Parkinson (MONDO) + baseline/followup (NCIT) + taxon +
    # unit (UCUM) + VHI-10 custom item/total codes (b2ai).
    assert {"LOINC", "MONDO", "NCIT", "NCBITaxon", "UCUM", "b2ai"} <= declared

    # The precomputed VHI-10 total (vhi_10_calc_score=17) is emitted as a Measurement.
    totals = [m for m in parsed.measurements if m.assay.id == "b2ai:vhi10.total"]
    assert len(totals) == 1
    assert totals[0].value.quantity.value == 17.0


@pytest.mark.skipif(
    not SYNTHETIC.is_dir(),
    reason="synthetic data not fetched (run scripts/fetch_synthetic_data.sh)",
)
def test_real_synthetic_slice_roundtrips(tmp_path: Path):
    participants = list(VoiceSource(root=SYNTHETIC, config_dir=CONFIG_DIR).read())
    assert len(participants) > 100  # union across demographics/diagnosis/questionnaires

    written = PhenopacketEmitter().write_all(participants, tmp_path)
    assert written == len(participants)

    # Every emitted file parses and has complete MetaData resources.
    for path in sorted(tmp_path.glob("*.json")):
        parsed = Parse(path.read_text(), pp.Phenopacket())
        declared = {resource.namespace_prefix for resource in parsed.meta_data.resources}
        assert _prefixes_used(parsed) <= declared


# --------------------------------------------------------- re-running the ingest


def _run_voice(tmp_path: Path, *extra: str):
    """Invoke ``b2ai-ingest voice`` against the synthetic fixture."""
    from typer.testing import CliRunner

    from b2ai_dataset_ingest.cli import app

    return CliRunner().invoke(
        app,
        ["voice", "--input", str(SYNTHETIC), "--output", str(tmp_path), *extra],
    )


# ------------------------------------------- restricting the questionnaire battery

BATTERY = "phq9,gad7_anxiety,vhi10,voice_perception"


@pytest.mark.skipif(not SYNTHETIC.is_dir(), reason="synthetic fixture not fetched")
def test_questionnaires_restricts_which_tables_are_read(tmp_path: Path):
    """Every HPO term comes from a questionnaire, so naming a subset is what bounds
    the phenotype vocabulary every participant draws from."""
    full = _run_voice(tmp_path / "full")
    limited = _run_voice(tmp_path / "limited", "--questionnaires", BATTERY)
    assert full.exit_code == limited.exit_code == 0

    assert "restricted to gad7_anxiety, phq9, vhi10, voice_perception" in limited.output
    # Same participants either way: naming questionnaires does not drop anyone.
    assert len(list((tmp_path / "limited").glob("*.json"))) == len(
        list((tmp_path / "full").glob("*.json"))
    )
    # But fewer terms, since the excluded questionnaires contributed some.
    def features(d: Path) -> int:
        return sum(
            len(json.loads(p.read_text()).get("phenotypicFeatures", []))
            for p in d.glob("*.json")
        )

    assert features(tmp_path / "limited") < features(tmp_path / "full")


@pytest.mark.skipif(not SYNTHETIC.is_dir(), reason="synthetic fixture not fetched")
def test_require_all_questionnaires_drops_partial_coverage(tmp_path: Path):
    """A participant never offered one of the named questionnaires has an absent term
    for an administrative reason, not a clinical one. That is the confound."""
    kept = _run_voice(tmp_path / "kept", "--questionnaires", BATTERY)
    strict = _run_voice(
        tmp_path / "strict", "--questionnaires", BATTERY, "--require-all-questionnaires"
    )
    assert kept.exit_code == strict.exit_code == 0

    n_kept = len(list((tmp_path / "kept").glob("*.json")))
    n_strict = len(list((tmp_path / "strict").glob("*.json")))
    assert 0 < n_strict < n_kept
    assert "dropped (partial)" in strict.output
    # Without the flag the confound is still there, and must be reported rather than
    # left for someone to discover in the clustering.
    assert "partial coverage" in kept.output


@pytest.mark.skipif(not SYNTHETIC.is_dir(), reason="synthetic fixture not fetched")
def test_require_all_questionnaires_needs_a_battery(tmp_path: Path):
    result = _run_voice(tmp_path, "--require-all-questionnaires")
    assert result.exit_code == 2
    assert "--questionnaires" in result.output
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.skipif(not SYNTHETIC.is_dir(), reason="synthetic fixture not fetched")
def test_output_directory_is_owner_only(tmp_path: Path):
    """Phenopackets are per-participant records; the default umask leaves them
    group- and world-readable, which is wrong for anything derived from the
    source datasets."""
    target = tmp_path / "packets"
    assert _run_voice(target).exit_code == 0
    assert target.stat().st_mode & 0o777 == 0o700

    # And an existing loose directory gets tightened rather than left alone.
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    assert _run_voice(loose).exit_code == 0
    assert loose.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(not SYNTHETIC.is_dir(), reason="synthetic fixture not fetched")
def test_rerun_into_populated_output_is_refused(tmp_path: Path):
    """A second run must not silently union itself with the first."""
    assert _run_voice(tmp_path).exit_code == 0
    before = {p.name for p in tmp_path.glob("*.json")}
    assert before

    result = _run_voice(tmp_path)
    assert result.exit_code == 2
    assert "--force" in result.output
    # The message must say what happened, not only what a re-run would have done:
    # the reader's first question is whether their existing output survived.
    assert "nothing was written" in result.output
    assert "unchanged" in result.output
    # And it must be unambiguous that --force destroys rather than merges, since
    # that is the question a reader actually has before typing it.
    assert "DELETES" in result.output
    assert "NOT merge" in result.output
    # And it must actually be true.
    assert {p.name for p in tmp_path.glob("*.json")} == before


@pytest.mark.skipif(not SYNTHETIC.is_dir(), reason="synthetic fixture not fetched")
def test_force_leaves_exactly_the_current_cohort(tmp_path: Path):
    """``--force`` clears the leftovers, which is the whole reason it exists.

    A participant no longer in the cohort keeps their file on a plain re-run,
    because the emitter only rewrites ids it still sees.
    """
    assert _run_voice(tmp_path).exit_code == 0
    cohort = {p.name for p in tmp_path.glob("*.json")}

    orphan = tmp_path / "participant-from-an-earlier-cohort.json"
    orphan.write_text("{}")

    result = _run_voice(tmp_path, "--force")
    assert result.exit_code == 0
    assert not orphan.exists()
    assert {p.name for p in tmp_path.glob("*.json")} == cohort


@pytest.mark.skipif(not SYNTHETIC.is_dir(), reason="synthetic fixture not fetched")
def test_force_keeps_the_old_set_when_the_ingest_fails(tmp_path: Path, monkeypatch):
    """Refuse early, delete late.

    Deleting before the source is read would leave a failed run with neither the
    previous cohort nor a new one, which on a real cohort is not recoverable.
    """
    assert _run_voice(tmp_path).exit_code == 0
    before = {p.name for p in tmp_path.glob("*.json")}
    assert before

    def explode(self):
        raise RuntimeError("source blew up mid-read")

    monkeypatch.setattr(VoiceSource, "read", explode)
    result = _run_voice(tmp_path, "--force")

    assert result.exit_code != 0
    assert {p.name for p in tmp_path.glob("*.json")} == before

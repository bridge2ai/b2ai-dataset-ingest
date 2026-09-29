"""AireadiSource against the hand-authored OMOP fixture, end to end.

The fixture is deliberately tiny and every row in it exists to pin one rule — see
``tests/data/aireadi/README.md``. These tests assert the rules, not the row counts, so a
config change that legitimately adds an item does not break them.
"""

from pathlib import Path

import phenopackets as pp
import pytest
from google.protobuf.json_format import Parse

from b2ai_dataset_ingest.emitters import PhenopacketEmitter
from b2ai_dataset_ingest.sources.aireadi import AireadiSource

CONFIG_DIR = Path(__file__).parents[1] / "config" / "aireadi"
FIXTURE = Path(__file__).parent / "data" / "aireadi"

# The VUMC synthetic release. Fetched locally by scripts/fetch_aireadi_synthetic.sh and
# gitignored, so these tests skip on a bare checkout and in CI.
VUMC = Path(__file__).parents[1] / "data" / "synthetic" / "aireadi"


@pytest.fixture
def participants():
    return {p.individual.id: p for p in AireadiSource(FIXTURE, CONFIG_DIR).read()}


@pytest.fixture
def source_report():
    source = AireadiSource(FIXTURE, CONFIG_DIR)
    list(source.read())
    return source.report


# ---------- the participant universe
def test_every_person_gets_a_packet_including_one_seen_only_in_measurement(participants):
    # 900004 appears in no participants.tsv row and no person.csv row.
    assert set(participants) == {"900001", "900002", "900003", "900004"}


def test_age_comes_from_participants_tsv(participants):
    assert participants["900001"].individual.age_iso8601 == "P52Y"
    # 900004 is measurement-only: no age, but still a valid Individual.
    assert participants["900004"].individual.age_iso8601 is None


def test_sex_is_unset_because_the_release_redacts_it(participants, source_report):
    assert all(p.individual.sex is None for p in participants.values())
    # ...and that is reported rather than passing as a clean run.
    assert source_report.fields_redacted["person.gender_concept_id"] == 3


def test_study_group_is_a_measurement_not_a_disease(participants):
    packet = participants["900002"]
    groups = [m for m in packet.measurements if m.assay.id == "b2ai:participants.study_group"]
    assert len(groups) == 1
    assert groups[0].value_term.id.endswith("insulin_dependent")
    # The arm is never asserted as a diagnosis.
    assert all("study_group" not in d.term.id for d in packet.diseases)


def test_cohort_is_recorded_as_provenance(participants):
    assert participants["900003"].cohort["clinical_site"] == "UAB"
    assert participants["900003"].cohort["recommended_split"] == "test"


# ---------- conditions
def test_conditions_map_by_item_key_and_dedupe(participants):
    # 900002 has mhterm_dm2 twice; it must yield one Disease.
    terms = [d.term.id for d in participants["900002"].diseases]
    assert terms.count("MONDO:0005148") == 1


def test_no_disease_carries_an_onset(participants):
    """condition_start_date is the medical-history form-fill date, not an onset."""
    assert all(d.onset is None for p in participants.values() for d in p.diseases)


def test_unresolved_and_unmapped_conditions_are_skipped_and_counted(
    participants, source_report
):
    emitted = {d.term.id for p in participants.values() for d in p.diseases}
    assert not any(t == "TODO" for t in emitted)
    # mhoccur_ua is configured but unresolved; mhoccur_nonesuch has no entry at all.
    assert source_report.placeholders_skipped["condition_occurrence.mhoccur_ua"] == 1
    assert source_report.items_unmapped["condition_occurrence.mhoccur_nonesuch"] == 1


def test_a_participant_with_no_conditions_still_emits(participants):
    assert participants["900003"].diseases == []
    assert participants["900003"].measurements  # but does have measurements


# ---------- measurements
def _by_assay(packet, assay_id):
    return [m for m in packet.measurements if m.assay.id == assay_id]


def test_two_readings_of_one_assay_stay_distinguishable(participants):
    readings = _by_assay(participants["900001"], "LOINC:8480-6")
    assert len(readings) == 2
    assert {m.description for m in readings} == {"First reading", "Second reading"}


def test_censored_values_are_dropped_not_reported_as_measured(participants, source_report):
    assert _by_assay(participants["900001"], "LOINC:30934-4") == []
    assert source_report.values_censored["measurement.import_nt_probnp"] == 1


def test_an_unrecorded_operator_still_emits(participants):
    """operator_concept_id == 0 is "not recorded", not "censored"."""
    assert _by_assay(participants["900001"], "b2ai:measurement.viaodplog")


def test_refusal_sentinels_are_dropped(participants, source_report):
    assert _by_assay(participants["900003"], "LOINC:4548-4") == []
    assert source_report.sentinel_answers["measurement.import_hba1c"] == 1


def test_two_sided_reference_range_is_emitted(participants):
    quantity = _by_assay(participants["900001"], "LOINC:4548-4")[0].value_quantity
    assert quantity.reference_range is not None
    assert (quantity.reference_range.low, quantity.reference_range.high) == (4.8, 5.6)


def test_one_sided_reference_range_is_dropped_and_counted(participants, source_report):
    quantity = _by_assay(participants["900002"], "LOINC:2160-0")[0].value_quantity
    assert quantity.reference_range is None
    assert source_report.reference_ranges_one_sided >= 1


def test_laterality_comes_from_config_including_the_unqualified_item(participants):
    right = _by_assay(participants["900001"], "b2ai:measurement.viaodplog")[0]
    left = _by_assay(participants["900001"], "b2ai:measurement.viaosplog")[0]
    assert right.procedure.body_site.id == "UBERON:0004549"
    assert left.procedure.body_site.id == "UBERON:0004548"
    # viaodsph carries NO qualifier column value at all, yet must still be sided.
    sphere = _by_assay(participants["900001"], "b2ai:measurement.viaodsph")[0]
    assert sphere.procedure.body_site.id == "UBERON:0004549"


def test_a_row_without_a_visit_falls_back_to_its_own_date(participants, source_report):
    diastolic = _by_assay(participants["900002"], "LOINC:8462-4")[0]
    assert diastolic.time is not None
    assert source_report.rows_without_visit >= 1


def test_two_visits_a_year_apart_stay_distinguishable(participants):
    """The case that silently collapsed before ages were derived per row.

    900002 has an HbA1c at each of two visits ~15 months apart. At age precision a
    TimeElement carries an age and nothing else, so if both rows reused the cohort table's
    single age the two timepoints would be byte-identical in the output — two readings more
    than a year apart, indistinguishable.
    """
    readings = _by_assay(participants["900002"], "LOINC:4548-4")
    assert len(readings) == 2
    ages = {m.time.age_iso8601 for m in readings}
    assert ages == {"P67Y", "P68Y"}, f"expected two distinct derived ages, got {ages}"


def test_derived_age_does_not_leak_the_date(participants):
    """The anchor date is read for the arithmetic only; it must not reach the output."""
    times = [m.time for m in participants["900002"].measurements if m.time]
    assert times and all(t.timestamp is None for t in times)


def test_default_time_precision_emits_age_never_a_date(participants):
    times = [m.time for p in participants.values() for m in p.measurements if m.time]
    assert times, "expected time-stamped measurements"
    assert all(t.timestamp is None for t in times)
    assert any(t.age_iso8601 for t in times)


# ---------- emitter round-trip
def test_end_to_end_roundtrip_and_metadata_completeness(tmp_path):
    written = PhenopacketEmitter().write_all(
        AireadiSource(FIXTURE, CONFIG_DIR).read(), tmp_path
    )
    assert written == 4
    for path in sorted(tmp_path.glob("*.json")):
        parsed = Parse(path.read_text(), pp.Phenopacket())
        declared = {r.namespace_prefix.upper() for r in parsed.meta_data.resources}
        assert _prefixes_used(parsed) <= declared
        assert "TODO" not in path.read_text()


def _prefixes_used(packet: pp.Phenopacket) -> set[str]:
    """Every CURIE prefix in the built message, via the emitter's own walker.

    Reusing the emitter's walker rather than a hand-rolled one is deliberate: a narrower
    local copy would let a newly-emitted block (a Procedure body_site, say) fall outside the
    ``<= declared`` invariant instead of failing loudly. It upper-cases (``prefix_of`` does,
    so a registry lookup is case-insensitive), so the caller compares upper-cased too.
    """
    from b2ai_dataset_ingest.emitters.phenopacket import _collect_prefixes

    return {p for p in _collect_prefixes(packet) if p}


# ---------- the VUMC synthetic release: local only, skipped in CI
requires_vumc = pytest.mark.skipif(
    not (VUMC / "clinical_data" / "measurement.csv").is_file(),
    reason="VUMC synthetic release not fetched (scripts/fetch_aireadi_synthetic.sh)",
)


@requires_vumc
def test_partial_release_degrades_to_missing_tables_rather_than_crashing():
    """The VUMC set ships 2 of the 6 tables and one index column instead of two.

    A release missing four tables must degrade to `tables_missing` and still emit, not
    crash — which is also what a future release adding a table has to survive.
    """
    source = AireadiSource(VUMC, CONFIG_DIR)
    first = next(iter(source.read()), None)
    assert first is not None
    assert {"participants", "person", "visit_occurrence"} <= set(source.report.tables_missing)
    assert {"condition_occurrence", "measurement"} <= set(source.report.tables_read)


@requires_vumc
def test_synthetic_release_emits_real_content_and_no_placeholder_curie(tmp_path):
    """Scale plus content, bounded so the test stays quick.

    10,518 synthetic participants and 767,814 measurement rows: the point is that the reader
    streams them (materializing this table costs ~1.2 GB of RSS against ~18 MB streamed) and
    that nothing unresolved leaks into a packet.
    """
    import itertools

    source = AireadiSource(VUMC, CONFIG_DIR)
    packets = list(itertools.islice(source.read(), 25))
    assert len(packets) == 25
    assert any(p.diseases for p in packets)
    assert any(p.measurements for p in packets)

    written = PhenopacketEmitter().write_all(packets, tmp_path)
    assert written == 25
    for path in tmp_path.glob("*.json"):
        text = path.read_text()
        assert '"TODO"' not in text
        parsed = Parse(text, pp.Phenopacket())
        declared = {r.namespace_prefix.upper() for r in parsed.meta_data.resources}
        assert _prefixes_used(parsed) <= declared


@requires_vumc
def test_configured_condition_items_all_exist_in_the_synthetic_release():
    """The check that catches a config naming a variable no release ships.

    Scoped to conditions because the synthetic release ships only 73 of the measurement
    items; the ophthalmic config is authored against AI-READI's published crosswalk and has
    no release here to preflight against.
    """
    from b2ai_dataset_ingest.sources.aireadi.validate import validate_aireadi

    report = validate_aireadi(VUMC, CONFIG_DIR)
    missing = [
        f for f in report.findings
        if f.table == "condition_occurrence" and "absent from this release" in f.message
    ]
    assert not missing, report.render()


def test_fixture_carries_no_real_looking_person_id():
    """Fixture ids are 6-digit 9000xx; real mini ids are 4-digit, VUMC ids start at 0.

    A guard against someone "improving" a fixture by pasting in a slice of the licensed
    release, which the AI-READI data licence does not permit in a public repo.
    """
    import csv

    for path in sorted(FIXTURE.rglob("*")):
        if path.suffix not in {".csv", ".tsv"}:
            continue
        delimiter = "\t" if path.suffix == ".tsv" else ","
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh, delimiter=delimiter):
                person_id = (row.get("person_id") or "").strip()
                assert person_id.startswith("9000"), (
                    f"{path.name} carries {person_id!r}, which is not a 9000xx fixture id"
                )


# ---------- the value-gated HPO path over measurement.csv
#
# The shipped set mappings/b2ai-aireadi-measurement.sssom.tsv is reference-range gated: a
# value beyond the interval asserts the phenotype, a value inside it rules the phenotype
# out (predicate_modifier Not -> excluded). Every fixture row below was written for one of these.


def _features(packet):
    return {(f.type.id, f.excluded) for f in packet.phenotypic_features}


def test_a_lab_value_beyond_its_interval_asserts_the_phenotype(participants):
    """900002's HbA1c of 8.1 is above the 6.0 upper bound -> Elevated hemoglobin A1c."""
    assert ("HP:0040217", False) in _features(participants["900002"])


def test_a_lab_value_inside_its_interval_rules_the_phenotype_out(participants):
    """900001's HbA1c of 5.4 is normal by both the lab and ADA -> excluded, not silent.

    This is the absent pole the Voice sets withdrew: it is allowed here because a measured
    value on a dated assay genuinely rules the abnormality out at that observation.
    """
    assert ("HP:0040217", True) in _features(participants["900001"])


def test_measurement_derived_features_carry_direct_assay_evidence_not_self_report(participants):
    feature = next(f for f in participants["900002"].phenotypic_features
                   if f.type.id == "HP:0040217" and not f.excluded)
    assert feature.evidence[0].evidence_code.id == "ECO:0007307"
    assert feature.evidence[0].reference.id == "b2ai:measurement.import_hba1c"
    assert "measured value" in feature.description and "self-report" not in feature.description


def test_each_draw_derives_its_own_feature_with_its_own_time(participants):
    """900002 has HbA1c 8.1 in 2024 and 7.4 in 2025 -- two observations, two features.

    Age precision renders the two as different Ages (P67Y and P68Y), which is exactly why the
    derivation is per row rather than per participant.
    """
    elevated = [f for f in participants["900002"].phenotypic_features
                if f.type.id == "HP:0040217" and not f.excluded]
    assert len(elevated) == 2
    assert {f.onset.age_iso8601 for f in elevated} == {"P67Y", "P68Y"}


def test_a_sentinel_on_a_measurement_derives_nothing(participants):
    """900003's HbA1c is 999.0, a refusal code. It is dropped before any gate sees it.

    This is the only defence for measured values: a continuous assay has no upper bound to
    write into a gate (a platelet count of 555 is a real thrombocytosis), so the screen in the
    reader has to be what stops a refusal code reading as a wildly abnormal result.
    """
    assert "HP:0040217" not in {f.type.id for f in participants["900003"].phenotypic_features}


def test_a_censored_value_derives_nothing(participants):
    """900001's NT-proBNP is a '< 36' bound. Neither pole may fire on a bound."""
    assert "HP:0031185" not in {f.type.id for f in participants["900001"].phenotypic_features}


def test_a_reading_inside_the_silent_band_asserts_nothing(participants):
    """900001's systolic readings are 128 and 124: AHA 'Elevated', below stage 1, above normal.

    Neither pole fires -- the band between the present and absent cut-points is deliberately
    silent, and this pins that it stays silent rather than being swallowed by either pole.
    """
    assert "HP:0004421" not in {f.type.id for f in participants["900001"].phenotypic_features}


def test_a_normal_reading_rules_the_vital_out(participants):
    """900004's single systolic reading of 118 is AHA 'Normal' -> excluded."""
    assert ("HP:0004421", True) in _features(participants["900004"])


def test_a_per_foot_item_asserts_presence_from_either_side(participants):
    """900003 felt 10/10 sites on the right foot and 7/10 on the left -> present, no absent."""
    assert ("HP:0002936", False) in _features(participants["900003"])
    assert ("HP:0002936", True) not in _features(participants["900003"])


def test_derived_features_are_counted_after_reconciliation(source_report):
    """The report counts what the packets carry, not what the rows fired."""
    packets = {p.individual.id: p for p in AireadiSource(FIXTURE, CONFIG_DIR).read()}
    measured = sum(
        1 for p in packets.values() for f in p.phenotypic_features
        if f.evidence and f.evidence[0].evidence_code.id == "ECO:0007307"
    )
    assert source_report.features_derived == measured > 0


# ---------- reconciliation and the float resolver, in isolation
def test_reconcile_lets_presence_win_over_absence_at_the_same_time():
    from b2ai_dataset_ingest.model import OntologyTerm, PhenotypicFeatureObservation, TimePoint
    from b2ai_dataset_ingest.sources.aireadi.reader import _reconcile

    when = TimePoint(session_id="visit-1", age_iso8601="P60Y")
    later = TimePoint(session_id="visit-2", age_iso8601="P61Y")
    term = OntologyTerm(id="HP:0004421", label="Elevated systolic blood pressure")
    present = PhenotypicFeatureObservation(type=term, onset=when)
    absent = PhenotypicFeatureObservation(type=term, excluded=True, onset=when)
    absent_later = PhenotypicFeatureObservation(type=term, excluded=True, onset=later)

    # bp1 elevated, bp2 normal, same visit: one present feature, the absent one is dropped.
    assert _reconcile([present, absent]) == [present]
    # bp1 and bp2 both elevated: identical assertions collapse to one.
    assert _reconcile([present, present]) == [present]
    # normal at a later visit: a change over time, kept alongside the earlier presence.
    assert _reconcile([present, absent_later]) == [present, absent_later]


def test_the_measurement_resolver_compares_the_float_not_a_truncated_int():
    """`>50` must fire on 50.5. `_ordinal_of` truncates to 50 and would not; `_raw_number`
    resolves nothing so the evaluator reads the raw cell as a float."""
    from b2ai_dataset_ingest.mapping.conditions import parse_condition
    from b2ai_dataset_ingest.mapping.hpo_rules import ConditionalRule, derive_features
    from b2ai_dataset_ingest.sources.aireadi.reader import _ordinal_of, _raw_number

    rule = ConditionalRule(
        subject_id="b2ai:measurement.lbscat_hct", table="measurement", column="lbscat_hct",
        object_id="HP:0001899", object_label="Increased hematocrit",
        predicate_id="skos:relatedMatch", condition=parse_condition(">50"), when_value=">50",
        evidence_code="ECO:0007307",
    )
    row, rules = {"lbscat_hct": "50.5"}, {"lbscat_hct": [rule]}
    assert len(derive_features(row, rules, _raw_number)) == 1
    assert derive_features(row, rules, _ordinal_of) == []  # the hazard, pinned

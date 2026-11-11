"""An emitted phenopacket stays small, by construction.

Measured on 2026-10-22 (docs/plans/0002-aireadi-flagship-ingest.md, step 20): a worst-case
flagship packet -- every clinical item at two visits, two ECG recordings, CGM and wearable
window summaries, the manifest maximum of 75 linked files, ten medications -- is about
200 KB pretty-printed. The only route to a multi-megabyte packet is emitting raw samples as
Measurements: a 10-day CGM trace is ~2,880 readings, per-minute wearable heart rate ~14,000,
an ECG waveform 12 leads x 500 Hz. Device data therefore enters a packet only as window
summaries with the raw file linked.

This test rebuilds that worst case and pins an upper bound, so a change that starts
embedding a time series shows up as a failing number rather than as a 10 GB output.
"""

from google.protobuf.json_format import MessageToJson

from b2ai_dataset_ingest.emitters import PhenopacketEmitter
from b2ai_dataset_ingest.model import (
    DiseaseObservation,
    Evidence,
    ExternalReference,
    Individual,
    MeasurementObservation,
    OntologyTerm,
    Participant,
    PhenotypicFeatureObservation,
    Quantity,
    ReferenceRange,
    TimePoint,
)

#: Pretty-printed bytes. Roughly 2.5x the measured worst case, so a legitimate growth in
#: clinical coverage fits and an embedded trace (megabytes) does not.
MAX_PACKET_BYTES = 512_000

#: The most Measurements a participant can plausibly carry: every clinical item at two
#: visits plus the device window summaries. Anything far above this is a time series.
MAX_MEASUREMENTS = 400


def _measurement(i: int, label: str, age: str, description: str | None = None):
    return MeasurementObservation(
        assay=OntologyTerm(id=f"LOINC:{1000 + i}-{i % 10}", label=label),
        value_quantity=Quantity(
            value=float(i % 300),
            unit=OntologyTerm(id="UCUM:mg/dL", label="milligram per deciliter"),
            reference_range=ReferenceRange(low=10, high=200),
        ),
        time=TimePoint(session_id="visit-1", age_iso8601=age),
        description=description,
    )


def worst_case_participant() -> Participant:
    measurements = []
    for visit, age in enumerate(("P63Y", "P64Y")):
        base = visit * 1000
        measurements += [
            _measurement(base + i, f"Lab analyte {i} [Mass/volume] in Serum or Plasma", age)
            for i in range(40)
        ]
        measurements += [_measurement(base + 100 + i, f"Vital sign {i}", age) for i in range(10)]
        measurements += [
            _measurement(base + 200 + i, f"Ophthalmic measure {i} right eye", age)
            for i in range(12)
        ]
        measurements += [_measurement(base + 300 + i, f"MoCA subscore {i}", age) for i in range(8)]
        measurements += [
            _measurement(base + 400 + i, f"Questionnaire item {i}", age) for i in range(15)
        ]
        for recording in range(2):
            measurements += [
                _measurement(base + 600 + i, name, age, f"ECG recording {recording}")
                for i, name in enumerate(
                    ("Heart rate", "PR", "QRS duration", "QT", "QTc", "P axis", "QRS axis",
                     "T axis")
                )
            ]
        measurements += [
            _measurement(base + 700 + i, name, age, "CGM 10-day window")
            for i, name in enumerate(
                ("Mean glucose", "Time in range", "Time below range", "Time above range",
                 "Glucose CV", "GMI")
            )
        ]
        measurements += [
            _measurement(base + 800 + i, name, age, "Wearable 10-day window")
            for i, name in enumerate(
                ("Resting heart rate", "Mean SpO2", "Total sleep", "Daily steps", "Stress")
            )
        ]
    features = [
        PhenotypicFeatureObservation(
            type=OntologyTerm(id=f"HP:{i:07d}", label=f"A reasonably long HPO label {i}"),
            excluded=bool(i % 2),
            onset=TimePoint(session_id="visit-1", age_iso8601="P63Y"),
            description="Derived from measured value of b2ai:measurement.item when value >200.",
            evidence=[
                Evidence(
                    evidence_code=OntologyTerm(
                        id="ECO:0007307", label="direct assay evidence used in automatic assertion"
                    ),
                    reference=ExternalReference(
                        id=f"b2ai:measurement.item{i}",
                        reference="https://github.com/sujaypatil96/b2ai-dataset-ingest#x",
                    ),
                )
            ],
        )
        for i in range(60)
    ]
    diseases = [
        DiseaseObservation(term=OntologyTerm(id=f"MONDO:{i:07d}", label=f"disease {i}"))
        for i in range(12)
    ]
    files = [
        f"retinal_photography/cfp/icare_eidon/0001/0001_eidon_mosaic_cfp_l_{i:040d}.dcm"
        for i in range(75)
    ]
    return Participant(
        individual=Individual(id="900001", sex="FEMALE", age_iso8601="P63Y"),
        measurements=measurements,
        phenotypic_features=features,
        diseases=diseases,
        audio_references=files,
    )


def test_worst_case_flagship_packet_stays_under_the_bound():
    participant = worst_case_participant()
    assert len(participant.measurements) < MAX_MEASUREMENTS
    packet = PhenopacketEmitter().emit(participant)
    size = len(MessageToJson(packet).encode())
    assert size < MAX_PACKET_BYTES, f"{size} bytes: is something embedding a time series?"
    assert len(packet.files) == 75

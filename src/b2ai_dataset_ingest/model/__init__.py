"""Canonical, target-neutral intermediate representation (IR).

A :class:`~b2ai_dataset_ingest.model.core.Participant` is the unit of output: one
participant carries an :class:`Individual` plus lists of time-stamped observations
(diseases, measurements, phenotypic features). Each observation carries its own
:class:`TimePoint`, which an emitter renders into a target's time construct (for
phenopackets, a ``TimeElement``).
"""

from b2ai_dataset_ingest.model.core import (
    DiseaseObservation,
    Evidence,
    ExternalReference,
    Individual,
    MeasurementObservation,
    OntologyTerm,
    Participant,
    PhenotypicFeatureObservation,
    ProcedureContext,
    Quantity,
    ReferenceRange,
    TimePoint,
)

__all__ = [
    "DiseaseObservation",
    "Evidence",
    "ExternalReference",
    "Individual",
    "MeasurementObservation",
    "OntologyTerm",
    "Participant",
    "PhenotypicFeatureObservation",
    "ProcedureContext",
    "Quantity",
    "ReferenceRange",
    "TimePoint",
]

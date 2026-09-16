"""IR -> GA4GH Phenopacket emitter — the first and committed output target.

Mapping:

    Individual                     -> Phenopacket.subject (Individual)
    DiseaseObservation             -> Phenopacket.diseases (Disease, MONDO term)
    MeasurementObservation         -> Phenopacket.measurements (Measurement, time_observed,
                                      reference_range, procedure.body_site)
    PhenotypicFeatureObservation   -> Phenopacket.phenotypic_features (PhenotypicFeature)
    TimePoint                      -> TimeElement (timestamp | age | ontologyClass)
    audio_references               -> Phenopacket.files (File, referenced only)

Built on the official GA4GH ``phenopackets`` package (protobuf, schema v2). Every
``OntologyClass`` used contributes a versioned ``Resource`` to ``MetaData`` so the document
is self-describing. Serialization uses ``google.protobuf.json_format.MessageToJson``
(canonical camelCase JSON).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

import phenopackets as pp
from google.protobuf.json_format import MessageToJson

from b2ai_dataset_ingest.emitters.base import Emitter
from b2ai_dataset_ingest.model import (
    Individual,
    MeasurementObservation,
    OntologyTerm,
    Participant,
    TimePoint,
)
from b2ai_dataset_ingest.ontology import prefix_of, resolve_label, resource_for

logger = logging.getLogger(__name__)

#: Default taxonomy for a voice participant (synthetic demographics never override it).
DEFAULT_TAXON = OntologyTerm(id="NCBITaxon:9606", label="Homo sapiens")

_SEX_ENUM = {"UNKNOWN_SEX", "FEMALE", "MALE", "OTHER_SEX"}

SCHEMA_VERSION = "2.0"
CREATED_BY = "b2ai-dataset-ingest"


class PhenopacketEmitter(Emitter):
    target_id = "phenopacket"

    def emit(self, participant: Participant) -> pp.Phenopacket:
        """Build a schema-v2 ``Phenopacket`` from one canonical participant."""
        packet = pp.Phenopacket(id=participant.individual.id)
        packet.subject.CopyFrom(_individual(participant.individual))

        for disease in participant.diseases:
            packet.diseases.append(_disease(disease))
        for measurement in participant.measurements:
            packet.measurements.append(_measurement(measurement))
        for feature in participant.phenotypic_features:
            packet.phenotypic_features.append(_phenotypic_feature(feature))
        for uri in participant.audio_references:
            packet.files.append(pp.File(uri=uri))

        # MetaData is derived from the fully-built body so it declares a Resource for
        # exactly the ontology prefixes actually present (no more, no less).
        packet.meta_data.CopyFrom(_meta_data(packet))
        return packet

    def write_all(self, participants: Iterable[Participant], out_dir: Path) -> int:
        """Emit each participant to ``out_dir/<participant_id>.json``; return the count."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for participant in participants:
            packet = self.emit(participant)
            path = out_dir / f"{participant.individual.id}.json"
            path.write_text(MessageToJson(packet))
            count += 1
        logger.info("wrote %d phenopackets to %s", count, out_dir)
        return count


# -- message builders --
def _ontology_class(term: OntologyTerm) -> pp.OntologyClass:
    label = term.label or resolve_label(term.id) or term.id
    return pp.OntologyClass(id=term.id, label=label)


def _individual(individual: Individual) -> pp.Individual:
    subject = pp.Individual(id=individual.id)
    subject.taxonomy.CopyFrom(_ontology_class(individual.taxonomy or DEFAULT_TAXON))
    sex = (individual.sex or "UNKNOWN_SEX").upper()
    if sex in _SEX_ENUM:
        subject.sex = pp.Sex.Value(sex)
    else:
        logger.warning("unknown sex %r; using UNKNOWN_SEX", individual.sex)
    if individual.age_iso8601:
        subject.time_at_last_encounter.CopyFrom(
            pp.TimeElement(age=pp.Age(iso8601duration=individual.age_iso8601))
        )
    return subject


def _disease(observation) -> pp.Disease:
    disease = pp.Disease(term=_ontology_class(observation.term), excluded=observation.excluded)
    time_element = _time_element(observation.onset)
    if time_element is not None:
        disease.onset.CopyFrom(time_element)
    return disease


def _measurement(observation: MeasurementObservation) -> pp.Measurement:
    measurement = pp.Measurement(assay=_ontology_class(observation.assay))
    if observation.description:
        measurement.description = observation.description
    if observation.value_quantity is not None:
        quantity = pp.Quantity(value=observation.value_quantity.value)
        if observation.value_quantity.unit is not None:
            quantity.unit.CopyFrom(_ontology_class(observation.value_quantity.unit))
        reference_range = _reference_range(
            observation.value_quantity.reference_range, observation.value_quantity.unit
        )
        if reference_range is not None:
            quantity.reference_range.CopyFrom(reference_range)
        measurement.value.CopyFrom(pp.Value(quantity=quantity))
    elif observation.value_term is not None:
        measurement.value.CopyFrom(
            pp.Value(ontology_class=_ontology_class(observation.value_term))
        )
    time_element = _time_element(observation.time)
    if time_element is not None:
        measurement.time_observed.CopyFrom(time_element)
    if observation.procedure is not None:
        measurement.procedure.CopyFrom(_procedure(observation.procedure))
    return measurement


def _reference_range(reference_range, fallback_unit) -> pp.ReferenceRange | None:
    """Build a ``ReferenceRange``; None when absent.

    ``ReferenceRange.unit`` is required for the range to mean anything, so it falls back to
    the measurement's own unit — a range in unnamed units is not a range.
    """
    if reference_range is None:
        return None
    unit = reference_range.unit or fallback_unit
    if unit is None:
        logger.warning("reference range has no unit and the value has none either; dropping")
        return None
    return pp.ReferenceRange(
        unit=_ontology_class(unit), low=reference_range.low, high=reference_range.high
    )


def _procedure(context) -> pp.Procedure:
    """Build a ``Procedure`` — in practice the carrier for ``body_site`` (laterality)."""
    procedure = pp.Procedure(code=_ontology_class(context.code))
    if context.body_site is not None:
        procedure.body_site.CopyFrom(_ontology_class(context.body_site))
    performed = _time_element(context.performed)
    if performed is not None:
        procedure.performed.CopyFrom(performed)
    return procedure


def _phenotypic_feature(observation) -> pp.PhenotypicFeature:
    feature = pp.PhenotypicFeature(
        type=_ontology_class(observation.type), excluded=observation.excluded
    )
    if observation.description:
        feature.description = observation.description
    if observation.severity is not None:
        feature.severity.CopyFrom(_ontology_class(observation.severity))
    onset = _time_element(observation.onset)
    if onset is not None:
        feature.onset.CopyFrom(onset)
    for item in observation.evidence:
        feature.evidence.append(_evidence(item))
    return feature


def _evidence(evidence) -> pp.Evidence:
    """Build a GA4GH ``Evidence`` (ECO code + optional source ``ExternalReference``)."""
    message = pp.Evidence(evidence_code=_ontology_class(evidence.evidence_code))
    reference = evidence.reference
    if reference is not None:
        external = pp.ExternalReference(id=reference.id)
        if reference.reference:
            external.reference = reference.reference
        if reference.description:
            external.description = reference.description
        message.reference.CopyFrom(external)
    return message


def _time_element(time: TimePoint | None) -> pp.TimeElement | None:
    """Pick the richest available representation; None when nothing is known.

    timestamp > age > ontology_class. A bare session label with no recognized term
    (e.g. a stray UUID session) yields no TimeElement rather than a fabricated one.
    """
    if time is None:
        return None
    if time.timestamp:
        element = pp.TimeElement()
        try:
            element.timestamp.FromJsonString(time.timestamp)
            return element
        except ValueError:
            logger.warning("bad timestamp %r; falling back", time.timestamp)
    if time.age_iso8601:
        return pp.TimeElement(age=pp.Age(iso8601duration=time.age_iso8601))
    if time.ontology_class is not None:
        return pp.TimeElement(ontology_class=_ontology_class(time.ontology_class))
    return None


def _meta_data(packet: pp.Phenopacket) -> pp.MetaData:
    meta = pp.MetaData(created_by=CREATED_BY, phenopacket_schema_version=SCHEMA_VERSION)
    for prefix in _collect_prefixes(packet):
        fields = resource_for(prefix)
        if fields is None:
            logger.warning("no Resource registered for prefix %r; MetaData incomplete", prefix)
            continue
        meta.resources.append(pp.Resource(**fields))
    return meta


def _collect_prefixes(message) -> list[str]:
    """Every CURIE prefix actually present in ``message``, in stable (sorted) order.

    Walks the built protobuf so the set matches exactly what was emitted — a term that
    was dropped by a oneof (e.g. a ``value_term`` shadowed by a ``value_quantity``) does
    not contribute a prefix, and nothing emitted is ever missed.
    """
    prefixes: set[str] = set()
    _walk_ontology_ids(message, prefixes)
    return sorted(prefixes)


def _walk_ontology_ids(message, prefixes: set[str]) -> None:
    if isinstance(message, pp.OntologyClass):
        if message.id:
            prefixes.add(prefix_of(message.id))
        return
    # An ExternalReference id (e.g. a b2ai: source item on an Evidence) is a CURIE too, so it
    # must contribute its prefix — the MetaData must declare a Resource for it to be resolvable.
    if isinstance(message, pp.ExternalReference):
        if message.id and ":" in message.id:
            prefixes.add(prefix_of(message.id))
        return
    for field, value in message.ListFields():  # only *set* fields
        if field.message_type is None:
            continue  # scalar / enum field
        if field.message_type.GetOptions().map_entry:
            continue  # maps carry no ontology classes in this schema
        if hasattr(value, "DESCRIPTOR"):  # a singular message
            _walk_ontology_ids(value, prefixes)
        else:  # a repeated container of messages
            for item in value:
                _walk_ontology_ids(item, prefixes)

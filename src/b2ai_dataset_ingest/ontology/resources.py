"""Ontology ``Resource`` metadata, keyed by CURIE prefix.

Every ``OntologyClass`` in an emitted phenopacket must have a matching ``Resource`` in the
document's ``MetaData`` (that's how a consumer knows what ``MONDO:0005180`` means). The
emitter collects the CURIE prefixes it actually used and asks :func:`resource_for` for the
metadata to build one ``Resource`` each.

Kept as plain dicts (not protobuf messages) so the ontology layer stays independent of the
phenopackets library; the emitter turns these into ``phenopackets.Resource`` messages.

Versions are recorded for provenance; update them when the pinned ontology release changes.
``b2ai`` is a project-local namespace for dataset items that have no public ontology code yet
(e.g. VHI-10 items, AI-READI lab assays) — a documented stopgap, see the per-dataset configs.
It spans the *project*, not one dataset; item ids are namespaced by table
(``b2ai:vhi10.total``, ``b2ai:measurement.import_hba1c``).
"""

from __future__ import annotations

# prefix -> Resource field values (names match phenopackets.Resource fields).
KNOWN_RESOURCES: dict[str, dict[str, str]] = {
    "MONDO": {
        "id": "mondo",
        "name": "Mondo Disease Ontology",
        "url": "http://purl.obolibrary.org/obo/mondo.owl",
        "version": "2025-06-03",
        "namespace_prefix": "MONDO",
        "iri_prefix": "http://purl.obolibrary.org/obo/MONDO_",
    },
    "HP": {
        "id": "hp",
        "name": "Human Phenotype Ontology",
        "url": "http://purl.obolibrary.org/obo/hp.owl",
        "version": "2025-05-06",
        "namespace_prefix": "HP",
        "iri_prefix": "http://purl.obolibrary.org/obo/HP_",
    },
    "LOINC": {
        "id": "loinc",
        "name": "Logical Observation Identifiers Names and Codes (LOINC)",
        "url": "https://loinc.org",
        "version": "2.80",
        "namespace_prefix": "LOINC",
        "iri_prefix": "https://loinc.org/",
    },
    "NCIT": {
        "id": "ncit",
        "name": "NCI Thesaurus OBO Edition",
        "url": "http://purl.obolibrary.org/obo/ncit.owl",
        "version": "2024-05-07",
        "namespace_prefix": "NCIT",
        "iri_prefix": "http://purl.obolibrary.org/obo/NCIT_",
    },
    "ECO": {
        "id": "eco",
        "name": "Evidence and Conclusion Ontology (ECO)",
        "url": "http://purl.obolibrary.org/obo/eco.owl",
        "version": "2026-05-15",
        "namespace_prefix": "ECO",
        "iri_prefix": "http://purl.obolibrary.org/obo/ECO_",
    },
    "NCBITAXON": {
        "id": "ncbitaxon",
        "name": "NCBI organismal classification",
        "url": "http://purl.obolibrary.org/obo/ncbitaxon.owl",
        "version": "2024-07-03",
        "namespace_prefix": "NCBITaxon",
        "iri_prefix": "http://purl.obolibrary.org/obo/NCBITaxon_",
    },
    "UBERON": {
        "id": "uberon",
        "name": "Uber-anatomy ontology (UBERON)",
        "url": "http://purl.obolibrary.org/obo/uberon.owl",
        # Release reported by EBI OLS4 on 2026-09-09. The local semantic-sql build may lag;
        # `validate-mappings` warns when a loaded release differs from a file's declared
        # object_source_version.
        "version": "2026-06-19",
        "namespace_prefix": "UBERON",
        "iri_prefix": "http://purl.obolibrary.org/obo/UBERON_",
    },
    "UCUM": {
        "id": "ucum",
        "name": "Unified Code for Units of Measure (UCUM)",
        "url": "https://ucum.org",
        "version": "2.1",
        "namespace_prefix": "UCUM",
        "iri_prefix": "https://ucum.org/",
    },
    "B2AI": {
        "id": "b2ai",
        "name": "Bridge2AI project-local codes (b2ai-dataset-ingest)",
        "url": "https://github.com/sujaypatil96/b2ai-dataset-ingest",
        "version": "0.0.1",
        "namespace_prefix": "b2ai",
        "iri_prefix": "https://github.com/sujaypatil96/b2ai-dataset-ingest#",
    },
}


def prefix_of(curie: str) -> str:
    """Return the (upper-cased) CURIE prefix, e.g. ``MONDO:0005180`` -> ``MONDO``."""
    return curie.split(":", 1)[0].upper()


def resource_for(prefix: str) -> dict[str, str] | None:
    """Return Resource field values for a CURIE prefix, or None if unknown."""
    return KNOWN_RESOURCES.get(prefix.upper())

"""Collapsing an HPO term asserted alongside its own ancestor.

The ontology is stubbed. What matters here is our decision logic -- which term
survives, what happens to the dropped one's provenance, and that a wrong release
is fatal -- not that hpo-toolkit can walk a graph.
"""

from __future__ import annotations

import pytest

from b2ai_dataset_ingest.model.core import (
    Evidence,
    ExternalReference,
    Individual,
    OntologyTerm,
    Participant,
    PhenotypicFeatureObservation,
)
from b2ai_dataset_ingest.ontology.hpo_coherence import (
    CollapseReport,
    OntologyVersionMismatch,
    collapse_participant,
    require_version,
)

DYSPNEA = "HP:0002094"
EXERTIONAL = "HP:0002875"  # a child of DYSPNEA
COUGH = "HP:0012735"  # unrelated


class _TermId:
    def __init__(self, value: str):
        self.value = value

    @staticmethod
    def from_curie(curie: str) -> _TermId:
        return _TermId(curie)


class _Graph:
    """Ancestry for the one relationship these tests turn on."""

    _ANCESTORS = {EXERTIONAL: [DYSPNEA], DYSPNEA: [], COUGH: []}

    def get_ancestors(self, term):
        return [_TermId(a) for a in self._ANCESTORS.get(term.value, [])]


class _Ontology:
    version = "2026-02-16"
    graph = _Graph()


@pytest.fixture(autouse=True)
def _stub_term_id(monkeypatch):
    """collapse_participant imports TermId from hpotk; point it at the stub."""
    import sys
    import types

    module = types.ModuleType("hpotk.model")
    module.TermId = _TermId
    monkeypatch.setitem(sys.modules, "hpotk.model", module)


def _feature(term_id: str, item: str) -> PhenotypicFeatureObservation:
    return PhenotypicFeatureObservation(
        type=OntologyTerm(id=term_id, label=term_id),
        description=f"Derived present from self-reported item {item}",
        evidence=[
            Evidence(
                evidence_code=OntologyTerm(id="ECO:0006160", label="self-reported"),
                reference=ExternalReference(id=item),
            )
        ],
    )


def _participant(*features: PhenotypicFeatureObservation) -> Participant:
    return Participant(
        individual=Individual(id="subject-1"), phenotypic_features=list(features)
    )


def test_ancestor_is_dropped_and_the_specific_term_survives():
    p = _participant(
        _feature(EXERTIONAL, "b2ai:dyspnea_index.di_exercise"),
        _feature(DYSPNEA, "b2ai:dyspnea_index.di_air_in"),
    )
    report = CollapseReport()

    assert collapse_participant(p, _Ontology(), report) is True
    assert [f.type.id for f in p.phenotypic_features] == [EXERTIONAL]
    assert report.collapses[(EXERTIONAL, DYSPNEA)] == 1


def test_the_dropped_ancestor_s_evidence_is_kept():
    """The whole reason to do this in the ingest rather than downstream: the
    ancestor records which questionnaire item fired, and that must survive."""
    p = _participant(
        _feature(EXERTIONAL, "b2ai:dyspnea_index.di_exercise"),
        _feature(DYSPNEA, "b2ai:dyspnea_index.di_air_in"),
    )
    collapse_participant(p, _Ontology(), CollapseReport())

    kept = p.phenotypic_features[0]
    refs = {e.reference.id for e in kept.evidence}
    assert refs == {"b2ai:dyspnea_index.di_exercise", "b2ai:dyspnea_index.di_air_in"}
    assert "di_air_in" in (kept.description or "")


def test_unrelated_terms_are_left_alone():
    p = _participant(
        _feature(EXERTIONAL, "b2ai:dyspnea_index.di_exercise"),
        _feature(COUGH, "b2ai:leicester_cough_questionnaire.lcq_cough"),
    )
    assert collapse_participant(p, _Ontology(), CollapseReport()) is False
    assert len(p.phenotypic_features) == 2


def test_excluded_features_are_not_collapsed():
    """Exclusion inverts the rule -- excluding a parent implies excluding its
    children, so the ancestor is the informative assertion there. None are emitted
    since the absent pole was withdrawn, so this passes them through untouched."""
    specific = _feature(EXERTIONAL, "item-a")
    ancestor = _feature(DYSPNEA, "item-b")
    specific.excluded = ancestor.excluded = True
    p = _participant(specific, ancestor)

    assert collapse_participant(p, _Ontology(), CollapseReport()) is False
    assert len(p.phenotypic_features) == 2


def test_a_different_release_is_fatal():
    with pytest.raises(OntologyVersionMismatch, match="2026-02-16"):
        require_version(_Ontology(), "2026-09-01")


def test_the_declared_release_passes():
    assert require_version(_Ontology(), "2026-02-16") == "2026-02-16"

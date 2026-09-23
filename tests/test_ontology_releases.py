"""The pinned-release backend: a file is checked against the ontology release it declares.

The hazard this pins: with oaklib's *current* build as the only backend, a correct mapping
turned red on `main` the week HPO relabelled `HP:0011950`, and a set pinned to 2026-02-16
could never be green again without re-curating every label. These tests run offline against
a hand-built obographs file seeded into a temporary cache.
"""

import json
from pathlib import Path

import pytest

from b2ai_dataset_ingest.ontology.releases import (
    CACHE_ENV,
    PinnedRelease,
    ReleaseUnavailable,
    cache_dir,
    release_date,
)
from b2ai_dataset_ingest.ontology.sssom_validate import validate_paths

RELEASE = "2099-01-01"


def _obographs(version: str = RELEASE) -> dict:
    """A tiny HP-shaped obographs graph: one live term with a synonym, one obsolete, one
    node with no meta at all (real releases ship a few, and it is what broke oaklib)."""
    obo = "http://purl.obolibrary.org/obo/"
    return {
        "graphs": [
            {
                "id": f"{obo}hp.json",
                "meta": {"version": f"{obo}hp/releases/{version}/hp.json"},
                "nodes": [
                    {
                        "id": f"{obo}HP_0002094",
                        "lbl": "Dyspnea",
                        "type": "CLASS",
                        "meta": {
                            "synonyms": [
                                {"pred": "hasExactSynonym", "val": "Shortness of breath"},
                                {"pred": "hasRelatedSynonym", "val": "Panting"},
                            ]
                        },
                    },
                    {
                        "id": f"{obo}HP_0020063",
                        "lbl": "obsolete Increased hemoglobin concentration",
                        "type": "CLASS",
                        "meta": {"deprecated": True},
                    },
                    {
                        "id": f"{obo}HP_0007815",
                        "lbl": "Kept label, deprecated flag",
                        "type": "CLASS",
                        "meta": {"deprecated": True},
                    },
                    {"id": f"{obo}HP_0000001", "lbl": "All", "type": "CLASS"},
                    {"id": f"{obo}BFO_0000050", "lbl": "part of", "type": "PROPERTY"},
                ],
                "edges": [],
            }
        ]
    }


@pytest.fixture
def cache(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    (tmp_path / f"hp-{RELEASE}.json").write_text(json.dumps(_obographs()))
    return tmp_path


# ---------- the reader
def test_release_date_is_read_from_the_declared_version():
    assert release_date("hp/releases/2026-02-16") == "2026-02-16"
    assert release_date("mondo/releases/2026-05-05") == "2026-05-05"
    assert release_date("hp") is None and release_date(None) is None


def test_pinned_release_exposes_the_oaklib_shaped_surface(cache: Path):
    release = PinnedRelease(cache / f"hp-{RELEASE}.json")
    assert release.version == RELEASE
    assert release.label("HP:0002094") == "Dyspnea"
    assert release.label("HP:9999999") is None
    # Deprecation comes from the flag, not the label: HP:0007815 keeps a normal label.
    assert set(release.obsoletes()) == {"HP:0020063", "HP:0007815"}
    assert release.entity_metadata_map("HP:0002094") == {
        "oio:hasExactSynonym": ["Shortness of breath"]
    }
    # A node with no meta, and a non-CLASS node, must neither crash nor leak in.
    assert release.label("HP:0000001") == "All"
    assert release.label("BFO:0000050") is None
    assert len(release) == 4


def test_an_unreadable_file_is_a_release_error(tmp_path: Path):
    bad = tmp_path / "hp-2000-01-01.json"
    bad.write_text("{not json")
    with pytest.raises(ReleaseUnavailable):
        PinnedRelease(bad)


def test_cache_dir_honours_the_override(cache: Path):
    assert cache_dir() == cache


# ---------- the validator, end to end, offline
_HEADER = (
    "# curie_map:\n"
    "#   b2ai: https://github.com/sujaypatil96/b2ai-dataset-ingest#\n"
    "#   HP: http://purl.obolibrary.org/obo/HP_\n"
    "#   skos: http://www.w3.org/2004/02/skos/core#\n"
    "#   semapv: https://w3id.org/semapv/vocab/\n"
    "# license: https://creativecommons.org/publicdomain/zero/1.0/\n"
    "# object_source: obo:hp\n"
    f"# object_source_version: hp/releases/{RELEASE}\n"
    "subject_id\tsubject_label\tpredicate_id\tobject_id\tobject_label\t"
    "mapping_justification\tconfidence\tcomment\n"
)


def _row(obj: str, label: str) -> str:
    return "\t".join(["b2ai:t.x", "x", "skos:exactMatch", obj, label,
                      "semapv:ManualMappingCuration", "0.9", ""]) + "\n"


def test_a_declared_release_is_checked_against_that_release_not_the_current_build(
    cache: Path, tmp_path: Path
):
    """The label is right for the declared release; no network, no oaklib, still checked."""
    path = tmp_path / "set.sssom.tsv"
    path.write_text(_HEADER + _row("HP:0002094", "Dyspnea"))
    result = validate_paths([path], check_ontology=True)
    assert result.ontology_checked
    assert result.ontology_versions == {"obo:hp": RELEASE}
    assert not result.errors, "\n".join(f.render() for f in result.errors)
    # ...and no drift warning, because the loaded release IS the declared one.
    assert not [f for f in result.warnings if f.code == "hpo-version-mismatch"]


def test_the_pinned_release_still_catches_every_fault(cache: Path, tmp_path: Path):
    path = tmp_path / "set.sssom.tsv"
    path.write_text(
        _HEADER
        + _row("HP:9999999", "Made up")
        + _row("HP:0002094", "Panting")  # related synonym only
        + _row("HP:0002094", "Shortness of breath")  # exact synonym -> warning
        + _row("HP:0007815", "Kept label, deprecated flag")
    )
    result = validate_paths([path], check_ontology=True)
    codes = {f.code for f in result.errors}
    assert {"hallucinated-term", "label-mismatch", "obsolete-term"} <= codes
    assert "noncanonical-label" in {f.code for f in result.warnings}


def test_an_unfetchable_release_is_an_error_only_when_strict(tmp_path: Path, monkeypatch):
    """No file in the cache and a version PURL that will never resolve: strict mode errors,
    lenient mode skips the ontology layer and says so."""
    monkeypatch.setenv(CACHE_ENV, str(tmp_path / "empty"))
    monkeypatch.setattr(
        "b2ai_dataset_ingest.ontology.releases.PURL", "http://127.0.0.1:9/{prefix}/{date}.json"
    )
    path = tmp_path / "set.sssom.tsv"
    path.write_text(_HEADER.replace(RELEASE, "1999-01-01") + _row("HP:0002094", "Dyspnea"))

    strict = validate_paths([path], check_ontology=True)
    assert "no-ontology-backend" in {f.code for f in strict.errors}

    lenient = validate_paths([path], check_ontology=None)
    assert not lenient.ontology_checked
    assert not lenient.errors

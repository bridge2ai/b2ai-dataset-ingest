"""Validate the shipped B2AI -> HPO SSSOM mappings, and test the validator itself.

Layered like the real-data test in ``test_end_to_end.py``: structural checks always run;
the subject-existence and HPO (oaklib) checks run only when their backend is available and
otherwise ``skip`` -- so the suite is green on a bare checkout but *enforcing* in CI, where
the ``validation`` extra (oaklib) and the fetched synthetic data are present.
"""

from pathlib import Path

import pytest

from b2ai_dataset_ingest.ontology.sssom_validate import (
    OBJECT_SOURCES,
    _get_ontology_adapter,
    default_mapping_files,
    parse_sssom,
    validate_paths,
)

REPO = Path(__file__).parents[1]
MAPPING_FILES = default_mapping_files(REPO)

# Candidate phenotype roots for the subject-existence check (first that exists wins): the
# fetched synthetic tree (CI) or the local real-data dictionaries.
# Synthetic only: the test suite reads from data/synthetic/, never from the source
# datasets under data/real/ (which may be owned by a separate account and unreadable
# here anyway).
DATA_ROOTS = [
    REPO / "data" / "synthetic" / "voice_dgp" / "b2ai-voice-synthetic-phenotype"
    / "output" / "phenotype",
]

SSSOM_HEADER = (
    "# curie_map:\n"
    "#   b2ai: https://github.com/sujaypatil96/b2ai-dataset-ingest#\n"
    "#   HP: http://purl.obolibrary.org/obo/HP_\n"
    "#   skos: http://www.w3.org/2004/02/skos/core#\n"
    "#   semapv: https://w3id.org/semapv/vocab/\n"
    "# license: https://creativecommons.org/publicdomain/zero/1.0/\n"
    "# object_source: obo:hp\n"
)
SSSOM_COLS = (
    "subject_id\tsubject_label\tpredicate_id\tobject_id\tobject_label\t"
    "mapping_justification\tconfidence\tcomment"
)


def _data_root() -> Path | None:
    return next((p for p in DATA_ROOTS if p.exists()), None)


def _oaklib_ready() -> bool:
    return all(_get_ontology_adapter(sel) is not None for _, sel in OBJECT_SOURCES.values())


def _sssom_ready() -> bool:
    try:
        import sssom.parsers  # noqa: F401
        import sssom.validators  # noqa: F401

        return True
    except ImportError:
        return False


def _row(subject: str, predicate: str, obj: str, obj_label: str, confidence: str = "0.9") -> str:
    """Build one TSV row (label/justification/comment filled with sensible defaults)."""
    return "\t".join(
        [subject, "x", predicate, obj, obj_label, "semapv:ManualMappingCuration", confidence, ""]
    )


def _write_sssom(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "case.sssom.tsv"
    path.write_text(SSSOM_HEADER + SSSOM_COLS + "\n" + "\n".join(rows) + "\n")
    return path


# --------------------------------------------------------------- the shipped mappings


def test_mapping_files_present():
    assert MAPPING_FILES, "no SSSOM mapping files shipped under mappings/"


@pytest.mark.parametrize("path", MAPPING_FILES, ids=lambda p: p.name)
def test_parses_and_structural(path: Path):
    metadata, rows = parse_sssom(path)
    assert metadata.get("curie_map"), f"{path.name} missing curie_map metadata"
    assert rows, f"{path.name} has no mappings"
    result = validate_paths([path], data_root=None, check_ontology=False)
    assert not result.errors, "\n".join(f.render() for f in result.errors)


@pytest.mark.skipif(_data_root() is None, reason="no phenotype data root available")
def test_subjects_exist_in_data_dicts():
    result = validate_paths(MAPPING_FILES, data_root=_data_root(), check_ontology=False)
    unknown = [f for f in result.errors if f.code == "unknown-column"]
    assert not unknown, "\n".join(f.render() for f in unknown)


@pytest.mark.skipif(not _sssom_ready(), reason="sssom-py not installed")
@pytest.mark.parametrize("path", MAPPING_FILES, ids=lambda p: p.name)
def test_official_sssom_schema_validation(path: Path):
    """Cross-check against the reference sssom-py validator (JSON-schema + prefix declarations)."""
    from sssom.parsers import parse_sssom_table
    from sssom.validators import DEFAULT_VALIDATION_TYPES, validate

    validate(parse_sssom_table(str(path)), DEFAULT_VALIDATION_TYPES)  # raises on any violation


@pytest.mark.skipif(not _oaklib_ready(), reason="oaklib/HPO backend unavailable")
def test_no_hallucinated_hpo_terms():
    result = validate_paths(MAPPING_FILES, data_root=None, check_ontology=True)
    assert result.ontology_checked
    assert not result.errors, "\n".join(f.render() for f in result.errors)


# ------------------------------------------------------------- the validator itself


def test_validator_catches_structural_faults(tmp_path: Path):
    """Offline negative control: malformed rows must be rejected without any backend."""
    path = _write_sssom(
        tmp_path,
        [
            _row("not_a_b2ai_subject", "skos:exactMatch", "HP:0001513", "Obesity"),
            _row("b2ai:no_dot_subject", "skos:exactMatch", "HP:0001513", "Obesity"),
            _row("b2ai:confounders.obesity", "skos:sameAs", "MONDO:0011122", "Obesity"),
            _row("b2ai:confounders.scoliosis", "skos:exactMatch", "HP:0002650", "Scoliosis", "5"),
        ],
    )
    codes = {f.code for f in validate_paths([path], check_ontology=False).errors}
    expected = {"bad-subject", "malformed-subject", "bad-predicate", "bad-object", "bad-confidence"}
    assert expected <= codes


_COND_COLS = (
    "subject_id\tsubject_label\tpredicate_id\tobject_id\tobject_label\t"
    "mapping_justification\tconfidence\twhen_value"
)


def _cond_row(subject, obj, obj_label, when="", predicate="skos:broadMatch"):
    return "\t".join(
        [subject, "x", predicate, obj, obj_label,
         "semapv:ManualMappingCuration", "0.8", when]
    )


def _write_conditional(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "cond.sssom.tsv"
    path.write_text(SSSOM_HEADER + _COND_COLS + "\n" + "\n".join(rows) + "\n")
    return path


def test_validator_catches_when_value_faults(tmp_path: Path):
    """Offline: an unparseable when_value, and a gate on a predicate that cannot carry one."""
    path = _write_conditional(
        tmp_path,
        [
            _cond_row("b2ai:phq9.no_energy", "HP:0012378", "Fatigue", when="totally bogus"),
            _cond_row("b2ai:phq9.feeling_depressed", "HP:0000716", "Depression", when=">=1",
                      predicate="skos:relatedMatch"),
        ],
    )
    codes = {f.code for f in validate_paths([path], check_ontology=False).errors}
    assert {"bad-when-value", "ungateable-predicate"} <= codes


def test_reintroducing_predicate_modifier_is_an_error(tmp_path: Path):
    """Absent poles were withdrawn on clinical review; the column cannot come back unnoticed."""
    path = tmp_path / "cond.sssom.tsv"
    path.write_text(
        SSSOM_HEADER + _COND_COLS + "\tpredicate_modifier\n"
        + _cond_row("b2ai:phq9.feeling_depressed", "HP:0000716", "Depression", when="==0")
        + "\tNot\n"
    )
    codes = {f.code for f in validate_paths([path], check_ontology=False).errors}
    assert "withdrawn-column" in codes


def test_repeated_conditional_rows_are_duplicates(tmp_path: Path):
    """Only presence is asserted, so one (subject, predicate, object) triple carries at most one
    row: a repeat is a duplicate however the when_value differs."""
    dup = _write_conditional(
        tmp_path,
        [
            _cond_row("b2ai:phq9.feeling_depressed", "HP:0000716", "Depression", when=">=1"),
            _cond_row("b2ai:phq9.feeling_depressed", "HP:0000716", "Depression", when=">=2"),
        ],
    )
    codes = {f.code for f in validate_paths([dup], check_ontology=False).errors}
    assert "duplicate" in codes


def test_object_prefix_follows_the_file_s_object_source(tmp_path: Path):
    """A MONDO object in a file declaring obo:hp is an error, and the mirror case is fine."""
    mondo_row = _row("b2ai:confounders.epilepsy", "skos:exactMatch", "MONDO:0005027", "epilepsy")
    hp_file = _write_sssom(tmp_path, [mondo_row])
    codes = {f.code for f in validate_paths([hp_file], check_ontology=False).errors}
    assert "bad-object" in codes

    mondo = tmp_path / "conditions.sssom.tsv"
    mondo.write_text(
        SSSOM_HEADER.replace("HP: http://purl.obolibrary.org/obo/HP_",
                             "MONDO: http://purl.obolibrary.org/obo/MONDO_")
                    .replace("object_source: obo:hp", "object_source: obo:mondo")
        + SSSOM_COLS + "\n" + mondo_row + "\n"
    )
    assert not validate_paths([mondo], check_ontology=False).errors


def test_unknown_object_source_is_an_error(tmp_path: Path):
    """An undeclared/unsupported object ontology must fail loudly, not skip the checks silently."""
    path = tmp_path / "weird.sssom.tsv"
    path.write_text(
        SSSOM_HEADER.replace("object_source: obo:hp", "object_source: obo:doid")
        + SSSOM_COLS + "\n"
        + _row("b2ai:confounders.obesity", "skos:exactMatch", "HP:0001513", "Obesity") + "\n"
    )
    codes = {f.code for f in validate_paths([path], check_ontology=False).errors}
    assert "unknown-object-source" in codes


def test_validator_requires_self_contained_curie_map(tmp_path: Path):
    """A prefix used in a row but absent from the file's own curie_map is an error (L1)."""
    # The header declares b2ai/HP/skos/semapv; MONDO is not declared here.
    path = _write_sssom(
        tmp_path, [_row("b2ai:confounders.obesity", "skos:exactMatch", "HP:0001513", "Obesity")]
    )
    text = path.read_text().replace("HP:0001513", "MONDO:0011122").replace(
        "\tHP:", "\tMONDO:"
    )
    path.write_text(text)
    codes = {f.code for f in validate_paths([path], check_ontology=False).errors}
    assert "unknown-prefix" in codes


@pytest.mark.skipif(not _oaklib_ready(), reason="oaklib/HPO backend unavailable")
def test_validator_catches_hpo_faults(tmp_path: Path):
    """oaklib negative control: fake CURIE, drifted label, and a deprecated term must be caught."""
    path = _write_sssom(
        tmp_path,
        [
            _row("b2ai:confounders.dizziness", "skos:exactMatch", "HP:9999999", "Made up"),
            _row("b2ai:confounders.ataxia", "skos:exactMatch", "HP:0001251", "Parkinsonism"),
            # HP:0007815 is deprecated but keeps a normal (non-"obsolete ") label -- the
            # label-heuristic hole; must be caught via owl:deprecated / adapter.obsoletes().
            _row(
                "b2ai:confounders.foo", "skos:exactMatch", "HP:0007815",
                "Abnormal distribution of retinal arterioles and venules",
            ),
        ],
    )
    codes = {f.code for f in validate_paths([path], check_ontology=True).errors}
    assert "hallucinated-term" in codes
    assert "label-mismatch" in codes
    assert "obsolete-term" in codes


@pytest.mark.skipif(not _oaklib_ready(), reason="oaklib/HPO backend unavailable")
def test_exact_synonym_warns_but_related_synonym_errors(tmp_path: Path):
    """An exact HPO synonym as object_label only warns; a related synonym is an error (L2)."""
    path = _write_sssom(
        tmp_path,
        [
            # "Shortness of breath" is an EXACT synonym of HP:0002094 Dyspnea -> warning
            _row("b2ai:confounders.shortness_breath", "skos:exactMatch", "HP:0002094", "Shortness of breath"),  # noqa: E501
            # "Panting" is a RELATED synonym only -> error
            _row("b2ai:confounders.baz", "skos:closeMatch", "HP:0002094", "Panting"),
        ],
    )
    result = validate_paths([path], check_ontology=True)
    assert {f.code for f in result.warnings} & {"noncanonical-label"}
    assert "label-mismatch" in {f.code for f in result.errors}


# --------------------------------------------------- measured-value rows (evidence_code)

_MEASURED_COLS = (
    "subject_id\tsubject_label\tpredicate_id\tpredicate_modifier\tobject_id\tobject_label\t"
    "mapping_justification\tconfidence\twhen_value\tevidence_code"
)
_MEASURED_HEADER = SSSOM_HEADER.replace(
    "#   HP: http://purl.obolibrary.org/obo/HP_\n",
    "#   HP: http://purl.obolibrary.org/obo/HP_\n#   ECO: http://purl.obolibrary.org/obo/ECO_\n",
)


def _measured_row(subject, obj, obj_label, when, *, modifier="", evidence="ECO:0007307",
                  predicate="skos:relatedMatch"):
    return "\t".join(
        [subject, "x", predicate, modifier, obj, obj_label, "semapv:ManualMappingCuration",
         "0.9", when, evidence]
    )


def _write_measured(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "measured.sssom.tsv"
    path.write_text(_MEASURED_HEADER + _MEASURED_COLS + "\n" + "\n".join(rows) + "\n")
    return path


def test_a_measured_value_row_may_gate_on_relatedmatch_and_carry_an_absent_pole(tmp_path: Path):
    """An assay is not the phenotype, so the predicate is loose; the reference-range gate is
    what entails the term. Both poles of one assay are distinct rows, not duplicates."""
    path = _write_measured(
        tmp_path,
        [
            _measured_row("b2ai:measurement.lbscat_hct", "HP:0031851", "Reduced hematocrit", "<37"),
            _measured_row("b2ai:measurement.lbscat_hct", "HP:0031851", "Reduced hematocrit",
                          ">=42 & <=47", modifier="Not"),
        ],
    )
    assert not validate_paths([path], check_ontology=False).errors


def test_the_same_rules_stay_errors_on_a_self_report_row(tmp_path: Path):
    """Declaring self-report evidence explicitly changes nothing from declaring none."""
    path = _write_measured(
        tmp_path,
        [
            _measured_row("b2ai:phq9.no_energy", "HP:0012378", "Fatigue", ">=1",
                          evidence="ECO:0006160"),
            _measured_row("b2ai:phq9.no_energy", "HP:0012378", "Fatigue", "==0",
                          modifier="Not", evidence="ECO:0006160", predicate="skos:exactMatch"),
        ],
    )
    codes = {f.code for f in validate_paths([path], check_ontology=False).errors}
    assert {"ungateable-predicate", "withdrawn-column"} <= codes


def test_an_absent_pole_needs_a_gate_and_a_known_modifier(tmp_path: Path):
    path = _write_measured(
        tmp_path,
        [
            _measured_row("b2ai:measurement.lbscat_hct", "HP:0031851", "Reduced hematocrit", "",
                          modifier="Not"),
            _measured_row("b2ai:measurement.lbscat_plt", "HP:0001873", "Thrombocytopenia",
                          "<150", modifier="NOT"),
        ],
    )
    codes = {f.code for f in validate_paths([path], check_ontology=False).errors}
    assert {"ungated-modifier", "bad-predicate-modifier"} <= codes


def test_an_unknown_evidence_code_is_an_error(tmp_path: Path):
    """The apply path can only stamp evidence terms it knows; anything else is refused here."""
    path = _write_measured(
        tmp_path,
        [_measured_row("b2ai:measurement.lbscat_hct", "HP:0031851", "Reduced hematocrit", "<37",
                       evidence="ECO:0000000")],
    )
    codes = {f.code for f in validate_paths([path], check_ontology=False).errors}
    assert "unknown-evidence-code" in codes

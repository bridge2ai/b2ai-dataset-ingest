"""Gates are authored in LinkML; the SSSOM sets are generated from them and must match.

Three layers:

- the ``when_value`` / ``when_subject`` grammar round-trips through LinkML slot
  expressions (inclusive bounds are ``minimum_value`` / ``maximum_value``, exclusive ones
  ``none_of``, membership ``any_of``, strata are conditions on ``sex`` / ``age``);
- every gate schema under ``schema/gates/`` loads through SchemaView and renders to exactly
  the committed ``mappings/*.sssom.tsv`` (the acceptance test ADR-0002 named);
- the rendered rows still pass the SSSOM validator, so nothing the generator emits is a
  shape the apply path would refuse.
"""

from pathlib import Path

import pytest
import yaml

from b2ai_dataset_ingest.mapping.conditions import parse_condition, parse_subject_condition
from b2ai_dataset_ingest.mapping.linkml_gates import (
    condition_to_slot_expression,
    slot_conditions_to_subject,
    slot_expression_to_condition,
    subject_to_slot_conditions,
)

REPO = Path(__file__).parents[1]
GATES = REPO / "schema" / "gates"
SETS = sorted(p for p in GATES.glob("*.yaml") if p.name != "gate_base.yaml")


@pytest.mark.parametrize(
    "text",
    [">=42 & <=47", "<37", ">50", ">=130", "<=-0.5", "in {1,2,3}", "==1", "!=0",
     ">=2 & <=365", ">=25 & <30", '== "Checked"', 'in {"a","b"}', "<0.38"],
)
def test_when_value_round_trips_through_a_slot_expression(text):
    expression = condition_to_slot_expression(parse_condition(text))
    back = slot_expression_to_condition(expression)
    assert back == text


def test_exclusive_bounds_become_none_of_and_inclusive_become_bounds():
    assert condition_to_slot_expression(parse_condition(">50")) == {
        "none_of": [{"maximum_value": 50}]
    }
    assert condition_to_slot_expression(parse_condition(">=42 & <=47")) == {
        "minimum_value": 42, "maximum_value": 47,
    }
    assert condition_to_slot_expression(parse_condition("in {1,2,3}")) == {
        "any_of": [{"equals_number": 1}, {"equals_number": 2}, {"equals_number": 3}],
    }


@pytest.mark.parametrize("text", ['sex == "FEMALE"', 'sex == "MALE" & age >= 50', "age < 50"])
def test_when_subject_round_trips(text):
    conditions = subject_to_slot_conditions(parse_subject_condition(text))
    assert slot_conditions_to_subject(conditions) == text


def test_every_set_has_a_schema_and_the_base_is_imported():
    assert {p.stem for p in SETS} >= {
        "b2ai-aireadi-measurement", "b2ai-aireadi-observation",
        "b2ai-aireadi-devices", "b2ai-aireadi-conditions",
    }
    for path in SETS:
        schema = yaml.safe_load(path.read_text())
        assert "gate_base" in schema["imports"]
        for name, cls in schema["classes"].items():
            assert cls["is_a"] == "GatedItem", name
            assert cls["class_uri"].startswith("b2ai:"), name
            for rule in cls.get("rules") or []:
                post = rule["postconditions"]["slot_conditions"]
                assert post["phenotype"]["equals_string"].startswith(("HP:", "MONDO:"))
                assert post["status"]["equals_string"] in ("present", "excluded")
                assert "value" in rule["preconditions"]["slot_conditions"]


# ---------- the acceptance test: schema -> SSSOM equals the committed SSSOM
linkml_runtime = pytest.importorskip("linkml_runtime")


@pytest.mark.parametrize("schema_path", SETS, ids=lambda p: p.stem)
def test_committed_sssom_is_exactly_what_the_schema_renders(schema_path, tmp_path):
    from b2ai_dataset_ingest.mapping.linkml_gates import schema_to_rows, write_sssom

    header, columns, rows = schema_to_rows(schema_path)
    rendered = tmp_path / "rendered.sssom.tsv"
    write_sssom(rendered, header, columns, rows)
    committed = REPO / "mappings" / f"{schema_path.stem}.sssom.tsv"
    assert rendered.read_text() == committed.read_text(), (
        f"{committed.name} differs from its schema: run scripts/gates_to_sssom.py"
    )


def test_rendered_rows_pass_the_sssom_validator(tmp_path):
    from b2ai_dataset_ingest.mapping.linkml_gates import schema_to_rows, write_sssom
    from b2ai_dataset_ingest.ontology.sssom_validate import validate_paths

    paths = []
    for schema_path in SETS:
        header, columns, rows = schema_to_rows(schema_path)
        target = tmp_path / f"{schema_path.stem}.sssom.tsv"
        write_sssom(target, header, columns, rows)
        paths.append(target)
    result = validate_paths(paths, check_ontology=False)
    assert result.errors == [], result.render()


def test_the_stratified_rule_reads_as_linkml_slot_conditions():
    from linkml_runtime.utils.schemaview import SchemaView

    view = SchemaView(str(GATES / "b2ai-aireadi-measurement.yaml"))
    hct = view.get_class("MeasurementLbscatHct")
    assert str(hct.class_uri) == "b2ai:measurement.lbscat_hct"
    male = [r for r in hct.rules if r.preconditions.slot_conditions.get("sex")
            and r.preconditions.slot_conditions["sex"].equals_string == "MALE"]
    assert male, "expected per-sex rules for hematocrit"
    def post(rule, slot):
        return rule.postconditions.slot_conditions[slot].equals_string

    reduced = [
        r for r in male if post(r, "phenotype") == "HP:0031851" and post(r, "status") == "present"
    ][0]
    value = reduced.preconditions.slot_conditions["value"]
    assert value.none_of[0].minimum_value == 42  # value < 42
    assert reduced.annotations["predicate_id"].value == "skos:relatedMatch"

"""schema/b2ai_ir.yaml and model/core.py describe the same IR, and stay that way.

The LinkML schema is the documented, language-neutral contract (ADR-0002, amended
2026-10-22); the pydantic classes are the executable model. This test needs no LinkML
runtime: it parses the YAML and compares class and field names against the pydantic
models, so a field added to one without the other fails here. When ``linkml-runtime`` is
installed (the ``schema`` extra) it additionally loads the schema through ``SchemaView`` so
a malformed schema fails too.
"""

import inspect
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from b2ai_dataset_ingest.model import core

SCHEMA = Path(__file__).parents[1] / "schema" / "b2ai_ir.yaml"

#: Pydantic ``dict`` fields are modelled as inlined key/value classes in LinkML.
MAP_CLASSES = {"FileAttribute", "CohortAttribute"}


@pytest.fixture(scope="module")
def schema():
    return yaml.safe_load(SCHEMA.read_text())


def _pydantic_models():
    return {
        name: cls
        for name, cls in inspect.getmembers(core, inspect.isclass)
        if issubclass(cls, BaseModel) and cls is not BaseModel and cls.__module__ == core.__name__
    }


def test_every_pydantic_class_is_a_schema_class_and_vice_versa(schema):
    models = set(_pydantic_models())
    classes = set(schema["classes"]) - MAP_CLASSES
    assert models == classes, (
        f"only in pydantic: {models - classes}; only in schema: {classes - models}"
    )


def test_every_field_is_a_schema_attribute_and_vice_versa(schema):
    for name, cls in _pydantic_models().items():
        fields = set(cls.model_fields)
        attributes = set(schema["classes"][name].get("attributes") or {})
        assert fields == attributes, f"{name}: pydantic {fields ^ attributes}"


def test_required_fields_agree(schema):
    for name, cls in _pydantic_models().items():
        required = {f for f, info in cls.model_fields.items() if info.is_required()}
        declared = {
            a for a, spec in (schema["classes"][name].get("attributes") or {}).items()
            if (spec or {}).get("required")
        }
        assert required == declared, f"{name}: required {required ^ declared}"


def test_multivalued_fields_are_lists_or_dicts(schema):
    for name, cls in _pydantic_models().items():
        for attr, spec in (schema["classes"][name].get("attributes") or {}).items():
            annotation = str(cls.model_fields[attr].annotation)
            if (spec or {}).get("multivalued"):
                assert annotation.startswith(("list[", "dict[")), f"{name}.{attr}: {annotation}"
            else:
                assert not annotation.startswith(("list[", "dict[")), f"{name}.{attr}: {annotation}"


def test_participant_is_the_tree_root(schema):
    roots = [n for n, c in schema["classes"].items() if (c or {}).get("tree_root")]
    assert roots == ["Participant"]


def test_schema_loads_in_linkml_when_available():
    linkml_runtime = pytest.importorskip("linkml_runtime")
    from linkml_runtime.utils.schemaview import SchemaView

    view = SchemaView(str(SCHEMA))
    assert "Participant" in view.all_classes()
    induced = view.induced_slot("individual", "Participant")
    assert induced.range == "Individual" and induced.required
    assert linkml_runtime is not None

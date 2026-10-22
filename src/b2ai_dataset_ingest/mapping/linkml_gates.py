"""Gates authored in LinkML, SSSOM generated from them.

ADR-0002 (amended 2026-10-22, second amendment) moves the *authoring* of value gates from
SSSOM extension columns into LinkML, where a gate is a standard ``ClassRule``:

    classes:
      LbscatHct:
        is_a: GatedItem
        class_uri: b2ai:measurement.lbscat_hct      # the SSSOM subject
        title: Hematocrit [Volume Fraction] of Blood by Automated count
        related_mappings: [HP:0000001]              # an UNGATED row: term-to-term only
        rules:
          - title: reduced hematocrit, men
            preconditions:
              slot_conditions:
                value: {none_of: [{minimum_value: 42}]}     # value < 42
                sex: {equals_string: MALE}                  # when_subject
            postconditions:
              slot_conditions:
                phenotype: {equals_string: "HP:0031851"}
                status: {equals_string: present}            # or excluded (predicate_modifier Not)
            annotations: {predicate_id: skos:relatedMatch, confidence: "0.9", ...}

Everything the pipeline executes -- the value gate, the stratum, the pole -- is expressed
with LinkML's own ``slot_conditions`` vocabulary (``minimum_value``, ``maximum_value``,
``equals_number``, ``equals_string``, ``any_of``, ``none_of``), so the semantics are
LinkML's and any LinkML tool can read them. What SSSOM needs beyond that (predicate, label,
confidence, provenance columns) rides in rule annotations; the set-level SSSOM header is
kept verbatim in a schema annotation.

This module is the translation in both directions:

- :func:`schema_to_rows` reads a gate schema through ``SchemaView`` and yields the SSSOM
  rows; ``scripts/gates_to_sssom.py`` writes them. The committed ``mappings/*.sssom.tsv``
  must equal that output (``tests/test_linkml_gates.py``), so the TSV is an artefact and the
  schema is the source.
- :func:`rows_to_schema` is the inverse, used once to migrate the hand-curated sets and
  kept so a reviewer's SSSOM edit (the sheet loop) can be folded back into the schema.

The apply path is unchanged: ``hpo_rules`` still reads the generated SSSOM, so the shareable
artefact and the executed artefact are the same file.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from b2ai_dataset_ingest.mapping.conditions import (
    Comparison,
    SubjectCondition,
    ValueCondition,
    parse_condition,
    parse_subject_condition,
)

#: SSSOM columns that become rule annotations (everything that is not the gate itself).
ANNOTATION_COLUMNS = (
    "predicate_id",
    "object_label",
    "mapping_justification",
    "confidence",
    "comment",
    "evidence_code",
    "loinc_id",
    "l2h_outcome",
    "threshold_source",
)
#: ``predicate_id`` -> the LinkML class-level mapping slot for an ungated row.
PREDICATE_SLOTS = {
    "skos:exactMatch": "exact_mappings",
    "skos:closeMatch": "close_mappings",
    "skos:broadMatch": "broad_mappings",
    "skos:narrowMatch": "narrow_mappings",
    "skos:relatedMatch": "related_mappings",
}
SLOT_PREDICATES = {v: k for k, v in PREDICATE_SLOTS.items()}
BASE_SCHEMA = "gate_base.yaml"


# ------------------------------------------------------------ when_value <-> slot expression
def condition_to_slot_expression(condition: ValueCondition) -> dict[str, Any]:
    """A parsed ``when_value`` as one LinkML slot expression (constraints are conjunctive)."""
    expression: dict[str, Any] = {}
    for comparison in condition.comparisons:
        for key, value in _comparison_fragments(comparison).items():
            if key == "none_of":
                expression.setdefault("none_of", []).extend(value)
            elif key in expression:
                raise ValueError(f"cannot express {condition.raw!r}: repeated {key}")
            else:
                expression[key] = value
    return expression


def _comparison_fragments(comparison: Comparison) -> dict[str, Any]:
    op, operand = comparison.op, comparison.operand
    if op == "in":
        items = list(operand)
        if all(isinstance(i, (int, float)) and not isinstance(i, bool) for i in items):
            return {"any_of": [{"equals_number": i} for i in items]}
        return {"equals_string_in": [str(i) for i in items]}
    if isinstance(operand, str):
        if op == "==":
            return {"equals_string": operand}
        if op == "!=":
            return {"none_of": [{"equals_string": operand}]}
        raise ValueError(f"ordering on a string operand: {op} {operand!r}")
    return {
        ">=": {"minimum_value": operand},
        "<=": {"maximum_value": operand},
        ">": {"none_of": [{"maximum_value": operand}]},
        "<": {"none_of": [{"minimum_value": operand}]},
        "==": {"equals_number": operand},
        "!=": {"none_of": [{"equals_number": operand}]},
    }[op]


def slot_expression_to_condition(expression: Any) -> str:
    """The canonical ``when_value`` text for a slot expression (dict or SchemaView object)."""
    get = _getter(expression)
    terms: list[str] = []
    if get("minimum_value") is not None:
        terms.append(f">={_num(get('minimum_value'))}")
    if get("maximum_value") is not None:
        terms.append(f"<={_num(get('maximum_value'))}")
    if get("equals_number") is not None:
        terms.append(f"=={_num(get('equals_number'))}")
    if get("equals_string"):
        terms.append(f'== "{get("equals_string")}"')
    if get("equals_string_in"):
        terms.append("in {" + ",".join(f'"{s}"' for s in get("equals_string_in")) + "}")
    if get("any_of"):
        numbers = [_getter(item)("equals_number") for item in get("any_of")]
        if any(n is None for n in numbers):
            raise ValueError("any_of is only used for numeric membership")
        terms.append("in {" + ",".join(_num(n) for n in numbers) + "}")
    for item in get("none_of") or []:
        inner = _getter(item)
        if inner("maximum_value") is not None:
            terms.append(f">{_num(inner('maximum_value'))}")
        elif inner("minimum_value") is not None:
            terms.append(f"<{_num(inner('minimum_value'))}")
        elif inner("equals_number") is not None:
            terms.append(f"!={_num(inner('equals_number'))}")
        elif inner("equals_string"):
            terms.append(f'!= "{inner("equals_string")}"')
        else:
            raise ValueError("unsupported none_of member")
    return " & ".join(terms)


def _getter(expression: Any):
    if isinstance(expression, dict):
        return lambda key: expression.get(key)
    return lambda key: getattr(expression, key, None)


def _num(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


# ------------------------------------------------------------ when_subject <-> slot conditions
def subject_to_slot_conditions(condition: SubjectCondition) -> dict[str, dict[str, Any]]:
    return {
        field: condition_to_slot_expression(value_condition)
        for field, value_condition in condition.conditions
    }


def slot_conditions_to_subject(conditions: dict[str, Any]) -> str:
    """``when_subject`` text from the non-value slot conditions, sex before age."""
    terms = []
    for field in ("sex", "age"):
        if field in conditions:
            text = slot_expression_to_condition(conditions[field])
            # one subject term per field, written `field op operand` with spaces
            for atom in text.split(" & "):
                match = _ATOM.match(atom)
                spaced = f"{match.group(1)} {match.group(2)}" if match else atom
                terms.append(f"{field} {spaced}")
    return " & ".join(terms)


_ATOM = re.compile(r"^(>=|<=|==|!=|>|<)\s*(.+)$")


# ------------------------------------------------------------ SSSOM rows -> schema
def rows_to_schema(
    name: str,
    header_yaml: str,
    columns: list[str],
    rows: Iterable[dict[str, str]],
    prefixes: dict[str, str],
) -> dict[str, Any]:
    """Build a gate schema (as a dict ready for YAML) from one SSSOM set's rows."""
    classes: dict[str, dict[str, Any]] = {}
    for row in rows:
        subject = row["subject_id"].strip()
        class_name = _class_name(subject)
        cls = classes.setdefault(
            class_name,
            {
                "is_a": "GatedItem",
                "class_uri": subject,
                "title": row.get("subject_label", "").strip(),
            },
        )
        when_value = (row.get("when_value") or "").strip()
        if not when_value:
            _add_semantic_mapping(cls, row)
            continue
        rule: dict[str, Any] = {
            "title": _rule_title(row),
            "preconditions": {
                "slot_conditions": {
                    "value": condition_to_slot_expression(parse_condition(when_value))
                }
            },
            "postconditions": {
                "slot_conditions": {
                    "phenotype": {"equals_string": row["object_id"].strip()},
                    "status": {
                        "equals_string": "excluded"
                        if (row.get("predicate_modifier") or "").strip()
                        else "present"
                    },
                }
            },
        }
        when_subject = (row.get("when_subject") or "").strip()
        if when_subject:
            rule["preconditions"]["slot_conditions"].update(
                subject_to_slot_conditions(parse_subject_condition(when_subject))
            )
        annotations = {
            c: row[c].strip() for c in ANNOTATION_COLUMNS if c in row and row[c].strip()
        }
        rule["annotations"] = annotations
        cls.setdefault("rules", []).append(rule)
    return {
        "id": f"https://w3id.org/bridge2ai/b2ai-dataset-ingest/gates/{name}",
        "name": name,
        "description": (
            "Value gates for one SSSOM mapping set, authored as LinkML class rules. "
            "The SSSOM file is GENERATED from this schema by scripts/gates_to_sssom.py; "
            "edit here, regenerate, commit both."
        ),
        "imports": ["linkml:types", BASE_SCHEMA.removesuffix(".yaml")],
        "prefixes": {"linkml": "https://w3id.org/linkml/", **prefixes},
        "default_prefix": "b2ai",
        "default_range": "string",
        "annotations": {
            "sssom_header": header_yaml,
            "sssom_columns": "\t".join(columns),
        },
        "classes": classes,
    }


def _add_semantic_mapping(cls: dict[str, Any], row: dict[str, str]) -> None:
    predicate = row["predicate_id"].strip()
    slot = PREDICATE_SLOTS[predicate]
    obj = row["object_id"].strip()
    cls.setdefault(slot, []).append(obj)
    meta = {
        c: row[c].strip()
        for c in ANNOTATION_COLUMNS
        if c in row and row[c].strip() and c != "predicate_id"
    }
    cls.setdefault("annotations", {})[obj] = {"annotations": meta}


def _class_name(subject: str) -> str:
    local = subject.split(":", 1)[1]
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[._\-]+", local))


def _rule_title(row: dict[str, str]) -> str:
    pole = "absent" if (row.get("predicate_modifier") or "").strip() else "present"
    stratum = (row.get("when_subject") or "").strip()
    label = (row.get("object_label") or row.get("object_id") or "").strip()
    return f"{label}, {pole}" + (f", {stratum}" if stratum else "")


# ------------------------------------------------------------ schema -> SSSOM rows
def schema_to_rows(path: Path) -> tuple[str, list[str], list[dict[str, str]]]:
    """``(header_yaml, columns, rows)`` from a gate schema, via SchemaView.

    Classes are emitted in schema order; within a class the ungated mappings come first
    (exact, close, broad, narrow, related), then the rules in order.
    """
    from linkml_runtime.utils.schemaview import SchemaView

    view = SchemaView(str(path))
    schema = view.schema
    header_yaml = schema.annotations["sssom_header"].value
    columns = schema.annotations["sssom_columns"].value.split("\t")
    rows: list[dict[str, str]] = []
    for class_name, cls in schema.classes.items():
        if not cls.class_uri or cls.is_a != "GatedItem":
            continue
        subject = str(cls.class_uri)
        label = cls.title or ""
        for slot in ("exact_mappings", "close_mappings", "broad_mappings", "narrow_mappings",
                     "related_mappings"):
            for obj in getattr(cls, slot) or []:
                meta = _nested_annotations(cls.annotations.get(str(obj)))
                rows.append(
                    _row(columns, subject, label, SLOT_PREDICATES[slot], "", str(obj), meta)
                )
        for rule in cls.rules or []:
            pre = rule.preconditions.slot_conditions if rule.preconditions else {}
            post = rule.postconditions.slot_conditions if rule.postconditions else {}
            meta = {k: v.value for k, v in (rule.annotations or {}).items()}
            predicate = meta.pop("predicate_id", "")
            status = post["status"].equals_string if "status" in post else "present"
            when_value = slot_expression_to_condition(pre["value"]) if "value" in pre else ""
            subject_text = slot_conditions_to_subject(
                {k: v for k, v in pre.items() if k in ("sex", "age")}
            )
            row = _row(
                columns, subject, label, predicate,
                "Not" if status == "excluded" else "",
                post["phenotype"].equals_string, meta,
            )
            row["when_value"] = when_value
            if "when_subject" in columns:
                row["when_subject"] = subject_text
            elif subject_text:
                raise ValueError(f"{class_name}: stratified rule but no when_subject column")
            rows.append(row)
    return header_yaml, columns, rows


def _nested_annotations(annotation: Any) -> dict[str, str]:
    if annotation is None:
        return {}
    return {k: v.value for k, v in (annotation.annotations or {}).items()}


def _row(
    columns: list[str],
    subject: str,
    subject_label: str,
    predicate: str,
    modifier: str,
    obj: str,
    meta: dict[str, str],
) -> dict[str, str]:
    row = dict.fromkeys(columns, "")
    row["subject_id"] = subject
    if "subject_label" in row:
        row["subject_label"] = subject_label
    row["predicate_id"] = predicate
    if "predicate_modifier" in row:
        row["predicate_modifier"] = modifier
    row["object_id"] = obj
    for key, value in meta.items():
        if key in row:
            row[key] = value
    return row


def write_sssom(
    target: Path, header_yaml: str, columns: list[str], rows: list[dict[str, str]]
) -> None:
    lines = ["# " + ln if ln else "#" for ln in header_yaml.rstrip("\n").splitlines()]
    with open(target, "w") as fh:
        fh.write("\n".join(lines) + "\n")
        fh.write("\t".join(columns) + "\n")
        for row in rows:
            fh.write("\t".join(row.get(c, "").replace("\t", " ") for c in columns) + "\n")


def sssom_header_yaml(path: Path) -> str:
    """The commented SSSOM header of a TSV, verbatim minus the comment markers."""
    out = []
    for line in path.read_text().splitlines():
        if not line.startswith("#"):
            break
        out.append(line[2:] if line.startswith("# ") else line[1:])
    return "\n".join(out) + "\n"


class _Dumper(yaml.SafeDumper):
    """Multi-line strings (the SSSOM header, long comments) as literal blocks, for diffs."""


def _represent_str(dumper: yaml.SafeDumper, value: str):
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_Dumper.add_representer(str, _represent_str)


def dump_schema(schema: dict[str, Any], target: Path) -> None:
    target.write_text(
        yaml.dump(schema, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)
    )


__all__ = [
    "condition_to_slot_expression",
    "slot_expression_to_condition",
    "subject_to_slot_conditions",
    "slot_conditions_to_subject",
    "rows_to_schema",
    "schema_to_rows",
    "write_sssom",
    "sssom_header_yaml",
    "dump_schema",
]

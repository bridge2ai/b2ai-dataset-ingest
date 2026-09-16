"""Load and validate YAML mapping configs.

A mapping config declares how one source table's columns become IR concepts. Two entry
points:

- :func:`load_mapping` parses one file into a dict.
- :func:`validate_mapping` returns a list of human-readable *warnings* (not errors) about
  a mapping — chiefly ontology terms still flagged as placeholders (``TODO`` /
  ``MONDO:0000000``) or missing the ``{id, label}`` shape. Placeholders are expected in
  v1 (many diagnosis conditions are deferred), so they warn rather than fail; the reader
  logs the warnings and simply skips unresolved terms.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Sentinels that mark a term as not-yet-resolved.
PLACEHOLDER_IDS = {"TODO", "MONDO:0000000", ""}

# Field names that mark a dict as a ReproSchema data-dictionary *element* (one per column).
# Used to tell a flat {column: element} dict apart from an unrelated JSON object.
DATA_ELEMENT_FIELDS = frozenset(
    {
        "description",
        "valueType",
        "datatype",
        "choices",
        "question",
        "termURL",
        "minValue",
        "maxValue",
    }
)


def load_mapping(path: Path) -> dict[str, Any]:
    """Load a single YAML mapping file into a dict."""
    with open(path) as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Mapping file {path} did not parse to a mapping/dict")
    return data


def normalize_data_dict(doc: Any, source: str = "<data dict>") -> dict[str, Any]:
    """Normalize a ReproSchema companion data dict to a flat ``{column: element}`` map.

    Two envelope shapes occur in the wild and must both be understood:

    - **Flat** — the shape the *real* b2aiprep release publishes: top-level keys are the
      column names, each value the element dict (``{description, valueType, choices, ...}``).
    - **Nested** — the shape the local synthetic generator emits:
      ``{<table>: {"description", "url", "data_elements": {<column>: {...}}}}``.

    Returns the inner ``{column: element}`` map for either shape, or ``{}`` (with a warning)
    for an empty/unreadable/unrecognized document, so callers get a single, flat contract.
    """
    if not isinstance(doc, dict) or not doc:
        return {}
    # Nested envelope: a top-level value carries a ``data_elements`` map.
    for value in doc.values():
        if isinstance(value, dict) and isinstance(value.get("data_elements"), dict):
            return value["data_elements"]
    # Flat envelope: every top-level value looks like a data-dictionary element.
    if all(isinstance(v, dict) and (DATA_ELEMENT_FIELDS & v.keys()) for v in doc.values()):
        return doc
    logger.warning(
        "%s: unrecognized data-dict envelope (%d top-level keys); choices/labels unavailable",
        source,
        len(doc),
    )
    return {}


def load_data_dict(path: Path) -> dict[str, Any]:
    """Read a companion data-dict JSON and return a flat ``{column: element}`` map.

    Delegates envelope handling to :func:`normalize_data_dict`. A missing or malformed file
    yields ``{}`` (the reader/engine then fall back to the config ``ordinal_scale``).
    """
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("could not read data dict %s: %s", path, exc)
        return {}
    return normalize_data_dict(doc, source=path.name)


def is_placeholder(term: Any) -> bool:
    """True if ``term`` is a missing / unresolved ``{id, label}`` ontology term."""
    if not isinstance(term, dict):
        return True
    return term.get("id") in PLACEHOLDER_IDS or term.get("label") in {None, "", "TODO"}


def iter_terms(mapping: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(context, term_dict)`` for every ``{id, label}`` term in a mapping.

    Understands the voice shapes — ``conditions`` (diagnosis), ``items`` and
    ``score.assay`` / ``score.unit`` (questionnaires) — and the OMOP shapes: ``measures``
    (per-item ``{assay, unit, procedure_code, body_site}``), ``units`` and ``laterality``.

    A block this function does not walk is a block ``validate_mapping`` cannot warn about,
    so every new config shape must be added here or its placeholders ship silently.
    """
    for name, term in (mapping.get("conditions") or {}).items():
        yield f"conditions.{name}", term
    for name, term in (mapping.get("items") or {}).items():
        yield f"items.{name}", term
    score = mapping.get("score")
    if isinstance(score, dict):
        if "assay" in score:
            yield "score.assay", score["assay"]
        if "unit" in score:
            yield "score.unit", score["unit"]
    # OMOP long-table shapes.
    for name, spec in (mapping.get("measures") or {}).items():
        if not isinstance(spec, dict):
            continue
        for slot in ("assay", "unit", "procedure_code", "body_site"):
            if isinstance(spec.get(slot), dict):
                yield f"measures.{name}.{slot}", spec[slot]
    for block in ("units", "laterality"):
        for name, term in (mapping.get(block) or {}).items():
            yield f"{block}.{name}", term
    study_group = mapping.get("study_group")
    if isinstance(study_group, dict):
        if isinstance(study_group.get("assay"), dict):
            yield "study_group.assay", study_group["assay"]
        for name, term in (study_group.get("value_terms") or {}).items():
            yield f"study_group.value_terms.{name}", term


def validate_mapping(mapping: dict[str, Any]) -> list[str]:
    """Return warnings about a mapping (unresolved / malformed ontology terms)."""
    warnings: list[str] = []
    for context, term in iter_terms(mapping):
        if is_placeholder(term):
            tid = term.get("id") if isinstance(term, dict) else term
            warnings.append(f"unresolved ontology term at {context} (id={tid!r})")
    return warnings

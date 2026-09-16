"""Read the B2AI -> HPO SSSOM/TSV mapping files, preserving every column.

This is the in-repo, dependency-light (stdlib + PyYAML) SSSOM/TSV reader shared by the
*validator* (:mod:`b2ai_dataset_ingest.ontology.sssom_validate`) and the *apply path*
(:mod:`b2ai_dataset_ingest.mapping.hpo_rules`). It exists because the reference ``sssom-py``
parser **drops** extension columns on load (retaining only ``extension_definitions`` in the
metadata), so it cannot carry the ``when_value`` condition; :func:`parse_sssom` keeps all
columns, including ``when_value`` (ADR-0002 decision 5).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class MetadataError(ValueError):
    """Raised when the SSSOM metadata header is not parseable YAML."""


def parse_sssom(path: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Parse a SSSOM/TSV file into ``(metadata, rows)``.

    The commented (``#``) prologue is joined and parsed as the SSSOM YAML metadata block; the
    remaining lines are a TSV whose first row is the header. Every column is preserved on each
    row (unlike ``sssom-py``, which drops extension columns). Raises :class:`MetadataError` if
    the header is not valid YAML.
    """
    meta_lines: list[str] = []
    tsv_lines: list[str] = []
    for raw in path.read_text().splitlines():
        if raw.startswith("#"):
            content = raw[1:]
            meta_lines.append(content[1:] if content.startswith(" ") else content)
        elif raw.strip():
            tsv_lines.append(raw)
    try:
        metadata = yaml.safe_load("\n".join(meta_lines)) if meta_lines else {}
    except yaml.YAMLError as exc:
        raise MetadataError(str(exc)) from exc
    if not isinstance(metadata, dict):
        metadata = {}
    rows: list[dict[str, str]] = []
    if tsv_lines:
        header = tsv_lines[0].split("\t")
        for line in tsv_lines[1:]:
            cells = line.split("\t")
            # pad short rows so trailing empty cells (e.g. blank comment) don't misalign
            cells += [""] * (len(header) - len(cells))
            rows.append(dict(zip(header, cells, strict=False)))
    return metadata, rows


def default_mapping_files(repo_root: Path | None = None, dataset: str | None = None) -> list[Path]:
    """The shipped SSSOM files under ``mappings/``.

    ``dataset`` narrows the glob to one dataset's sets (``b2ai-<dataset>-*.sssom.tsv``).
    A reader must pass it: ``load_conditional_rules`` indexes rules by bare table name
    across every file it is given, so an unscoped load would let one dataset's value-gated
    rules fire on another dataset's identically-named table.
    """
    root = repo_root or Path(__file__).resolve().parents[3]
    pattern = f"b2ai-{dataset}-*.sssom.tsv" if dataset else "*.sssom.tsv"
    return sorted((root / "mappings").glob(pattern))

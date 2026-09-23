"""Validate the B2AI -> HPO SSSOM mapping files.

This is the durable, in-repo guardrail against hallucinated / drifted ontology terms. It runs
three layers of checks and returns structured :class:`Finding` objects:

1. **Structural** (always, offline, no deps): required columns, a known ``skos:`` predicate,
   a ``mapping_justification``, a well-formed ``b2ai:<table>.<column>`` subject, a well-formed
   ``HP:`` object CURIE, in-range ``confidence``, no duplicate ``(subject, predicate,
   predicate_modifier, object)`` (across the whole set), a known ``evidence_code``, a
   ``when_value``/``predicate_modifier`` that the row's evidence kind permits, and every CURIE
   prefix used declared in the file's own ``curie_map`` (self-containment).
2. **Subject existence** (offline, when a ``data_root`` is given): each ``b2ai:<table>.<column>``
   subject must name a real column in the corresponding data dictionary
   (``<data_root>/**/<table>.json``, via :func:`mapping.loaders.load_data_dict`). Skipped (not
   failed) when no data root is available; unresolved tables are counted and surfaced.
3. **Anti-hallucination**: every ``object_id`` must exist, not be deprecated (checked against
   ``owl:deprecated`` via ``adapter.obsoletes()`` -- not merely the ``"obsolete "`` label
   convention), and its ``object_label`` must equal the ontology's authoritative label (an
   *exact* synonym only warns). **Checked against the release the file declares**: a file whose
   ``object_source_version`` names a release date is validated against that release's published
   obographs JSON (:mod:`ontology.releases`, fetched from PURL and cached), so a relabel in a
   later release cannot turn a correct file red. A file declaring no release falls back to
   oaklib's current SQLite build, and the loaded release is surfaced either way. Skipped (not
   failed) when no backend is available, unless ``check_ontology`` forces it.

**Which ontology a file maps to is the file's own declaration.** Each set names one in its
``object_source`` metadata (SSSOM makes it a mapping-*set*-level slot, which is why a MONDO set is
a separate file rather than MONDO rows in the HPO file); :data:`OBJECT_SOURCES` turns that into
the CURIE prefix every ``object_id`` must carry and the oaklib adapter to check it against.

The SSSOM/TSV parser is intentionally dependency-light (stdlib + PyYAML, already a project
dep) so structural + subject checks never need the heavy ontology stack.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from b2ai_dataset_ingest.mapping.conditions import ConditionParseError, parse_condition
from b2ai_dataset_ingest.mapping.hpo_rules import EVIDENCE_CODES, is_self_report
from b2ai_dataset_ingest.mapping.loaders import load_data_dict
from b2ai_dataset_ingest.mapping.sssom_io import (
    MetadataError,
    default_mapping_files,
    parse_sssom,
)
from b2ai_dataset_ingest.ontology.releases import (
    PinnedRelease,
    ReleaseUnavailable,
    cache_dir,
    release_date,
)

logger = logging.getLogger(__name__)

#: Object ontologies a mapping set may declare in its ``object_source``, mapped to the CURIE
#: prefix its ``object_id``s must use and the oaklib selector that verifies them offline (the
#: HP selector matches ontology/terms.py). A file declaring anything else is an error rather
#: than an unchecked pass -- an unknown source would silently skip the anti-hallucination layer.
OBJECT_SOURCES = {
    "obo:hp": ("HP", "sqlite:obo:hp"),
    "obo:mondo": ("MONDO", "sqlite:obo:mondo"),
    "obo:uberon": ("UBERON", "sqlite:obo:uberon"),
}

ALLOWED_PREDICATES = frozenset(
    {
        "skos:exactMatch",
        "skos:broadMatch",
        "skos:narrowMatch",
        "skos:relatedMatch",
        "skos:closeMatch",
    }
)
REQUIRED_COLUMNS = ("subject_id", "predicate_id", "object_id", "mapping_justification")
#: Only these predicates may carry a ``when_value`` on a SELF-REPORT row: deriving "the
#: participant has this phenotype" from an endorsement is sound only when the HPO term is the
#: same as, or broader than, what the item asked (ADR-0002, amended after clinical review).
GATEABLE_PREDICATES = frozenset({"skos:exactMatch", "skos:broadMatch"})
#: A MEASURED-VALUE row (``evidence_code`` other than self-report) may additionally gate on
#: ``skos:relatedMatch``: an assay is not the phenotype, so the term-to-term relation is loose,
#: but the phenotype is *defined* by the assay falling outside its reference range, so the gate
#: plus the assay entails the term (docs/mapping-conventions.md, "Measured-value mappings").
MEASURED_GATEABLE_PREDICATES = GATEABLE_PREDICATES | {"skos:relatedMatch"}
#: Withdrawn after clinical review (2026-08-24) FOR SELF-REPORT ROWS: ``predicate_modifier:
#: Not`` asserted an *unqualified* absence from an answer scoped to the instrument's recall
#: window. A measured value is different -- a normal result on a dated assay rules the
#: abnormality out at that observation, and the derived feature carries that time -- so the
#: modifier is allowed there and refused here (docs/mapping-conventions.md).
WITHDRAWN_SELF_REPORT_MODIFIER = (
    "absent poles were withdrawn for self-report on clinical review; a questionnaire's "
    "lowest answer denies the symptom within the instrument's recall window, not the "
    "phenotype. Only a measured-value row (evidence_code ECO:0007307) may carry one"
)
#: The one value SSSOM defines for ``predicate_modifier``.
PREDICATE_MODIFIERS = frozenset({"Not"})
_RELEASE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class Finding:
    """One validation problem. ``error`` findings should fail CI; ``warning`` are advisory."""

    file: str
    subject: str
    severity: str  # "error" | "warning"
    code: str
    message: str

    def render(self) -> str:
        return f"[{self.severity.upper()}] {self.file}: {self.subject}: {self.code}: {self.message}"


@dataclass
class ValidationResult:
    findings: list[Finding] = field(default_factory=list)
    n_rows: int = 0
    ontology_checked: bool = False
    subjects_checked: bool = False
    #: object_source -> loaded release date, for every ontology actually consulted
    ontology_versions: dict[str, str] = field(default_factory=dict)
    subjects_unresolved: int = 0  # subjects whose data-dict table could not be resolved

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    def render(self) -> str:
        lines = [f.render() for f in self.findings]
        loaded = ", ".join(f"{s}@{v}" for s, v in sorted(self.ontology_versions.items()))
        ont = f"ontology={loaded or '?'}" if self.ontology_checked else "ontology=SKIPPED"

        subj = "subjects=data-dict" if self.subjects_checked else "subjects=SKIPPED"
        if self.subjects_checked and self.subjects_unresolved:
            subj += f" ({self.subjects_unresolved} unresolved)"
        lines.append(
            f"checked {self.n_rows} mappings ({ont}, {subj}): "
            f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)"
        )
        return "\n".join(lines)


# ------------------------------------------------------------------- ontology backend


def _get_ontology_adapter(selector: str):
    """Return an oaklib adapter for ``selector``, or None if oaklib / that backend is missing."""
    try:
        from oaklib import get_adapter
    except ImportError:
        return None
    try:
        return get_adapter(selector)
    except Exception as exc:  # noqa: BLE001 - oaklib raises many backend/network errors
        logger.warning("oaklib adapter %s unavailable (%s); ontology checks skipped", selector, exc)
        return None


def _adapter_version(adapter) -> str | None:
    """Best-effort ``owl:versionIRI`` release date of the loaded ontology (e.g. ``2026-02-16``)."""
    try:
        from sqlalchemy import text

        with adapter.engine.connect() as conn:
            rows = conn.execute(
                text("select object from statements where predicate='owl:versionIRI' limit 1")
            ).fetchall()
        if rows:
            m = _RELEASE_DATE.search(str(rows[0][0]))
            return m.group(1) if m else str(rows[0][0])
    except Exception as exc:  # noqa: BLE001 - version is best-effort, never fatal
        logger.debug("could not read HPO versionIRI: %s", exc)
    return None


def _obsolete_ids(adapter, prefix: str) -> set[str]:
    """The set of deprecated CURIEs in ``prefix`` (``owl:deprecated``), or empty on failure."""
    try:
        return {c for c in adapter.obsoletes() if isinstance(c, str) and c.startswith(f"{prefix}:")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("adapter.obsoletes() failed (%s); falling back to label heuristic", exc)
        return set()


def _exact_synonyms(adapter, curie: str) -> set[str]:
    """Lower-cased exact synonyms of ``curie`` (``oio:hasExactSynonym``), or empty."""
    try:
        meta = adapter.entity_metadata_map(curie)
    except Exception:  # noqa: BLE001
        return set()
    out: set[str] = set()
    for key in ("oio:hasExactSynonym", "IAO:0000118"):
        for v in meta.get(key, []) or []:
            out.add(str(v).lower())
    return out


# ----------------------------------------------------------------- subject resolution


def _resolve_data_dict(table: str, data_root: Path) -> Path | None:
    """Find ``<table>.json`` anywhere under ``data_root`` (table stems are unique)."""
    matches = sorted(data_root.glob(f"**/{table}.json"))
    return matches[0] if len(matches) == 1 else None


# ------------------------------------------------------------------------- validation


def _check_row(
    row: dict[str, str],
    fname: str,
    file_prefixes: set[str],
    seen: set[tuple[str, str, str, str]],
    object_prefix: str,
) -> Iterable[Finding]:
    subj = row.get("subject_id", "").strip()
    pred = row.get("predicate_id", "").strip()
    obj = row.get("object_id", "").strip()
    ident = subj or "<no subject_id>"

    def err(code: str, message: str) -> Finding:
        return Finding(fname, ident, "error", code, message)

    for col in REQUIRED_COLUMNS:
        if not row.get(col, "").strip():
            yield err("missing-field", f"empty required column {col!r}")

    if subj and not subj.startswith("b2ai:"):
        yield err("bad-subject", f"subject_id must be a b2ai: CURIE, got {subj!r}")
    elif subj and "." not in subj.split(":", 1)[1]:
        yield err("malformed-subject", f"b2ai subject must be b2ai:<table>.<column>: {subj!r}")
    if pred and pred not in ALLOWED_PREDICATES:
        yield err("bad-predicate", f"predicate_id {pred!r} not in {sorted(ALLOWED_PREDICATES)}")
    if obj and not obj.startswith(f"{object_prefix}:"):
        yield err(
            "bad-object",
            f"object_id must be a {object_prefix}: CURIE (this file declares object_source "
            f"for {object_prefix}), got {obj!r}",
        )

    evidence = row.get("evidence_code", "").strip()
    modifier = row.get("predicate_modifier", "").strip()

    # self-containment: every CURIE used must be declared in this file's own curie_map
    for curie in (subj, pred, obj, row.get("mapping_justification", "").strip(), evidence):
        if curie and ":" in curie:
            prefix = curie.split(":", 1)[0]
            if prefix not in file_prefixes:
                yield err("unknown-prefix", f"prefix {prefix!r} not in the file's curie_map")

    conf = row.get("confidence", "").strip()
    if conf:
        try:
            val = float(conf)
            if not 0.0 <= val <= 1.0:
                yield err("bad-confidence", f"confidence {val} out of [0,1]")
        except ValueError:
            yield err("bad-confidence", f"confidence {conf!r} is not a number")

    # The evidence kind decides which gating rules apply. Absent means self-report (the Voice
    # sets predate the column); anything else must be a code the apply path knows how to stamp.
    if evidence and evidence not in EVIDENCE_CODES:
        yield err(
            "unknown-evidence-code",
            f"evidence_code {evidence!r} not in {sorted(EVIDENCE_CODES)}; the apply path "
            "cannot stamp an evidence term it does not know",
        )
    self_report = is_self_report(evidence)

    # The value gate (ADR-0002): a parseable when_value, and only on a predicate that can carry
    # one. The validator checks structure only — a cut-point being *clinically* right needs a
    # curator.
    when_value = row.get("when_value", "").strip()
    if when_value:
        try:
            parse_condition(when_value)
        except ConditionParseError as exc:
            yield err("bad-when-value", f"when_value {when_value!r} does not parse: {exc}")
        gateable = GATEABLE_PREDICATES if self_report else MEASURED_GATEABLE_PREDICATES
        if pred and pred not in gateable:
            yield err(
                "ungateable-predicate",
                f"when_value {when_value!r} on a {pred} row: only {sorted(gateable)} "
                + (
                    "assert a phenotype the item's endorsement actually establishes"
                    if self_report
                    else "may carry a reference-range gate"
                ),
            )

    # The absent pole. Row-level rather than column-level, so one file can carry measured
    # rows that rule a phenotype out alongside rows that do not.
    if modifier:
        if modifier not in PREDICATE_MODIFIERS:
            yield err(
                "bad-predicate-modifier",
                f"predicate_modifier {modifier!r} not in {sorted(PREDICATE_MODIFIERS)}",
            )
        if self_report:
            yield err("withdrawn-column", f"predicate_modifier: {WITHDRAWN_SELF_REPORT_MODIFIER}")
        elif not when_value:
            yield err(
                "ungated-modifier",
                "predicate_modifier: Not without a when_value asserts nothing; the gate is "
                "what names the values that rule the phenotype out",
            )

    if subj and pred and obj:
        key = (subj, pred, modifier, obj)
        if key in seen:
            yield err("duplicate", f"duplicate mapping {key}")
        seen.add(key)


def _check_subject_exists(
    row: dict[str, str], fname: str, data_root: Path, result: ValidationResult
) -> Iterable[Finding]:
    subj = row.get("subject_id", "").strip()
    if not subj.startswith("b2ai:") or "." not in subj:
        return  # malformed subjects are flagged structurally in _check_row
    local = subj.split(":", 1)[1]
    table, column = local.split(".", 1)  # first dot: table stem never contains a dot
    dict_path = _resolve_data_dict(table, data_root)
    if dict_path is None:
        result.subjects_unresolved += 1
        yield Finding(
            fname, subj, "warning", "table-not-found",
            f"no unique {table}.json under {data_root}; subject left unverified",
        )
        return
    columns = load_data_dict(dict_path)
    if column not in columns:
        yield Finding(
            fname, subj, "error", "unknown-column",
            f"column {column!r} not found in {dict_path.name}",
        )


def _check_object_in_ontology(
    row: dict[str, str], fname: str, adapter, obsolete_ids: set[str], prefix: str
) -> Iterable[Finding]:
    subj = row.get("subject_id", "").strip() or "<no subject_id>"
    obj = row.get("object_id", "").strip()
    declared = row.get("object_label", "").strip()
    if not obj.startswith(f"{prefix}:"):
        return  # structural check already flagged it
    label = adapter.label(obj)
    if label is None:
        yield Finding(
            fname, subj, "error", "hallucinated-term",
            f"{obj} does not exist in the loaded {prefix} release",
        )
        return
    # Deprecation via the authoritative owl:deprecated flag, with the label convention as a
    # redundant backstop (some deprecated terms keep a non-"obsolete " label, e.g. HP:0007815).
    if obj in obsolete_ids or label.lower().startswith("obsolete"):
        yield Finding(
            fname, subj, "error", "obsolete-term",
            f"{obj} is obsolete/deprecated in {prefix} ({label!r})",
        )
        return
    if not declared:
        yield Finding(
            fname, subj, "warning", "missing-object-label",
            f"{obj} has no object_label; label drift cannot be checked ({prefix} label {label!r})",
        )
        return
    if declared.lower() == label.lower():
        return
    if declared.lower() in _exact_synonyms(adapter, obj):
        yield Finding(
            fname, subj, "warning", "noncanonical-label",
            f"{obj} object_label {declared!r} is an exact synonym, not the primary label {label!r}",
        )
    else:
        yield Finding(
            fname, subj, "error", "label-mismatch",
            f"{obj} object_label {declared!r} != HPO label {label!r} (and not an exact synonym)",
        )


def validate_paths(
    paths: Iterable[Path],
    data_root: Path | None = None,
    check_ontology: bool | None = None,
) -> ValidationResult:
    """Validate SSSOM files.

    ``check_ontology``: None = auto (use oaklib if present), True = require it, False = skip.
    Structural and subject checks always run; only the HPO layer depends on the backend.
    """
    result = ValidationResult()
    result.subjects_checked = data_root is not None
    # One adapter per (object ontology, declared release), loaded on first use: a repo that
    # maps only to HPO must not pay for MONDO, and two files pinned to the same release share
    # one load.
    backends: dict[tuple[str, str | None], tuple[object, set[str]] | None] = {}

    def backend(source: str, prefix: str, selector: str, declared: str):
        """(adapter, obsolete ids) for one object ontology at one release, or None.

        A declared release date selects the pinned obographs build for that date; no date
        selects oaklib's current build. The distinction is what keeps a correct file green
        when the ontology relabels a term later.
        """
        date = release_date(declared)
        key = (source, date)
        if key not in backends:
            adapter = None
            if check_ontology is not False:
                if date:
                    try:
                        adapter = PinnedRelease.load(source.split(":", 1)[1], date, cache_dir())
                    except ReleaseUnavailable as exc:
                        logger.warning("%s: pinned release unavailable (%s)", source, exc)
                else:
                    adapter = _get_ontology_adapter(selector)
            if adapter is None:
                backends[key] = None
                if check_ontology is True:
                    what = (
                        f"pinned release {source}@{date}" if date else f"oaklib backend {selector}"
                    )
                    result.findings.append(
                        Finding("<config>", "-", "error", "no-ontology-backend",
                                f"{what} required (--strict-ontology) but unavailable")
                    )
            else:
                backends[key] = (adapter, _obsolete_ids(adapter, prefix))
                version = getattr(adapter, "version", None) or _adapter_version(adapter)
                if version:
                    result.ontology_versions[source] = version
        return backends[key]

    seen: set[tuple[str, str, str, str]] = set()  # cross-file duplicate detection
    for path in paths:
        fname = path.name
        try:
            metadata, rows = parse_sssom(path)
        except MetadataError as exc:
            result.findings.append(
                Finding(fname, "-", "error", "bad-metadata", f"unparseable SSSOM header: {exc}")
            )
            continue
        file_prefixes = set((metadata.get("curie_map") or {}).keys())
        if not file_prefixes:
            result.findings.append(
                Finding(fname, "-", "error", "no-curie-map", "file has no curie_map metadata")
            )
        source = str(metadata.get("object_source") or "").strip()
        if source not in OBJECT_SOURCES:
            result.findings.append(
                Finding(fname, "-", "error", "unknown-object-source",
                        f"object_source {source!r} not in {sorted(OBJECT_SOURCES)}; the objects "
                        "in this file cannot be checked against any ontology")
            )
            continue  # without a known source there is no prefix to check rows against
        prefix, selector = OBJECT_SOURCES[source]
        declared = str(metadata.get("object_source_version") or "")
        loaded = backend(source, prefix, selector, declared)
        result.findings.extend(
            _check_version(metadata, fname, result.ontology_versions.get(source))
        )
        result.n_rows += len(rows)
        for row in rows:
            result.findings.extend(_check_row(row, fname, file_prefixes, seen, prefix))
            if data_root is not None:
                result.findings.extend(_check_subject_exists(row, fname, data_root, result))
            if loaded is not None:
                result.findings.extend(
                    _check_object_in_ontology(row, fname, loaded[0], loaded[1], prefix)
                )
    result.ontology_checked = any(b is not None for b in backends.values())
    return result


def _check_version(metadata: dict[str, Any], fname: str, loaded: str | None) -> Iterable[Finding]:
    """Warn if the loaded release differs from the file's declared ``object_source_version``.

    With the pinned-release backend the two agree by construction; this still fires for a
    file that declares a version whose release could not be fetched and was checked against
    a fallback, or whose declared version has no parseable date.
    """
    declared = str(metadata.get("object_source_version") or "").strip()
    if not declared or not loaded:
        return
    m = _RELEASE_DATE.search(declared)
    declared_date = m.group(1) if m else declared
    if declared_date != loaded:
        yield Finding(
            fname, "-", "warning", "hpo-version-mismatch",
            f"validated against {loaded} but file declares object_source_version "
            f"{declared!r}; label/obsolescence results reflect {loaded}",
        )


# ``parse_sssom``/``MetadataError``/``default_mapping_files`` now live in
# :mod:`b2ai_dataset_ingest.mapping.sssom_io` (shared with the apply path) and are re-exported
# here so ``from ...sssom_validate import parse_sssom`` (CLI, tests) keeps working.
__all__ = [
    "Finding",
    "MetadataError",
    "ValidationResult",
    "default_mapping_files",
    "parse_sssom",
    "validate_paths",
]

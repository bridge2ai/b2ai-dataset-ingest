"""Collapse redundant HPO annotations, keeping the most specific term.

Two questionnaire items can map to a term and to one of its ancestors, so a
participant who answers both is annotated with, say, ``HP:0002875 Exertional
dyspnea`` *and* ``HP:0002094 Dyspnea``. Neither assertion is wrong, but the
ancestor is implied by the descendant, and downstream tools that reason over the
HPO graph treat the pair as an inconsistency to resolve. Stratiphy asks about
every such pair, once per participant.

This collapses them at the source, keeping the descendant and **merging the
ancestor's evidence onto it** rather than discarding it. That matters here: each
derived feature records which questionnaire item fired the rule, so dropping the
ancestor outright would erase the fact that three separate dyspnea items were
answered. Stratiphy's own sanitizer cannot do this, because by the time it sees
the data there are only terms.

Only *present* features are collapsed. Excluded ones invert the rule, since
excluding a parent implies excluding its children, so the ancestor is the
informative assertion there. The 2026-08-24 clinical review withdrew the absent
pole set-wide so none are currently emitted, and rather than write a branch that
cannot be exercised, excluded features are passed through untouched.

The ontology is supplied, never downloaded. It has to be the same graph the
downstream clustering uses, or a term could be collapsed on the strength of a
subsumption the clustering does not believe in.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from b2ai_dataset_ingest.model.core import Participant, PhenotypicFeatureObservation

logger = logging.getLogger(__name__)


class OntologyUnavailable(RuntimeError):
    """hpo-toolkit is not installed, or the supplied ontology could not be read."""


class OntologyVersionMismatch(RuntimeError):
    """The supplied ontology is not the release the mappings were curated against."""


@dataclass
class CollapseReport:
    """What was collapsed, aggregated. Term pairs only — never participant ids."""

    hpo_version: str = ""
    participants_affected: int = 0
    collapses: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.collapses.values())

    def render(self) -> str:
        if not self.total:
            return f"HPO coherence (hp@{self.hpo_version}): nothing to collapse"
        lines = [
            f"HPO coherence (hp@{self.hpo_version}): collapsed {self.total} redundant "
            f"annotation(s) across {self.participants_affected} participant(s)",
        ]
        for (kept, dropped), n in self.collapses.most_common():
            lines.append(f"  {dropped} -> {kept}  ({n})")
        return "\n".join(lines)


_RELEASE_DATE = __import__("re").compile(r"(\d{4}-\d{2}-\d{2})")


def declared_hpo_version(repo_root: Path | None = None) -> str | None:
    """The HPO release the shipped mappings were curated against, or None.

    Read from ``object_source_version`` in the SSSOM files rather than hardcoded, so
    there is one source of truth and re-curating against a newer release moves this
    automatically. MONDO sources are skipped; only the HPO pin governs collapsing.
    """
    from b2ai_dataset_ingest.mapping.sssom_io import default_mapping_files, parse_sssom

    versions: set[str] = set()
    for path in default_mapping_files(repo_root):
        metadata, _ = parse_sssom(path)
        declared = str(metadata.get("object_source_version") or "")
        if not declared.startswith("hp/"):
            continue
        match = _RELEASE_DATE.search(declared)
        if match:
            versions.add(match.group(1))
    if len(versions) > 1:
        raise OntologyVersionMismatch(
            f"the mapping files disagree about the HPO release: {sorted(versions)}. "
            "Collapsing needs one pinned graph."
        )
    return versions.pop() if versions else None


def load_ontology(hpo_json: Path):
    """Load the supplied hp.json, or raise :class:`OntologyUnavailable`."""
    try:
        from hpotk.ontology.load.obographs import load_minimal_ontology
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise OntologyUnavailable(
            "hpo-toolkit is not installed. Install the 'hpo' extra: "
            "uv sync --extra hpo"
        ) from exc
    if not hpo_json.is_file():
        raise OntologyUnavailable(f"no such ontology file: {hpo_json}")
    try:
        return load_minimal_ontology(str(hpo_json))
    except Exception as exc:  # noqa: BLE001 - hpotk raises a variety of parse errors
        raise OntologyUnavailable(f"could not read {hpo_json}: {exc}") from exc


def require_version(ontology, expected: str | None) -> str:
    """Fail unless the ontology is the release the mappings declare.

    Collapsing is destructive and decided by the graph's subsumptions, so doing it
    against a different release than the curators approved would silently discard
    assertions on the strength of relationships they never saw. A warning is not
    enough for that; a mismatch is fatal.
    """
    actual = str(ontology.version or "").strip()
    if expected and actual != expected:
        raise OntologyVersionMismatch(
            f"the mappings declare HPO {expected} but the supplied ontology is {actual}. "
            "Collapsing terms against a different release could drop an assertion on the "
            "strength of a subsumption the mappings were never curated against. Supply "
            f"the {expected} hp.json, or re-curate the mappings against {actual}."
        )
    return actual


def _merge(keep: PhenotypicFeatureObservation, drop: PhenotypicFeatureObservation) -> None:
    """Fold the dropped feature's provenance into the one being kept."""
    for ev in drop.evidence:
        if ev not in keep.evidence:
            keep.evidence.append(ev)
    if drop.description and drop.description != keep.description:
        keep.description = f"{keep.description or ''}\nAlso implied by: {drop.description}".strip()


def collapse_participant(participant: Participant, ontology, report: CollapseReport) -> bool:
    """Collapse one participant's present features in place. True if anything changed."""
    from hpotk.model import TermId

    present = [f for f in participant.phenotypic_features if not f.excluded]
    if len(present) < 2:
        return False

    by_id = {f.type.id: f for f in present}
    # A term is redundant when another present term is its descendant. Resolve against
    # the descendant's ancestors rather than the ancestor's descendants: the ancestor
    # set is small, the descendant set can be thousands of terms.
    redundant: dict[str, str] = {}
    for feature in present:
        try:
            ancestors = ontology.graph.get_ancestors(TermId.from_curie(feature.type.id))
        except Exception as exc:  # noqa: BLE001 - unknown/obsolete terms raise
            logger.debug("no ancestry for %s: %s", feature.type.id, exc)
            continue
        for anc in ancestors:
            anc_curie = anc.value
            if anc_curie in by_id and anc_curie != feature.type.id:
                redundant[anc_curie] = feature.type.id

    if not redundant:
        return False
    for dropped, kept in redundant.items():
        _merge(by_id[kept], by_id[dropped])
        report.collapses[(kept, dropped)] += 1
    participant.phenotypic_features = [
        f for f in participant.phenotypic_features if f.type.id not in redundant
    ]
    return True


def collapse_all(participants: list[Participant], ontology) -> CollapseReport:
    """Collapse every participant, returning an aggregate report."""
    report = CollapseReport(hpo_version=str(ontology.version or "?"))
    for participant in participants:
        if collapse_participant(participant, ontology, report):
            report.participants_affected += 1
    return report

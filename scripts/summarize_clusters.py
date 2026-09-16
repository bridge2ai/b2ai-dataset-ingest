#!/usr/bin/env python3
"""Postprocess a clustering run: read `results.pb` and summarise it.

The last step of `scripts/cluster_phenopackets.sh`, which runs it for you. Run it
directly only to re-summarise an existing `results.pb` without re-clustering.

Stratiphy does the clustering; this only reads its output. Its CLI covers `setup`,
`preprocess` and `compute`, but the result is a protobuf and the only way to look
inside is the Python API, so this fills that one gap and adds nothing else.

Two things to know about the file it reads. The top-level message is
`StratiphyResult`, which *wraps* the `ClusteringWorkflowResult` alongside the
cohort and run metadata, so parsing it as the latter fails with a bare
`DecodeError`. And the headline is not the cluster assignment: it is
`split_check`, Stratiphy's own verdict on whether the cohort should be split at
all, computed from the gap statistic against randomised cohorts. A partition
exists for every k whether or not it means anything.

Output is aggregate. Cluster *sizes* and term frequencies are safe to share; the
per-participant assignment is not, and is written to its own file so the two are
never confused.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def load(results: Path):
    """Return (ClusteringWorkflowResult, StratiphyResult) from a `results.pb`."""
    from stratiphy.workflow import ClusteringWorkflowResult
    from stratiphy.workflow.workflow_pb2 import StratiphyResult

    wrapper = StratiphyResult()
    wrapper.ParseFromString(results.read_bytes())
    return ClusteringWorkflowResult.from_protobuf(wrapper.clustering_result), wrapper


def term_table(associations, k: int, top: int = 10) -> dict[str, Any]:
    """Which terms distinguish the clusters at this k, and whether that is real.

    Stratiphy tests each term for association with the partition by Monte Carlo and
    reports both a nominal and a multiple-testing-corrected p-value, alongside the
    observed effect and the minimum effect the cohort had power to detect. Counts
    alone would say which terms are common where; only the test says whether the
    difference is distinguishable from chance, which is the question being asked.

    Sorted by corrected p-value, so the terms that actually separate the clusters
    come first rather than merely the frequent ones.
    """
    rows: list[dict] = []
    untested = 0
    for entry in associations[k]:
        # Most listed terms are never tested: the power analysis drops any whose
        # minimum detectable effect exceeds --max-mde, because the cohort could not
        # have detected a difference in them. Their test messages are absent, and
        # protobuf yields 0.0 for a missing field, so reading pval blind would rank
        # every untested term as maximally significant. HasField is the distinction.
        if not entry.HasField("nominal_test"):
            untested += 1
            continue
        per_cluster = {}
        for block in entry.counts:
            # OBSERVATION_STATE_PRESENT
            present = next((c.count for c in block.counts if c.state == 1), 0)
            per_cluster[block.cluster_id] = present
        nominal = entry.nominal_test
        effect = float(nominal.effect)
        rows.append(
            {
                "hpo_id": entry.term_id,
                "present_by_cluster": per_cluster,
                "total_present": sum(per_cluster.values()),
                "p_nominal": round(float(nominal.pval), 6),
                "p_corrected": round(float(entry.corrected_test.pval), 6)
                if entry.HasField("corrected_test")
                else None,
                # nan where the contingency table is degenerate, e.g. a cluster with
                # no positives; kept as None rather than rendered as a number
                "effect": None if effect != effect else round(effect, 4),
                "min_detectable_effect": round(float(nominal.min_detectable_effect), 4),
            }
        )
    rows.sort(key=lambda r: (r["p_corrected"] if r["p_corrected"] is not None else 1.0,
                             -r["total_present"]))
    return {"tested": rows[:top], "n_tested": len(rows), "n_underpowered": untested}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path, required=True, help="path to results.pb")
    ap.add_argument("--outdir", type=Path, required=True)
    ap.add_argument("--top", type=int, default=10, help="terms to report per k")
    args = ap.parse_args()
    # Owner-only: stratiphy_assignments.tsv is per-participant, and derived from the
    # source data it carries that data's restrictions. The default umask would leave
    # it group- and world-readable. Set explicitly, since mkdir's mode is masked when
    # creating and ignored when the directory already exists.
    args.outdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.outdir.chmod(0o700)

    result, wrapper = load(args.results)
    sizes = {
        k: sorted(Counter(list(labels)).values(), reverse=True)
        for k, labels in sorted(result.cluster_labels.items())
    }
    # split_check is optional on the workflow result. Absent means the gap statistic
    # was never computed, not that the cohort should not be split, so say so rather
    # than reporting a verdict there is no evidence for.
    check = result.split_check
    should_split = None if check is None else bool(check.should_split)

    summary = {
        "stratiphy_version": wrapper.meta_data.stratiphy_version,
        "hpo_version": wrapper.meta_data.hpo_version,
        "n_samples": len(next(iter(result.cluster_labels.values()))),
        "should_split": should_split,
        "split_probability": None if check is None else round(float(check.split_proba), 4),
        "cluster_sizes_by_k": sizes,
        "alpha": round(float(wrapper.meta_data.association_metadata.alpha), 4),
        "beta": round(float(wrapper.meta_data.association_metadata.beta), 4),
        "terms_by_k": {k: term_table(result.term_associations, k, top=args.top)
                       for k in sizes},
    }
    (args.outdir / "stratiphy_summary.json").write_text(json.dumps(summary, indent=2))

    verdict = {True: "SPLIT", False: "DO NOT SPLIT", None: "NO VERDICT (not computed)"}[
        should_split
    ]
    print(f"stratiphy {summary['stratiphy_version']}, HPO {summary['hpo_version']}")
    print(f"{summary['n_samples']} samples")
    print(f"verdict: {verdict}  (split probability {summary['split_probability']})")
    for k, s in sizes.items():
        print(f"  k={k}: sizes {s}")
    if should_split is False:
        print("\nThe partitions above exist for every k regardless; the verdict is what")
        print("says whether any of them is worth interpreting.")

    # Which terms distinguish the clusters, and whether that survives correction for
    # having tested many of them. Printed for the smallest k, which is the partition
    # most likely to be real; the rest are in the JSON.
    alpha = summary["alpha"]
    smallest_k = min(sizes)
    table = summary["terms_by_k"][smallest_k]
    hits = [
        r for r in table["tested"]
        if r["p_corrected"] is not None and r["p_corrected"] <= alpha
    ]
    print(
        f"\nTerm-cluster association at k={smallest_k}: "
        f"{table['n_tested']} term(s) tested, {table['n_underpowered']} skipped as "
        f"underpowered"
    )
    if not hits:
        print(f"  none reach corrected p <= {alpha}")
    for row in hits:
        counts = " / ".join(f"{c}:{n}" for c, n in sorted(row["present_by_cluster"].items()))
        effect = "n/a" if row["effect"] is None else f"{row['effect']}"
        print(f"  {row['hpo_id']:<14} p={row['p_corrected']:<10.2e} "
              f"effect={effect:<7} counts {counts}")
    if hits and should_split is False:
        print("  These describe a partition the verdict says is not real; read them as")
        print("  what would separate the clusters if one were imposed, not as findings.")

    print(f"\nwrote {args.outdir}/stratiphy_summary.json")

    # Per-participant assignment, kept separate: it is participant-level data.
    # Labels are indexed by member position in the cohort, not by `sort_order`,
    # which is a display permutation rather than an identifier list.
    ids = [m.labels.label for m in wrapper.cohort.members]
    lines = ["sample_id\t" + "\t".join(f"k{k}" for k in sizes)]
    for i, sid in enumerate(ids):
        lines.append(sid + "\t" + "\t".join(str(result.cluster_labels[k][i]) for k in sizes))
    (args.outdir / "stratiphy_assignments.tsv").write_text("\n".join(lines) + "\n")
    print(f"wrote {args.outdir}/stratiphy_assignments.tsv  (PER-PARTICIPANT)")


if __name__ == "__main__":
    main()

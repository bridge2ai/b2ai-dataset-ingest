#!/usr/bin/env python3
"""Profile the HPO terms the emitter derives, per phenopacket and across a cohort.

Answers the QC question "is the ingest actually producing phenotype signal, and
how much per participant" without looking at a single cell value.

Present and excluded are counted separately throughout, and never summed into a
headline number. A `PhenotypicFeature` with `excluded: true` is an assertion that
the phenotype is ABSENT, which this pipeline derives when an item's zero denies
the phenotype. Folding those into a "terms per case" count would report a
participant with ten explicit absences as richly phenotyped.

Outputs, and what is safe to share:

  hpo_summary.json          aggregate  distribution stats, cohort totals
  hpo_term_frequency.tsv    aggregate  one row per HPO term, counts across cohort
  hpo_profile.png           aggregate  per-packet distribution, most frequent terms
  hpo_per_packet.tsv        PER-PARTICIPANT, keyed by phenopacket id

Run it against a real cohort and every output inherits that data's restrictions
under AI-READI §3.E. The per-participant file is the one that matters.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

# Validated categorical palette, slot 1. Single series, so no pair separation to check.
SERIES = "#2a78d6"
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_MUTED = "#52514e"


def source_label(input_dir: Path) -> str:
    """Name the run from its input path, so a figure never has to be trusted blind.

    Never asserts a provenance the path does not support. A figure that claims to
    be synthetic when it is not is how restricted output gets shared by accident.
    """
    parts = input_dir.resolve().parts
    tail = parts[-2:] if parts[-1] in ("phenopackets", "packets") else parts[-1:]
    return "/".join(tail)


def load(indir: Path) -> tuple[pd.DataFrame, Counter, Counter, dict[str, str]]:
    """Return (per-packet counts, present tally, excluded tally, term labels)."""
    rows: list[dict[str, object]] = []
    present_tally: Counter = Counter()
    excluded_tally: Counter = Counter()
    labels: dict[str, str] = {}

    paths = sorted(indir.glob("*.json"))
    if not paths:
        raise SystemExit(f"no phenopackets found in {indir}")

    for path in paths:
        packet = json.loads(path.read_text())
        present: set[str] = set()
        excluded: set[str] = set()
        for feature in packet.get("phenotypicFeatures", []):
            term = feature["type"]
            labels[term["id"]] = term.get("label", term["id"])
            # a term asserted both ways in one packet is a mapping bug, not a
            # phenotype; count it on both sides so the contradiction shows up
            (excluded if feature.get("excluded") else present).add(term["id"])
        present_tally.update(present)
        excluded_tally.update(excluded)
        rows.append({
            "phenopacket_id": packet["id"],
            "n_present": len(present),
            "n_excluded": len(excluded),
            "n_contradictory": len(present & excluded),
            "n_diseases": len(packet.get("diseases", [])),
            "n_measurements": len(packet.get("measurements", [])),
        })

    return pd.DataFrame(rows).set_index("phenopacket_id"), present_tally, excluded_tally, labels


def describe(counts) -> dict[str, float]:
    """Distribution summary. Untyped input: a DataFrame column is a Series at
    runtime but widens to Series | DataFrame for a checker, and asarray copes
    with either."""
    values = np.asarray(counts)
    return {
        "n": int(values.size),
        "mean": round(float(values.mean()), 2),
        "sd": round(float(values.std(ddof=1)) if values.size > 1 else 0.0, 2),
        "min": int(values.min()),
        "p25": float(np.percentile(values, 25)),
        "median": float(np.median(values)),
        "p75": float(np.percentile(values, 75)),
        "max": int(values.max()),
        "zero": int((values == 0).sum()),
        "pct_zero": round(float((values == 0).mean() * 100), 1),
    }


def plot(per_packet: pd.DataFrame, present_tally: Counter, labels: dict[str, str],
         outdir: Path, label: str, top: int) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax_hist, ax_freq) = plt.subplots(
        1, 2, figsize=(12.5, 5.4), facecolor=SURFACE,
        gridspec_kw={"width_ratios": [1, 1.25]})

    # Left: distribution of present terms per packet. Counts are small integers,
    # so one bar per value rather than a smoothed histogram over fake bins.
    counts = per_packet["n_present"]
    dist = counts.value_counts().sort_index()
    ax_hist.bar(dist.index, dist.to_numpy(), color=SERIES, width=0.8)
    ax_hist.set_title(f"HPO terms present per phenopacket\nn={len(counts)}, "
                      f"mean {counts.mean():.1f}, median {counts.median():.0f}",
                      fontsize=10, color=INK)
    ax_hist.set_xlabel("HPO terms asserted present", fontsize=8, color=INK_MUTED)
    ax_hist.set_ylabel("phenopackets", fontsize=8, color=INK_MUTED)
    if len(dist) <= 25:
        ax_hist.set_xticks(list(dist.index))

    # Right: most frequent terms, horizontal so the labels stay readable.
    common = present_tally.most_common(top)[::-1]
    if common:
        names = [str(labels.get(str(t), str(t)))[:44] for t, _ in common]
        vals = [c for _, c in common]
        ax_freq.barh(range(len(vals)), vals, color=SERIES, height=0.75)
        ax_freq.set_yticks(range(len(vals)))
        ax_freq.set_yticklabels(names, fontsize=7)
        ax_freq.set_title(f"{min(top, len(present_tally))} most frequent HPO terms",
                          fontsize=10, color=INK)
        ax_freq.set_xlabel("phenopackets asserting the term", fontsize=8, color=INK_MUTED)

    for ax in (ax_hist, ax_freq):
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#d5d4cf")
        ax.tick_params(labelsize=7, colors=INK_MUTED, length=3, width=0.7)
        ax.set_facecolor(SURFACE)

    fig.suptitle(f"HPO term profile — source: {label}", fontsize=13, color=INK)
    fig.tight_layout()
    path = outdir / "hpo_profile.png"
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    print(f"wrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, required=True,
                    help="directory of phenopacket .json files")
    ap.add_argument("--outdir", type=Path, required=True)
    ap.add_argument("--top", type=int, default=20,
                    help="how many terms to show in the frequency panel")
    ap.add_argument("--label", default=None,
                    help="provenance label for the figure; defaults to the input path")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()
    # Owner-only: hpo_per_packet.tsv is per-participant, and derived from the source
    # data it carries that data's restrictions. The default umask would leave it
    # group- and world-readable. Set explicitly, since mkdir's mode is masked when
    # creating and ignored when the directory already exists.
    args.outdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.outdir.chmod(0o700)

    per_packet, present_tally, excluded_tally, labels = load(args.input)
    label = args.label or source_label(args.input)

    summary = {
        "source": label,
        "n_phenopackets": int(len(per_packet)),
        "distinct_terms_present": len(present_tally),
        "distinct_terms_excluded": len(excluded_tally),
        "total_present_assertions": int(sum(present_tally.values())),
        "total_excluded_assertions": int(sum(excluded_tally.values())),
        "contradictory_packets": int((per_packet["n_contradictory"] > 0).sum()),
        "per_packet": {
            "present": describe(per_packet["n_present"]),
            "excluded": describe(per_packet["n_excluded"]),
        },
    }
    (args.outdir / "hpo_summary.json").write_text(json.dumps(summary, indent=2))

    freq = pd.DataFrame(
        [{"hpo_id": term,
          "label": labels.get(term, term),
          "n_present": present_tally.get(term, 0),
          "n_excluded": excluded_tally.get(term, 0),
          "pct_present": round(present_tally.get(term, 0) / len(per_packet) * 100, 1)}
         for term in sorted(set(present_tally) | set(excluded_tally))]
    ).sort_values("n_present", ascending=False)
    freq.to_csv(args.outdir / "hpo_term_frequency.tsv", sep="\t", index=False)
    per_packet.to_csv(args.outdir / "hpo_per_packet.tsv", sep="\t")

    p = summary["per_packet"]["present"]
    print(f"source: {label}")
    print(f"{summary['n_phenopackets']} phenopackets, "
          f"{summary['distinct_terms_present']} distinct HPO terms asserted present")
    print(f"present per packet: mean {p['mean']}, median {p['median']}, "
          f"range {p['min']} to {p['max']}, sd {p['sd']}")
    print(f"packets with no present term: {p['zero']} ({p['pct_zero']}%)")
    e = summary["per_packet"]["excluded"]
    print(f"excluded per packet: mean {e['mean']}, median {e['median']}, max {e['max']}")
    if summary["contradictory_packets"]:
        print(f"WARNING: {summary['contradictory_packets']} packet(s) assert a term "
              f"both present and excluded — check the cut-point rules")
    print(f"wrote {args.outdir}/hpo_summary.json, hpo_term_frequency.tsv, hpo_per_packet.tsv")

    if not args.no_plot:
        plot(per_packet, present_tally, labels, args.outdir, label, args.top)


if __name__ == "__main__":
    main()

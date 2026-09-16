#!/usr/bin/env bash
# Cluster a directory of phenopackets with Stratiphy, then summarise the result.
#
# Source: https://github.com/P2GX/stratiphy — phenotype-driven clustering of cohorts.
# Install it with `uv sync --extra clustering`.
#
# This exists for one reason: Stratiphy's --data option defaults to ./data, which in this
# repo is the protected input tree, so `setup download` would drop a 22 MB HPO build into
# it. Every call below passes -d explicitly. It is a sequence of four commands, not a
# wrapper: nothing here reimplements anything Stratiphy or the report script already do.
#
# Usage:
#   scripts/cluster_phenopackets.sh <phenopacket dir> <analysis dir>
#
# Anything after the two positional arguments is passed through to `stratiphy compute`,
# so a quick coarse pass is:
#   scripts/cluster_phenopackets.sh out/synthetic/voice_dgp/{phenopackets,analysis} \
#     --rand-iter 20 --mc-iter 10000
# Defaults (100 randomised cohorts, 1e6 Monte-Carlo iterations) are what a real run wants.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESOURCES="${ROOT}/.stratiphy"

PACKETS="${1:-}"
ANALYSIS="${2:-}"
if [ -z "${PACKETS}" ] || [ -z "${ANALYSIS}" ]; then
  echo "usage: $0 <phenopacket dir> <analysis dir> [--controversy LEVEL]" \
       "[stratiphy compute options...]" >&2
  exit 1
fi
shift 2

# --controversy belongs to `preprocess`, everything else to `compute`, so pull it
# out rather than forwarding the whole tail to one of them.
CONTROVERSY=()
REST=()
while [ $# -gt 0 ]; do
  case "$1" in
    --controversy) CONTROVERSY=(--controversy "${2:?--controversy needs a level}"); shift 2 ;;
    --controversy=*) CONTROVERSY=(--controversy "${1#*=}"); shift ;;
    *) REST+=("$1"); shift ;;
  esac
done
set -- "${REST[@]+"${REST[@]}"}"

[ -d "${PACKETS}" ] || { echo "no such phenopacket dir: ${PACKETS}" >&2; exit 1; }
mkdir -p "${ANALYSIS}"

# Idempotent: skips the download when the HPO build is already there.
uv run stratiphy setup download -d "${RESOURCES}"

# Prompts for every annotation issue at or above the controversy threshold, which
# defaults to `small`. On a cohort whose mappings assert both a term and its ancestor
# that is one prompt per participant. `--controversy high` lets Stratiphy apply its own
# default action instead, which for the ancestor case is to keep the more specific term
# and drop the ancestor; it still prints what it decided. The genuinely ambiguous cases
# are ranked HIGH and keep prompting even then, which is what you want.
uv run stratiphy preprocess "${ANALYSIS}" "${PACKETS}"/*.json -d "${RESOURCES}" \
  "${CONTROVERSY[@]+"${CONTROVERSY[@]}"}"

uv run stratiphy compute "${ANALYSIS}" -d "${RESOURCES}" "$@"

uv run python "${ROOT}/scripts/summarize_clusters.py" \
  --results "${ANALYSIS}/results.pb" --outdir "${ANALYSIS}"

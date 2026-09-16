#!/usr/bin/env bash
# Bridge2AI-Voice, end to end:
#
#   1. fetch the pinned HPO release the mappings declare
#   2. b2ai-ingest validate            preflight, reads no cell values
#   3. b2ai-ingest voice --normalize-hpo
#   4. scripts/profile_hpo_terms.py    terms per participant
#   5. scripts/cluster_phenopackets.sh which itself runs stratiphy's three
#      commands and then scripts/summarize_clusters.py
#
# Works on any Voice phenotype tree. Real versus synthetic is an input path, not a mode,
# so there is nothing here that knows which you gave it.
#
# The one thing this owns that the individual steps cannot: it pins the ontology. HPO
# term collapsing and the clustering must reason over the same graph, and the release the
# mappings were curated against is the one both should use. Stratiphy's `setup download`
# fetches the *current* release and skips an existing file, so this fetches the pinned one
# into .stratiphy/hp.json first and both steps then agree by construction.
#
# Usage:
#   scripts/voice_pipeline.sh <phenotype dir> <run dir> [stratiphy compute options...]
#
# Example, with a fast coarse clustering pass:
#   scripts/voice_pipeline.sh \
#     data/synthetic/voice_dgp/b2ai-voice-synthetic-phenotype/output/phenotype \
#     out/synthetic/voice_dgp --rand-iter 20 --mc-iter 10000
#
# Fails at the first step that fails. Nothing is written before `validate` passes.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESOURCES="${ROOT}/.stratiphy"
HPO_JSON="${RESOURCES}/hp.json"

PHENOTYPE="${1:-}"
RUN_DIR="${2:-}"
if [ -z "${PHENOTYPE}" ] || [ -z "${RUN_DIR}" ]; then
  echo "usage: $0 <phenotype dir> <run dir> [stratiphy compute options...]" >&2
  exit 1
fi
shift 2

[ -d "${PHENOTYPE}" ] || { echo "no such phenotype dir: ${PHENOTYPE}" >&2; exit 1; }

PACKETS="${RUN_DIR}/phenopackets"
ANALYSIS="${RUN_DIR}/analysis"

# The release the mappings were curated against, read from the SSSOM rather than
# hardcoded, so re-curating moves it and this follows.
PINNED="$(sed -n 's|^# object_source_version: hp/releases/\([0-9-]*\)$|\1|p' \
  "${ROOT}"/mappings/*.sssom.tsv | sort -u)"
[ "$(printf '%s\n' "${PINNED}" | wc -l | tr -d ' ')" = "1" ] || {
  echo "the mapping files disagree about the HPO release: ${PINNED}" >&2; exit 1; }

# ---------------------------------------------------------------- 0. ontology ----
if [ -f "${HPO_JSON}" ] \
   && grep -q "releases/${PINNED}/" "${HPO_JSON}" 2>/dev/null; then
  echo "==> HPO ${PINNED} already at ${HPO_JSON}"
else
  echo "==> fetching pinned HPO ${PINNED} (the release the mappings declare)"
  mkdir -p "${RESOURCES}"
  curl -sSLf -o "${HPO_JSON}.tmp" \
    "http://purl.obolibrary.org/obo/hp/releases/${PINNED}/hp.json"
  mv "${HPO_JSON}.tmp" "${HPO_JSON}"
fi

# ------------------------------------------------------------- 1. preflight ----
# Reads headers, dictionary keys and cell counts, never a cell value, so it is safe
# on the source data. Exits non-zero if the configs no longer match the layout.
echo "==> validating the layout"
uv run b2ai-ingest validate --input "${PHENOTYPE}"

# ---------------------------------------------------- 2. ingest + normalise ----
# --normalize-hpo refuses outright if hp.json is not the pinned release, so the fetch
# above is what makes this step and the clustering below agree.
echo "==> ingesting -> ${PACKETS}"
uv run b2ai-ingest voice \
  --input "${PHENOTYPE}" --output "${PACKETS}" \
  --normalize-hpo --hpo-json "${HPO_JSON}"

# ---------------------------------------------------------------- 3. profile ----
# Terms per participant: the measure of whether the clustering below has anything to
# compute similarity over. Read this before trusting the verdict.
echo "==> profiling HPO terms -> ${ANALYSIS}"
uv run python "${ROOT}/scripts/profile_hpo_terms.py" \
  --input "${PACKETS}" --outdir "${ANALYSIS}"

# ------------------------------------------ 4. cluster, 5. summarise ----
# Both steps, via one delegate. cluster_phenopackets.sh runs, in order:
#
#     stratiphy setup download      -> .stratiphy/hp.json (already fetched above)
#     stratiphy preprocess          -> <analysis>/cohort.pb
#     stratiphy compute             -> <analysis>/results.pb
#     scripts/summarize_clusters.py -> stratiphy_summary.json, _assignments.tsv
#
# Kept as a delegate rather than inlined because it stands alone against any
# phenopacket directory, not just one this pipeline produced.
#
# Deliberately not passing --controversy: with the ancestor pairs already collapsed
# upstream, a prompt here means something else is wrong and is worth seeing.
echo "==> clustering and summarising -> ${ANALYSIS}"
"${ROOT}/scripts/cluster_phenopackets.sh" "${PACKETS}" "${ANALYSIS}" "$@"

echo
echo "Done. Aggregate results are in ${ANALYSIS}:"
echo "  hpo_summary.json         terms per participant"
echo "  stratiphy_summary.json   split verdict, cluster sizes, term associations"
echo "  hpo_profile.png          distribution and most frequent terms"
echo "Per-participant, and carrying the input's restrictions:"
echo "  hpo_per_packet.tsv  stratiphy_assignments.tsv"

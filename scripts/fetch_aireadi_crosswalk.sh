#!/usr/bin/env bash
# Fetch AI-READI's published item -> concept crosswalk into ./data/synthetic/ (gitignored).
#
# Source: the AI-READI documentation repository, which is CC-BY-4.0.
#   repo:    https://github.com/ai-readi/ai-readi-docs
#   rendered: https://docs.aireadi.org
#
# WHY THIS IS DOCUMENTATION AND NOT DATA
# --------------------------------------
# The AI-READI Data License covers participant Data, including data that has been
# "excerpted or otherwise altered". This file is neither: it is the project's own published
# data dictionary -- REDCap variable names, question text, OMOP concept ids, laterality and
# form names. No participant appears in it. The AI-READI documentation is released under
# CC-BY-4.0, which is what makes it usable here.
#
# It is fetched rather than vendored so that (a) the provenance stays a URL rather than a
# copy, and (b) `data/` remains the single gitignored home for inputs.
#
# It lands in its OWN directory, `aireadi-docs/`, deliberately NOT inside the licensed
# `aireadi/` tree. That tree is protected -- the WashU AI-READI Synthetic Data License
# Agreement §4.A limits sharing and §1.B/C extend every restriction to data generated from
# it -- and this crosswalk is CC-BY-4.0 documentation under none of that. Putting public
# documentation inside a protected tree would mean carving an exemption out of it, which is
# exactly what `.claude/hooks/guard_data_dir.py` says has caused every access-control hole
# this repo has had. The unprotected thing gets its own name instead.
#
# WHAT IT IS USED FOR
# -------------------
# `config/aireadi/` uses it for two things the VUMC synthetic release cannot supply:
#   1. UNTRUNCATED item labels. In the data, <domain>_source_value is hard-truncated at 49
#      characters ("mhoccur_ua, Urinary problems (Examples: urinary t"), so it is never a
#      usable label source.
#   2. Items the synthetic release does not ship at all -- the 26 per-eye ophthalmic items
#      and their laterality, and the second blood-pressure and pulse readings.
#
# IT IS A CANDIDATE GENERATOR, NEVER AN ORACLE
# --------------------------------------------
# It carries codes that do not resolve. It gives bp1_sysbp_vsorres `LOINC:2403450`, for
# which the NLM Clinical Table service returns no hits; the real code is 8480-6. So NO code
# is taken from it directly. Every ontology term this repo emits is independently verified
# -- MONDO/HPO/UBERON/NCIT against a pinned release with oaklib (and re-checked in CI by
# `b2ai-ingest validate-mappings --strict-ontology`), LOINC against the NLM service. Use
# this file to find *candidates* and to name items; verify before anything ships.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${ROOT}/data/synthetic/aireadi-docs"
BASE="https://raw.githubusercontent.com/ai-readi/ai-readi-docs/main/docs/static/json"

command -v curl >/dev/null || { echo "error: curl not found" >&2; exit 1; }

mkdir -p "${DEST}"
echo "Fetching the AI-READI crosswalk (CC-BY-4.0) -> ${DEST}"
for f in mappings.json moCA.json clinicalLabData.json; do
  if curl -fsSL --max-time 120 -o "${DEST}/${f}" "${BASE}/${f}"; then
    printf '  %-22s %s bytes\n' "${f}" "$(wc -c < "${DEST}/${f}" | tr -d ' ')"
  else
    echo "  ${f}: not available at ${BASE}/${f} (skipped)" >&2
  fi
done

echo
echo "NOTE: ${DEST} is gitignored (all of data/ is) and must stay that way."
echo "Attribution: AI-READI Consortium, ai-readi-docs, CC-BY-4.0."

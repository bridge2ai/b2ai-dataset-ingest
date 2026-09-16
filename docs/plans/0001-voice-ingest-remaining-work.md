# Plan: remaining work on the Bridge2AI-Voice ingest

- **Date:** 2026-09-10
- **Related:** [SDD: voice-ingest](../design/voice-ingest.md), [ADR-0001](../adr/0001-name-architecture-tooling.md)

## Context

There are four ingests to build across the programme: Voice and AI-READI, each against
synthetic and real input. Work so far has drifted between them without saying which cell
it served, which produced a shell script named for one cell and a clustering script that
duplicated an existing tool.

The correcting observation is that **real versus synthetic is not a code axis**. It is an
input path. The same reader, config and emitter serve both; only `--input` differs. The
only real axis is the data generation project, because AI-READI is OMOP CDM and needs its
own reader.

|            | voice_dgp             | aireadi          |
| ---------- | --------------------- | ---------------- |
| synthetic  | `voice` reader, works | no reader        |
| real       | `voice` reader, works | no reader        |

So four ingests, two readers. **This plan covers the Voice column only.** AI-READI is
deliberately out of scope; see below.

The layout merged in #24 already expresses the matrix, with `data/{real,synthetic}/<dgp>/`
mirroring `out/{real,synthetic}/<dgp>/{phenopackets,analysis}`, so no new structure is
needed to hold the four outputs.

The ingest itself works. The intended outcome is that both Voice cells run from the same
two documented commands,
with no bespoke wrapper, and that the phenotype yield of each run is measurable.

## Steps

1. **Delete `scripts/ingest_real.sh`.** It is 250 lines of untested shell wrapping a
   tested Python CLI, and its name encodes the axis that does not matter. Everything in it
   except the privilege dance is ordinary logic that belongs where pytest and ruff can see
   it. The privilege dance is already a documented one-liner in the README.

2. **Refuse a non-empty output directory in `b2ai-ingest voice`, with `--force` to
   proceed.** The ingest writes one file per participant, keyed by participant id.
   Re-running into a populated directory overwrites everyone still in the cohort but
   leaves a stale file behind for anyone who has since dropped out, so the output silently
   becomes a mix of two runs. Default is to fail; `--force` removes the existing
   phenopackets first and writes a clean set.

   Named `--force` rather than `--overwrite` because overwriting is what already happens
   by default, and is the bug: the flag's actual job is to delete the leftovers from
   participants who are *not* being overwritten. The error message must say what `--force`
   deletes, since the flag name cannot carry that on its own.

3. **Document the four commands** in the README as two subcommands against four input
   paths, so the matrix is visible rather than implied.

4. **Land the HPO term profiler** (#26). It reports terms derived per participant, which
   is the measure of whether a run produced anything worth analysing, and it is
   DGP-agnostic so it serves all four cells unchanged.

5. **Wire up [stratiphy](https://github.com/P2GX/stratiphy)** for clustering. Done in the
   PR stacked on this one. It consumes phenopackets directly, pins the same
   `phenopackets ~= 2.0.2` the emitter produces, and clusters on HPO semantic similarity
   rather than flat vectors. Only the result reader is ours: Stratiphy's own CLI covers
   `setup`, `preprocess` and `compute`, but its output is a protobuf with no CLI to read
   it.

6. **Collapse an HPO term asserted alongside its own ancestor**, behind
   `--normalize-hpo`. Two items can map to a term and its ancestor, so anyone answering
   both carries a redundant pair that downstream graph-aware tools treat as an
   inconsistency. Keeps the specific term and merges the ancestor's evidence into it,
   so the record of which item fired survives. Off by default: the raw output is the
   faithful record of what the instruments said.

   Uses the same ontology library and the same `hp.json` as the clustering, and refuses
   to run unless that file is the release the mappings declare. Collapsing is destructive
   and decided by subsumptions, so a version mismatch is fatal rather than a warning.

## Critical files

- `scripts/ingest_real.sh` — deleted.
- `src/b2ai_dataset_ingest/cli.py` — `voice` refuses a non-empty output directory, with
  `--force` to remove the existing phenopackets and write a clean set.
- `tests/test_end_to_end.py` — cover the refusal, and that `--force` leaves behind exactly
  the current cohort with no stale files from a previous run.
- `README.md` — the four commands, replacing the wrapper's documentation.
- `scripts/profile_hpo_terms.py` — unchanged, lands via #26.

## Verification

Run both Voice cells and confirm they differ only by `--input`:

```bash
uv run b2ai-ingest validate --input <phenotype dir>
uv run b2ai-ingest voice --input <phenotype dir> --output out/<provenance>/voice_dgp/phenopackets
uv run python scripts/profile_hpo_terms.py \
  --input out/<provenance>/voice_dgp/phenopackets --outdir out/<provenance>/voice_dgp/analysis
```

For the real cell under the ownership split, the same two commands go through the data
account with `.venv/bin/` called directly, as the README already documents.

The profiler's terms-per-participant figure is the acceptance signal. On the synthetic
cohort it is 0.6 with 98 of 173 participants carrying no term at all, so a real run should
be markedly higher; if it is not, the mapping coverage is the problem, not the clustering.

`uv run pytest`, `uv run ruff check`, and `uv run b2ai-ingest validate-mappings
--strict-ontology` must all pass.

## Out of scope

- **AI-READI, entirely.** It needs an OMOP CDM reader, a `config/aireadi/`, and its own
  plan. Nothing here should be generalised in anticipation of it.
- **Ingesting further Voice tables.** `enrollment/`, `confounders/`, `task/` and the
  pediatric tables remain unmapped, per the SDD.
- **Audio and derived acoustic features**, which stay referenced rather than ingested.
- **Reinstating absent phenotype assertions.** The 2026-08-24 clinical review withdrew the
  absent pole set-wide and the validator rejects a `predicate_modifier` column; the
  profiler reports the excluded count so a regression is visible.

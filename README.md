# b2ai-dataset-ingest

Ingest [Bridge2AI](https://bridge2ai.org/) datasets by converting their **phenotype
tables** into the [GA4GH Phenopacket](https://www.ga4gh.org/product/phenopackets/) schema.

> **"Ingest" here means: parse source data tables → emit one phenopacket per participant.**
> It does **not** build a knowledge graph (despite the Monarch/KGX sense of "ingest").

Phenopackets is the committed NIH deliverable and the first output target, but the
pipeline is deliberately **target-neutral**: mappings produce a canonical intermediate
representation (IR), and pluggable *emitters* render that IR to a target format.

```
raw tables  ->  source reader  ->  YAML mapping engine  ->  canonical IR  ->  emitter(s)
```

## Datasets

| Dataset | Status | Data |
| --- | --- | --- |
| [Bridge2AI-Voice](https://bridge2ai.org/data-voice/) | pilot / in progress | real data is PII/credentialed; we develop against public **synthetic** data ([justaddcoffee/b2ai-voice-synthetic-phenotype](https://github.com/justaddcoffee/b2ai-voice-synthetic-phenotype)) |
| [Bridge2AI AI-READi](https://bridge2ai.org/data-ai-readi/) | planned | no synthetic data yet |

## Scope (current)

Phenotype tables only:

| Source table | → | IR / Phenopacket element |
| --- | --- | --- |
| `demographics/` | → | `Individual` |
| `diagnosis/` (per-condition files) | → | `Disease` (file basename → MONDO) |
| `questionnaire/` (PHQ-9, GAD-7, VHI-10) | → | `Measurement` (per-item ordinals + precomputed totals) |
| audio / derived acoustic features | → | referenced, **not** ingested |

v1 emits `Measurement`s only; HPO `PhenotypicFeature` derivation (which needs an
ordinal→present/absent threshold policy) is a planned follow-up.

One phenopacket per participant, with **time-stamped observations** — time-course is
native to phenopackets via `TimeElement` (`PhenotypicFeature.onset`,
`Measurement.time_observed`). Each session is its own time-stamped entry. (The synthetic
data has only `ses-baseline`; multi-session handling is exercised by a dedicated test
fixture.)

## Layout

```
src/b2ai_dataset_ingest/
  model/        canonical, target-neutral intermediate representation (IR)
  sources/      dataset readers (raw tables -> IR), e.g. sources/voice/
  mapping/      YAML mapping engine (column -> concept, condition -> MONDO, item -> HPO/LOINC)
  emitters/     output writers; emitters/phenopacket.py is the first target
  ontology/     MONDO/HPO/LOINC term helpers
config/         per-dataset YAML mappings (config/voice/) + shared value sets
mappings/       SSSOM term mappings: B2AI dataset terms (b2ai:) -> HPO, with a validator
docs/           design docs (SDDs), ADRs, plans, mapping conventions
examples/       committed sample output: phenopackets built from the synthetic data
tests/          fixtures + tests, incl. tests/data/multisession/ for time-course
data/           (gitignored) all raw input, split real vs synthetic — see below
out/            (gitignored) all output, split the same way
```

### Real vs synthetic

Both trees split at the top on provenance, then by data generation project, and `out/`
mirrors `data/` exactly:

```
data/real/<dgp>/...            out/real/<dgp>/{phenopackets,analysis}
data/synthetic/<dgp>/...       out/synthetic/<dgp>/{phenopackets,analysis}
```

| | contents | used by the pipeline? |
| --- | --- | --- |
| `data/synthetic/` | synthetic Voice phenotype tables; synthetic AI-READI OMOP tables | yes, by default |
| `data/real/` | the source datasets (B2AI-Voice, AI-READI `clinical_data`) | only in explicit runs |

Everything routine — tests, fetch scripts, CLI examples — reads from `data/synthetic/`.
The source datasets are used only when someone deliberately runs the pipeline against
them. That is what the data use agreements ask for: the AI-READI Data License (WashU
v2.0) §3.C limits onward sharing and §3.E extends the agreement to derived outputs, and
the B2AI-Voice PhysioNet DUA is comparable.

The split is at the top of each tree on purpose. It means "real" is a path prefix you can
name directly, so tooling that protects it never needs an exemption carved out of a
protected tree. Every access-control bug this repo has had came from such an exemption.

**"Synthetic" does not mean "freely shareable".** The two synthetic datasets are in very
different legal positions, and the directory name hides that:

| | license | redistributable? |
| --- | --- | --- |
| `synthetic/voice_dgp/` | MIT, over an Apache-2.0 upstream (`sensein/b2aiprep`) | yes, with attribution |
| `synthetic/aireadi/` | WashU AI-READI **Synthetic** Data License Agreement v1.0 | **no** |

§4.D of that agreement forbids republishing the AI-READI synthetic data "as a standalone
downloadable dataset (e.g., via a public repository, zip archive, or code package)" without
written consent, and §4.A limits sharing to other licensees, academic collaborators, and
commercial collaborators who have agreed in writing. Critically, §1.B/C define "Generated
Data" as anything generated from it and extend **every** restriction to that — so the
phenopackets in `out/synthetic/aireadi/` are covered too, not just the input. §8 means a
breach obliges deleting the derived output as well.

That is why `scripts/fetch_aireadi_synthetic.sh` fetches rather than vendoring, and why
each user requests their own key.

**Separating ownership (recommended).** Tooling that runs under your account is
indistinguishable from you at the OS level, so the simplest way to keep routine work off
the source data is to give it a different owner. Only the `real/` halves change hands;
synthetic stays yours, so day-to-day work is unaffected:

```bash
sudo sysadminctl -addUser b2aidata -fullName "B2AI Source Data" -home /var/empty -shell /usr/bin/false
sudo dscl . -create /Users/b2aidata IsHidden 1
sudo chown -R b2aidata:staff data/real out/real   # out/real too — it derives from data/real
sudo chmod 700 data/real out/real                 # 700, not 750 — your account is in staff
```

This is the only airtight control. Anything enforced in software above the filesystem is
advisory and can be wrong about a path; the kernel cannot.

Runs against the source data then go through that account, calling the venv binary
directly (`uv run` will try to write caches into an unwritable home):

```bash
sudo -u b2aidata .venv/bin/b2ai-ingest voice \
  --input data/real/voice_dgp/<...>/phenotype --output out/real/voice_dgp/phenopackets
```

Undo with `sudo chown -R "$USER":staff data/real out/real && sudo chmod 755 data/real out/real`.

### Term mappings to HPO (SSSOM)

`mappings/` holds [SSSOM](https://mapping-commons.github.io/sssom/) files mapping Bridge2AI-Voice
dataset terms (a project-local `b2ai:` namespace) to the Human Phenotype Ontology — a standalone,
shareable artifact, separate from the ETL configs and not yet consumed by the emitter. Every HPO
code is machine-verified against a pinned HPO release (via oaklib) so nothing is hallucinated;
`b2ai-ingest validate-mappings` (and CI) enforce it. See
[docs/mapping-conventions.md](docs/mapping-conventions.md#term-mappings-to-hpo-sssom).

```bash
uv sync --extra validation            # install oaklib (the offline HPO backend)
uv run b2ai-ingest validate-mappings  # verify no HPO term is hallucinated / obsolete / mislabeled
```

### Example output (committed)

`examples/phenopackets/voice-synthetic/` holds 173 phenopackets built from the public
synthetic Voice data, so downstream tooling can be tested without credentialed access to
the real dataset. Everything there is synthetic; see its
[README](examples/phenopackets/voice-synthetic/README.md) for provenance, counts, and the
known gaps in the snapshot.

### Exploratory analysis

`scripts/profile_hpo_terms.py` reports how many HPO terms the emitter actually derived,
per phenopacket and across a cohort. It lives in `scripts/` rather than in the package so
it does not ship in the wheel, and it is wired into neither the CLI nor CI. It needs the
`analysis` extra.

```bash
uv sync --extra analysis
uv run python scripts/profile_hpo_terms.py \
  --input out/synthetic/voice_dgp/phenopackets --outdir out/synthetic/voice_dgp/analysis
```

It counts asserted-present and explicitly-excluded separately and never sums them. Since
the 2026-08-24 clinical review withdrew the absent pole set-wide, the mappings assert only
presence and the excluded count should be zero; it is reported anyway so a regression that
reintroduces absent assertions shows up as a number rather than silently changing what the
cohort means. The same reason the SSSOM validator rejects a `predicate_modifier` column.

On the synthetic cohort the answer is that there is very little phenotype signal: a mean
of 0.6 terms present per participant, with 98 of 173 carrying none at all. That is a
property of the synthetic tables, which are 2.4% dense.

That number is the gate on the clustering below. Phenotype-driven clustering needs terms
to compute similarity over, so run the profiler first and read the terms-per-participant
figure before investing in a clustering run.

### Clustering

[Stratiphy](https://github.com/P2GX/stratiphy) does the clustering. It reads phenopackets
directly, groups participants by HPO semantic similarity rather than by flat vectors, and
decides *whether the cohort should be split at all* using the gap statistic against
randomised cohorts. Install it with the `clustering` extra.

```bash
uv sync --extra clustering
scripts/cluster_phenopackets.sh \
  out/synthetic/voice_dgp/phenopackets out/synthetic/voice_dgp/analysis
```

That runs Stratiphy's `setup`, `preprocess` and `compute`, then the report. Anything after
the two directories is passed through to `compute`, so `--rand-iter 20 --mc-iter 10000`
gives a fast coarse pass; the defaults of 100 randomised cohorts and a million Monte-Carlo
iterations are what a real run wants.

The script exists for one reason. Stratiphy's `--data` defaults to the repo's own input
tree, which is protected here, so without an explicit `-d` its `setup download` drops a
22 MB HPO build into it. Every call passes `-d .stratiphy`, which is gitignored.

Only the last step is ours. Stratiphy's CLI covers the clustering; its result is a
protobuf with no CLI to read it, so `scripts/summarize_clusters.py` fills that one gap.
It runs as part of the script above; call it directly only to re-summarise an existing
`results.pb` without re-clustering.

The headline it prints is the verdict, not the partition. A partition exists at every k
whether or not it means anything. On the synthetic cohort the verdict is **do not split**,
at a split probability of 0.11, and the sizes show why: k=2 gives 171 and 2. That is what
0.6 terms per participant buys, and it is the expected answer rather than a failure.

### The whole thing, in one command

`scripts/voice_pipeline.sh` runs validate, ingest with HPO normalisation, profile,
cluster and summarise, stopping at the first step that fails.

```bash
uv sync --extra validation --extra analysis --extra clustering --extra hpo
scripts/voice_pipeline.sh <the phenotype dir> out/<provenance>/voice_dgp
```

Anything after the two directories goes to `stratiphy compute`, so
`--rand-iter 20 --mc-iter 10000` gives a fast coarse pass.

It owns one thing the individual steps cannot: **pinning the ontology**. Term collapsing
and clustering have to reason over the same graph, and the release the mappings were
curated against is the one both should use. `stratiphy setup download` fetches the
*current* release, so this fetches the pinned one into `.stratiphy/hp.json` first and both
steps agree by construction. The version comes from the SSSOM files, so re-curating moves
it. Without this the clustering runs on whatever HPO happened to be current, which is how
`2026-09-01` ended up clustering mappings curated against `2026-02-16`.

It deliberately does not pass `--controversy`. With the ancestor pairs already collapsed
upstream, a sanitation prompt means something else is wrong and is worth seeing.

The steps below are the same thing spelled out, for when you want to run one of them
on its own.

### The four ingests

Four ingests, but only two readers. Real versus synthetic is not a code axis, it is an
input path: the same reader, config and emitter serve both, and only `--input` differs.
The only real axis is the data generation project.

| | voice_dgp | aireadi |
| --- | --- | --- |
| synthetic | `b2ai-ingest voice` | no reader yet |
| real | `b2ai-ingest voice` | no reader yet |

So each Voice cell is the same pair of commands against a different input path:

```bash
uv run b2ai-ingest validate --input <the phenotype dir>
uv run b2ai-ingest voice    --input <the phenotype dir> \
                            --output out/<provenance>/voice_dgp/phenopackets
```

Run `validate` first. It reads headers, dictionary keys and cell counts but never a cell
value, so it is safe on the source data and tells you whether the configs still match the
layout before anything is written.

`voice` refuses to write into a directory that already holds phenopackets. The emitter
writes one file per participant, so a plain re-run overwrites everyone still in the cohort
but leaves a stale file behind for anyone who has since dropped out, and the directory
becomes a silent union of two runs. Pass `--force` to delete the existing set first.

#### Restricting to a questionnaire battery

Participants were given different questionnaires, and **every HPO term comes from a
questionnaire**. On the real Voice cohort 413 people received four, 242 received six and
51 received eleven, with mean term counts of 3.57, 5.62 and 9.84. Term count tracks
coverage almost linearly, so clustering finds that gradient before it finds anything
clinical, and it is administrative rather than phenotypic.

`scripts/cohort_overlap.py` reports who was offered what and what a common battery would
cost. `--questionnaires` then applies only the named tables, so every participant draws
from the same phenotype vocabulary:

```bash
uv run b2ai-ingest voice --input <the phenotype dir> \
  --output out/<provenance>/voice_dgp/phenopackets_gad7_anxiety_phq9_vhi10_voice_perception \
  --questionnaires phq9,gad7_anxiety,vhi10,voice_perception \
  --require-all-questionnaires
```

`--require-all-questionnaires` additionally emits only participants offered every one of
them, where **offered means a row exists**, filled in or not. Without it, a participant
missing one of the named questionnaires still has a gap inside the battery, which is the
same confound at smaller scale. The ingest summary reports how many are in that position
either way.

**Name the output directory after the battery, with the questionnaires sorted.** There
will be more than one, and a directory called `phenopackets-battery` tells you nothing six
months later. Sorting matters because `phq9,vhi10` and `vhi10,phq9` otherwise produce
differently-named directories holding identical output.

Note that a directory name is a weak record: rename it and the provenance is gone. A
manifest would be more robust but cannot live in the phenopackets directory, since the
profiler, stratiphy's `preprocess` and `summarize_clusters.py` all glob `*.json` there and
would parse it as a phenopacket.

#### Collapsing redundant HPO terms

Two questionnaire items can map to a term and to one of its ancestors. Four
`dyspnea_index` items map to `HP:0002094 Dyspnea` and one to its child `HP:0002875
Exertional dyspnea`, so anyone who answers both is annotated with both. Neither
assertion is wrong, but the ancestor is implied, and tools that reason over the HPO
graph treat the pair as an inconsistency to resolve. Stratiphy asks about every one,
once per participant.

`--normalize-hpo` collapses them at the source, keeping the more specific term and
**merging the ancestor's evidence into it** rather than discarding it, so the record that
three separate dyspnea items were answered survives. It is off by default: the raw
output is the faithful record of what the instruments said.

```bash
uv sync --extra hpo
uv run b2ai-ingest voice --input <the phenotype dir> \
  --output out/<provenance>/voice_dgp/phenopackets \
  --normalize-hpo --hpo-json .stratiphy/hp.json
```

**The ontology is supplied, never downloaded, and must be the release the mappings
declare.** A mismatch is fatal rather than a warning, because collapsing is destructive
and decided by the graph's subsumptions: normalising against a different release would
drop assertions on the strength of relationships the curators never approved. The version
comes from `object_source_version` in the SSSOM files, so re-curating moves it
automatically.

Pass the same `hp.json` the clustering reads, for the same reason. `stratiphy setup
download` fetches the *current* release and skips the download when a file is already
there, so putting the pinned release at `.stratiphy/hp.json` makes both steps agree.
Today they do not: the mappings pin `2026-02-16` and a fresh `setup download` fetches
`2026-09-01`.

For the real cells under the ownership split, the same two commands go through the data
account and call the venv binary directly, since `uv run` needs a writable home. See
**Separating ownership** above.

See [docs/plans/0001-voice-ingest-remaining-work.md](docs/plans/0001-voice-ingest-remaining-work.md) for
what remains, and why AI-READI is deliberately not being generalised for yet.

## Getting started

```bash
uv sync                       # create the env and install deps
uv run b2ai-ingest --help     # CLI help
scripts/fetch_synthetic_data.sh   # pull the public synthetic voice data into data/synthetic/
```

## Development

```bash
uv run pytest        # tests
uv run ruff check    # lint
```

## License

MIT — see [LICENSE](LICENSE).

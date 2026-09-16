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
| [Bridge2AI AI-READi](https://bridge2ai.org/data-ai-readi/) | v1 implemented | clinical data is **OMOP CDM v5.4**; developed against the public [VUMC synthetic release](https://hiplab.vumc.org/synthetix/ai-readi/) plus AI-READI's published CC-BY-4.0 crosswalk, with a committed hand-authored fixture. Nothing AI-READI-derived is committed — see below. |

## Scope (current)

Phenotype tables only:

| Source table | → | IR / Phenopacket element |
| --- | --- | --- |
| `demographics/` | → | `Individual` |
| `diagnosis/` (per-condition files) | → | `Disease` (file basename → MONDO) |
| `questionnaire/` (PHQ-9, GAD-7, VHI-10) | → | `Measurement` (per-item ordinals + precomputed totals) |
| audio / derived acoustic features | → | referenced, **not** ingested |

AI-READI (`clinical_data/`, OMOP CDM v5.4 — long/EAV, keyed by `*_concept_id`):

| Source table | → | IR / Phenopacket element |
| --- | --- | --- |
| `participants.tsv` + `person.csv` | → | `Individual` (age; sex where a release ships it) + cohort provenance |
| `visit_occurrence.csv` | → | `TimeElement` (age by default — see the SDD on date precision) |
| `condition_occurrence.csv` | → | `Disease` (item → MONDO; no onset — the date is the form-fill date) |
| `measurement.csv` | → | `Measurement` (UCUM units, per-row reference ranges, per-eye `procedure.bodySite`) |
| `observation.csv`, `procedure_occurrence.csv`, the 8 modality dirs | → | **not** ingested in v1 |

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
  sources/      dataset readers (raw tables -> IR): sources/voice/ (wide TSV),
                sources/aireadi/ (OMOP CDM long CSV)
  mapping/      YAML mapping engine (column -> concept, condition -> MONDO, item -> HPO/LOINC)
  emitters/     output writers; emitters/phenopacket.py is the first target
  ontology/     MONDO/HPO/LOINC term helpers
config/         per-dataset YAML mappings (config/voice/, config/aireadi/) + shared value sets
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
| `data/synthetic/` | synthetic Voice phenotype tables; synthetic AI-READI OMOP tables; AI-READI's CC-BY-4.0 published crosswalk (`scripts/fetch_aireadi_crosswalk.sh`) | yes, by default |
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

**There is no AI-READI counterpart, deliberately.** The AI-READI Data License (WashU v2.0)
extends to data that has been "excerpted or otherwise altered", which covers phenopackets
derived from the release, and the VUMC synthetic licence bars republishing as a standalone
dataset. AI-READI is covered instead by a hand-authored, synthetic-by-construction fixture
under `tests/data/aireadi/` that runs in CI.

`examples/phenopackets/voice-synthetic/` holds 173 phenopackets built from the public
synthetic Voice data, so downstream tooling can be tested without credentialed access to
the real dataset. Everything there is synthetic; see its
[README](examples/phenopackets/voice-synthetic/README.md) for provenance, counts, and the
known gaps in the snapshot.

## Getting started

```bash
uv sync                       # create the env and install deps
uv run b2ai-ingest --help     # CLI help
scripts/fetch_synthetic_data.sh   # pull the public synthetic voice data into data/synthetic/

# AI-READI (OMOP CDM). Preflight first — it is PHI-safe and reports what will be dropped.
uv run b2ai-ingest validate-aireadi -i tests/data/aireadi -c config/aireadi
uv run b2ai-ingest aireadi -i tests/data/aireadi -o out/synthetic/aireadi/phenopackets
```

## Development

```bash
uv run pytest        # tests
uv run ruff check    # lint
```

## License

MIT — see [LICENSE](LICENSE).

# SDD: Bridge2AI AI-READI (OMOP CDM) → phenopacket pipeline

- **Status:** Implemented (v1)
- **Author(s):** Sujay Patil
- **Date:** 2026-09-09

## 1. Overview & problem statement

Convert the AI-READI `clinical_data/` tables into one GA4GH phenopacket per participant.
AI-READI is the second dataset named in [ADR-0001](../adr/0001-name-architecture-tooling.md),
and it exercises the IR + pluggable-emitter claim for the first time: its clinical payload is
**OMOP CDM v5.4 in CSV**, not the wide ReproSchema TSVs the Voice pipeline was built for.

Developed against two sources, neither of them a licensed AI-READI release:

- the **VUMC synthetic AI-READI release** (`data/synthetic/aireadi/`, fetched by
  `scripts/fetch_aireadi_synthetic.sh`, gitignored) — 10,518 synthetic participants,
  767,814 measurement rows and 59,169 condition rows, but only two of the six OMOP tables;
- **AI-READI's own published crosswalk** (`scripts/fetch_aireadi_crosswalk.sh`), which is
  CC-BY-4.0 *documentation* rather than licensed Data. It supplies the item keys,
  untruncated labels and laterality for what the synthetic release does not ship.

plus a hand-authored fixture (`tests/data/aireadi/`, committed, synthetic by construction).

**The crosswalk is a candidate generator, never an oracle.** It carries codes that do not
resolve — it gives `bp1_sysbp_vsorres` `LOINC:2403450`, for which the NLM Clinical Table
service returns nothing (the real code is `8480-6`). Nothing from it is emitted without
independent verification.

## 2. Goals & non-goals

- **Goals:** `participants.tsv` + `person.csv` → `Individual`; `visit_occurrence` →
  `TimePoint`; `condition_occurrence` → `Disease` (MONDO); `measurement` → `Measurement`
  with UCUM units, per-row reference ranges and per-eye laterality; one phenopacket per
  participant; a PHI-safe preflight (`validate-aireadi`) and run report.
  `observation.csv` contributes the CES-D-10 and PAID-5 instruments as ordinal Measurements,
  with every other family covered by an explicit, reviewable drop policy.
- **Non-goals (now):** the rest of `observation.csv` — the medical-history yes/no grid, the
  PhenX SDOH modules, the imaging-acquisition flags, and several instruments that are
  ingestable but uncurated (each with a stated reason in
  `config/aireadi/observation/scope.yaml`); the eight non-clinical modality directories;
  medications (no `drug_exposure` table is published); race/ethnicity.
- **`procedure_occurrence.csv` is not ingested, and that is a conclusion rather than a
  deferral.** It carries 22 monofilament rows per participant — ten sites per foot plus two
  "foot tested" flags — and every one records only that a site *was tested*. None carries a
  result. The finding, the count of sites felt per foot, lives in `measurement.csv` as
  `mssrffl`/`msslffl`, and that is what is ingested
  (`config/aireadi/measurement/monofilament.yaml`). Ingesting the procedure table would emit
  twenty `MedicalAction`s per participant asserting that a test happened, with the finding
  stored elsewhere — more output, no more information. Revisit only if a release starts
  carrying per-site results.

## 3. Design

`AireadiSource` (`src/b2ai_dataset_ingest/sources/aireadi/reader.py`) streams each OMOP CSV,
`mapping/omop.py` turns a long row into an IR fragment, and the existing
`PhenopacketEmitter` renders each participant. `participants.tsv` is the one wide table and
feeds the *existing* `MappingEngine.individual_fields` `columns:` path unchanged.

`mapping/omop.py` sits in `mapping/`, not in `sources/aireadi/`, because OMOP CDM is a
published standard: a second OMOP dataset reuses the module and writes only its own config.

## 4. Data model / mappings

- **The item key is the REDCap variable**, taken from the text before the first comma of
  `<domain>_source_value` — never `*_concept_id`. Concept `3004249` backs both
  `bp1_sysbp_vsorres` and `bp2_sysbp_vsorres`, `3012888` both diastolic readings, `4239408`
  both pulse readings, and `4047085` all twenty monofilament sites. Every variable maps to
  exactly one concept, but not the reverse.
- **`*_source_value` is truncated at 49 characters**, so its label half is a curator hint and
  never a label source. Untruncated labels come from AI-READI's published CC-BY-4.0
  crosswalk.
- **Subjects and assay ids are `b2ai:<omop_table>.<redcap_var>`.** The table qualifier is
  mandatory: the `condition_occurrence` variables also appear in `observation`, where they are
  a self-reported yes/no answer rather than an asserted condition — a different item.
- **No `Disease.onset`.** `condition_start_date == condition_end_date` on every row: it
  records when the medical-history form was filled in, not any onset. Emitting it as onset
  would assert a false natural history a consumer could not detect.
- **`study_group` is a Measurement, not a Disease.** The arm is a recruitment stratum, not a
  finding: a participant recruited into an arm need not carry the corresponding condition in
  their own medical history. Asserting it as a diagnosis would state something the clinical
  data does not support. It is also recorded on `Participant.cohort`.
- **Units are declared per item in config; the data column is a cross-check.**
  `unit_source_value` is blank, a bare space or `"N/A"` on 504,864 of the 767,814 measurement
  rows in the synthetic release, and every non-lab family keeps its unit inside the truncated
  label. `Quantity.unit` is required by the schema, so a value whose unit resolves to nothing
  is dropped and counted.
- **Laterality is declared per item, not read from `qualifier_concept_id`.** Per the
  published crosswalk, six autorefraction items are per-eye by name and carry no qualifier at
  all, and the same column on a medical-history row holds a *condition name*. It goes on
  `Measurement.procedure.body_site`, the only laterality slot a GA4GH `Measurement` has.
- **A reference range is emitted only when both bounds are present.** `ReferenceRange.low`
  and `.high` are proto3 doubles with no field presence, so a one-sided range would read back
  as "the normal range is 39 to 0". Opt-in per **family** (`reference_ranges:` at the top of a
  measurement config), because a reference interval is a property of the family rather than
  of one analyte: a lab result has a normal range, while a cognitive subscore's 0–3 bound is
  a *scoring* range. OMOP carries the interval on the row, not the item, precisely because it
  can be age- and sex-specific.
- **Censoring.** `operator_concept_id 4171756` (`<`) marks a bounded result; GA4GH `Quantity`
  has no operator slot, so those rows are dropped and counted rather than reported as
  measured. Note the polarity: the column is `0` ("not recorded") on 178,806 of the 767,814
  synthetic rows — every vital and every CBC item — so a "must equal `=`" gate would have
  deleted them all.
- **Sentinels.** `555`/`777`/`888`/`999` in `value_as_number` are REDCap refusal codes and are
  dropped. `0` is *not* a sentinel in a value column — it is a valid answer — while `0` in a
  `*_concept_id` column means "no matching concept". Two separate null sets.
- **Time.** `time_precision: age` is the default, so every emitted `TimeElement` carries an
  Age and no date leaves the machine: day-precision dates are HIPAA identifiers and the
  licence extends to derived output. `date`/`datetime` are opt-in and normalize to RFC3339-Z
  — protobuf rejects every other spelling and the emitter *catches* the error and falls back,
  so an un-normalized value would lose every `time_observed` silently.
- **Laterality is structural where the ontology allows it, and in the label where it does
  not.** The per-eye ophthalmic items carry `UBERON:0004549 right eye` / `UBERON:0004548
  left eye` on `Measurement.procedure.body_site`. The per-foot monofilament items cannot:
  UBERON has `UBERON:0002387 pes` and no lateralized child, so they carry `pes` as the site
  and the side in the curated assay label. `UBERON:8300003`/`8300004` look like the missing
  terms and are not — they are *hindlimb*, the whole limb. The asymmetry is deliberate and
  documented at both configs; a consumer filtering on `body_site` can separate the eyes but
  not the feet.
- **Age is derived per observation, not copied.** A cohort table records one age at one
  reference date; OMOP records a date on every row. Reusing the single value gives every
  observation the same `Age` — and at age precision the `Age` *is* the whole `TimeElement`,
  so two visits become byte-identical in the output. `AgeAnchor` pairs the cohort age with
  the date it was measured on and derives each row's age by anniversary. The date is read for
  the arithmetic only and is not emitted, so this buys per-visit resolution without widening
  what leaves the pipeline. A release shipping no anchor date degrades to the single-age
  behaviour rather than losing age. This matters for AI-READI specifically: the published
  study design has a follow-up visit for a subgroup, which is the point at which reusing one
  age stops being harmless.
- **Sex may be redacted, and that must be visible.** AI-READI states that sex and
  race/ethnicity are removed from published releases, so `gender_concept_id` can be `0` on
  every row. `person.yaml` is written for a release that carries demographics and recodes `0`
  to nothing; the redaction is reported per column, so a run of all-`UNKNOWN_SEX` subjects
  cannot read as success.

### Value-gated HPO derivation

The machinery is wired and tested; no curated AI-READI → HPO rows ship yet.

`observation.csv` rows pivot to the plain `{item: value}` view via `omop.as_row`, which is
exactly the contract `hpo_rules.derive_features` already takes — so the derivation runs with
**no** OMOP-specific changes to `hpo_rules`. Measurements emit per row as the table streams,
while the gated path buffers only the items that actually carry a rule rather than all ~355.

Two defences stop a refusal code asserting a phenotype, and both are needed.
`conditions._match_scalar` falls back to the raw cell when the ordinal is `None`, so `>=1`
matches an answer of `777` whether or not the resolver returns `None`. The reader therefore
screens sentinels before buffering, **and** every shipped `when_value` must be bounded
(`in {1,2,3}`, `>=n & <=m`) rather than open-ended — enforced by a test over the AI-READI
mapping sets.

Nothing curated ships because the curation is not settled: an adversarial review of proposed
CES-D-10 mappings refuted five of the seven it judged, on predicate direction and on
cut-point. In this repo a cut-point is a curator judgement — the Voice set got clinical
review before it shipped — so the AI-READI set waits for the same. The path is proven against
an injected mapping in `tests/test_aireadi_observation.py`, the pattern
`tests/test_conditional_features.py` already uses for Voice, and a test asserts the shipped
sets are empty so that landing rows is a deliberate act.

## 5. Testing strategy

Two tiers, and the hand fixture carries more of the weight than Voice's does — the VUMC
synthetic release ships only two of the six OMOP tables, so on *shape* it covers **less**
than the fixture does.

- **Tier 1, always run, in CI:** `tests/data/aireadi/` (4 participants, ~30 rows), every row
  encoding one specific hazard; see that directory's README. Covered by
  `tests/test_aireadi_omop.py` (primitives, no data on disk) and
  `tests/test_aireadi_reader.py`. This is the only tier that exercises `person`,
  `visit_occurrence`, laterality, censoring and reference ranges end to end.
- **Tier 2, local, skipped in CI:** the VUMC synthetic release — scale (bounded with
  `islice` so it stays quick), graceful degradation to `tables_missing` when four of six
  tables are absent, and the check that every configured *condition* item exists in a real
  release. Measurement coverage is deliberately not asserted there, since the release ships
  73 of the items.

**Gap, stated plainly:** `config/aireadi/measurement/ophthalmic.yaml`, `participants.yaml`
and `person.yaml` describe tables and items that no release available here ships. They are
authored against AI-READI's published crosswalk and the OMOP CDM / CDS specifications, and
exercised only by the fixture. `validate-aireadi --strict-coverage` against a full release
is the check that closes that gap, and it is the first thing to run when one is available.
The flag asserts the release is *complete* — it promotes both "a configured item is absent"
and "a whole table is absent" from warning to error. Without it the tool stays usable as a
preflight on a partial release, which every source available here is.

**On a subset release, expect it to flag the prevalence tail.** The configs cover the union
of the releases seen, so a condition occurring in well under 1% of participants is absent
from a small subset by sampling rather than by schema — `mhoccur_ad` (0.06%) and
`mhoccur_rvo` (0.87%) are absent from a 100-participant subset for exactly that reason. That
is expected and is not grounds for dropping the mapping: removing it because a small sample
lacks the condition would silently lose it on the full release.

No golden/snapshot phenopackets: verification is a protobuf round-trip plus structural
invariants (`_prefixes_used(pkt) <= declared`, and no `TODO` CURIE in any emitted packet).

**Nothing AI-READI-derived is committed.** The AI-READI Data License extends to data that has
been "excerpted or otherwise altered", so fixtures are hand-authored rather than sampled, and
there is no `examples/phenopackets/aireadi-*/` counterpart to the Voice examples.

## 6. Risks & mitigations

- **No OMOP `CONCEPT` table ships and the Athena API returns HTTP 403 here**, so no
  `concept_id → CURIE` resolution is free. Every emitted code is curated and machine-checked:
  MONDO via the existing oaklib gate, LOINC via the NLM Clinical Table service (the route
  `phq9.yaml` already used).
- **LOINC cannot be an SSSOM `object_source`**: `sqlite:obo:loinc` resolves to a 0-byte
  database. LOINC assay codes therefore live in the YAML config with a dated verification
  header rather than behind a gate that cannot check them.
- **Partial mapping is the steady state.** 32 measurement items (MoCA, monofilament) and 4
  conflated conditions are deliberately unmapped; they are counted per item on the report, and
  `has_degradation` deliberately excludes them so the flag stays meaningful.
- **Scale.** Materializing the 117 MB synthetic `measurement.csv` costs 1206 MB of RSS against
  18 MB streamed, so every table is read row by row.

## 7. Open questions

- **Date precision** — `time_precision: age` is the shipped default; whether day-precision
  dates may be emitted is a DUA question, not a code question.
- **The 2498 explicit medical-history negatives** in `observation.csv` would give
  `excluded: true` Diseases. The data is unusually clean (an exact bijection with
  `condition_occurrence`) and, unlike the Voice questionnaires, these are lifetime-scoped
  items — so the reason the absent pole was withdrawn for Voice does not apply. Still off
  pending clinical review.
- **The four conflated conditions** (`mhoccur_ua`, `mhoccur_cvdot`, `mhoccur_circ`, and the
  lab-finding `mh_a1c`) need a curator's call on what was actually asked.
- **The LOINC upgrade tranche** — ~40 items currently carry `b2ai:` local assay ids and can be
  swapped for verified LOINC codes in place, changing no code.

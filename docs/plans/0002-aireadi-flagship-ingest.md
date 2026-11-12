# Plan: AI-READI flagship ingest — the full release, the OMOP long tail, and the mapping loop

- **Date:** 2026-10-22
- **Related:** [SDD: aireadi-ingest](../design/aireadi-ingest.md), [ADR-0002](../adr/0002-conditional-hpo-mapping.md),
  [#35 Align to LinkML](https://github.com/bridge2ai/b2ai-dataset-ingest/issues/35),
  the brief at "Plan for AI-READI flagship ingest" (Google Doc, 2026-10-22)
- **Status:** proposed — an investigation and plan, no code changes

## Context

The AI-READI ingest (v1, SDD above) was built and run against the **mini** release
(Fairhub dataset 4, 100 participants). It reads `participants.tsv`, `person.csv`,
`visit_occurrence`, `condition_occurrence`, `measurement`, and two instruments out of
`observation.csv` (CES-D-10, PAID-5), emits one phenopacket per participant, and derives
value-gated HPO features from labs, vitals, monofilament and CES-D-10. Everything else is
either dropped by a named policy (`config/aireadi/observation/scope.yaml`) or not looked at.

Three things have changed since:

1. **The flagship release has landed** (DUA P26-10500 executed 2026-09-10). Its file
   manifest, the two protected supplements and the protected data dictionary were staged for
   planning under `out/update_plan/` (outside every protected tree; the manifest and
   dictionary are documentation, the medications sheet is Data — see "What was and was not
   read" at the end).
2. **Two protected tables exist outside the OMOP tree** — a demographics sheet and a
   medications sheet — which v1 assumed did not exist ("no `drug_exposure` table is
   published"; "sex is redacted"). Sex being available in particular unlocks the
   sex-stratified reference-range gates that are currently collapsed to sex-blind ones.
3. **The mapping loop has a human bottleneck.** The clinical reviewer (Sek Won Kong) works in
   Google Sheets, not GitHub, and the pipeline's mapping logic has outgrown SSSOM extension
   columns (#35).

This plan covers five work streams the brief names, in the order they should run. Each
stream is sized against what the investigation found, and every number below comes from
the manifest, the dictionaries, public AI-READI documentation, or public mapping sets —
never from a data row.

### What the flagship delivery contains (from `file-manifest.tsv`)

| Top-level directory | Files | What it is |
| --- | ---: | --- |
| `retinal_photography/` | 94,253 | DICOM; `cfp` (4 devices), `ir` (4 devices), `faf` (1) |
| `wearable_activity_monitor/` | 15,245 | Garmin Vivosmart 5; 7 sub-modalities, mHealth JSON |
| `cardiac_ecg/` | 4,515 | Philips TC30 12-lead; WFDB `.hea` + `.dat` |
| `wearable_blood_glucose/` | 2,246 | Dexcom G6; one mHealth JSON per participant |
| `environment/` | 2,232 | LeeLab Anura; one ESDS CSV per participant |
| `clinical_data/` | 7 | OMOP: person, visit, condition, measurement, observation, **procedure_occurrence**, `dqd_omop.json` |
| root | 9 | `participants.tsv/.json`, `dataset_description.json`, `healthsheet.md`, … |

Total 118,508 files; about 2.4k distinct participant folder ids (docs.aireadi.org v3.0.0
documents 2,280 participants, 15 modalities, 356,343 files, 3.82 TB).

**Gap to confirm first:** `retinal_oct/`, `retinal_octa/` and `retinal_flio/` are absent from
the manifest, as is any modality flag for them, yet v3 ships all three. Either the delivery is
a subset or the manifest is. Nothing below depends on them, but the modality work in stream 3
should know whether they are coming.

### The protected supplements (headers and dictionary only)

- **Demographics** (`columns_from_AIREADi_Demographics.txt`, dictionary sheet "Protected -
  demographics"): `studyid`; `scrsex` (sex assigned at birth: F/M/I/888/777); `genderid`
  (man / woman / non-binary / other / prefer not); `race___*` (9 checkboxes) and `race2`
  (single best); `ethnic___*` (10 checkboxes); `raceot`, `ethnicot`, `racetrib`, `ancestry`,
  `genderidot` (free text); `rp_childpot` (pregnant — an exclusion criterion, so always 0);
  `cl_maristat`; `mhcat_dmtaot`, `mhoccur_cnsot`, `mhoccur_cnrot` (free-text "other" for
  diabetes type, nervous-system and cancer history); `pxhic6`, `pxhic9` (insurance);
  `dvenvlocn` (sensor location); `exportzip`.
- **Medications** (`AIREADi_Medications_Protected Dataset_PilotthruWave4.xlsx`): one row per
  participant × medication, REDCap repeat instrument `current_medications`. Columns
  `cmname` (free text), `cmroute` (12 coded routes + other), `cmrouteot`, `cmdos`, `cmdosu`
  (20 coded units), `cmdosfrq` (ONCE/PRN/QD/BID/TID/QID/QOD/QW/QM/UNK/888), `rxnorm_code`,
  `rxnorm_term` (BioPortal RxNorm). Structural aggregates only: 19,252 rows over 2,179
  participants; RxNorm code present on 91% of rows; 4,892 distinct codes; 8,661 distinct
  free-text names.

## Steps

### Stream 0 — preflight on the real release (the user runs it; the tool prints counts only)

1. Run the existing preflight against the flagship tree:
   `b2ai-ingest validate-aireadi --input data/real/aireadi/<root> --strict-coverage`.
   It already distinguishes `items_unmapped` from `items_dropped`, reports redaction per
   column, and flags configured-but-absent items. The SDD names this as "the first thing to
   run when a full release is available"; it has not been run yet.
2. Add to `validate-aireadi`: read `clinical_data/dqd_omop.json` (OHDSI Data Quality
   Dashboard output that ships with the release) and surface its failed checks as counts, so
   known source-side quality issues are visible before we chase them as ingest bugs.
3. Add a `validate-aireadi --modalities` pass that reads each `<datatype>/manifest.tsv` and
   reports files per modality/device and participants-with-data per modality — numbers, no
   ids. This is also the dry run for stream 3 and the check for the OCT/OCTA/FLIO gap.

### Stream 1 — the protected supplements: demographics and medications

These are two new wide tables, not OMOP, so they are two small readers that feed the IR the
same way `participants.tsv` does. Both are optional inputs: absent files leave the output
exactly as v1 emits it.

4. **`config/aireadi/demographics.yaml` + reader hook.** `scrsex` → `Individual.sex`
   (F→FEMALE, M→MALE, I→OTHER_SEX, 888/777→unset, counted as declined). `genderid` →
   `Individual.gender` as an `OntologyClass` — the phenopacket slot exists and is unused;
   pick one vocabulary (LOINC `LA22878-5`-style answer codes or NCIT) and verify the codes
   through the existing validator route. Record the `person.csv` redaction counts as before,
   so the report shows sex came from the supplement, not from OMOP.
5. **Race, ethnicity, ancestry: decide not to emit, and write it down.** Phenopacket v2
   `Individual` has no slot for them; AI-READI itself withheld them from the public tier "to
   prevent stigmatizing findings"; and the free-text fields are Data under §3.C that no model
   may read. Record the decision in `config/shared/value_sets.yaml` (which still carries a
   "decide ontology — NCIT / HANCESTRO" TODO) and in the SDD. If a downstream consumer needs
   cohort composition, it belongs in a cohort-level summary, not in per-person packets.
   `rp_childpot`, marital status, insurance, sensor location and the `*ot` free-text fields
   are out of scope for the same reasons (not phenotype, or free text).
6. **`config/aireadi/medications.yaml` + reader → `MedicalAction.Treatment`.** This needs
   one new IR class (`TreatmentObservation`: agent, route, dose quantity, frequency, time) and
   one emitter branch; the phenopacket side is `MedicalAction { treatment { agent,
   route_of_administration, dose_intervals[] } }`. Mapping:
   - `rxnorm_code` → `agent` as `RxNorm:<cui>` with `rxnorm_term` as label. RxNorm is a
     legitimate `Resource` in `MetaData` (add it to `ontology/resources.py` with a dated
     version). No ontology translation is required to be correct; ChEBI/DrugCentral can be
     layered later via RxNav (public, no licence) → ingredient → OMOP2OBO drug mappings.
   - `cmroute` (coded) → NCIT route terms; `cmdosfrq` (coded) → NCIT frequency terms; `cmdosu`
     (coded) → UCUM. All three are closed code lists in the dictionary, so they are curated
     once in config and verified like any other code.
   - Rows with no `rxnorm_code` (9%) are **counted and dropped**. `cmname` is free text and is
     Data; we do not read it, normalise it, or send it anywhere. If the project wants those
     rows, the fix is upstream (ask AI-READI to complete the BioPortal coding) or a curator
     working inside the DUA boundary with RxNav, with the pipeline consuming the result.
   - Time: `current_medications` is a point-in-time list at the study visit; use the
     participant's visit age (same `AgeAnchor` path) and no interval end.
7. **Re-split the sex-blind reference-range gates.** Every lab row in
   `mappings/b2ai-aireadi-measurement.sssom.tsv` whose comment says "sex-blind gate" can
   now carry the lab's real F/M intervals. This is the first concrete need for stratified
   conditions and is the forcing function for stream 5 — do it there, not by hand-editing
   `when_value`.

### Stream 2 — everything in the OMOP tables

8. **Build the item inventory from public sources, not from the data.**
   `AI-READI/DataElementMaps` (2,557 rows, CC-BY) plus the v3 OMOP mapping table on
   docs.aireadi.org give every REDCap variable, its concept id, vocabulary and predicate.
   Generate `config/aireadi/inventory.tsv` from them (a script under `scripts/`, committed
   output) and have `validate-aireadi` diff the release's `items_unmapped` against it, so the
   to-do list is reproducible from public material.
9. **`observation.csv`, by family**, in this order (each is a `config/aireadi/observation/*.yaml`
   plus SSSOM rows, all through the curation gate):
   - *Physical assessment* (height, weight, BMI, waist, hip, WHR) — these are in `measurement`
     already per the docs; confirm with the preflight and gate BMI ≥ 30 → obesity (HP:0001513)
     with a `threshold_source`. Note WHO cut-points are sex-specific: another stratification case.
   - *MoCA* total and the seven subscores — Measurements; total gate (< 26) pending reviewer.
   - *Medical-history grid* `mhoccur_*` / `mhterm_*` as self-report — the 2,498 explicit
     negatives would give `excluded: true` Diseases. Lifetime-scoped items, so the Voice reason
     for withdrawing the absent pole does not apply. Needs the clinical reviewer's yes; ship
     presence-only until then.
   - *Vision function* `via1-3`, *falls* — term-to-term rows already exist ungated; add gates.
   - *Substance use* (smoking status, alcohol) — Measurements with LOINC answer codes; the
     HPO scope here is thin, so do not force phenotype derivation.
   - *Diabetes self-care, diet, SDOH (PhenX), discrimination, food insecurity, neighbourhood* —
     Measurements keyed by PhenX protocol ids where DataElementMaps has them, or stay dropped
     with the reason already in `scope.yaml`. These are not phenotypes; the value of ingesting
     them is completeness for consumers, not HPO. Decide per family with the reviewer; default
     is **stay dropped** until someone asks.
10. **The LOINC tranche.** 40 measurement items still carry `b2ai:measurement.*` assay ids;
    35 already carry verified LOINC. Swap in place, verified through the NLM Clinical Table
    service route `phq9.yaml` used. No code changes.
11. **Units.** Flagship `unit_source_value` is blank on most rows (two thirds in synthetic);
    config-declared units stay authoritative. Add the preflight check "declared unit ≠
    source unit where source is present" as a count, so a lab changing units is caught.
12. **`procedure_occurrence`: keep the v1 conclusion** (22 per-site monofilament rows, no
    results; the finding is in `measurement`). Re-check once with the preflight on the
    flagship that it still carries nothing but monofilament.
13. **Dates.** Keep `time_precision: age`. The flagship's `dataset_description.json` is the
    place to re-read `deIdentDates`; if the DUA conversation ever permits day precision it is
    one config line.

### Stream 3 — the rest of the flagship: file links and device-derived measurements

The phenopacket answer for bulk data is `Phenopacket.files[]`: `File { uri,
individual_to_file_identifiers, file_attributes }`. The emitter already writes `File(uri=…)`
for Voice audio; it needs `file_attributes` and a source that fills them.

14. **One `File` per manifest row, read from `<datatype>/manifest.tsv`, never by walking the
    tree.** Each modality ships a manifest keyed by participant with path, device, protocol
    and (for imaging) laterality columns. `file_attributes` carries `datatype`, `modality`,
    `device`, `laterality`, `protocol`, `fileFormat` (DICOM / WFDB / mHealth-JSON / ESDS-CSV)
    and `sha256`/`md5` from the top-level manifest. The URI is **relative to the release
    root** with the release DOI (`10.60775/fairhub.<n>`) and version in `MetaData.resources`;
    a `--file-uri-base` flag lets a consumer rewrite to their own storage. Retinal
    photography is ~40 files per participant; that is acceptable packet size and keeps
    per-file laterality and protocol queryable. A `--files {none,manifest,all}` switch keeps
    the v1 output byte-identical by default.
15. **ECG: ingest the device's numeric intervals as Measurements.** `cardiac_ecg/manifest.tsv`
    carries Rate, PR, QRSD, QT, QTc and P/QRS/T axes per recording — LOINC has codes for all
    of them (8867-4 heart rate, 8625-6 PR, 8633-0 QRS duration, 8634-8 QT, 8636-3 QTc). Gate
    QTc and PR against guideline cut-points with `ECO:0007307`, and add a `file` pointer
    back to the WFDB pair. The `.hea` interpretation strings ("RBBB and LAFB", "OTHERWISE
    NORMAL ECG") are explicitly "not reviewed by a physician": **do not** map them to HPO in
    this pass; record them as a `description` on the ECG Measurement at most.
16. **CGM: summarise 10 days of 5-minute glucose into the international consensus metrics**
    (mean glucose, time in range 70–180, time below 70 / above 180, glucose variability as
    CV, GMI) — one Measurement each per participant, LOINC where a code exists, `b2ai:` where
    not, with the window length recorded. Gate time-in-range < 70% and GMI as HPO
    hyperglycaemia with a guideline `threshold_source`. The raw JSON is linked as a `File`.
17. **Garmin: daily summaries only** (resting HR, mean SpO2, total sleep, steps) as
    Measurements over the 10-day window; link the seven JSON files. No HPO derivation from
    consumer-wearable data in this pass.
18. **Environment and retinal imaging: `File` links only.** Neither carries a phenotype as
    released (no grading in the DICOM headers is documented); the sensor data is exposure,
    not phenotype.
19. **Scale.** The reader already streams the OMOP tables (18 MB RSS on 768k rows). The new
    inputs are one JSON/CSV per participant per modality, read one at a time; the 28 MB
    manifest is read once. No architecture change.
20. **Packet size rule: never embed a time series or waveform.** Measured on a worst-case
    packet (every clinical item at two visits, two ECG recordings, CGM and Garmin summaries,
    75 linked files, 10 medications): 203 KB pretty-printed, 119 KB compact, 7 KB gzipped;
    about 460 MB for the cohort. Half of that is the existing clinical measurements; ECG
    intervals are 8 KB and file links 35 KB. The only route to a multi-GB packet is emitting
    raw samples as Measurements — a 10-day CGM trace is ~2,880 readings, per-minute Garmin
    heart rate ~14,000, an ECG waveform 12 leads × 500 Hz — so device data enters the packet
    only as window summaries (steps 15–17) with the raw file linked. A test asserts an upper
    bound on emitted packet size so a regression shows up in CI.

### Stream 4 — a correction loop that does not require GitHub

The reviewer edits Google Sheets. The cheapest loop that preserves every guarantee the
repo has (codes verified against the pinned release, `when_value` parses, CI) is a
**sheet ↔ SSSOM sync**, not a new UI.

20. `scripts/sssom_sheet.py push <mapping.sssom.tsv> <sheet-id>` writes the SSSOM rows plus
    two review columns (`curator_decision` ∈ {accept, reject, change}, `curator_comment`)
    and a locked `row_key`; `pull` reads the sheet back, applies decisions to the TSV,
    runs `b2ai-ingest validate-mappings`, and opens a PR with the diff and the sheet
    comments as the PR body. `gws` (already installed and authenticated for Drive; Sheets
    scope needed) or the `claude.ai Google Drive` connector does the transport.
21. Only after the sync has been used for one review cycle, decide whether a UI is
    warranted. If it is, the shape is a single-page app over the same `push`/`pull`
    functions with a GitHub App that opens PRs — not a replacement for the SSSOM files.
22. Feed `OMOP2OBO` into the **candidate** column of the sheet (see assessment below), so the
    reviewer sees "OMOP2OBO says X, we propose Y" side by side.

### Stream 5 — LinkML (#35): stratified gates first, schema second

23. **Short term, no LinkML:** add a `when_subject` SSSOM extension column alongside
    `when_value` (`sex == "FEMALE"`, `age >= 65`), evaluated by the same grammar over
    `Individual` fields. This unblocks step 7 and the BMI/WHO cut-points in a week, and the
    SSSOM stays consumable by SSSOM tooling (unknown columns are passed through).
24. **Medium term:** promote `model/core.py` to a LinkML schema (ADR-0001 anticipated this),
    generate the Pydantic from it, and validate emitted packets against
    `cmungall/linkml-phenopackets` **for validation only** — it is experimental, last
    touched 2024-07, and generated from the protobuf, so it is a check on our output, not an
    emit model.
25. **Spike, time-boxed:** express the measurement gates (including ratios — ACR, sites felt
    ÷ sites tested — and paired readings) as LinkML `rules`/`linkml-map` and generate the
    SSSOM from them. `linkml-map` is 0.5.4 (2026-08-26), still pre-1.0; the `ast.Num` crash
    is Python 3.14-only. Decide after the spike whether the generated SSSOM round-trips
    through the existing validator; if it does, ADR-0002 is amended, if not, `when_subject`
    stays.

## Assessment: OMOP2OBO

`callahantiff/OMOP2OBO` is two things: a **mapping pipeline** (Python 3.6, OWLTools, requires
a UMLS licence for MRCONSO/MRSTY; last commit 2023-03; v1.0.0 2021-11) and three **released
mapping sets** on Zenodo (Oct 2020 vocabularies): Conditions (SNOMED concept ids → MONDO and
HPO, 109,719 rows), Measurements (3,090 LOINC concepts × Low/Normal/High → HPO, plus
UBERON/ChEBI/CL/PR/NCBITaxon), Drug ingredients (RxNorm → ChEBI). Coverage against our
curated sets, by OMOP concept id:

| Our set | Covered | Agreement with our curated term |
| --- | ---: | --- |
| Conditions (30 ids, 26 mapped) | 24/30 | Same MONDO on ~17; our 6 misses are the AI-READI-local `2005…` ids it cannot know. Disagreements both ways: it gives dyslipidaemia → *inherited lipid metabolism disorder* (wrong), arthritis → MONDO:0005578 *arthritis* (arguably better than our *arthropathy*), T2DM → T2DM **plus** MODY1 (one-to-many noise), and it resolves `mhoccur_ua` to *urinary tract infection*, which our curator left as a conflated TODO. |
| Measurements (35 LOINC) | 14/35 | Low/High poles match our HPO choice on most labs; but "Normal" rows are mapped to the *abnormal* parent (ALT normal → *elevated ALT*), ALT "Low" → *decreased muscle mass*, haemoglobin only Positive/Negative. The 21 misses are our vitals, CBC, lipids and ophthalmic items. |

**Conclusion:** adopt the released mapping sets as a **candidate generator inside the
curation-assist flow** (import as SSSOM with `mapping_justification` from `MAPPING_CATEGORY`
and the Zenodo DOI as `mapping_source`), never as an oracle and never auto-shipped; do not
adopt the pipeline (licence, Python 3.6, 2020 vocabularies). Its measurement set models
result-level mapping the way our `when_value` rows do, so it is also a useful cross-check for
the HPO *term* on each pole, not for the threshold.

## Critical files

- `src/b2ai_dataset_ingest/sources/aireadi/reader.py` — two supplement readers (steps 4, 6),
  manifest-driven `File` source (14), ECG/CGM/Garmin summarisers (15–17); each a separate
  module under `sources/aireadi/` so the OMOP reader stays as it is.
- `src/b2ai_dataset_ingest/model/core.py` — `TreatmentObservation`, `FileReference` with
  attributes; `Individual.gender` already exists.
- `src/b2ai_dataset_ingest/emitters/phenopacket.py` — `MedicalAction`, `File.file_attributes`.
- `src/b2ai_dataset_ingest/ontology/resources.py` — RxNorm, NCIT route/frequency resources.
- `src/b2ai_dataset_ingest/sources/aireadi/validate.py` — DQD, modality and unit checks (1–3, 11).
- `src/b2ai_dataset_ingest/mapping/conditions.py`, `hpo_rules.py` — `when_subject` (23).
- `config/aireadi/{demographics,medications}.yaml`, `config/aireadi/observation/*.yaml`,
  `config/aireadi/modalities.yaml` — new configs.
- `mappings/b2ai-aireadi-*.sssom.tsv` — re-split gates, new families, ECG/CGM rows.
- `scripts/build_aireadi_inventory.py`, `scripts/sssom_sheet.py`, `scripts/import_omop2obo.py`.
- `docs/design/aireadi-ingest.md` — amend for supplements, files, race/ethnicity decision.

## Verification

- Every new reader has a hand-authored fixture under `tests/data/aireadi/` (never a sampled
  row), following that directory's README; CI runs them.
- `validate-aireadi --strict-coverage` and `--modalities` on the real release are run by the
  user and reported as counts; the plan's first milestone is that report.
- `validate-mappings` stays the gate on every SSSOM change; `tests/test_aireadi_*` pin every
  shipped cut-point.
- Packet-level: round-trip through `phenopackets` protobuf, `_prefixes_used ⊆ declared`,
  no `TODO` CURIE; later, linkml-phenopackets validation (24).
- Outputs from a real run land only under `out/real/aireadi/` (§3.E); the guard hook and the
  ownership split in the README are unchanged.

## Sequencing

| Milestone | Streams | Gate | Status (2026-10-22) |
| --- | --- | --- | --- |
| M0 preflight on the flagship | 0 | counts report; OCT/OCTA/FLIO status known | PR #37: DQD + `--modalities` preflight, size test. **Run on the real release pending (user).** |
| M1 supplements | 1 (4–6), 5 (23) | sex populated; medications emitted; sex-split gates | PR #38: demographics, medications, `when_subject`, 39 stratified rows |
| M2 OMOP long tail | 2 | unmapped count near zero by policy; LOINC tranche done | PR #40: public inventory (0 unmapped of 496), falls / vision / substance use / MoCA, 16 LOINC swaps + 3 corrections |
| M3 files + device measurements | 3 | packets carry `files[]`; ECG/CGM Measurements | PR #39: `--files`, `--device-summaries`, devices SSSOM |
| M4 review loop | 4 | one reviewer cycle round-tripped through the sheet | PR #41: `sssom_sheet.py`, `import_omop2obo.py`. **First reviewer cycle pending; gws Sheets scope pending.** |
| M5 schema | 5 (24–25) | IR in LinkML; spike decision recorded as ADR | `schema/b2ai_ir.yaml` + lockstep test; ADR-0002 amended. **linkml-map spike still open.** |

Still needing a human: the preflight run against the real release; clinical sign-off on
the gates marked *pending clinical review* (MoCA, falls, vision, GMI, CES-D-10); the
medical-history absent pole; the first review cycle through the sheet.

## Out of scope

- Reading, sampling, or summarising values from any real or AI-READI-synthetic row; every
  mapping is built from public dictionaries, and the one Data file staged for planning was
  opened for header and null-count aggregates only.
- Normalising free-text medication names or any `*ot` free-text field with a model (§3.C).
- Image-derived phenotypes (OCT thickness, DR grading): nothing in the release carries them.
- A knowledge graph or any target other than phenopackets (the emitter stays pluggable).

## What was and was not read for this plan

Read: the Google Doc brief; `file-manifest.tsv` (paths only, aggregated); the protected data
dictionary (documentation under the DUA's own definition); the demographics **column list**;
the medications sheet's **header, row count, null fraction and distinct-value counts per
column** (no values); public docs.aireadi.org v3 pages; `AI-READI/DataElementMaps`; the
OMOP2OBO Zenodo mapping sets. Not read: anything under `data/`, `out/real/`,
`out/synthetic/aireadi/`, or the planning inventories under `out/aireadi_mapping_planning/`.

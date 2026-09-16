# Mapping conventions

How YAML mapping configs under `config/` are authored, as implemented by
`src/b2ai_dataset_ingest/mapping/`.

## Where mappings live

- `config/<dataset>/` — per-dataset table mappings (e.g. `config/voice/demographics.yaml`).
- `config/shared/` — value sets reused across datasets (sex recodes, default taxonomy, …).

## Ontology terms

- Reference terms by **CURIE**: `MONDO:0005180`, `HP:0001609`, `LOINC:44261-6`,
  `NCBITaxon:9606`.
- Use a `{ id, label }` pair. `id` is authoritative; `label` is a human aid and may be
  filled/verified at emit time (see `src/b2ai_dataset_ingest/ontology/terms.py`).
- Placeholders use `id: "TODO"` (or `MONDO:0000000`) with `label: "TODO"` until verified.
  These are flagged before a mapping is considered complete.

## Per-table mapping shape

Common keys: `table` / `table_group` (which source table[s]), `keyed_by`
(`[participant_id, session_id]`), `produces` (the IR type). Then, by table kind:

- **Demographics** (`produces: Individual`) — a `columns` block of `<source column>:
  {target: Individual.<field>, value_map: {...}, transform: <name>}`. `value_map` recodes a
  cell (case-insensitively, code or label); `transform` normalizes it (v1: `years_to_iso8601`).
  A column that recodes to nothing is dropped; `Individual.id` comes from `participant_id`.
- **Diagnosis** (`table_group: diagnosis`) — a `conditions` map of `<file basename>:
  {id, label}` (MONDO). Membership is presence in `<basename>.tsv`. `control_handling: skip`
  leaves controls without a Disease. Unresolved conditions (placeholder id) are skipped.
- **Questionnaire** — an `items` map of `<column>: {id, label}` (the assay term, e.g. LOINC),
  plus an optional `score: {source_column, assay, unit}` for a precomputed whole-instrument
  total. Each answered item becomes an ordinal `Quantity(value=<int>, unit=UCUM {score})`.
  The answer→value map is read from the companion data dict's per-item `choices`; an
  `ordinal_scale` map is the fallback when no data dict is present (e.g. test fixtures).

## OMOP long tables (AI-READI)

A dataset delivered as OMOP CDM is long/EAV: a *row* is one observation, so the shapes above
(`columns:`, `items:`) do not apply. `mapping/omop.py` holds the primitives and
`config/aireadi/` the configs. Four rules are load-bearing.

- **The item key is the source variable, never `*_concept_id`.** A concept id is the *assay*
  code and is not unique per item — in AI-READI, concept `3004249` backs both
  `bp1_sysbp_vsorres` and `bp2_sysbp_vsorres`, and `4047085` backs twenty monofilament sites.
  The key is the text before the first comma of `<domain>_source_value`.
- **`*_source_value` is truncated** (49 characters in AI-READI) and its label half is a
  curator hint, never a label source.
- **Subjects and assay ids are `b2ai:<omop_table>.<variable>`.** The table qualifier is
  mandatory because the same variable appears in more than one domain table with a different
  sense — an AI-READI `mhoccur_hbp` row in `observation` is a self-reported yes/no answer,
  the same variable in `condition_occurrence` is an asserted condition. The first-dot split
  the rest of the tooling relies on works unchanged.
- **`b2ai:` spans the project, not one dataset.** Two datasets must not claim the same
  `<table>` namespace: `hpo_rules` indexes value-gated rules by bare table name, so a
  collision would let one dataset's rules fire on another's. Readers scope discovery with
  `default_mapping_files(dataset=...)` and
  `tests/test_config_mappings.py::test_sssom_table_names_are_disjoint_across_datasets` is
  the second lock.

Per-table keys: `table`, `id_column`, `key_column`, `date_column`, `visit_column`,
`value_columns`, plus one of `conditions:` (item -> MONDO term, as in the Voice diagnosis
shape) or `measures:` (item -> `{assay, unit, description?, reference_range?,
procedure_code?, body_site?}`). A `units:` block crosswalks OMOP `unit_concept_id` to UCUM.

**Absence has two meanings, and which applies depends on the column.** `0` is "no matching
concept" in a `*_concept_id` column but a *valid answer* in `value_as_number`, where on a
screening instrument it is typically the *modal* answer. Hence two null sets. Source
refusal codes (`555`/`777`/`888`/`999`) are dropped separately, which is also why a
`when_value` cut-point over such a scale is written `== n` and never `>= n`.

**Units are declared in config; the data column is a cross-check.** `Quantity.unit` is
required by the phenopacket schema, and an OMOP release routinely leaves `unit_source_value`
blank (two thirds of rows in the synthetic AI-READI release), keeping the unit inside the
truncated label instead.

**Laterality is declared per item, on `Measurement.procedure.body_site`** — the only
laterality slot a GA4GH `Measurement` has. Do not pass `qualifier_concept_id` through: in
AI-READI six autorefraction items are per-eye by name with no qualifier at all, and the same
column on a medical-history row holds a condition name.

**A reference range needs both bounds.** `ReferenceRange.low`/`.high` are proto3 doubles with
no field presence, so a one-sided range reads back as "the normal range is 39 to 0".

**Timestamps must be RFC3339 with a trailing `Z`.** `TimeElement.timestamp.FromJsonString`
rejects `2023-12-12`, `2023-12-12 08:19:00` and `2023-12-12T08:19:00` alike, and the emitter
catches that error and falls back — so an un-normalized value loses every `time_observed`
without anything failing. Normalize with `omop.to_rfc3339` before building a `TimePoint`.

## Time

- Every observation gets a `TimePoint` from its `session_id`. The reader attaches an NCIT
  term for recognized session labels (`ses-baseline`, `ses-followup`); unrecognized session
  ids (stray UUIDs) leave the `TimeElement` unset. Richer precision (timestamp, age) is used
  when present, but the synthetic data carries none.

## Term mappings to HPO (SSSOM)

Separate from the ETL configs above, `mappings/` holds **SSSOM** files that map Bridge2AI-Voice
dataset *terms* to the Human Phenotype Ontology (HPO). These are a standalone, shareable
term-to-term artifact. A row with an empty `when_value` is a pure semantic mapping (the item is
*about* the HPO concept) and produces no output. A row that carries a **`when_value`** condition
is *value-gated*: the pipeline derives a `PhenotypicFeature` from a participant's answer (see
"Conditional (value-gated) mappings" below and [ADR-0002](adr/0002-conditional-hpo-mapping.md)).

- **Namespace.** Dataset terms use the project-local `b2ai:` CURIE prefix (canonical expansions
  live in `src/b2ai_dataset_ingest/ontology/curie_map.py`, derived from the emitter's
  `Resource` registry so `b2ai:`/`HP:` mean the same thing everywhere). A subject id is
  `b2ai:<table>.<column>` — `<table>` is the data-dict file stem (unique across the tree,
  e.g. `confounders`, `phq9`, `parkinsons_disease`), `<column>` the data-dict key; they split
  on the **first** dot (stems and column names never contain a dot).

  **The same id names the same item everywhere — including the ETL configs' assay ids.** A
  questionnaire config that mints `b2ai:phq9-no_interest` while the SSSOM subject is
  `b2ai:phq9.no_interest` produces *two distinct IRIs for one item* (both expand under the
  single `b2ai` Resource), and the derived `PhenotypicFeature`'s `Evidence.reference` can
  then no longer be joined to the `Measurement` it was derived from. Use a dot, never a
  dash. Enforced by `tests/test_config_mappings.py::test_config_b2ai_ids_use_dot_separator`
  and `::test_gated_sssom_subjects_have_a_matching_config_assay`.
- **Files.** One SSSOM/TSV per domain **and object ontology** (`b2ai-voice-signs-symptoms`,
  `b2ai-voice-questionnaires`, `b2ai-voice-conditions`), each self-contained: a `#`-commented
  SSSOM YAML metadata header (`curie_map`, `license`, `subject_source`, `object_source` + pinned
  `object_source_version`, `mapping_tool`) then a TSV of `subject_id, subject_label,
  predicate_id, object_id, object_label, mapping_justification, confidence, comment`. SSSOM/TSV
  only (no JSON/RDF).

  **One object ontology per file, because SSSOM says so.** `object_source` is a mapping-*set*-level
  slot, so a set with both `HP:` and `MONDO:` objects cannot declare it honestly. The validator
  reads each file's `object_source`, and `OBJECT_SOURCES` in `ontology/sssom_validate.py` turns it
  into the CURIE prefix every `object_id` must carry and the oaklib adapter that verifies it —
  currently `obo:hp` → `HP:` and `obo:mondo` → `MONDO:`. A file declaring anything else is an
  **error** (`unknown-object-source`), not an unchecked pass: an unrecognised source would
  silently skip the anti-hallucination layer.
- **Phenotype or disease?** A column reporting a **sign or symptom** maps to HPO; a column
  reporting a **diagnosis** maps to MONDO (`b2ai-voice-conditions.sssom.tsv`). A disease column
  cannot `exactMatch` or hierarchically match a phenotype term — `confounders.epilepsy` is not a
  kind of `HP:0001250` *Seizure*, nor the reverse — so where such a column keeps an HPO row for
  its characteristic phenotype, that row is a `relatedMatch`. Where the HPO term denotes the
  *same* concept as the column (`asthma`, `bipolar_disorder`, …), both rows can stay
  `exactMatch`: mapping one subject into two ontologies is normal SSSOM. Established on clinical
  review (2026-08-24), which is also why several voice/laryngology columns stay HPO-only —
  MONDO has no term for them (see that file's header `comment`).
- **Predicates.** `skos:exactMatch` (same concept / exact ontology synonym), `skos:broadMatch`
  (the ontology term is *genuinely broader* and subsumes the source), `skos:narrowMatch` (ontology
  term is a subtype of the source), `skos:relatedMatch` (loose; a `comment` says why).
  `mapping_justification` is `semapv:ManualMappingCuration`. **Conflated "A or B" columns** (a
  single checkbox meaning two distinct concepts, e.g. `pneumothorax_atelectasis`) do **not**
  `broadMatch` to just one sense — that inverts the direction. Either split into one
  `narrowMatch` row per sense, or retarget to a term that truly subsumes both (e.g.
  `no_appetite` "poor appetite or overeating" → `Abnormal eating behavior`). **Direction is
  decided by the concepts, not by how close the wording looks**: if the HPO term is the stronger,
  more specific phenomenon (`Sense of impending doom` vs the item's "something awful might
  happen"), it is a `narrowMatch`, however near-synonymous the two read.
- **No hallucinated terms.** Every `object_id` was found by *searching the object ontology itself*
  (not guessed) and is re-verified by `ontology/sssom_validate.py`: it must exist and not be
  **deprecated** (checked against the `owl:deprecated` flag via `adapter.obsoletes()`, not merely
  the `"obsolete "` label convention — some deprecated terms keep a normal label), and its
  `object_label` must equal the ontology's authoritative label (an *exact* synonym only warns).
  The validator surfaces each loaded release and warns if one differs from that file's
  `object_source_version`. It also enforces structure — known predicates, a **self-contained**
  `curie_map`, in-range confidence, no duplicate triples (across files), well-formed subjects —
  and, when a phenotype data root is supplied, that each `b2ai:` subject names a real data-dict
  column. Run it with `b2ai-ingest validate-mappings` (add `--data-root <phenotype/>` for the
  subject check); it is enforced in CI via the `validation` extra (oaklib) and
  `tests/test_sssom_mappings.py`, which also cross-checks the files with the reference `sssom-py`
  validator. Rationale: LLM-proposed ontology codes are unreliable and must be machine-checked
  before they ship (the same reason the ETL configs' MONDO/LOINC/NCIT terms are verified).

### Conditional (value-gated) mappings

A term→term mapping records that an item is *about* an HPO concept; it does not assert a
participant *has* it — that depends on the answer. One optional column turns a mapping into a
value-gated one that derives a `PhenotypicFeature` (per [ADR-0002](adr/0002-conditional-hpo-mapping.md)):

- **`when_value`** — a condition on the participant's answer. Grammar: comparisons `>=1`, `<=3`,
  `>0`, `<2`, `==0`, `!=0`; string equality `== "Checked"`; membership `in {1,2,3}` /
  `in {"a","b"}`; and `&` conjunction (`>=1 & <=3`). Numeric conditions are evaluated against the
  same **ordinal score** the emitted Measurement carries (resolved from the data dict's
  `choices`); string conditions against the raw cell (case-insensitively). An **empty**
  `when_value` is an inert semantic mapping — additive, changing no output.
- **Only presence is asserted.** There is no `predicate_modifier` column and no
  `excluded = true`: see *Why absence is not asserted* below. The validator **errors**
  (`withdrawn-column`) if the column reappears.
- **Only `skos:exactMatch` and `skos:broadMatch` rows may carry a `when_value`.** Deriving "the
  participant has this phenotype" from an endorsement is sound only when the HPO term is the same
  as, or broader than, what the item asked. A `narrowMatch` row says the HPO term is one sub-sense
  of the item, and an endorsement cannot say which sense was meant; a `relatedMatch` row says
  neither concept subsumes the other. Both stay inert semantic mappings. Validator:
  `ungateable-predicate`.

#### Choosing a cut-point

Two things bear on a `when_value`, and they are not the same:

1. **The instrument's own scoring rule** — what its designers count as a positive endorsement
   (e.g. the ASRS scores its hyperactivity items positive only at *Often*/*Very Often*).
2. **What the HPO term itself requires** — `HP:5200273` *Pathological sadness* is defined as
   sadness "excessive in intensity, duration, or resistance to self-regulation", which a single
   rare day does not meet whatever the instrument says.

They usually agree. **Where they conflict, weight (2)**: the assertion being emitted is about the
HPO term, not about the questionnaire.

**Compare answer labels, never the integers.** Two instruments can gate the same HPO term at the
same *severity* while using different numbers, because their scales differ in length and in kind.
`HP:0012154` *Anhedonia* is gated three ways:

| Item | Scale | Cut-point | Answer it fires at |
| --- | --- | --- | --- |
| `phq9.no_interest` | frequency, 0–3 | `>=1` | "Several days" |
| `dsm5_adult.little_interest` | frequency, 0–4 | `>=2` | "Mild (**Several days**)" |
| `ptsd_adult.losing_interest` | **severity**, 0–4 | `>=2` | "Moderately" |

The first two are the same severity at different integers — `dsm5_adult` has an extra rung at the
bottom ("Slight — rare, less than a day or two"), so its 2 is phq9's 1. Setting both to `>=1`
looks like harmonization and is the opposite: it drops the DSM-5 bar to "rare, less than a day or
two" while leaving PHQ-9 at "several days".

The third cannot be aligned with either: PTSD rates *severity*, not frequency, and no answer on
that scale is equivalent to "several days".

**This is a curator judgment, not a machine check.** An earlier automated test required every
instrument gating a term to share one `when_value` string; it was removed because that premise is
false in both directions — equal integers are not equal severities, and non-commensurable scales
(frequency vs severity) have no equal labels to compare.

#### Why absence is not asserted

`excluded = true` is a strong claim, and an **unqualified** one: a phenopacket carries no time
scope on an excluded feature unless it has an `onset`, and a derived feature gets no `TimeElement`
when the session id is an opaque hash. Emitted from a two-week item it does not say "absent over
the last fortnight" — it says the participant does not have the phenotype.

Nearly every gated item asks about a bounded period, so its lowest answer means *not during that
window*:

| Instrument | Window in the data dict | Published instrument |
| --- | --- | --- |
| PHQ-9, GAD-7, Leicester Cough | two weeks | two weeks |
| DSM-5 adult | mostly absent (paraphrased out) | two weeks |
| ASRS (`adhd_adult`) | none | past 6 months |
| PTSD (`ptsd_adult`) | none | past month |
| Dyspnea Index | none | no window found — unverified |

**The data dict is not authoritative here.** Three instruments record no window, but ASRS and PTSD
are bounded in their published form, so the omission is a gap in the dict rather than a property
of the instrument. Never infer that an item is unbounded from the dict's silence. The clean case
would be a lifetime item — "have you *ever* experienced X?" — and no gated instrument in this
dataset is phrased that way.

**So the absent pole was withdrawn set-wide** on clinical review (2026-08-24), which reached the
same conclusion independently: *"'Not at all' only indicates that the symptom was not reported
during the past two weeks. It does not establish that the phenotype is absent more generally."*
All 26 `predicate_modifier: Not` rows are gone, along with the column and the code that read it
([ADR-0002](adr/0002-conditional-hpo-mapping.md), *Amended 2026-08-27*). A low answer now asserts
nothing at all — the same as a blank cell.

Reinstating absence needs a way to carry the instrument's recall window into the output (via
`PhenotypicFeature.onset`, or a term that is genuinely unbounded), not just a column.

Declare `when_value` once in the file's SSSOM `extension_definitions` metadata; one
`subject_id`/`predicate_id`/`object_id` triple carries at most one row, and a repeat is a
duplicate however its `when_value` differs. Each derived feature is
stamped with provenance: a human-readable `description` and a GA4GH `Evidence` whose
`evidenceCode` is `ECO:0006160` ("self-reported patient statement … in automatic assertion") with
an `ExternalReference` back to the source item — self-report-derived phenotypes stay
distinguishable from clinician-observed findings. The validator checks only that `when_value`
**parses** and sits on a gateable predicate; whether a cut-point is *clinically* right is a
curator judgment (see the `curation-assist` skill). The apply path lives in
`mapping/hpo_rules.py` (loader + derivation) and `mapping/conditions.py` (the grammar); the reader
runs it during questionnaire ingestion. Note the reference `sssom-py` parser **drops**
`when_value` on load, so the pipeline reads mappings with the column-preserving `parse_sssom`
in `mapping/sssom_io.py`.

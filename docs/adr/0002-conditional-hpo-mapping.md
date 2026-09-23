# ADR-0002: Conditional HPO mapping — carry the value-condition in SSSOM, execute in the pipeline

- **Status:** Accepted; decision 3 **withdrawn 2026-08-27** for self-report (see *Amended
  2026-08-27*) and **reinstated 2026-09-23 for measured values only** (see *Amended
  2026-09-23*)
- **Date:** 2026-07-23

## Context

The B2AI→HPO mappings (`mappings/*.sssom.tsv`, see [mapping-conventions](../mapping-conventions.md))
are term→term: `b2ai:phq9.feeling_depressed` → `HP:5200273 Pathological sadness` records that an
item is *about* that phenotype. It does not assert a participant *has* it — that depends on the
answer, a 0–3 ordinal. Turning "answer ≥ threshold" into a present/absent `PhenotypicFeature`
(the ordinal→present/absent policy deferred in the SDD and [ADR-0001](0001-name-architecture-tooling.md))
requires a **conditional, value-gated** mapping.

An earlier draft of this ADR concluded the condition should live only in the pipeline and
**rejected** putting it in SSSOM (on the belief that a custom column forfeits interoperability).
That was revisited after an **OpenScientist** research report designed, implemented, and tested a
value-gated SSSOM approach on a separate branch, and — decisively — after **independent
verification in this repo** of its load-bearing claims. Claims below marked *(verified)* were
reproduced here against the installed stack (Python 3.14.3, `linkml`/`linkml-runtime` 1.11.1,
`sssom` 0.4.17, `sssom-schema` 1.0.0); claims marked *(reported)* come from the OpenScientist
report and are not yet independently checked (its branch is not in this repo).

## Decision

**Carry the value-condition in SSSOM; execute it in the pipeline; assert absence natively; stamp provenance.**

1. **Condition in SSSOM via a `when_value` extension slot.** Declare a custom slot in the SSSOM
   `extension_definitions` metadata and add a `when_value` column (`>=1`, `==0`, `in {1,2,3}`,
   `== "Checked"`, `&` conjunction, …). *(verified)* This is spec-compliant — sssom-py
   `validate(DEFAULT_VALIDATION_TYPES)` passes — and backward-compatible: an SSSOM-core consumer
   silently ignores `when_value` and still reads the pure semantic mapping. A row with **empty**
   `when_value` stays an inert semantic mapping and changes no output, so this is purely additive
   to what we ship today.
2. **Execute in the pipeline, not in SSSOM/LinkML.** A small condition grammar + evaluator
   (Python) parses `when_value` and emits `PhenotypicFeature`s from answered cells. Do **not** use
   LinkML `equals_expression`/`--infer` to execute — *(verified)* it crashes on our Python
   (`AttributeError: module 'ast' has no attribute 'Num'`, removed in 3.12+; even a trivial
   expression fails). LinkML stays the **definition/validation** layer: a `ValueCondition` class
   mirroring LinkML `slot_conditions` operators, used to structure/validate the condition, not run it.
3. ~~**Absent pole via the standard `predicate_modifier: Not`**~~ **(withdrawn 2026-08-27)** → phenopacket
   `PhenotypicFeature.excluded = true` (phenopackets-native negation), not a new invented column.
   *(verified)* `predicate_modifier` is a core slot preserved by sssom-py.
4. **Provenance on every derived feature.** Attach a human-readable description (source item,
   label, predicate, `when_value`, confidence) and a GA4GH `Evidence` with
   `evidenceCode = ECO:0006160` ("self-reported patient statement evidence used in automatic
   assertion" — *(verified)* real, non-obsolete via OLS4) + an `ExternalReference` to the source
   item CURIE, plus a versioned ECO `Resource`. A self-report-derived phenotype must never be
   weighted like a clinician-observed diagnosis.
5. **Parser constraint.** Applying conditions requires a parser that **preserves** extension
   columns. *(verified)* sssom-py **drops** them on load (retaining only `extension_definitions`
   in metadata), so it cannot carry the condition — the apply path uses our in-repo `parse_sssom`,
   which preserves all columns.
6. **`when_value` is curator judgment.** The validator checks that `when_value` **parses** and
   sits on a predicate that can carry one; it does **not** check the cut-point is clinically
   right. Threshold choice needs expert sign-off — the `curation-assist` skill authors it.

> **Amended 2026-08-27 (clinical review).** A clinician review of the shipped questionnaire
> mappings (Sek Won Kong, 2026-08-24) rejected the absent pole, and decision 3 is **withdrawn**:
>
> > "An answer of 'Not at all' does not necessarily indicate the absence of the phenotype in an
> > individual. For example, in the PHQ-9, 'Not at all' only indicates that the symptom was not
> > reported during the past two weeks. It does not establish that the phenotype is absent more
> > generally. My concern is that this could be interpreted computationally as an explicit absence
> > of the phenotype without preserving the instrument-specific time window."
>
> This is the failure mode `mapping-conventions.md` had already flagged as *undecided* under
> *Recall windows*: a derived feature gets no `TimeElement` when the session id is an opaque hash,
> so `excluded = true` emitted from a two-week item reads as unqualified absence. The review
> settles it. All 26 absent-pole rows and the `predicate_modifier` column are gone; the validator
> now **errors** on the column's reappearance (`withdrawn-column`), and the mechanism is out of
> `hpo_rules.py`. The set asserts only presence.
>
> Two rules replace it, both already true of every shipped row:
> - **Only `skos:exactMatch` and `skos:broadMatch` rows may carry a `when_value`** — deriving "the
>   participant has this phenotype" from an endorsement is sound only when the HPO term is the same
>   as, or broader than, what the item asked (validator: `ungateable-predicate`).
> - **One row per `(subject, predicate, object)` triple** — the present/absent pair was the only
>   thing that needed two, so duplicate detection is back to the plain triple.
>
> Decisions 1, 2, 4, 5 and 6 stand. Reinstating absence needs a way to carry the instrument's
> recall window into the output (e.g. `PhenotypicFeature.onset`), not just a column.

> **Amended 2026-09-23 (measured values).** Decision 3 is **reinstated for measured-value rows
> only**, and the mechanism is the one the 2026-08-27 amendment asked for. The AI-READI
> `measurement.csv` set (`mappings/b2ai-aireadi-measurement.sssom.tsv`) gates on a reference
> range over a dated assay: a value inside the interval rules the abnormality out *at that
> observation*, and the derived feature carries the measurement's own time, so the exclusion
> is qualified rather than unbounded. Such rows declare `evidence_code: ECO:0007307` (*direct
> assay evidence used in automatic assertion*, verified against ECO) and may carry the standard
> `predicate_modifier: Not`; the emitted feature is `excluded = true` with that evidence term.
> Two consequences for the validator: (a) the duplicate key is now `(subject, predicate,
> modifier, object)`, so one assay carries at most one present and one absent row per term;
> (b) a `relatedMatch` row may gate when its evidence is a measured value, because the
> phenotype is *defined* by the assay falling outside its range and the gate plus the assay
> entails the term — a stronger warrant than an endorsement, not a weaker one. Self-report
> rows are unchanged: the modifier stays an error (`withdrawn-column`) and only `exactMatch`
> / `broadMatch` gate. Decision 4 extends accordingly: the ECO term stamped on a derived
> feature is the row's `evidence_code`, so a lab-derived phenotype is never mistaken for
> self-report. See `docs/mapping-conventions.md`, "Measured-value (reference-range) mappings".

## Worked example — `b2ai:phq9.feeling_depressed` → HP:5200273 Pathological sadness

Two SSSOM rows (the `when_value` extension column is declared in `extension_definitions`):

```
subject_id                   predicate_id     object_id   when_value
b2ai:phq9.feeling_depressed  skos:exactMatch  HP:5200273  >=1
```

- Answer ≥ 1 ("Several days" … "Nearly every day") → emit `HP:5200273` **present**, with an
  ECO:0006160 self-report `Evidence` and an onset `TimePoint`.
- Answer `== 0` ("Not at all") → nothing is asserted. It denies the symptom over the item's
  two-week window, which is not the same as denying the phenotype (*Amended 2026-08-27*); the
  row that used to emit `excluded` here is gone.
- A blank cell satisfies no condition → nothing is asserted.

*(verified: our `parse_sssom` reads `when_value` on this row; sssom-py drops it but still returns
the mapping and validates it.)*

> **Amended 2026-08-11 (PR #10 review).** The worked example originally used
> `skos:broadMatch` → `HP:0000716 Depression`. HP:0000716 denotes a depressive *episode* — its
> exact synonym is "Depressive episode" and it is [proposed for renaming to
> "Depressive Episode"](https://github.com/obophenotype/human-phenotype-ontology/issues/11460) —
> so it overstates a single PHQ-9 item. The row now targets `HP:5200273 Pathological sadness`,
> whose exact synonyms ("Down in the dumps", "Feeling hopeless all the time") match the item
> text, which also upgrades the predicate to `skos:exactMatch`. The decision this ADR records —
> carry the value-condition in SSSOM, execute it in the pipeline — is unchanged.

## Consequences

- **The condition travels with the mapping** — curator-authored alongside the concept + predicate,
  one shareable artifact, checkable in CI. Better for the curation goal than a separate pipeline
  config divorced from the term mapping.
- **Backward-compatible and additive** — condition-less rows are unchanged; existing consumers and
  our shipped output are unaffected until a `when_value` is authored.
- **Do not rely on sssom-py to carry conditions** — the apply path must use `parse_sssom`. A
  documented constraint, not a blocker (we already have the preserving parser).
- **Execution stays hand-written Python** — flexible for edge cases (bidirectional items,
  per-instrument thresholds); LinkML defines/validates the condition structure but cannot run it
  on our Python until `linkml-runtime` fixes the `ast.Num` (3.12+) bug.
- **Derived features are auditable** — ECO provenance keeps self-report-derived phenotypes
  distinguishable from observed findings.
- **Implementation is a tracked follow-up, not part of this ADR.** The OpenScientist code is
  report-only here; porting/re-implementing the grammar, rule loader, reader hook, provenance
  emitter, the validator `when_value` checks, and the skill's `when_value` authoring step is a
  separate work item to be verified in-repo.

## Alternatives considered

- **Conditions only in the pipeline, none in SSSOM** (this ADR's earlier draft). Rejected: it
  separates the condition from the mapping it modifies (worse for curation/sharing) with **no**
  interoperability benefit — the `extension_definitions` mechanism is *(verified)* backward-
  compatible, so the original rejection's stated reason was false.
- **LinkML `equals_expression` / `linkml-map` for execution.** Rejected now: `equals_expression`
  is *(verified)* broken on our Python (`ast.Num`); `linkml-map` is not installed and its docs
  call the transformation model "not yet fully stable." Revisit if the `ast.Num` fix lands and
  `linkml-map` stabilizes.
- **Reify value-qualified subjects** (`…feeling_depressed.present` → HP:0000716). Achieves the
  same but heavier — subject-minting plus a validator rule — than one `when_value` column.
  Deferred.
- **Free-text `comment`, or a bespoke sidecar condition file.** Rejected *(reported; consistent
  with our validation goals)*: unparseable/invisible to validation, or a redundant second artifact
  to keep in sync.
- **SSSOM `curation_rule_text`.** Fine for *documenting* the rule as provenance, but not
  executable or machine-checkable — kept as optional provenance, not the mechanism.

## Provenance

Design adopted from an OpenScientist research report (2026-07); the interoperability, spec-
compliance, `predicate_modifier`, ECO:0006160, and `parse_sssom`-vs-sssom-py claims were
independently re-verified in this repo before acceptance. Supersedes the earlier
"conditional-hpo-derivation" draft of ADR-0002.

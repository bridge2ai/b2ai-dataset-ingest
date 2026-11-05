# Value conditions — authoring `when_value`

> The `when_value` **grammar** and how the pipeline runs it live in
> `docs/mapping-conventions.md` (and [ADR-0002](../../../docs/adr/0002-conditional-hpo-mapping.md)).
> This file is the **curation heuristic**: when to add a condition and how to pick the cut-point.
> Experts: add worked examples as calls get settled.

A term mapping (`b2ai:phq9.feeling_depressed → HP:5200273 Pathological sadness`) says the item is
*about* that phenotype. It does **not** say a participant *has* it — that depends on the answer. A
`when_value` is the gate that turns an answer into a **present** `PhenotypicFeature`.

> **Only presence is ever asserted.** Absent poles (`predicate_modifier: Not` →
> `excluded = true`) were withdrawn set-wide on clinical review (2026-08-24): a questionnaire's
> lowest answer denies the symptom *within the instrument's recall window*, and that window does
> not survive into the phenopacket, so `excluded = true` read as an unqualified "does not have
> this phenotype". Never author one; the validator errors on the column
> (`withdrawn-column`). An answer below the gate now asserts nothing, exactly like a blank cell.
>
> **This is the self-report rule.** Measured-value rows (`evidence_code: ECO:0007307`, a lab or
> vital against a reference range) are the exception: there a `relatedMatch` may gate and a `Not`
> row may assert the absent pole, because the exclusion carries the assay's own time — see
> `docs/mapping-conventions.md`, *Measured-value (reference-range) mappings*. Even there, do not
> author new `Not` rows for now: the clinical reviewer has asked (2026-11-03) that a result within
> normal limits read as one "normal" assertion rather than "not increased and not decreased", and
> the representation is under review.

## When to add one (and when not to)

- **Add** a `when_value` when the item is an in-scope sign/symptom whose **answer encodes
  severity/frequency** (an ordinal screener item like PHQ-9/GAD-7, or a checkbox), and asserting
  the phenotype **present** is the intended output.
- **Leave it empty** when the mapping is only meant to record *aboutness* (a shareable semantic
  link), or when the item is out of scope (admin, score, psychosocial impact, task data — see
  `scope-checklist.md`). An empty `when_value` changes no output; it is always safe.
- **Don't** invent a condition to force a phenotype out of an item that doesn't support one. If
  no answer cleanly establishes the phenotype, say so and leave it semantic-only.

## Picking the cut-point (the part that needs sign-off)

The threshold is a **clinical judgment**, not a lexical one — the validator will not catch a
wrong cut-point. Default reasoning, to be confirmed by a domain expert:

- **Ordinal 0–3 screener items** (PHQ-9, GAD-7: "Not at all / Several days / More than half /
  Nearly every day): the conventional cut-point is **`>=1`** — any endorsement above "Not at all"
  asserts the symptom. A stricter clinician may prefer `>=2`; record which and why. Do not
  silently pick one.
- **Checkbox items** ("Checked"/"Unchecked"): `== "Checked"` present; "Unchecked" asserts nothing.
- **One item is not a construct.** A gate says *this answer establishes this phenotype*. Where the
  HPO term names a multi-indicator construct that the instrument itself scores over several items,
  one item cannot carry it — `adhd_adult.interrupt_others` → HP:0100710 *Impulsivity* was ungated
  on clinical review for exactly this ("≥3 of this item alone does not support the presence of
  impulsivity"): the ASRS scores impulsivity across several items and interrupting has
  non-impulsive causes. Leave the semantic row, drop the gate.

Flag the cut-point as **needs expert sign-off** in the review artifact, separately from the
(auto-verified) ontology code.

## The predicate decides whether you may gate at all

Which rows may carry a `when_value` is not a case-by-case feel — it follows from the direction
you already chose in `predicate-rules.md`. Endorsement travels **up** the hierarchy, so a gate is
sound only when the HPO term is the same as, or broader than, what the item asked. **The validator
enforces this** (`ungateable-predicate`).

| Predicate | May carry a `when_value`? | Why |
| --- | --- | --- |
| `exactMatch` | ✅ | item ≡ phenotype, so an endorsement *is* an assertion of the term |
| `broadMatch` (object broader) | ✅ | endorsement travels up: a specific instance present ⇒ the umbrella present |
| `narrowMatch` (object narrower) | ❌ | endorsing the broad item cannot say *which* narrow sense was meant |
| `relatedMatch` | ❌ | only partial overlap — neither concept subsumes the other |

Worked examples:

- `phq9.trouble_sleeping` → HP:0002360 *Sleep disturbance* (broad). Endorsing insomnia or
  hypersomnia does establish *some* sleep disturbance, so the gate is sound.
- `phq9.feeling_bad_self` → HP:0031469 *Low self-esteem* + HP:6000011 *Guilt* (narrow ×2).
  A `>=1` answer cannot say which half of the conflated item was endorsed, so asserting either
  would be a coin flip — ungated, on both rows.
- `vhi10.strain_voice` → HP:0001618 *Dysphonia* (related). Ungated: HPO has no term for
  effortful phonation (searched "strained voice", "vocal strain", "vocal fatigue", "phonation",
  "effortful speech" — no hits), and a relatedMatch cannot carry a gate.

**Consequence: the predicate is load-bearing for output.** A wrong `broad`/`narrow` direction is
not a metadata blemish; it decides whether a participant's phenopacket asserts the phenotype at
all. When authoring a gate, re-read the object's definition and confirm the direction before
trusting it — and beware a `comment` that argues one direction while the `predicate_id` says the
other. `gad7_anxiety.afraid_of_things` → HP:0033845 *Sense of impending doom* was labelled
`broadMatch` while its own comment argued the HPO term is *stronger* than the item
("life-threatening or tragic" vs "something awful"), which is the **narrow** direction; the
clinical review caught it and the row is now `narrowMatch`, ungated. **When the comment and the
predicate disagree, the comment is usually the one that did the thinking.**

## Writing the rows

Declare the slot once in the file's SSSOM metadata:

```yaml
# extension_definitions:
#   - slot_name: when_value
#     property: b2ai:when_value
#     type_hint: xsd:string
```

Then the gate is one column on the row you already wrote:

```
subject_id                   predicate_id     object_id   when_value
b2ai:phq9.feeling_depressed  skos:exactMatch  HP:5200273  >=1
```

- One `(subject, predicate, object)` triple carries **one** row; a repeat is a duplicate however
  its `when_value` differs.
- The `predicate` stays the term-mapping predicate you chose in step 5; the condition rides
  alongside it, it does not replace it — and per the table above, only `exactMatch` and
  `broadMatch` rows may carry one at all.

## Guardrails

- The validator checks the condition **parses** and sits on a gateable predicate — nothing more.
  A green gate is not sign-off on the threshold.
- Numeric conditions are evaluated against the **ordinal score** (the same value the emitted
  Measurement carries), resolved from the data dict's `choices`. If an item has no resolvable
  ordinal, a numeric `when_value` will never fire — use a string/`in {...}` condition instead.
- **A membership table is gated `== 1`.** Where a row exists only for a Yes (OMOP
  `condition_occurrence`, a Voice `diagnosis/<condition>.tsv`), there is no answer cell to test; the
  reader feeds each row to the rule as the value `1`. Write `== 1`, not `>=1` — and not nothing: an
  empty `when_value` makes the row inert, silently.
- Every derived feature is auto-stamped with ECO self-report provenance; you do not author that.

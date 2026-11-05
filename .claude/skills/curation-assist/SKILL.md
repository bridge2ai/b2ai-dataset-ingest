---
name: curation-assist
description: Assist an expert-in-the-loop curator mapping Bridge2AI dataset terms (data-dictionary columns) to ontology terms (HPO, MONDO, LOINC) as SSSOM. Use when proposing new term mappings, deciding whether a column is in scope, choosing a SKOS predicate, or reviewing candidate mappings; it proposes and justifies against project conventions and gates every code through a deterministic verifier so ontology codes are never hallucinated, while a human makes the final call.
user-invocable: true
---

# Curation assistant (expert-in-the-loop)

Mapping a dataset term to an ontology term is **two questions**, and this skill keeps them
separate:

1. **Which concept does this column mean, and how close is the match?** -- *judgment.* There
   is often no single right answer; a curator (ideally a domain expert) decides. This skill
   *proposes and justifies*; it never silently commits a judgment call.
2. **What is the real code/label for that concept, and is it current?** -- *deterministic.*
   Never type an ontology id from memory. Search the ontology, take the code from it, and let
   the validator re-check it. Reproducible.

The whole point is to make step 1 **more consistent** (rule-guided, reviewable) without
pretending it is deterministic, and to make step 2 **airtight**.

## When to use / not use

- **Use** when: proposing mappings for new columns/tables; deciding if a column is in scope;
  picking a predicate; or reviewing/critiquing existing candidate mappings.
- **Don't use** for the mechanics of the pipeline itself (reading tables, emitting
  phenopackets) -- that is ordinary code. This skill is only the *curation judgment* layer.

## Workflow

1. **Scope filter.** For each column, read its data-dictionary `description` (not just the
   name) and apply `references/scope-checklist.md`. Most columns are *not* phenotypes
   (admin, scores, psychosocial impact, triggers, task data, history) -- record those as
   out-of-scope with the bucket, and move on. Only sign/symptom/finding columns proceed.
2. **Name the concept** from the description, in ontology-agnostic words (e.g. "poor appetite
   or overeating" -> the concept is *abnormal eating behaviour*, not literally "poor appetite").
3. **Search, don't guess.** Run `scripts/search_hpo.py "<concept>"` (or `--ontology mondo`).
   Take candidate ids + authoritative labels from the output. If the source description
   *contradicts* the lexical match (classic trap: "dizziness" vs HPO *Vertigo* = spinning),
   **flag it, don't auto-accept.**
4. **Inspect the term before committing to it** — `scripts/search_hpo.py <CURIE>` prints the
   full definition, every synonym class, the editor comment and the is_a parents. A label is
   not enough to judge a term: HPO *Depression* (HP:0000716) reads like a symptom but its exact
   synonym is "Depressive episode", i.e. a syndrome that overstates a single questionnaire item.
   Conversely the synonym list is often what *justifies* an `exactMatch` the label would never
   suggest. Both directions are the "granularity trap" in `references/predicate-rules.md`. For
   a **standard instrument**, also diff against an independent curation if one exists (see the
   same file) — it catches omissions and judgment calls the validator cannot.
5. **Choose a predicate** per `references/predicate-rules.md` (exact / broad / narrow /
   related, and the "A-or-B conflation" rule). When unsure, prefer the weaker predicate + a
   comment. Before reusing a term that was rejected elsewhere — or changing one — check the
   **kind** of each column using it: a screener *item* and a reported *diagnosis* can share
   wording and still need different terms, so never blanket-replace across the mapping files.
6. **Author the value condition (`when_value`), when the item should derive a phenotype.**
   A term mapping only says the item is *about* a concept; a `when_value` says *which answers
   assert it is present*. Follow `references/when-value.md`: pick the cut-point, add the column
   to the row you already wrote, and flag the threshold as **needs expert sign-off** -- the
   validator checks that it *parses*, never that it is clinically right. Leave `when_value`
   empty for a purely semantic mapping. For self-report rows **only presence is ever asserted**
   (absent poles were withdrawn on clinical review) and only `exactMatch`/`broadMatch` rows may
   carry a gate; measured-value rows differ, see `references/when-value.md`.
7. **Emit a review artifact**, not a fait accompli -- one row per candidate in the format in
   `references/review-artifact.md`: proposal, rationale, alternatives, confidence, and an
   explicit split of *auto-verified* (code is real/current) vs *needs expert sign-off*
   (concept + predicate + `when_value` cut-point).
8. **Run the deterministic gate.** Nothing ships until
   `uv run b2ai-ingest validate-mappings --data-root <phenotype/>` is clean (existence,
   `owl:deprecated`, label match, structure, subject-column existence, `when_value` parses and
   sits on a gateable predicate). This is the guarantee; treat a red gate as blocking.
9. **Close the loop.** When an expert overrides a proposal, add the decision back into
   `references/scope-checklist.md`, `references/predicate-rules.md`, or
   `references/when-value.md` as a rule or worked example. Over time this shrinks the variance
   in step 1 -- the reason this skill exists.

## Guardrails (do not skip)

- Codes are the ontology's, never yours. If `search_hpo.py` returns nothing, the concept may
  be out of HPO scope -- say so; don't invent a code.
- `exactMatch` only when the source term is a verbatim ontology label or **exact** synonym;
  otherwise broad/narrow/related.
- A green validator means *the codes are real and correctly labelled*, and that any
  `when_value` **parses** -- it does **not** mean the concept, the predicate, or the
  cut-point is right. Keep the human sign-off visible; don't let "the validator passed" stand
  in for review.
- `mapping_justification` stays `semapv:ManualMappingCuration` -- these are curated, and the
  files should say so.

## Sources of truth (cross-reference, don't duplicate)

- Format + predicate spec + how the validator works: `docs/mapping-conventions.md`.
- The deterministic gate: `b2ai-ingest validate-mappings` (src/.../ontology/sssom_validate.py).
- Scope buckets and predicate heuristics with worked examples: the `references/` files here --
  these are **meant to be edited by domain experts** in PRs.

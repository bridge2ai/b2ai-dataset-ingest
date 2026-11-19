# ADR-0001: Repo name, architecture, and tooling

- **Status:** Accepted
- **Date:** 2026-06-12

## Context

We are starting a project to convert Bridge2AI datasets into the GA4GH Phenopacket schema.
Phenopackets is the **committed NIH deliverable**, but it may not be the only output target
long-term. The pilot is the Bridge2AI-Voice dataset (developed against public synthetic
data); the next dataset will be AI-READi. We need a name and a structural approach before
writing conversion logic.

## Decision

1. **Name:** `b2ai-dataset-ingest` — target-neutral so the name doesn't bind us to a single
   output format, dataset-general (voice, AI-READi, …), and matching house style
   (`<project>-<function>`, kebab-case).
2. **Architecture:** a normalized **intermediate representation (IR)** plus **pluggable
   emitters**:

       raw tables -> source reader -> YAML mapping engine -> canonical IR -> emitter(s)

   The phenopacket emitter is the first and committed target; the IR is slightly more
   abstract than any one target so emitters stay thin.
3. **Mappings:** declarative **YAML** config (column→concept, condition→MONDO, item→HPO/LOINC),
   per dataset under `config/<dataset>/`, with shared value sets under `config/shared/`.
4. **Tooling:** **uv** for packaging and environments (`pyproject.toml` + `uv.lock`).
5. **Libraries:** official GA4GH `phenopackets` package (protobuf, schema v2) for output;
   Pydantic v2 for the IR.

## Consequences

- Adding AI-READi = a new `sources/aireadi/` reader + its own `config/aireadi/`, reusing the
  mapping engine and emitter. A second output target = a new emitter, reusing all mappings.
- Slightly more upfront structure than a direct TSV→phenopacket script, justified by the
  multi-dataset, possibly-multi-target scope.
- The IR is Pydantic v1-of-the-project; if we later want a documented, language-neutral
  schema it is the natural thing to promote to **LinkML** (see "Alternatives").

## Alternatives considered

- **Direct TSV → phenopacket** (no IR): simpler now, but would need refactoring the moment a
  second target appears. Rejected given the stated multi-target possibility.
- **Python transform modules instead of YAML**: more flexible for edge cases but less
  inspectable and harder to reuse across datasets. Rejected as the default; Python hooks can
  still be added later if YAML proves insufficient.
- **LinkML schema for the IR now**: fits the team's ecosystem and would document the IR, but
  adds upfront complexity. Deferred — start with Pydantic, revisit if the IR stabilizes.
  *(2026-10-22: the IR stabilised through the AI-READI flagship work and now has a LinkML
  contract, `schema/b2ai_ir.yaml`, held in lockstep with `model/core.py` by
  `tests/test_ir_schema.py`. Pydantic remains the executable model; see ADR-0002, amended
  2026-10-22.)*

# Documentation

Written design/decision/plan artifacts for `b2ai-dataset-ingest`.

| Folder | What goes here |
| --- | --- |
| [`design/`](design/) | **Software Design Documents (SDDs)** — fuller write-ups of a feature/pipeline. Start from [`design/_template.md`](design/_template.md). |
| [`adr/`](adr/) | **Architecture Decision Records** — one short, numbered doc per decision (Nygard format). Start from [`adr/_template.md`](adr/_template.md). |
| [`plans/`](plans/) | **Implementation plans** — step-by-step plans for a chunk of work. Start from [`plans/_template.md`](plans/_template.md). |
| [`mapping-conventions.md`](mapping-conventions.md) | How YAML mappings and ontology terms are authored. |

## How to add one

- **ADR:** copy `adr/_template.md` to `adr/NNNN-short-title.md` (next number), set status,
  keep it short. Supersede rather than rewrite history.
- **SDD:** copy `design/_template.md` to `design/<feature>.md`.
- **Plan:** copy `plans/_template.md` to `plans/<dated-or-named>.md`.

## Index

- ADR-0001 — [Repo name, architecture, and tooling](adr/0001-name-architecture-tooling.md)
- ADR-0002 — [Conditional HPO mapping — value-condition in SSSOM, executed in the pipeline](adr/0002-conditional-hpo-mapping.md) *(accepted)*
- SDD — [Bridge2AI-Voice phenotype → phenopacket pipeline](design/voice-ingest.md) *(draft)*
- SDD — [Bridge2AI AI-READI (OMOP CDM) → phenopacket pipeline](design/aireadi-ingest.md) *(implemented)*

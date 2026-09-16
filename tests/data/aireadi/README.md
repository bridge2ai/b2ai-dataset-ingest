# AI-READI OMOP fixture

A hand-crafted, minimal AI-READI-shaped tree used to exercise the OMOP reader in CI.

**Every value here is invented.** Nothing is excerpted from, derived from, or sampled out of
the real AI-READI release or the VUMC synthetic release. That is a licence requirement, not
tidiness: the AI-READI Data License (WashU v2.0) extends to data that has been "excerpted or
otherwise altered", so a trimmed slice of the real tables would still be licensed Data and
could not be committed to a public repo. What *is* freely reusable is the release's
*vocabulary* — table names, column names, REDCap variable names and OMOP concept ids — which
AI-READI publishes under CC-BY-4.0 at docs.aireadi.org. Rows are Data; schema is not.

`person_id`s are 6-digit `9000xx`. Real mini ids are 4-digit and VUMC synthetic ids start at
0, so a fixture row can never be mistaken for either.

## What each row is here to exercise

| Trap | Where |
| --- | --- |
| Two leading index columns (`""` and `X`) | `measurement.csv` — the mini shape. The VUMC release ships only one, so the reader must never key on header equality. |
| Item key is the REDCap variable, not the concept id | `bp1_sysbp_vsorres` and `bp2_sysbp_vsorres` share concept `3004249`; keying on the concept would merge two readings. |
| `description` keeps same-assay readings apart | the same two rows: both carry `LOINC:8480-6` after mapping. |
| A `*_source_value` with no comma | `moca_total_score` — 30 measurement items have none. |
| A 49-character truncated label | `import_hba1c, Hemoglobin A1c/Hemoglobin.total in` |
| A censored value (`operator_concept_id = 4171756`) | the `import_nt_probnp` row — must be dropped, not reported as a measured 36.0. |
| An operator of `0` ("not recorded") that must still emit | every ophthalmic row. Gating on "must equal 4172703" would delete them. |
| A refusal sentinel in `value_as_number` | `999.0` on 900003's `import_hba1c` — dropped and counted. |
| Laterality from the qualifier column | `viaodplog` / `viaosplog` (`45876703` / `45883829`). |
| Laterality with **no** qualifier | `viaodsph` — per-eye by name only, which is why the config declares the side. |
| A one-sided reference range | `import_creatinine` has `range_high` but no `range_low` — must be dropped, since proto3 would read the missing bound back as 0. |
| A real two-sided reference range | `import_hba1c`. |
| A scoring range that is *not* a reference range | `moca_total_score` (0–30) — the item is unmapped in v1, so it also covers the unmapped path. |
| `visit_occurrence_id = 0` | 900002's `bp1_diabp_vsorres` — falls back to the row's own date. |
| A participant only in `measurement.csv` | `900004` — must still emit a phenopacket. |
| A participant with no conditions | `900003`. |
| A duplicate condition row | 900002 has `mhterm_dm2` twice — one Disease, not two. |
| A configured-but-unresolved condition | `mhoccur_ua` — skipped, never emitted as a `TODO` CURIE. |
| A condition with no config entry at all | `mhoccur_nonesuch` — counted as unmapped. |
| Fully redacted sex | `gender_concept_id = 0` on every `person.csv` row — AI-READI states that sex and race/ethnicity are removed from published releases. |

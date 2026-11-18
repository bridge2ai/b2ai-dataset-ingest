# The mapping review loop

Mappings are curated in SSSOM files under `mappings/`, verified by `validate-mappings`
against pinned ontology releases, and merged through pull requests. The clinical reviewer
does not work in GitHub. This is the loop that lets them review in a spreadsheet without
any of those guarantees moving.

```
mappings/x.sssom.tsv  --push-->  review sheet (CSV or Google Sheet)
                                   reviewer fills: decision, new term, new gate, comment
mappings/x.sssom.tsv  <--pull--  review sheet      (validator must be clean to write)
          |
          +--> git branch, PR with the diff and the reviewer's comments
```

## Push

```bash
scripts/sssom_sheet.py push mappings/b2ai-aireadi-measurement.sssom.tsv --to review.csv
# or, with the Sheets scope authorised for the gws CLI:
scripts/sssom_sheet.py push mappings/b2ai-aireadi-measurement.sssom.tsv --to gws:<spreadsheetId>
```

The sheet carries every SSSOM column (read-only for the reviewer) plus six review columns:

| column | what the reviewer puts there |
| --- | --- |
| `row_key` | leave alone — the stable identity the pull matches on, so sorting and filtering the sheet is safe |
| `decision` | `accept`, `reject` or `change`; blank means not reviewed yet |
| `new_object_id`, `new_object_label` | for `change`: the replacement term (the validator checks it exists and the label matches) |
| `new_when_value` | for `change`: a replacement gate, or blank to keep the current one |
| `reviewer_comment` | free text; appended to the row's `comment` with the date |

## Pull

```bash
scripts/sssom_sheet.py pull mappings/b2ai-aireadi-measurement.sssom.tsv \
    --from review.csv --reviewer orcid:0000-0002-XXXX-XXXX          # dry run
scripts/sssom_sheet.py pull ... --write                               # apply
```

- `accept` stamps `reviewer_id` and `mapping_date` (standard SSSOM slots) on the row.
- `change` replaces the term (and the gate, if given) and stamps the same.
- `reject` removes the row from the set and appends it, with the reason, to
  `mappings/rejected/<set>.rejected.sssom.tsv`, so a rejected proposal is not re-proposed.
- Unreviewed rows are untouched.
- Nothing is written unless `validate-mappings` is structurally clean on the result; the
  CI run on the PR then adds the ontology check against the pinned release.

Then: branch, commit, open a PR whose body is the pull's summary line and the reviewer's
comments. The reviewer's name goes on the rows, not just on the PR.

## Candidates from OMOP2OBO

`scripts/import_omop2obo.py` turns the OMOP2OBO Zenodo mapping sets into
`mappings/candidates/omop2obo-aireadi-{conditions,measurement}.sssom.tsv`, each proposal
labelled `agree` / `differs` / `new` against what the repo ships. The candidates directory
is outside the apply path; a candidate becomes a shipped row only by being pushed through
this loop and accepted. OMOP2OBO is a candidate generator, not an oracle: see
`docs/plans/0002-aireadi-flagship-ingest.md` for the assessment (24/30 condition ids and
14/35 LOINC codes covered; some proposals wrong).

## Why a sheet and not a UI

A sheet already exists, the reviewer already uses it, and every guarantee lives in the
validator and CI rather than in the editing surface. If one review cycle through this loop
shows the sheet is the bottleneck, a UI is a thin front end over the same `push`/`pull`
functions, not a replacement for the SSSOM files.

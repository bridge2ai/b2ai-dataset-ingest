#!/usr/bin/env python3
"""Round-trip a SSSOM mapping set through a review sheet, so a reviewer never needs GitHub.

The clinical reviewer works in Google Sheets. The cheapest loop that keeps every guarantee
this repo has -- codes verified against the pinned release, gates that parse, CI -- is to
push the SSSOM rows into a sheet with a few review columns, let the reviewer fill those in,
pull the decisions back into the TSV, run the validator, and open a PR from the diff. This
script is the push and the pull; the sheet is the review surface, the TSV stays the truth.

    sssom_sheet.py push  mappings/x.sssom.tsv  --to review.csv            # or --to gws:<sheet-id>
    sssom_sheet.py pull  mappings/x.sssom.tsv  --from review.csv --reviewer orcid:0000-...
                                               [--write] [--rejected mappings/rejected/]

Review columns appended to every row (the rest of the row is read-only on the sheet):

    row_key           stable identity: subject | predicate | modifier | object | when_subject
    decision          accept | reject | change | (blank = not reviewed)
    new_object_id     for `change`: the replacement term (verified on pull)
    new_object_label  for `change`
    new_when_value    for `change`: a replacement gate, or blank to keep
    reviewer_comment  free text, appended to the row's comment on accept/change

On pull, an accepted row gets `reviewer_id` (a standard SSSOM slot) and the date; a changed
row gets its new object / gate plus the same; a rejected row is removed from the set and
written to a `.rejected.tsv` beside it so the decision is not lost. Unreviewed rows are left
untouched. Nothing is written unless `--write` is passed and the structural validator is
clean on the result.

Transports: a CSV file (upload / download by hand, or via the Drive connector), or a Google
Sheet through the `gws` CLI (`gws sheets spreadsheets values ...`), which needs the Sheets
scope authorised. The merge logic is transport-independent and is what the tests cover.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from b2ai_dataset_ingest.mapping.sssom_io import parse_sssom  # noqa: E402
from b2ai_dataset_ingest.ontology.sssom_validate import validate_paths  # noqa: E402

REVIEW_COLUMNS = [
    "row_key",
    "decision",
    "new_object_id",
    "new_object_label",
    "new_when_value",
    "reviewer_comment",
]
DECISIONS = {"accept", "reject", "change"}
KEY_COLUMNS = ("subject_id", "predicate_id", "predicate_modifier", "object_id", "when_subject")


def row_key(row: dict[str, str]) -> str:
    return " | ".join((row.get(c) or "").strip() for c in KEY_COLUMNS)


# --------------------------------------------------------------------------- push
def to_review_rows(
    rows: list[dict[str, str]], header: list[str]
) -> tuple[list[str], list[list[str]]]:
    columns = header + REVIEW_COLUMNS
    out = []
    for row in rows:
        values = [row.get(c, "") for c in header]
        values += [row_key(row), "", "", "", "", ""]
        out.append(values)
    return columns, out


def push(sssom: Path, target: str) -> None:
    metadata, rows = parse_sssom(sssom)
    header = _header(sssom)
    columns, values = to_review_rows(rows, header)
    if target.startswith("gws:"):
        _gws_write(target[4:], columns, values)
    else:
        with open(target, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            writer.writerows(values)
    print(f"pushed {len(values)} rows from {sssom.name} to {target}")


# --------------------------------------------------------------------------- pull
def merge(
    rows: list[dict[str, str]],
    review: list[dict[str, str]],
    reviewer: str,
    today: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, int]]:
    """Apply review decisions to ``rows``; return (kept, rejected, tally).

    Rows are matched on ``row_key``, never on position, so a reviewer who sorts or filters
    the sheet changes nothing. An unknown decision word is an error rather than a silent skip.
    """
    decisions = {r["row_key"]: r for r in review if (r.get("row_key") or "").strip()}
    kept: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    tally = {"accept": 0, "reject": 0, "change": 0, "unreviewed": 0}
    for row in rows:
        verdict = decisions.get(row_key(row))
        decision = (verdict or {}).get("decision", "").strip().lower()
        if not verdict or not decision:
            kept.append(row)
            tally["unreviewed"] += 1
            continue
        if decision not in DECISIONS:
            raise ValueError(f"unknown decision {decision!r} on {row_key(row)}")
        comment = (verdict.get("reviewer_comment") or "").strip()
        updated = dict(row)
        updated["reviewer_id"] = reviewer
        updated["reviewer_label"] = updated.get("reviewer_label", "")
        updated["mapping_date"] = today
        original = (row.get("comment") or "").strip()
        if comment:
            updated["comment"] = f"{original} REVIEW {today}: {comment}".strip()
        if decision == "reject":
            updated["comment"] = f"{original} REJECTED {today} by {reviewer}: {comment}".strip()
            rejected.append(updated)
            tally["reject"] += 1
            continue
        if decision == "change":
            new_id = (verdict.get("new_object_id") or "").strip()
            if not new_id:
                raise ValueError(f"`change` without new_object_id on {row_key(row)}")
            updated["object_id"] = new_id
            updated["object_label"] = (verdict.get("new_object_label") or "").strip()
            new_gate = (verdict.get("new_when_value") or "").strip()
            if new_gate:
                updated["when_value"] = new_gate
            tally["change"] += 1
        else:
            tally["accept"] += 1
        kept.append(updated)
    return kept, rejected, tally


def pull(
    sssom: Path, source: str, reviewer: str, write: bool, rejected_dir: Path | None
) -> int:
    metadata, rows = parse_sssom(sssom)
    header = _header(sssom)
    review = _gws_read(source[4:]) if source.startswith("gws:") else _read_csv(Path(source))
    today = dt.date.today().isoformat()
    kept, rejected, tally = merge(rows, review, reviewer, today)
    for slot in ("reviewer_id", "reviewer_label", "mapping_date"):
        if slot not in header and any(r.get(slot) for r in kept):
            header.append(slot)
    print(
        f"{sssom.name}: {tally['accept']} accepted, {tally['change']} changed, "
        f"{tally['reject']} rejected, {tally['unreviewed']} unreviewed"
    )
    candidate = sssom.with_suffix(".reviewed.tmp")
    _write_sssom(candidate, sssom, header, kept)
    result = validate_paths([candidate], check_ontology=None)
    print(result.render())
    if result.errors:
        candidate.unlink()
        print("validator errors: nothing written", file=sys.stderr)
        return 1
    if not write:
        candidate.unlink()
        print("dry run: pass --write to apply")
        return 0
    candidate.replace(sssom)
    if rejected:
        target_dir = rejected_dir or sssom.parent / "rejected"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / sssom.name.replace(".sssom.tsv", ".rejected.sssom.tsv")
        existing = parse_sssom(target)[1] if target.exists() else []
        _write_sssom(target, sssom, header, existing + rejected)
        print(f"wrote {len(rejected)} rejected row(s) to {target}")
    print(f"wrote {sssom}")
    return 0


# --------------------------------------------------------------------------- IO
def _header(sssom: Path) -> list[str]:
    for line in sssom.read_text().splitlines():
        if line and not line.startswith("#"):
            return line.split("\t")
    return []


def _write_sssom(target: Path, source: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    meta = [ln for ln in source.read_text().splitlines() if ln.startswith("#")]
    with open(target, "w") as fh:
        fh.write("\n".join(meta) + "\n")
        fh.write("\t".join(header) + "\n")
        for row in rows:
            fh.write("\t".join((row.get(c) or "").replace("\t", " ") for c in header) + "\n")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _gws_write(sheet_id: str, columns: list[str], values: list[list[str]]) -> None:
    body = {"range": "A1", "majorDimension": "ROWS", "values": [columns] + values}
    subprocess.run(
        [
            "gws", "sheets", "spreadsheets", "values", "update",
            "--params",
            json.dumps({"spreadsheetId": sheet_id, "range": "A1", "valueInputOption": "RAW"}),
            "--json", json.dumps(body),
        ],
        check=True,
    )


def _gws_read(sheet_id: str) -> list[dict[str, str]]:
    out = subprocess.run(
        [
            "gws", "sheets", "spreadsheets", "values", "get",
            "--params", json.dumps({"spreadsheetId": sheet_id, "range": "A:ZZ"}),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    values = json.loads(out).get("values") or []
    if not values:
        return []
    columns = values[0]
    return [
        dict(zip(columns, row + [""] * (len(columns) - len(row)), strict=False))
        for row in values[1:]
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("push", help="SSSOM -> review sheet")
    p.add_argument("sssom", type=Path)
    p.add_argument("--to", required=True, help="a CSV path, or gws:<spreadsheetId>")
    q = sub.add_parser("pull", help="review sheet -> SSSOM")
    q.add_argument("sssom", type=Path)
    q.add_argument(
        "--from", dest="source", required=True, help="a CSV path, or gws:<spreadsheetId>"
    )
    q.add_argument("--reviewer", required=True, help="reviewer CURIE, e.g. orcid:0000-0002-...")
    q.add_argument("--write", action="store_true", help="apply (default: dry run)")
    q.add_argument("--rejected", type=Path, default=None, help="where rejected rows go")
    args = parser.parse_args()
    if args.command == "push":
        push(args.sssom, args.to)
        return 0
    return pull(args.sssom, args.source, args.reviewer, args.write, args.rejected)


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["merge", "row_key", "to_review_rows", "REVIEW_COLUMNS"]
_ = io  # keep the import for transports that stream

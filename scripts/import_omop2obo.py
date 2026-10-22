#!/usr/bin/env python3
"""Turn the OMOP2OBO released mapping sets into candidate SSSOM rows for review.

OMOP2OBO (callahantiff/OMOP2OBO; Callahan et al.) published, on Zenodo, mappings from OMOP
standard concepts to OBO ontologies: condition concepts (SNOMED ids) -> MONDO and HPO, and
measurement concepts (LOINC) -> HPO at the Low / Normal / High result level. Assessed on
2026-10-22 (docs/plans/0002): it covers 24/30 of this repo's condition concept ids and
14/35 of its LOINC codes, agrees with the curated term on most, and is wrong on some
(dyslipidaemia -> "inherited lipid metabolism disorder"; a Normal ALT -> "elevated ALT").
So it is a **candidate generator**: this script lines its proposals up against what the
repo ships so a reviewer sees "OMOP2OBO says X, we ship Y" side by side, and never writes
into the live sets.

    import_omop2obo.py --conditions <OMOP2OBO conditions .xlsx> --measurements <... .xlsx>
                       [--out mappings/candidates/]

Inputs are the Zenodo workbooks (conditions 94 MB, measurements 3 MB); download them once
(records 6774363 and 6774443) and point at the files. Reading them needs openpyxl (the
`supplements` extra). Outputs, under ``mappings/candidates/``:

    omop2obo-aireadi-conditions.sssom.tsv    one row per (item, MONDO proposal)
    omop2obo-aireadi-measurement.sssom.tsv   one row per (item, pole, HPO proposal)

each with ``status`` = agree (same term we ship) | differs (we ship another term) | new
(we ship nothing for that item/pole), ``omop2obo_category`` (their mapping category) and a
confidence derived from it. The candidates directory is outside the apply path on purpose:
``default_mapping_files`` reads ``mappings/*.sssom.tsv`` only.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from b2ai_dataset_ingest.mapping.sssom_io import parse_sssom  # noqa: E402

CONFIG = REPO / "config" / "aireadi"
OUT = REPO / "mappings" / "candidates"
ZENODO = {
    "conditions": "https://doi.org/10.5281/zenodo.6774363",
    "measurements": "https://doi.org/10.5281/zenodo.6774443",
}
#: OMOP2OBO MAPPING_CATEGORY -> a confidence a reviewer can read as "how much to trust it".
CONFIDENCE = {
    "Manual One-to-One Concept": "0.8",
    "Automatic One-to-One Concept": "0.6",
    "Cosine Similarity One-to-One Concept": "0.5",
    "Manual One-to-Many Concept": "0.4",
    "Automatic One-to-Many Concept": "0.4",
    "Automatic One-to-One Ancestor": "0.3",
    "Automatic One-to-Many Ancestor": "0.2",
}
HEADER = [
    "subject_id",
    "subject_label",
    "predicate_id",
    "object_id",
    "object_label",
    "mapping_justification",
    "confidence",
    "mapping_source",
    "status",
    "shipped_object_id",
    "omop2obo_category",
    "omop2obo_result_type",
    "comment",
]
_CONCEPT_COMMENT = re.compile(r"#\s*(\d{5,10})\b")


# --------------------------------------------------------------- what the repo ships
def shipped_conditions() -> dict[str, tuple[str, str]]:
    """``item -> (OMOP concept id, MONDO id)`` from conditions.yaml (the id is in a comment)."""
    out: dict[str, tuple[str, str]] = {}
    text = (CONFIG / "conditions.yaml").read_text()
    mapping = yaml.safe_load(text) or {}
    for item, term in (mapping.get("conditions") or {}).items():
        line = next((ln for ln in text.splitlines() if ln.strip().startswith(f"{item}:")), "")
        m = _CONCEPT_COMMENT.search(line)
        if m:
            out[item] = (m.group(1), str((term or {}).get("id") or ""))
    return out


def shipped_measurements() -> dict[str, str]:
    """``item -> LOINC code`` for every measurement item whose assay is LOINC."""
    out: dict[str, str] = {}
    for path in sorted((CONFIG / "measurement").glob("*.yaml")):
        mapping = yaml.safe_load(path.read_text()) or {}
        for item, spec in (mapping.get("measures") or {}).items():
            code = str(((spec or {}).get("assay") or {}).get("id") or "")
            if code.startswith("LOINC:"):
                out[item] = code.split(":", 1)[1]
    return out


def shipped_hpo_rows() -> dict[tuple[str, str], str]:
    """``(item, pole) -> HP id`` from the measurement set, pole = low / high by gate sign."""
    out: dict[tuple[str, str], str] = {}
    path = REPO / "mappings" / "b2ai-aireadi-measurement.sssom.tsv"
    if not path.exists():
        return out
    for row in parse_sssom(path)[1]:
        if row.get("predicate_modifier") or not row.get("when_value"):
            continue
        item = row["subject_id"].split(".", 1)[1]
        gate = row["when_value"].strip()
        pole = "low" if gate.startswith("<") else "high" if gate.startswith(">") else ""
        if pole:
            out.setdefault((item, pole), row["object_id"])
    return out


# --------------------------------------------------------------- reading OMOP2OBO
def _sheet_rows(path: Path, sheet: str):
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True)
    try:
        ws = workbook[sheet]
        rows = ws.iter_rows(values_only=True)
        header = [str(c) for c in next(rows)]
        for raw in rows:
            yield dict(zip(header, raw, strict=False))
    finally:
        workbook.close()


def _curies(uri_cell) -> list[str]:
    """``"MONDO_0005148 | MONDO_0007452"`` -> ``["MONDO:0005148", "MONDO:0007452"]``."""
    out = []
    for part in str(uri_cell or "").split("|"):
        part = part.strip()
        if "_" in part and not part.upper().startswith("NOT"):
            prefix, local = part.rsplit("_", 1)
            out.append(f"{prefix.rsplit('/', 1)[-1]}:{local}")
    return out


def condition_candidates(xlsx: Path) -> list[dict[str, str]]:
    shipped = shipped_conditions()
    by_concept = {cid: (item, mondo) for item, (cid, mondo) in shipped.items()}
    out = []
    for r in _sheet_rows(xlsx, "OMOP2OBO_Mondo_Mapping_Results"):
        cid = str(r.get("CONCEPT_ID") or "").split(".")[0]
        if cid not in by_concept:
            continue
        item, ours = by_concept[cid]
        labels = [s.strip() for s in str(r.get("ONTOLOGY_LABEL") or "").split("|")]
        for i, curie in enumerate(_curies(r.get("ONTOLOGY_URI"))):
            category = str(r.get("MAPPING_CATEGORY") or "")
            out.append(
                {
                    "subject_id": f"b2ai:condition_occurrence.{item}",
                    "subject_label": str(r.get("CONCEPT_NAME") or ""),
                    "predicate_id": "skos:closeMatch",
                    "object_id": curie,
                    "object_label": labels[i] if i < len(labels) else "",
                    "mapping_justification": "semapv:MappingChaining",
                    "confidence": CONFIDENCE.get(category, "0.2"),
                    "mapping_source": ZENODO["conditions"],
                    "status": "agree" if curie == ours else ("differs" if ours else "new"),
                    "shipped_object_id": ours,
                    "omop2obo_category": category,
                    "omop2obo_result_type": "",
                    "comment": f"OMOP2OBO v1.5 (Oct 2020 vocabularies) for OMOP concept {cid}",
                }
            )
    return out


def measurement_candidates(xlsx: Path) -> list[dict[str, str]]:
    shipped = shipped_measurements()
    by_code: dict[str, list[str]] = {}
    for item, code in shipped.items():
        by_code.setdefault(code, []).append(item)
    ours = shipped_hpo_rows()
    out = []
    for r in _sheet_rows(xlsx, "OMOP2OBO_HPO_Mapping_Results"):
        code = str(r.get("CONCEPT_CODE") or "")
        if code not in by_code:
            continue
        result = str(r.get("RESULT_TYPE") or "")
        pole = {"Low": "low", "High": "high"}.get(result, "")
        if not pole:
            continue  # the "Normal" rows map to the abnormal parent and are not a candidate
        labels = [s.strip() for s in str(r.get("ONTOLOGY_LABEL") or "").split("|")]
        for item in by_code[code]:
            for i, curie in enumerate(_curies(r.get("ONTOLOGY_URI"))):
                category = str(r.get("MAPPING_CATEGORY") or "")
                mine = ours.get((item, pole), "")
                out.append(
                    {
                        "subject_id": f"b2ai:measurement.{item}",
                        "subject_label": str(r.get("CONCEPT_NAME") or ""),
                        "predicate_id": "skos:relatedMatch",
                        "object_id": curie,
                        "object_label": labels[i] if i < len(labels) else "",
                        "mapping_justification": "semapv:MappingChaining",
                        "confidence": CONFIDENCE.get(category, "0.2"),
                        "mapping_source": ZENODO["measurements"],
                        "status": "agree" if curie == mine else ("differs" if mine else "new"),
                        "shipped_object_id": mine,
                        "omop2obo_category": category,
                        "omop2obo_result_type": result,
                        "comment": (
                            f"OMOP2OBO v1 measurement mapping for LOINC {code}, {result} "
                            "result; the gate (when_value) is the curator's to set"
                        ),
                    }
                )
    return out


def write(rows: list[dict[str, str]], target: Path, what: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", newline="") as fh:
        fh.write(
            "# OMOP2OBO candidate mappings for review -- NOT in the apply path. Generated by\n"
            f"# scripts/import_omop2obo.py from {ZENODO[what]} against the configs as of today.\n"
            "# status: agree = same term the repo ships; differs = the repo ships another\n"
            "# term; new = the repo ships nothing for this item/pole. A row leaves this file\n"
            "# only through the review loop (scripts/sssom_sheet.py) and the validator.\n"
        )
        writer = csv.DictWriter(fh, fieldnames=HEADER, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--conditions", type=Path, help="OMOP2OBO condition workbook (.xlsx)")
    parser.add_argument("--measurements", type=Path, help="OMOP2OBO measurement workbook (.xlsx)")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    if not args.conditions and not args.measurements:
        parser.error("give --conditions and/or --measurements")
    if args.conditions:
        rows = condition_candidates(args.conditions)
        write(rows, args.out / "omop2obo-aireadi-conditions.sssom.tsv", "conditions")
        print(f"conditions: {len(rows)} candidate(s); " + _tally(rows))
    if args.measurements:
        rows = measurement_candidates(args.measurements)
        write(rows, args.out / "omop2obo-aireadi-measurement.sssom.tsv", "measurements")
        print(f"measurements: {len(rows)} candidate(s); " + _tally(rows))
    return 0


def _tally(rows: list[dict[str, str]]) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


if __name__ == "__main__":
    sys.exit(main())

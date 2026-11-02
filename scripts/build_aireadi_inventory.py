#!/usr/bin/env python3
"""Build config/aireadi/inventory.tsv from AI-READI's published crosswalk, with our status.

The crosswalk (``docs/static/json/mappings.json`` in github.com/ai-readi/ai-readi-docs,
CC-BY-4.0) lists every REDCap variable AI-READI mapped into OMOP: form, description, target
domain, vocabulary and code, and whether the item is open-source or protected. It is
documentation, not Data -- no participant appears in it -- so it is the right place to
build the to-do list from, rather than from the licensed release (docs/plans/0002, step 8).

For each *question* row this script records what this repo does with the variable:

    mapped    some config emits it (measurement/observation measures, conditions, the
              participants / person / supplement columns, the modality manifests)
    dropped   a config names it under drop_rules / dropped_columns with a reason
    unmapped  nobody has looked at it

so ``tests/test_aireadi_inventory.py`` can hold the line that nothing is silently unmapped,
and ``validate-aireadi`` on a real release can be read against a public baseline.

Usage:
    scripts/build_aireadi_inventory.py                # fetch, write config/aireadi/inventory.tsv
    scripts/build_aireadi_inventory.py --json <path>  # use a downloaded crosswalk

Needs no dependency beyond PyYAML (already a project dependency).
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
from fnmatch import fnmatch
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "aireadi"
OUT = CONFIG / "inventory.tsv"
URL = "https://raw.githubusercontent.com/ai-readi/ai-readi-docs/main/docs/static/json/mappings.json"

COLUMNS = [
    "variable",
    "form",
    "tier",
    "omop_domain",
    "vocabulary",
    "code",
    "predicate",
    "description",
    "status",
    "where",
]

#: Crosswalk OMOP domain -> the config families that can claim a variable of that domain.
DOMAIN_CONFIGS = {
    "Measurement": ("measurement", "modalities"),
    "Observation": ("observation", "supplements"),
    "Condition": ("conditions",),
    "Procedure": ("procedure",),
}


def fetch(json_path: Path | None) -> list[dict]:
    if json_path is None:
        tmp = Path(tempfile.mkdtemp()) / "mappings.json"
        subprocess.run(["curl", "-fsSL", "-o", str(tmp), URL], check=True)
        json_path = tmp
    payload = json.loads(json_path.read_text())
    rows = payload if isinstance(payload, list) else next(
        v for v in payload.values() if isinstance(v, list)
    )
    return [r for r in rows if r.get("temp_Question_or_Answer") == "Question"]


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def claims() -> tuple[dict[str, str], dict[str, str]]:
    """``variable -> where`` for mapped items, and ``glob -> where`` for dropped ones.

    The crosswalk keys the central-lab rows (form ``import_data``) by their LOINC code
    rather than by the ``import_<analyte>`` variable the release uses, so a config assay
    ``LOINC:<code>`` also claims the row whose variable is ``<code>``.
    """
    mapped: dict[str, str] = {}
    dropped: dict[str, str] = {}
    for path in sorted(CONFIG.rglob("*.yaml")):
        mapping = _load(path)
        rel = str(path.relative_to(CONFIG))
        for block in ("measures", "conditions", "columns", "items"):
            for item, spec in (mapping.get(block) or {}).items():
                mapped.setdefault(str(item), rel)
                assay = spec.get("assay") if isinstance(spec, dict) else None
                code = str((assay or {}).get("id") or "")
                if code.startswith("LOINC:"):
                    mapped.setdefault(code.split(":", 1)[1], rel)
                # Where the crosswalk keyed the row by a LOINC code this repo chose not
                # to adopt, the config says which (`crosswalk_code`) so the row is still
                # claimed and the disagreement is visible next to the item.
                if isinstance(spec, dict) and spec.get("crosswalk_code"):
                    mapped.setdefault(str(spec["crosswalk_code"]), rel)
        group = mapping.get("study_group") or {}
        if group.get("source_column"):
            mapped.setdefault(str(group["source_column"]), rel)
        for block in ("drop_rules", "dropped_columns"):
            for pattern in mapping.get(block) or {}:
                dropped.setdefault(str(pattern), rel)
        for block in ("agent", "route", "frequency", "dose"):
            spec = mapping.get(block) or {}
            for key, value in spec.items():
                if key.endswith("column") and isinstance(value, str):
                    mapped.setdefault(value, rel)
    return mapped, dropped


def status_of(variable: str, mapped: dict[str, str], dropped: dict[str, str]) -> tuple[str, str]:
    if variable in mapped:
        return "mapped", mapped[variable]
    for pattern, where in dropped.items():
        if variable == pattern or fnmatch(variable, pattern):
            return "dropped", f"{where}:{pattern}"
    return "unmapped", ""


def build(rows: list[dict]) -> list[dict[str, str]]:
    mapped, dropped = claims()
    out = []
    seen = set()
    for r in rows:
        variable = str(r.get("SRC_CODE") or "").strip()
        if not variable or variable in seen:
            continue
        seen.add(variable)
        status, where = status_of(variable, mapped, dropped)
        vocab = str(r.get("TARGET_VOCABULARY_ID") or "").strip()
        code = str(r.get("TARGET_CONCEPT_CODE") or "").strip()
        out.append(
            {
                "variable": variable,
                "form": str(r.get("FORM_NAME") or ""),
                "tier": str(r.get("Protected_or_open-source") or ""),
                "omop_domain": str(r.get("TARGET_DOMAIN_ID") or ""),
                "vocabulary": vocab,
                "code": code,
                "predicate": str(r.get("PREDICATE_ID") or ""),
                "description": " ".join(str(r.get("SRC_CD_DESCRIPTION") or "").split())[:120],
                "status": status,
                "where": where,
            }
        )
    return sorted(out, key=lambda x: (x["form"], x["variable"]))


def write(rows: list[dict[str, str]], target: Path) -> None:
    with open(target, "w", newline="") as fh:
        fh.write(
            "# AI-READI variable inventory, generated by scripts/build_aireadi_inventory.py "
            "from AI-READI's published crosswalk (ai-readi-docs, CC-BY-4.0). Documentation, "
            "not Data. `status` is this repo's position on each variable.\n"
        )
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", type=Path, default=None, help="a downloaded mappings.json")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    rows = build(fetch(args.json))
    write(rows, args.out)
    tally: dict[str, int] = {}
    for r in rows:
        tally[r["status"]] = tally.get(r["status"], 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(tally.items()))
    print(f"wrote {len(rows)} variables to {args.out}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

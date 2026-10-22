#!/usr/bin/env python3
"""Fold a SSSOM mapping set into its LinkML gate schema (the inverse of gates_to_sssom.py).

Used once to migrate the hand-curated sets, and again whenever a reviewer edits the SSSOM
directly (the sheet loop): fold the TSV back into the schema, then regenerate the TSV so the
two agree and the schema stays the source.

    scripts/sssom_to_gates.py mappings/b2ai-aireadi-measurement.sssom.tsv
        -> schema/gates/b2ai-aireadi-measurement.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from b2ai_dataset_ingest.mapping.linkml_gates import (  # noqa: E402
    dump_schema,
    rows_to_schema,
    sssom_header_yaml,
)
from b2ai_dataset_ingest.mapping.sssom_io import parse_sssom  # noqa: E402

GATES = REPO / "schema" / "gates"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sssom", type=Path, nargs="+")
    parser.add_argument("--out", type=Path, default=GATES)
    args = parser.parse_args()
    for path in args.sssom:
        metadata, rows = parse_sssom(path)
        header = sssom_header_yaml(path)
        columns = next(
            ln for ln in path.read_text().splitlines() if ln and not ln.startswith("#")
        ).split("\t")
        name = path.name.replace(".sssom.tsv", "")
        prefixes = {k: v for k, v in (metadata.get("curie_map") or {}).items()}
        schema = rows_to_schema(name, header, columns, rows, prefixes)
        target = args.out / f"{name}.yaml"
        dump_schema(schema, target)
        n_rules = sum(len(c.get("rules") or []) for c in schema["classes"].values())
        print(f"{path.name}: {len(schema['classes'])} item(s), {n_rules} gate(s) -> {target}")
    _ = yaml  # keep the dependency explicit
    return 0


if __name__ == "__main__":
    sys.exit(main())

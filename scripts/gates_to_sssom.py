#!/usr/bin/env python3
"""Render the LinkML gate schemas to their SSSOM mapping sets.

The schemas under schema/gates/ are the source; mappings/*.sssom.tsv are generated from
them and committed. CI runs this with --check so the two cannot drift.

    scripts/gates_to_sssom.py                 # regenerate every set that has a schema
    scripts/gates_to_sssom.py --check         # exit 1 if any committed set differs
    scripts/gates_to_sssom.py schema/gates/b2ai-aireadi-measurement.yaml

Needs linkml-runtime (the `schema` extra): the schema is read through SchemaView, so a
schema LinkML cannot load fails here rather than silently rendering.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from b2ai_dataset_ingest.mapping.linkml_gates import schema_to_rows, write_sssom  # noqa: E402

GATES = REPO / "schema" / "gates"
MAPPINGS = REPO / "mappings"


def render(schema_path: Path) -> tuple[Path, str]:
    header, columns, rows = schema_to_rows(schema_path)
    target = MAPPINGS / f"{schema_path.stem}.sssom.tsv"
    tmp = target.with_suffix(".tmp")
    write_sssom(tmp, header, columns, rows)
    text = tmp.read_text()
    tmp.unlink()
    return target, text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("schemas", type=Path, nargs="*")
    parser.add_argument("--check", action="store_true", help="compare, do not write")
    args = parser.parse_args()
    schemas = args.schemas or sorted(
        p for p in GATES.glob("*.yaml") if p.name != "gate_base.yaml"
    )
    drift = []
    for schema_path in schemas:
        target, text = render(schema_path)
        current = target.read_text() if target.exists() else None
        if args.check:
            if current != text:
                drift.append(target.name)
            continue
        target.write_text(text)
        print(f"{schema_path.name} -> {target.name} ({text.count(chr(10)) - 1} line(s))")
    if args.check:
        if drift:
            print("generated SSSOM differs from committed: " + ", ".join(drift), file=sys.stderr)
            print("run scripts/gates_to_sssom.py and commit the result", file=sys.stderr)
            return 1
        print(f"{len(schemas)} set(s) match their schema")
    return 0


if __name__ == "__main__":
    sys.exit(main())

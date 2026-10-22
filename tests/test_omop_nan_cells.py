"""A pandas-exported ``nan`` is a blank cell, never a unit and never a value.

Found by the first preflight on the flagship release (2026-10-22): every central-lab row
carries the literal string ``nan`` in ``unit_source_value``. The validator reported 22 unit
mismatches against it, which is noise, and the same string in ``value_as_number`` would
have parsed as ``float('nan')`` and emitted a NaN Quantity that no gate can catch.
"""

import math
from pathlib import Path

from b2ai_dataset_ingest.mapping.omop import as_number, is_blank
from b2ai_dataset_ingest.sources.aireadi.validate import validate_aireadi

CONFIG_DIR = Path(__file__).parents[1] / "config" / "aireadi"
FIXTURE = Path(__file__).parent / "data" / "aireadi"


def test_nan_spellings_are_blank():
    for spelling in ("nan", "NaN", "None", "null", "N/A", "", " "):
        assert is_blank(spelling), spelling
        assert as_number(spelling) is None, spelling
    assert not is_blank("0")  # zero is an answer


def test_non_finite_numbers_are_not_values():
    assert as_number("inf") is None and as_number("-inf") is None
    assert as_number("1e400") is None  # overflows to inf
    assert as_number("7.2") == 7.2
    assert as_number("0") == 0.0
    assert math.isfinite(as_number("3.0"))


def test_a_nan_unit_string_is_not_a_unit_mismatch(tmp_path):
    import shutil

    root = tmp_path / "release"
    shutil.copytree(FIXTURE, root)
    path = root / "clinical_data" / "measurement.csv"
    rows = path.read_text().splitlines()
    header = rows[0].split(",")
    unit_col = header.index("unit_source_value")
    patched = [rows[0]]
    for line in rows[1:]:
        cells = line.split(",")
        if "import_hba1c" in line and len(cells) > unit_col:
            cells[unit_col] = "nan"
        patched.append(",".join(cells))
    path.write_text("\n".join(patched) + "\n")
    report = validate_aireadi(root, CONFIG_DIR)
    assert not any(
        "import_hba1c: config declares unit" in f.message for f in report.findings
    ), report.render()


def test_a_nan_value_cell_emits_nothing(tmp_path):
    import shutil

    from b2ai_dataset_ingest.sources.aireadi import AireadiSource

    root = tmp_path / "release"
    shutil.copytree(FIXTURE, root)
    path = root / "clinical_data" / "measurement.csv"
    text = path.read_text().replace(
        ',27.0,0,0,0.0,30.0,804,"moca_total_score"', ',nan,0,0,0.0,30.0,804,"moca_total_score"'
    )
    path.write_text(text)
    participants = {p.individual.id: p for p in AireadiSource(root, CONFIG_DIR).read()}
    moca = [m for m in participants["900003"].measurements if m.assay.id == "LOINC:72172-0"]
    assert moca == []

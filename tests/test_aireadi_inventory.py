"""The committed variable inventory holds the line: nothing in the crosswalk is unaccounted for.

``config/aireadi/inventory.tsv`` is generated from AI-READI's published crosswalk by
``scripts/build_aireadi_inventory.py`` and records, for every REDCap variable AI-READI mapped
into OMOP, whether this repo maps it, drops it with a reason, or has not looked at it. The
first two are policy; the third is a gap. These tests keep the gap at zero and keep the
inventory honest against the configs it cites, without touching the network.
"""

import csv
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
CONFIG_DIR = REPO / "config" / "aireadi"
INVENTORY = CONFIG_DIR / "inventory.tsv"


def _builder():
    """The generator script, loaded from scripts/ (not a package; it does not ship)."""
    spec = importlib.util.spec_from_file_location(
        "build_aireadi_inventory", REPO / "scripts" / "build_aireadi_inventory.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


claims = _builder().claims
status_of = _builder().status_of


@pytest.fixture(scope="module")
def inventory():
    with open(INVENTORY, newline="") as fh:
        body = (ln for ln in fh if not ln.startswith("#"))
        rows = list(csv.DictReader(body, delimiter="\t"))
    assert rows
    return rows


def test_no_variable_is_silently_unmapped(inventory):
    unmapped = sorted(r["variable"] for r in inventory if r["status"] == "unmapped")
    assert unmapped == [], f"variables nobody has looked at: {unmapped}"


def test_every_claim_points_at_a_real_config(inventory):
    for row in inventory:
        if not row["where"]:
            continue
        rel = row["where"].split(":", 1)[0]
        assert (CONFIG_DIR / rel).exists(), f"{row['variable']} cites missing {rel}"


def test_the_committed_inventory_agrees_with_the_configs(inventory):
    """Re-derive each status from the configs as they are now; a config change that is not
    followed by a rebuild shows up here rather than in a stale TSV."""
    mapped, dropped = claims()
    stale = []
    for row in inventory:
        status, _ = status_of(row["variable"], mapped, dropped)
        if status != row["status"]:
            stale.append((row["variable"], row["status"], status))
    assert not stale, f"rebuild the inventory (scripts/build_aireadi_inventory.py): {stale[:10]}"


def test_inventory_carries_only_vocabulary_never_values(inventory):
    """Form names, variables, codes and short descriptions -- and no participant could be
    in it, since the crosswalk is documentation."""
    for row in inventory:
        assert not row["variable"].startswith("9000")
        assert len(row["description"]) <= 120


def test_the_sdd_decisions_are_visible_in_the_inventory(inventory):
    by_var = {r["variable"]: r for r in inventory}
    assert by_var["mssrf1"]["status"] == "dropped"
    assert "procedure.yaml" in by_var["mssrf1"]["where"]
    assert by_var["mhoccur_fall"]["status"] == "mapped"
    assert by_var["via1"]["status"] == "mapped" and by_var["via5"]["status"] == "dropped"
    assert by_var["susmkncf"]["status"] == "mapped"
    # Crosswalk rows keyed by a LOINC code this repo did not adopt are still claimed.
    assert by_var["2336-6"]["status"] == "mapped"

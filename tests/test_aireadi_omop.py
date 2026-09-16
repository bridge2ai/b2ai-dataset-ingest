"""OMOP primitives — the rules that decide what a long row means, tested with no data on disk.

Each test here pins a rule that, if it silently flipped, would corrupt output rather than
crash: a mis-parsed item key merges two measurements, an un-normalized timestamp is
discarded by the emitter without an error, and confusing "0 the concept" with "0 the answer"
deletes the modal response of a screening instrument.
"""

import phenopackets as pp
import pytest

from b2ai_dataset_ingest.mapping.omop import (
    NULL_CONCEPT_IDS,
    AgeAnchor,
    LongCell,
    OmopTableSpec,
    as_number,
    as_row,
    is_blank,
    is_bounded,
    is_sentinel,
    item_key,
    pivot,
    to_rfc3339,
)


# ---------- item key
@pytest.mark.parametrize(
    ("source_value", "expected"),
    [
        ("import_hba1c, Hemoglobin A1c/Hemoglobin.total in ", "import_hba1c"),  # truncated at 49
        ("moca_total_score", "moca_total_score"),  # 30 real items have no comma at all
        ("mhoccur_ua, Urinary problems (Examples: urinary t", "mhoccur_ua"),
        ("  bp1_sysbp_vsorres , Systolic (mmHg)", "bp1_sysbp_vsorres"),
        ("", ""),
    ],
)
def test_item_key_reads_the_variable_not_the_label(source_value, expected):
    assert item_key(source_value) == expected


def test_one_concept_id_can_back_two_items():
    """Why the item key is the variable: concept 3004249 is both BP readings.

    Keying on the concept id would silently merge the first and second reading of a visit.
    """
    first = "bp1_sysbp_vsorres, Systolic (mmHg)"
    second = "bp2_sysbp_vsorres, Systolic (mmHg)"
    assert item_key(first) != item_key(second)


# ---------- timestamps
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2023-12-12 08:19:00", "2023-12-12T08:19:00Z"),
        ("2023-12-12T08:19:00", "2023-12-12T08:19:00Z"),
        ("2023-12-12", "2023-12-12T00:00:00Z"),
        ("2023-12-12T08:19:00Z", "2023-12-12T08:19:00Z"),
        ("12/12/23", None),
        ("", None),
        ("   ", None),
    ],
)
def test_to_rfc3339(raw, expected):
    assert to_rfc3339(raw) == expected


@pytest.mark.parametrize("raw", ["2023-12-12", "2023-12-12 08:19:00", "2023-12-12T08:19:00"])
def test_protobuf_rejects_every_unnormalized_omop_spelling(raw):
    """The reason to_rfc3339 exists.

    ``TimeElement.timestamp.FromJsonString`` accepts only the trailing-Z form, and the
    emitter *catches* the resulting ValueError and falls back — so handing it a raw OMOP
    datetime loses every ``time_observed`` without anything failing.
    """
    with pytest.raises(ValueError):
        pp.TimeElement().timestamp.FromJsonString(raw)
    pp.TimeElement().timestamp.FromJsonString(to_rfc3339(raw))  # normalized: accepted


# ---------- absence has two meanings
def test_zero_is_absent_in_a_concept_column_but_an_answer_in_a_value_column():
    """`0` is an ordinary answer -- the modal one on the first CES-D-10 item.

    Conflating the two null sets would delete the modal answer of a depression screener.
    """
    assert is_blank("0", NULL_CONCEPT_IDS) is True
    assert is_blank("0") is False
    assert as_number("0") == 0.0


@pytest.mark.parametrize("raw", ["", " ", "N/A"])
def test_blank_spellings(raw):
    assert is_blank(raw) is True


@pytest.mark.parametrize(("raw", "expected"), [("555", True), ("777.0", True), ("5", False)])
def test_sentinel_answers_are_refusal_codes_not_values(raw, expected):
    assert is_sentinel(raw) is expected


# ---------- the censoring gate, and its polarity
@pytest.mark.parametrize(
    ("operator", "bounded"),
    [
        ("4171756", True),  # "<" — a detection floor, not a measured value
        ("4172703", False),  # "=" — a point value
        ("0", False),  # "not recorded" — the majority spelling on some items; must still emit
        ("", False),
    ],
)
def test_only_an_explicit_bound_censors(operator, bounded):
    assert is_bounded(operator) is bounded


# ---------- pivot
def _row(person, source_value, value, **extra):
    row = {
        "person_id": person,
        "measurement_source_value": source_value,
        "value_as_number": value,
        "measurement_date": "2024-01-15",
    }
    row.update(extra)
    return row


def test_pivot_groups_by_person_and_keeps_metadata_per_cell():
    spec = OmopTableSpec({"table": "measurement", "date_column": "measurement_date"})
    rows = [
        _row("900001", "bp1_sysbp_vsorres, Systolic (mmHg)", "128"),
        _row(
            "900001",
            "import_nt_probnp, Natriuretic peptide",
            "36",
            operator_concept_id="4171756",
        ),
        _row("900002", "bp1_sysbp_vsorres, Systolic (mmHg)", "118"),
    ]
    grouped = dict(pivot(rows, spec))
    assert set(grouped) == {"900001", "900002"}
    assert set(grouped["900001"]) == {"bp1_sysbp_vsorres", "import_nt_probnp"}
    # The metadata a plain dict[str, str] would have thrown away:
    assert grouped["900001"]["import_nt_probnp"].censored is True
    assert grouped["900001"]["bp1_sysbp_vsorres"].censored is False


def test_as_row_projects_the_wide_view_for_dataset_agnostic_consumers():
    cells = {"ces1": LongCell(item="ces1", value="0"), "ces2": LongCell(item="ces2", value="3")}
    assert as_row(cells) == {"ces1": "0", "ces2": "3"}


def test_cell_takes_the_first_non_blank_value_column():
    spec = OmopTableSpec(
        {"table": "observation", "value_columns": ["value_as_number", "value_as_string"]}
    )
    cell = spec.cell(
        {
            "observation_source_value": "ces1, I was bothered",
            "value_as_number": " ",
            "value_as_string": "0.0",
            "observation_date": "2024-01-15",
        }
    )
    assert cell is not None and cell.value == "0.0"


# ---------- age derivation
def test_age_is_derived_per_date_not_reused():
    """A cohort table records one age; OMOP records a date on every row.

    Reusing the single value gives every observation the same `Age`, which at age precision
    is the *whole* TimeElement — so two visits become indistinguishable in the output.
    """
    anchor = AgeAnchor.build("52", "2024-01-15")
    assert anchor is not None
    assert anchor.age_at("2024-01-15") == "P52Y"
    assert anchor.age_at("2025-04-28 08:00:00") == "P53Y"
    assert anchor.age_at("2027-06-01") == "P55Y"


def test_age_advances_on_the_anniversary_not_by_elapsed_days():
    """A participant is 52 until the day they turn 53; dividing by 365.25 rounds wrongly."""
    anchor = AgeAnchor.build("52", "2024-01-15")
    assert anchor.age_at("2025-01-14") == "P52Y"
    assert anchor.age_at("2025-01-15") == "P53Y"


def test_an_observation_before_the_anchor_yields_a_younger_age():
    """Surveys are completed before the study visit, so this is ordinary, not an error."""
    anchor = AgeAnchor.build("52", "2024-01-15")
    assert anchor.age_at("2023-06-01") == "P51Y"


def test_a_negative_age_is_refused_rather_than_emitted():
    anchor = AgeAnchor.build("2", "2024-01-15")
    assert anchor.age_at("2020-01-01") is None


@pytest.mark.parametrize("when", [None, "", "12/12/23", "not-a-date"])
def test_no_usable_date_falls_back_to_the_anchor_age(when):
    anchor = AgeAnchor.build("52", "2024-01-15")
    assert anchor.age_at(when) == "P52Y"


def test_an_anchor_without_a_date_is_a_constant():
    """A release shipping no cohort date degrades to single-age behaviour.

    Assuming an epoch instead would invent an age decades wrong.
    """
    anchor = AgeAnchor.build("52")
    assert anchor is not None and anchor.on_date is None
    assert anchor.age_at("2030-01-01") == "P52Y"


def test_no_age_means_no_anchor():
    assert AgeAnchor.build("", "2024-01-15") is None
    assert AgeAnchor.build("not-a-number", "2024-01-15") is None

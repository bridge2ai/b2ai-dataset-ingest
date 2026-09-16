"""Aggregate, PHI-safe run reporting.

Silent degradation is the dominant failure mode for this pipeline: a real data dictionary
whose shape the reader doesn't expect, a session column that isn't there, an answer the
ordinal map can't resolve — none of these raise, they just quietly shrink the output. The
only end-of-run signal used to be "wrote N phenopackets", a count of *files* that stays
reassuringly high even when the files are content-free.

:class:`IngestReport` is the antidote: sub-readers and the mapping engine record what they
skipped, merged, or couldn't map, and the CLI prints the tally so degradation is visible.

Everything here is keyed by *table* / *table.column* — never by cell value or
``participant_id`` — so the report can be printed and logged even when the source is the
real, HIPAA-sensitive dataset.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class IngestReport:
    """Counts accumulated over one ingest run. All keys are structural, never PHI."""

    participants: int = 0
    tables_read: list[str] = field(default_factory=list)
    tables_missing: list[str] = field(default_factory=list)
    #: Data tables present on disk with no mapping config (present but not ingested).
    tables_unmapped: list[str] = field(default_factory=list)
    #: Tables whose declared ``session_columns`` were all absent from the header — rows were
    #: emitted as independent timepoints (never merged) rather than silently collapsed.
    tables_without_session: list[str] = field(default_factory=list)

    measurements_emitted: int = 0
    diseases_emitted: int = 0
    #: HPO PhenotypicFeatures derived from value-gated (``when_value``) SSSOM mappings.
    features_derived: int = 0
    #: Rows collapsed by "last non-empty wins" within a resolved (participant, session) group.
    rows_merged: int = 0

    #: "table.column" -> count of answers that resolved against no choice/scale (item skipped).
    items_unresolved: Counter = field(default_factory=Counter)
    #: "table.column" -> count of demographics values with no ``value_map`` entry (dropped).
    value_map_misses: Counter = field(default_factory=Counter)
    #: "table.column" -> count of precomputed totals that weren't numeric (skipped).
    totals_unparsed: Counter = field(default_factory=Counter)
    #: "table.item" -> count of items skipped because their ontology term is a placeholder.
    placeholders_skipped: Counter = field(default_factory=Counter)

    # -- OMOP long-table counters (see sources/aireadi/) --
    #: "table.item" -> count of long rows whose item has no mapping config entry.
    items_unmapped: Counter = field(default_factory=Counter)
    #: "table.item" -> count of long rows a config deliberately ignores (drop_rules).
    #: Kept separate from items_unmapped: "we decided not to" and "nobody looked" are
    #: different facts, and on a 355-item table the difference is the whole signal.
    items_dropped: Counter = field(default_factory=Counter)
    #: "table.item" -> count of values dropped because an operator marked them censored
    #: (e.g. a below-detection-limit assay). GA4GH Quantity has no operator slot.
    values_censored: Counter = field(default_factory=Counter)
    #: "table.item" -> count of REDCap refusal/don't-know codes (555/777/888/999) dropped.
    sentinel_answers: Counter = field(default_factory=Counter)
    #: "table.unit_concept_id" -> count of values whose unit had no UCUM mapping.
    units_unmapped: Counter = field(default_factory=Counter)
    #: Reference ranges dropped for carrying only one bound (proto3 would read the other as 0).
    reference_ranges_one_sided: int = 0
    #: "table.column" -> rows whose source column is fully redacted in this release.
    fields_redacted: Counter = field(default_factory=Counter)
    #: Long rows with no resolvable visit; the row's own date is used instead.
    rows_without_visit: int = 0

    # -- recorders (called by the engine / reader; no-ops are cheap) --
    def note_unresolved_item(self, table: str, column: str) -> None:
        self.items_unresolved[f"{table}.{column}"] += 1

    def note_value_map_miss(self, table: str, column: str) -> None:
        self.value_map_misses[f"{table}.{column}"] += 1

    def note_total_unparsed(self, table: str, column: str) -> None:
        self.totals_unparsed[f"{table}.{column}"] += 1

    def note_placeholder_skipped(self, table: str, item: str) -> None:
        self.placeholders_skipped[f"{table}.{item}"] += 1

    def note_item_unmapped(self, table: str, item: str) -> None:
        self.items_unmapped[f"{table}.{item}"] += 1

    def note_item_dropped(self, table: str, item: str) -> None:
        self.items_dropped[f"{table}.{item}"] += 1

    def note_value_censored(self, table: str, item: str) -> None:
        self.values_censored[f"{table}.{item}"] += 1

    def note_sentinel_answer(self, table: str, item: str) -> None:
        self.sentinel_answers[f"{table}.{item}"] += 1

    def note_unit_unmapped(self, table: str, unit_concept_id: str) -> None:
        self.units_unmapped[f"{table}.{unit_concept_id}"] += 1

    def note_field_redacted(self, table: str, column: str, rows: int = 1) -> None:
        self.fields_redacted[f"{table}.{column}"] += rows

    @property
    def has_degradation(self) -> bool:
        """True if a *fixable* gap was hit — something a config or a curation pass can close.

        Deliberately excludes ``items_unmapped``, ``values_censored`` and
        ``sentinel_answers``: those are the expected steady state of a partially-mapped OMOP
        release (a censored lab result is correctly dropped, not a defect), and including
        them would pin the flag True forever and make it useless. They are still counted and
        printed. Same reasoning the existing ``tables_unmapped`` exclusion already uses.
        """
        return bool(
            self.tables_missing
            or self.tables_without_session
            or self.items_unresolved
            or self.value_map_misses
            or self.totals_unparsed
            or self.placeholders_skipped
            or self.units_unmapped
            or self.fields_redacted
        )

    def render(self) -> str:
        """A compact, human-readable, PHI-free summary block."""
        lines = [
            "Ingest summary",
            f"  participants:        {self.participants}",
            f"  measurements:        {self.measurements_emitted}",
            f"  diseases:            {self.diseases_emitted}",
            f"  features derived:    {self.features_derived}",
            f"  tables read:         {len(self.tables_read)}"
            + (f" ({', '.join(self.tables_read)})" if self.tables_read else ""),
            f"  rows merged:         {self.rows_merged}",
        ]
        if self.tables_unmapped:
            lines.append(f"  tables not mapped:   {', '.join(sorted(self.tables_unmapped))}")
        if self.tables_missing:
            lines.append(f"  tables missing:      {', '.join(sorted(self.tables_missing))}")
        if self.tables_without_session:
            lines.append(
                "  no session column:   "
                + ", ".join(sorted(self.tables_without_session))
                + "  (rows emitted un-merged; check session_columns)"
            )
        if self.rows_without_visit:
            lines.append(f"  rows without a visit: {self.rows_without_visit}  (row date used)")
        if self.reference_ranges_one_sided:
            lines.append(
                f"  ref ranges dropped:  {self.reference_ranges_one_sided}  (only one bound)"
            )
        for label, counter in (
            ("items skipped (unresolved answers)", self.items_unresolved),
            ("demographics values dropped (no value_map)", self.value_map_misses),
            ("totals skipped (non-numeric)", self.totals_unparsed),
            ("items skipped (placeholder term)", self.placeholders_skipped),
            ("long rows skipped (item not mapped)", self.items_unmapped),
            ("long rows skipped (dropped by policy)", self.items_dropped),
            ("values dropped (censored by operator)", self.values_censored),
            ("answers dropped (refusal/unknown code)", self.sentinel_answers),
            ("values dropped (unit not in UCUM map)", self.units_unmapped),
            ("source fields fully redacted", self.fields_redacted),
        ):
            if counter:
                total = sum(counter.values())
                detail = ", ".join(f"{k}={v}" for k, v in counter.most_common())
                lines.append(f"  {label}: {total}  [{detail}]")
        if not self.has_degradation:
            lines.append("  no degradation detected")
        return "\n".join(lines)

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

    #: Questionnaires the run was restricted to, empty when all configured ones ran.
    questionnaires_selected: list[str] = field(default_factory=list)
    #: Participants with a row in some but not all of those questionnaires. Reported
    #: whether or not they were dropped: an uneven battery means an absent HPO term
    #: can mean "never asked" for some participants and "answered no" for others,
    #: which any clustering will find before it finds anything clinical.
    participants_partial_coverage: int = 0
    #: Of those, how many were excluded by --require-all-questionnaires.
    participants_dropped_partial: int = 0

    #: "table.column" -> count of answers that resolved against no choice/scale (item skipped).
    items_unresolved: Counter = field(default_factory=Counter)
    #: "table.column" -> count of demographics values with no ``value_map`` entry (dropped).
    value_map_misses: Counter = field(default_factory=Counter)
    #: "table.column" -> count of precomputed totals that weren't numeric (skipped).
    totals_unparsed: Counter = field(default_factory=Counter)

    # -- recorders (called by the engine / reader; no-ops are cheap) --
    def note_unresolved_item(self, table: str, column: str) -> None:
        self.items_unresolved[f"{table}.{column}"] += 1

    def note_value_map_miss(self, table: str, column: str) -> None:
        self.value_map_misses[f"{table}.{column}"] += 1

    def note_total_unparsed(self, table: str, column: str) -> None:
        self.totals_unparsed[f"{table}.{column}"] += 1

    @property
    def has_degradation(self) -> bool:
        """True if anything was skipped, dropped, or emitted without proper session keying."""
        return bool(
            self.tables_missing
            or self.tables_without_session
            or self.items_unresolved
            or self.value_map_misses
            or self.totals_unparsed
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
        if self.questionnaires_selected:
            lines.append(
                f"  questionnaires:      restricted to "
                f"{', '.join(self.questionnaires_selected)}"
            )
            if self.participants_dropped_partial:
                lines.append(
                    f"  dropped (partial):   {self.participants_dropped_partial} "
                    "participant(s) were not offered all of them"
                )
            elif self.participants_partial_coverage:
                # Not dropped, so the confound is still in the output. Say so with a
                # number rather than leaving it to be discovered downstream.
                lines.append(
                    f"  partial coverage:    {self.participants_partial_coverage} "
                    "participant(s) were not offered all of them (kept; pass "
                    "--require-all-questionnaires to exclude)"
                )
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
        for label, counter in (
            ("items skipped (unresolved answers)", self.items_unresolved),
            ("demographics values dropped (no value_map)", self.value_map_misses),
            ("totals skipped (non-numeric)", self.totals_unparsed),
        ):
            if counter:
                total = sum(counter.values())
                detail = ", ".join(f"{k}={v}" for k, v in counter.most_common())
                lines.append(f"  {label}: {total}  [{detail}]")
        if not self.has_degradation:
            lines.append("  no degradation detected")
        return "\n".join(lines)

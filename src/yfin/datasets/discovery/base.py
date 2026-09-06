"""Common base for discovery datasets.

Does two things:

1. Swaps the gate table. `asof_state` cannot be used: that table's
   `symbol` column is defined with `symbol_fk_column`, i.e. it carries an
   `ON DELETE RESTRICT` FK to `symbols.symbol`. A free-text search term
   (`"Turkish Airlines"`) does not exist in `symbols`, so the gate row
   would hit an FK violation (23503). A length limit does not fix this.
   Domain hit the same wall and opened `domain_asof_state`; here
   `discovery_asof_state` is opened instead, reusing `AsOfGate`'s
   `asof_gate_table` / `asof_gate_key_columns` / `gate_identity` extension
   points -- no new gate class is written.

2. Splits the gate scope. `AsOfGate._gate_write` reads the gate row's
   `as_of_date` and `fetched_at` from `_first_row(result)`, i.e. the first
   populated row in `writes`; the `gate_identity` override expects
   `query_term` from that same row. Four tables do not satisfy this
   contract (see below). "Just being careful about ordering" is not
   enough: `search_quotes` can be empty while `news` or `research_reports`
   is populated (measured for "Turkish Airlines") -- in that case the
   first populated write is one of those, and ordering discipline does not help.
"""

from __future__ import annotations

from typing import Any

from yfin.datasets.asof_base import AsOfDataset, _first_row
from yfin.datasets.base import NormalizedResult, WriteStats, merge_stats
from yfin.storage.persistence import RowWriter, apply_write

DISCOVERY_GATE_TABLE = "discovery_asof_state"
DISCOVERY_GATE_KEY_COLUMNS = ("query_term", "dataset")

# Tables left out of the gate.
#
# `symbols` is the universe record; `news`/`news_symbols`/`research_reports`
# are shared entities with their own identity space. A search term's
# content hash cannot decide whether they get written -- `Ticker.news`
# writes the same news item, and `Sector.research_reports` writes the same
# report.
#
# All four share one technical trait: no `query_term` column (the first
# three also lack `as_of_date`/`fetched_at`). Inside the gated side,
# `_first_row` would return the wrong row and the gate write would raise
# KeyError.
UNGATED_TABLES = frozenset({"symbols", "news", "news_symbols", "research_reports"})


class DiscoveryDataset[RawT](AsOfDataset[RawT]):
    asof_gate_table = DISCOVERY_GATE_TABLE
    asof_gate_key_columns = DISCOVERY_GATE_KEY_COLUMNS

    def gate_identity(self, result: NormalizedResult) -> dict[str, Any]:
        """Gate key: (query_term, dataset).

        The default would read `_first_row(result)["symbol"]`; here the
        scope is the search term, not a symbol -- one search term's result
        can carry multiple symbols.
        """
        return {"query_term": _first_row(result)["query_term"], "dataset": self.name}

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        ungated = [w for w in result.writes if w.table in UNGATED_TABLES]
        gated = [w for w in result.writes if w.table not in UNGATED_TABLES]

        # 1. Ungated writes first: `search_report_hits`'s FK points at
        #    `research_reports`, so the parent must be written first.
        stats = WriteStats(skipped=dict(result.skipped))
        for write in ungated:
            apply_write(writer, write, stats)

        # 2. `skipped` is passed empty. Two reasons:
        #    (a) the outer `stats` already seeded it; summing both would
        #        double the `rows_skipped` count.
        #    (b) `NormalizedResult.is_empty` means "no rows AND skipped is
        #        empty" (base.py). If `gated` has no rows but `skipped` is
        #        populated, `is_empty` is False, `_first_row` raises
        #        ValueError, and the cell would be wrongly marked `failed`.
        gated_result = NormalizedResult(writes=gated, skipped={})
        return merge_stats(stats, super().upsert(writer, gated_result))

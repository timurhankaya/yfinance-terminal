"""Common base for discovery datasets.

Own gate table: `asof_state.symbol` carries an FK to `symbols`, and a search
term is not a symbol. Tables without `query_term` are written ungated.
"""

from __future__ import annotations

from typing import Any

from yfin.datasets.asof_base import AsOfDataset
from yfin.datasets.base import NormalizedResult, merge_stats
from yfin.storage.contracts import RowWriter, WriteStats, apply_write

DISCOVERY_GATE_TABLE = "discovery_asof_state"
DISCOVERY_GATE_KEY_COLUMNS = ("query_term", "dataset")

# Shared entities with their own identity space, also written by other
# datasets; a search term's hash cannot decide whether they get written.
UNGATED_TABLES = frozenset({"symbols", "news", "news_symbols", "research_reports"})


class DiscoveryDataset[RawT](AsOfDataset[RawT]):
    asof_gate_table = DISCOVERY_GATE_TABLE
    asof_gate_key_columns = DISCOVERY_GATE_KEY_COLUMNS

    def gate_identity(self, result: NormalizedResult) -> dict[str, Any]:
        """Gate key: (query_term, dataset); one term's result carries many symbols."""
        return {"query_term": self.gate_row(result)["query_term"], "dataset": self.name}

    def upsert(
        self, writer: RowWriter, result: NormalizedResult, *, full_refresh: bool = False
    ) -> WriteStats:
        ungated = [w for w in result.writes if w.table in UNGATED_TABLES]
        gated = [w for w in result.writes if w.table not in UNGATED_TABLES]

        # 1. Ungated writes first: `search_report_hits`'s FK points at
        #    `research_reports`, so the parent must be written first.
        stats = WriteStats(skipped=dict(result.skipped))
        for write in ungated:
            apply_write(writer, write, stats)

        # 2. `skipped` is passed empty: the outer `stats` already carries it,
        #    and a populated `skipped` with no gated rows would make
        #    `is_empty` False and `gate_row` raise.
        gated_result = NormalizedResult(writes=gated, skipped={})
        return merge_stats(
            stats, super().upsert(writer, gated_result, full_refresh=full_refresh)
        )

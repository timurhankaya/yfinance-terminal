"""as-of dataset base.

The codebase already has two hash gates; this is a third sibling. Why the
others do not fit:

- `SnapshotDataset`: the compared table (`snapshot_table`) and the written
  table (`history_table`) differ, and what gets skipped is the history
  table's rows. Here the gate is not even a data table, and there can be
  1-4 target tables.
- `HashGatedDataset`: the gate and the child share one key space
  (`financial_periods` -> `financial_facts`), and the child's delete scope
  derives from `gate_key_columns`. Here the gate key is (symbol, dataset);
  children have no `dataset` column, and delete scope is the child's own
  `scope_columns`.

The shared rule holds: the gate row is always written; if the hash is
unchanged, only `fetched_at` is updated. So `fetched_at` means "last time
this was verified," not "last time this changed."

Gate logic is split out of the `Dataset` hierarchy: `AsOfGate` is a mixin
shared by `AsOfDataset` (symbol side) and `DomainAsOfDataset` (sector /
industry side). The two hierarchies cannot merge: one has
`fetch(SyncContext)` / `normalize(raw, symbol)`, the other
`fetch(DomainContext)` / `normalize(raw, key)`. The mixin touches neither
signature -- it only supplies `content_hash` and `upsert`.

`asof_gate_table` cannot be named plain `gate_table`: `HashGatedDataset`
already uses that name for something different (there the gate is a data
table). Same name, different contract across sibling classes would be a
silent trap.
"""

from __future__ import annotations

from typing import Any

from yfin.core import normalize as nz
from yfin.datasets.base import Dataset, NormalizedResult
from yfin.datasets.hash_gated import UNCHANGED_UPDATE_COLUMNS
from yfin.storage.contracts import RowWriter, TableWrite, WriteStats, apply_write

GATE_TABLE = "asof_state"
GATE_KEY_COLUMNS = ("symbol", "dataset")

# Gate for the domain (sector / industry) side. The symbol side's
# `asof_state` cannot be reused: its key is (symbol, dataset), and `symbol`
# in domain data tables is the company's symbol, not the domain's.
DOMAIN_GATE_TABLE = "domain_asof_state"
DOMAIN_GATE_KEY_COLUMNS = ("domain_key", "dataset", "region")

# Marker written to the gate row by region-less domain datasets
# (mirrors market_runner.GLOBAL_SCOPE_MARKER).
GLOBAL_REGION_MARKER = "*"

# Columns excluded from the hash. All three change on every run; if they
# were part of the hash body it would never match, and every row would be
# rewritten every day with nobody noticing.
#
# `first_seen_at` was added later. No data table has this column today
# (only `asof_state` does, in models/asof.py), so the hash of the existing
# 13 as-of datasets is unchanged. Without the exclusion,
# `research_reports.first_seen_at` would change on every run, enter the
# hash body, and the gate would never match.
VOLATILE_COLUMNS = frozenset({"as_of_date", "fetched_at", "first_seen_at"})

# Columns updated on the gate row when the hash changes. `first_seen_at` is
# deliberately excluded: if ON DUPLICATE KEY UPDATE covered it, the rule
# "written only on the first INSERT" would break.
GATE_UPDATE_COLUMNS = ("as_of_date", "content_hash", "row_count", "fetched_at")


def asof_produces(*tables: str, gate: str = GATE_TABLE) -> tuple[str, ...]:
    """Target tables plus the gate table.

    The `produces` contract means "table names this dataset writes," and
    the `_failed_records` error path relies on it; if the gate is not
    declared, the `asof_state` row falls out of auditing and `produces`
    diverges from actual output. This helper is the one place that knows
    the gate table's name.

    `gate` is a later addition; its default is unchanged, so all 13
    existing call sites produce identical output.
    """
    return (*tables, gate)


def _sort_key(row: dict[str, Any], key_columns: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(row.get(name)) for name in key_columns)


def _first_row(result: NormalizedResult) -> dict[str, Any]:
    """First data row in `writes` order.

    `next(..., None)`: `upsert` only calls this when `is_empty` is False,
    but `is_empty` means "no rows AND skipped is empty" -- a result with no
    rows but a populated `skipped` would raise StopIteration here. Not
    reachable today; kept explicit because the contract does not forbid it.
    """
    first = next((row for write in result.writes for row in write.rows), None)
    if first is None:  # pragma: no cover - defensive
        raise ValueError("kapi satiri icin veri satiri yok")
    return first


class AsOfGate:
    """as-of gate -- a mixin independent of the `Dataset` hierarchy.

    The subclass supplies `name` and `produces`; the mixin only provides
    `content_hash` and `upsert`. `gate_identity()` returns the gate row's key
    fields, defaulting to today's (symbol-side) behavior.
    """

    # Supplied by the subclass. `produces` is declared here too: `prune.py`
    # derives scope through `AsOfGate`, and the two sides (`Dataset` /
    # `DomainDataset`) share no common ancestor.
    name: str
    produces: tuple[str, ...]
    asof_gate_table: str = GATE_TABLE
    asof_gate_key_columns: tuple[str, ...] = GATE_KEY_COLUMNS

    def gate_identity(self, result: NormalizedResult) -> dict[str, Any]:
        """Key fields of the gate row (default: symbol side)."""
        return {"symbol": _first_row(result)["symbol"], "dataset": self.name}

    def content_hash(self, result: NormalizedResult) -> str:
        """SHA-256 of the canonical body of `result.writes`.

        Rows are sorted by `key_columns`. `nz.canonical_json` only sorts
        dict keys (sort_keys=True); list order is preserved. If a source --
        Yahoo's "top 10 institutions" list, insider_roster, domain
        `topCompanies` -- reorders with the same content, the hash would
        change and the gate would rewrite unnecessarily every day.
        """
        payload: list[dict[str, Any]] = []
        for write in sorted(result.writes, key=lambda w: w.table):
            stripped: list[dict[str, Any]] = [
                {k: v for k, v in row.items() if k not in VOLATILE_COLUMNS}
                for row in write.rows
            ]
            stripped.sort(key=lambda row: _sort_key(row, write.key_columns))
            payload.append({"table": write.table, "rows": stripped})
        return nz.content_hash(canonical=nz.canonical_json(payload))

    def _gate_write(
        self,
        result: NormalizedResult,
        *,
        digest: str,
        unchanged: bool,
    ) -> TableWrite:
        first = _first_row(result)
        row = {
            **self.gate_identity(result),
            "as_of_date": first["as_of_date"],
            "content_hash": digest,
            "row_count": sum(len(w.rows) for w in result.writes),
            "first_seen_at": first["fetched_at"],
            "fetched_at": first["fetched_at"],
        }
        return TableWrite(
            table=self.asof_gate_table,
            rows=[row],
            key_columns=self.asof_gate_key_columns,
            update_columns=UNCHANGED_UPDATE_COLUMNS if unchanged else GATE_UPDATE_COLUMNS,
        )

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        stats = WriteStats(skipped=dict(result.skipped))
        if result.is_empty:
            # No gate row is written for an empty result: otherwise every
            # non-fund symbol (or industry with no listing block) would
            # accumulate a dead row, and `first_seen_at` would drift to mean
            # "first time an empty result was returned." The cell is `empty`.
            return stats

        digest = self.content_hash(result)
        identity = self.gate_identity(result)
        current = writer.current_hash(self.asof_gate_table, identity)
        unchanged = current == digest

        if unchanged:
            # Data tables are not written. `skipped` is recorded only for a
            # table that carries rows; a target with an empty TableWrite
            # stays `empty` -- e.g. fund_top_holdings has 0 rows for BND.
            for write in result.writes:
                if write.rows:
                    stats.skipped[write.table] = stats.skipped.get(write.table, 0) + len(
                        write.rows
                    )
                else:
                    # A target carrying no rows would otherwise appear in no
                    # counter, fall outside `stats.tables()`, and never reach
                    # `_record_items` -- the table would drop out of
                    # auditing for that run. Zero attempted + zero skipped =
                    # `empty`: BND's fund_top_holdings is empty while its
                    # three sibling tables are `skipped`.
                    stats.attempted.setdefault(write.table, 0)
                    stats.verified.setdefault(write.table, 0)
        else:
            for write in result.writes:
                apply_write(writer, write, stats)

        # The gate row is written on both branches and enters the counters
        # (`apply_write`), matching how `HashGatedDataset` treats its header
        # row. Without that, `_failed_records` would produce a gate row from
        # `produces` on the error path but none on success, leaving
        # auditing asymmetric.
        apply_write(writer, self._gate_write(result, digest=digest, unchanged=unchanged), stats)
        return stats


class AsOfDataset[RawT](AsOfGate, Dataset[RawT]):
    """PK includes as_of_date; data tables are not written if content_hash is unchanged.

    Behavior is unchanged from before the gate logic was split out: the
    mixin's defaults (`GATE_TABLE`, `GATE_KEY_COLUMNS`, symbol identity) are
    exactly the values that were previously hardcoded here.
    """

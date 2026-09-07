"""The write contract, shared by the dataset side and the storage side.

This module exists to break a real cycle. `datasets/base.py` defined
`TableWrite`/`WriteStats` and `persistence.py` imported them; at the same
time `Dataset.upsert` calls `apply_write` and the dataset contract is
typed against `RowWriter`, both of which lived in `persistence.py`. The
cycle was held open only by a `TYPE_CHECKING` guard and a function-level
import -- that is, it was never resolved, only hidden.

Both sides now import downwards from here. Note what this module does NOT
import: no SQLAlchemy, no `yfin.datasets`, no `yfin.models`, no config.
That is the whole point -- if it ever gains one of those, the cycle comes
back through the new edge.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

WriteMode = Literal["upsert", "replace_scope"]

#: Columns that move on every run without anything having changed.
#:
#: They are excluded from the content hash for that reason, and the change
#: collector excludes them from the distinctness predicate for the same one:
#: a row whose only difference is `fetched_at` did not change, and
#: publishing it would make every consumer rewrite its mirror daily.
#:
#: One definition, imported by both sides. `datasets/asof_base.py` used to
#: carry its own copy, and two lists that must agree are a drift waiting to
#: happen -- `first_seen_at` was added to one of them months after the other.
#:
#: `last_seen_at` is here on the same grounds and was MEASURED into it: it
#: is written on every run by definition, and without it a settled daily
#: sync published one `symbols` update per symbol -- 4,500 events a night
#: saying only that the pipeline had looked. Only `symbols` carries the
#: column today, exactly as only `asof_state` carries `first_seen_at`; the
#: list is about what a name MEANS, so the next table to grow one inherits
#: the answer instead of repeating the discovery.
VOLATILE_COLUMNS: tuple[str, ...] = (
    "fetched_at",
    "first_seen_at",
    "last_seen_at",
    "as_of_date",
)


@dataclass(frozen=True)
class TableWrite:
    table: str
    rows: list[dict[str, Any]]
    key_columns: tuple[str, ...]  # used by the verification query
    update_columns: tuple[str, ...]  # columns updated on conflict
    mode: WriteMode = "upsert"
    # Columns that define replace_scope's delete scope. Default ("symbol",)
    # leaves existing call sites unchanged.
    scope_columns: tuple[str, ...] = ("symbol",)
    # Explicit scope values, for when they cannot be derived from rows (e.g.
    # rows is empty because every line item for the period came back NaN).
    # If omitted, derived from rows.
    scope_values: tuple[Mapping[str, Any], ...] | None = None
    # Columns where ON DUPLICATE KEY UPDATE applies GREATEST(current, new).
    # If the source can report 1 for the same row one run and 0 the next
    # (price_history.is_repaired), a plain upsert would write the
    # information backward; a monotonic column only moves forward.
    monotonic_columns: tuple[str, ...] = ()
    # Column that must move FORWARD for the update to apply, as a whole.
    # Distinct from monotonic_columns, which is per-column GREATEST: here
    # an older row updates NOTHING. The live quote table needs that --
    # applying it column by column would blend two different instants
    # into a state that never existed on any exchange.
    guard_column: str | None = None
    # Columns the distinctness predicate ignores, because they move on every
    # run whether or not anything changed. They are still WRITTEN -- the
    # writer touches them separately, so `fetched_at` (which the hash gate
    # reads as "last verified at") and `as_of_date` (which `prune_asof`
    # reads) end up exactly where a plain upsert would have put them.
    #
    # Per write rather than global, so a table that turns out to move a
    # fourth column on every run can say so without changing the default for
    # the other 67.
    volatile_columns: tuple[str, ...] = VOLATILE_COLUMNS


@dataclass
class WriteStats:
    attempted: dict[str, int] = field(default_factory=dict)
    verified: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)

    def tables(self) -> list[str]:
        seen: dict[str, None] = {}
        for src in (self.attempted, self.verified, self.skipped):
            for name in src:
                seen[name] = None
        return list(seen)


class VariantState(Protocol):
    """What a variant-scoped dataset needs to know about stored state.

    One method on purpose. The variant list used to be computed from a
    live `sqlalchemy.orm.Session` handed straight into the dataset layer,
    which put the ORM in the one package that is supposed to be free of
    it. The dataset asks a question; the storage layer answers it.
    """

    def disabled_variants(self) -> frozenset[str]:
        """Variant keys explicitly switched off in the database."""
        ...


class RowSink(Protocol):
    """Write-only capability.

    apply_write and plain upsert datasets need nothing more; code that
    reads no hashes and looks up no symbols depends on this narrow view.
    """

    def write(self, write: TableWrite) -> int:
        """Writes the rows and returns the count of *verified* rows."""
        ...


class HashReader(Protocol):
    """Read capability for the snapshot/hash gate."""

    def current_hash(self, table: str, key: Mapping[str, Any]) -> str | None:
        """Current content_hash at that key, or None.

        The key can span several columns: market_summary is
        (region, board_code), financial_periods is
        (symbol, statement, freq, period_end).
        """
        ...


class SymbolLookup(Protocol):
    """Lookup capability, used to flag out-of-universe symbols."""

    def known_symbols(self, candidates: set[str]) -> set[str]:
        """Those of the candidates that exist in `symbols`."""
        ...


class SnapshotWriter(RowSink, HashReader, Protocol):
    """What snapshot and hash-gated datasets see."""


class RowWriter(RowSink, HashReader, SymbolLookup, Protocol):
    """The full interface the dataset contract sees.

    Concrete PostgreSQL details (ON CONFLICT, the key-existence query)
    stay behind this protocol. Dataset.upsert takes the full interface;
    internal helpers depend on the narrowest one they need.
    """


def distinct_key_count(write: TableWrite) -> int:
    """Rows that can actually land, i.e. distinct key tuples.

    `len(rows)` is the wrong denominator: the writer collapses rows that
    share a key (ON CONFLICT cannot touch one row twice in a statement),
    so a batch carrying a duplicate key stores fewer rows than it holds.
    Counting the raw list made `verified != attempted` and marked a
    correct write FAILED.
    """
    if not write.key_columns:
        return len(write.rows)
    return len({tuple(row.get(name) for name in write.key_columns) for row in write.rows})


def apply_write(writer: RowSink, write: TableWrite, stats: WriteStats) -> None:
    """Applies one TableWrite and updates the stats.

    Also the single place every pipeline write passes through, which is why
    the row counters are here rather than at 57 call sites. `attempted` is
    the DISTINCT-KEY count the writer proposes, and keeps that meaning
    under the change design's distinctness predicate.
    """
    from yfin.core import metrics

    attempted = distinct_key_count(write)
    verified = writer.write(write)
    stats.attempted[write.table] = stats.attempted.get(write.table, 0) + attempted
    stats.verified[write.table] = stats.verified.get(write.table, 0) + verified
    metrics.inc("yfin_sync_write_rows_total", attempted, table=write.table, op="attempted")
    metrics.inc("yfin_sync_write_rows_total", verified, table=write.table, op="verified")

"""The write contract, shared by the dataset side and the storage side.

Both sides import downwards from here. This module must not import
SQLAlchemy, `yfin.datasets`, `yfin.models` or config, or the cycle returns.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

WriteMode = Literal["upsert", "replace_scope"]

#: Columns that move on every run without anything having changed. Excluded
#: from the content hash and from the change collector's distinctness
#: predicate; the one definition both sides import.
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
    # Columns where the upsert applies GREATEST(current, new): a source that
    # flips a flag back (price_history.is_repaired) must not move it backward.
    monotonic_columns: tuple[str, ...] = ()
    # Column that must move FORWARD for the update to apply as a whole; an
    # older row updates NOTHING. Per-column GREATEST would blend two
    # instants of a live quote into a state that never existed.
    guard_column: str | None = None
    # Columns the distinctness predicate ignores. They are still WRITTEN:
    # the writer touches them separately, so `fetched_at` and `as_of_date`
    # land exactly where a plain upsert would have put them.
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

    The dataset layer must stay free of the ORM, so it asks a question
    and the storage layer answers it.
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
        """Current content_hash at that key, or None. The key can span several columns."""
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

    Dataset.upsert takes this; internal helpers depend on the narrowest one they need.
    """


def distinct_key_count(write: TableWrite) -> int:
    """Rows that can actually land, i.e. distinct key tuples.

    The writer collapses rows sharing a key (ON CONFLICT cannot touch one
    row twice), so `len(rows)` would mark a correct write FAILED.
    """
    if not write.key_columns:
        return len(write.rows)
    return len({tuple(row.get(name) for name in write.key_columns) for row in write.rows})


def apply_write(writer: RowSink, write: TableWrite, stats: WriteStats) -> None:
    """Applies one TableWrite and updates the stats.

    Every pipeline write passes through here, so the row counters live here.
    `attempted` is the distinct-key count the writer proposes.
    """
    from yfin.core import metrics

    attempted = distinct_key_count(write)
    verified = writer.write(write)
    stats.attempted[write.table] = stats.attempted.get(write.table, 0) + attempted
    stats.verified[write.table] = stats.verified.get(write.table, 0) + verified
    metrics.inc("yfin_sync_write_rows_total", attempted, table=write.table, op="attempted")
    metrics.inc("yfin_sync_write_rows_total", verified, table=write.table, op="verified")

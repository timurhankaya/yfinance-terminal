"""as-of dataset base.

The gate row is always written; unchanged, only `fetched_at` ("last verified").
`AsOfGate` is a mixin: the symbol and domain hierarchies cannot share a base.
"""

from __future__ import annotations

from typing import Any

from yfin.core import normalize as nz
from yfin.datasets.base import Dataset, NormalizedResult
from yfin.datasets.hash_gated import UNCHANGED_UPDATE_COLUMNS
from yfin.storage.contracts import VOLATILE_COLUMNS as _VOLATILE_COLUMNS
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

# Columns excluded from the hash: they change on every run, so including
# them would make the gate never match.
VOLATILE_COLUMNS = frozenset(_VOLATILE_COLUMNS)

# Columns updated on the gate row when the hash changes. `first_seen_at` is
# deliberately excluded: if the conflict branch covered it, the rule
# "written only on the first INSERT" would break.
GATE_UPDATE_COLUMNS = ("as_of_date", "content_hash", "row_count", "fetched_at")


def asof_produces(*tables: str, gate: str = GATE_TABLE) -> tuple[str, ...]:
    """Target tables plus the gate table.

    `produces` must include the gate table or `_failed_records` drops the
    gate row from auditing on the error path.
    """
    return (*tables, gate)


def _sort_key(row: dict[str, Any], key_columns: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(row.get(name)) for name in key_columns)


class AsOfGate:
    """as-of gate -- a mixin independent of the `Dataset` hierarchy.

    The subclass supplies `name`, `produces` and `gate_source_tables`; the
    mixin only provides `content_hash` and `upsert`.
    """

    # Supplied by the subclass. `produces` is declared here too: `prune.py`
    # derives scope through `AsOfGate`, and the two sides (`Dataset` /
    # `DomainDataset`) share no common ancestor.
    name: str
    produces: tuple[str, ...]
    asof_gate_table: str = GATE_TABLE
    asof_gate_key_columns: tuple[str, ...] = GATE_KEY_COLUMNS

    #: Tables the gate row takes `as_of_date`, `fetched_at` and identity from,
    #: in the order to try. A tuple because for some datasets no single table
    #: is guaranteed to carry rows; the order here, not the order of `writes`,
    #: decides.
    gate_source_tables: tuple[str, ...] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """A concrete gated dataset that declares no source fails at import.

        `__abstractmethods__` is still empty here (ABCMeta fills it after this
        hook), so abstractness is checked by resolving MRO attributes via `cls`.
        """
        super().__init_subclass__(**kwargs)
        if not isinstance(getattr(cls, "name", None), str):
            # A class with no `name` is a base (cannot be registered), even
            # when it already implements `fetch` and `normalize`.
            return
        names = {name for klass in cls.__mro__ for name in vars(klass)}
        if any(
            getattr(getattr(cls, name, None), "__isabstractmethod__", False) for name in names
        ):
            return
        if not cls.gate_source_tables:
            raise TypeError(
                f"{cls.__name__} is an as-of gated dataset and declares no "
                "`gate_source_tables`; the gate row has nowhere to take its "
                "as_of_date, fetched_at and identity from"
            )

    def gate_row(self, result: NormalizedResult) -> dict[str, Any]:
        """First row of the first declared source table that has rows.

        Raises on a non-empty result with no such row: the declaration then
        names tables this dataset does not fill.
        """
        by_table = {write.table: write.rows for write in result.writes}
        for table in self.gate_source_tables:
            rows = by_table.get(table)
            if rows:
                return rows[0]
        raise ValueError(
            f"{self.name}: none of the declared gate source tables "
            f"{self.gate_source_tables} carries a row"
        )

    def gate_identity(self, result: NormalizedResult) -> dict[str, Any]:
        """Key fields of the gate row (default: symbol side)."""
        return {"symbol": self.gate_row(result)["symbol"], "dataset": self.name}

    def content_hash(self, result: NormalizedResult) -> str:
        """SHA-256 of the canonical body of `result.writes`.

        Rows are sorted by `key_columns` because `canonical_json` preserves
        list order, and sources reorder rows with unchanged content.
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
        first = self.gate_row(result)
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

    def upsert(
        self, writer: RowWriter, result: NormalizedResult, *, full_refresh: bool = False
    ) -> WriteStats:
        stats = WriteStats(skipped=dict(result.skipped))
        if result.is_empty:
            # No gate row is written for an empty result: otherwise every
            # non-fund symbol (or industry with no listing block) would
            # accumulate a dead row, and `first_seen_at` would drift to mean
            # "first time an empty result was returned." The cell is `empty`.
            return stats

        digest = self.content_hash(result)
        # `--full-refresh` bypasses the gate: it exists to repair data rows
        # lost while the gate row survived.
        unchanged = False
        if not full_refresh:
            identity = self.gate_identity(result)
            current = writer.current_hash(self.asof_gate_table, identity)
            unchanged = current == digest

        if unchanged:
            # Data tables are not written. `skipped` is recorded only for a
            # table that carries rows; a target with an empty TableWrite
            # stays `empty`.
            for write in result.writes:
                if write.rows:
                    stats.skipped[write.table] = stats.skipped.get(write.table, 0) + len(
                        write.rows
                    )
                else:
                    # Keep a row-less target in the counters so it stays in
                    # auditing; zero attempted + zero skipped = `empty`.
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
    """PK includes as_of_date; data tables are not written if content_hash is unchanged."""

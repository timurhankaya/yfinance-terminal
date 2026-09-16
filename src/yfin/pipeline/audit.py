"""The run's audit trail: sync_runs, sync_run_items, and the exit code.

The run's outcome is computed from the item rows, never from what the
workers report, so the two cannot diverge.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.logging_setup import get_logger
from yfin.datasets.meta import DatasetMeta
from yfin.datasets.registry import SYMBOL_DATASETS, Registry
from yfin.models import ItemStatus, RunScope, RunStatus, SyncRun, SyncRunItem
from yfin.pipeline.payload import SymbolPayload
from yfin.scheduler.jobs import JOB_RUN_ID_VAR
from yfin.storage.contracts import WriteStats

log = get_logger(__name__)

EXIT_OK = 0
EXIT_NO_SYMBOL_RESOLVED = 1
EXIT_PARTIAL = 2
EXIT_ALL_FAILED = 3
EXIT_LOCK_NOT_ACQUIRED = 4
EXIT_NO_PROXY = 5  # --require-proxy given but no usable proxy

@dataclass
class ItemRecord:
    symbol: str
    dataset: str
    status: ItemStatus
    table_name: str | None = None
    rows_fetched: int = 0
    rows_written: int = 0
    rows_verified: int = 0
    rows_skipped: int = 0
    duration_ms: int | None = None
    error: str | None = None
    # Region axis for domain cells. NULL for symbol and market cells:
    # `market_runner` writes region into `symbol`, and audit queries rely on that.
    region: str | None = None
    # The `ErrorKind` behind `error`, when one was classified. `error` is for
    # a human; this is what a dashboard groups by, and grouping by the free
    # text would give one bucket per Yahoo error string.
    error_kind: str | None = None


def record_items(
    dataset: DatasetMeta,
    symbol: str,
    stats: WriteStats,
    fetched: int,
    duration_ms: int,
    *,
    region: str | None = None,
) -> list[ItemRecord]:
    """One row per table for datasets that write to multiple tables."""
    records: list[ItemRecord] = []
    # `or [None]`: without this, a tracking-only dataset (sustainability,
    # produces=()) writes no row at all and vanishes from the audit.
    tables: list[str | None] = list(stats.tables() or dataset.produces) or [None]
    for position, table in enumerate(tables):
        # table is None only for a tracking-only dataset (writes nothing);
        # all counters are 0 there and the cell is EMPTY.
        attempted = stats.attempted.get(table, 0) if table else 0
        verified = stats.verified.get(table, 0) if table else 0
        skipped = stats.skipped.get(table, 0) if table else 0
        if attempted == 0 and skipped == 0:
            status = ItemStatus.EMPTY  # no source data - not an error
        elif attempted == 0 and skipped:
            status = ItemStatus.SKIPPED  # content_hash unchanged
        elif verified == attempted:
            status = ItemStatus.OK
        else:
            status = ItemStatus.FAILED
        records.append(
            ItemRecord(
                symbol=symbol,
                dataset=dataset.name,
                status=status,
                table_name=table,
                region=region,
                # `fetched` counts the whole dataset, not one table; only the
                # first table row carries it so sync_runs.rows_fetched is not inflated.
                rows_fetched=fetched if position == 0 else 0,
                rows_written=attempted,
                rows_verified=verified,
                rows_skipped=skipped,
                duration_ms=duration_ms,
                error=(
                    f"rows_verified({verified}) != rows_attempted({attempted})"
                    if status is ItemStatus.FAILED
                    else None
                ),
            )
        )
    return records


def failed_records(
    symbol: str,
    dataset_name: str,
    error: str,
    registry: Registry[Any] = SYMBOL_DATASETS,
    *,
    region: str | None = None,
    kind: str | None = None,
) -> list[ItemRecord]:
    """One record per table for a failed cell, so audit queries can filter per table.

    `kind` is the classified `ErrorKind`, or `write` / `crash`, which the
    classifier never sees; NULL when nothing classified it.
    """
    dataset = registry.get(dataset_name)
    # Same gap exists on the error path: produces=() -> tuple(()) -> no
    # rows. `or (None,)` covers it.
    tables: tuple[str | None, ...] = (tuple(dataset.produces) if dataset else ()) or (None,)
    return [
        ItemRecord(
            symbol=symbol,
            dataset=dataset_name,
            status=ItemStatus.FAILED,
            table_name=table,
            error=error,
            region=region,
            error_kind=kind,
        )
        for table in tables
    ]


def skipped_records(
    symbol: str,
    dataset_name: str,
    reason: str,
    registry: Registry[Any] = SYMBOL_DATASETS,
    status: ItemStatus = ItemStatus.SKIPPED,
    *,
    region: str | None = None,
) -> list[ItemRecord]:
    """One record per table for a cell excluded before running.

    `status` also covers OUT_OF_SCOPE; the reason travels in `error`.
    """
    dataset = registry.get(dataset_name)
    tables: tuple[str | None, ...] = (tuple(dataset.produces) if dataset else ()) or (None,)
    return [
        ItemRecord(
            symbol=symbol,
            dataset=dataset_name,
            status=status,
            table_name=table,
            error=reason,
            region=region,
        )
        for table in tables
    ]


def channel_records(payload: SymbolPayload) -> list[ItemRecord]:
    """Audit rows for the channels that never reached the writer.

    Both the success and the retry-exhausted path must write these, or
    those cells leave no row at all.
    """
    records: list[ItemRecord] = []
    for dataset_name, error, kind in payload.failures:
        records.extend(failed_records(payload.symbol, dataset_name, error, kind=kind))
    for dataset_name, reason in payload.skipped:
        records.extend(skipped_records(payload.symbol, dataset_name, reason))
    for dataset_name, reason in payload.out_of_scope:
        records.extend(
            skipped_records(payload.symbol, dataset_name, reason, status=ItemStatus.OUT_OF_SCOPE)
        )
    return records


def _job_run_id() -> int | None:
    """`scheduler_runs.id` from the environment, when a scheduler set it.

    A malformed value yields a manual run rather than a crash.
    """
    raw = os.environ.get(JOB_RUN_ID_VAR)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("ignoring malformed job run id", value=raw)
        return None


def open_run(
    factory: sessionmaker[Session],
    *,
    symbol_count: int,
    dataset_count: int,
    shard_count: int = 1,
    scope: RunScope = RunScope.SYMBOLS,
    selector: str | None = None,
) -> int:
    """Opens a sync_runs row and commits it.

    Children write items over a separate connection, so an uncommitted
    run_id would fail the FK. `job_run_id` comes from `YF_JOB_RUN_ID`.
    """
    with factory() as session:
        run = SyncRun(
            started_at=datetime.now(UTC),
            scope=scope,
            status=RunStatus.RUNNING,
            symbol_count=symbol_count,
            dataset_count=dataset_count,
            shard_count=shard_count,
            # Without this, which universe a run covered can't be
            # reconstructed later, and completeness can't be audited.
            selector=selector,
            job_run_id=_job_run_id(),
        )
        session.add(run)
        session.commit()
        return int(run.id)


# Shards can write the same news / news_symbols row concurrently, so
# serialization_failure (40001) and deadlock_detected (40P01) are retried.
# 55P03 is not listed: NOWAIT / SKIP LOCKED are not used on this path.
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})


def write_items(
    factory: sessionmaker[Session],
    run_id: int,
    records: Sequence[ItemRecord],
    *,
    shard_index: int = 0,
    proxy_id: int | None = None,
    proxy_label: str | None = None,
) -> None:
    """Writes audit records in a separate transaction.

    In the same transaction as the symbol write, a rollback would erase
    the audit trail too -- exactly when a record of the failure matters.
    """
    if not records:
        return
    with factory() as session:
        session.add_all(
            SyncRunItem(
                run_id=run_id,
                symbol=item.symbol,
                dataset=item.dataset,
                status=item.status,
                table_name=item.table_name,
                rows_fetched=item.rows_fetched,
                rows_written=item.rows_written,
                rows_verified=item.rows_verified,
                rows_skipped=item.rows_skipped,
                duration_ms=item.duration_ms,
                error=item.error,
                error_kind=item.error_kind,
                region=item.region,
                shard_index=shard_index,
                proxy_id=proxy_id,
                proxy_label=proxy_label,
            )
            for item in records
        )
        session.commit()


def record_not_attempted(
    factory: sessionmaker[Session], run_id: int, symbols: Sequence[str]
) -> None:
    """Symbols left unprocessed in the queue.

    Without these rows the run could report 'ok' while symbols were never fetched.
    """
    write_items(
        factory,
        run_id,
        [
            ItemRecord(
                symbol=symbol,
                dataset=SYMBOL_DATASETS.bootstrap or "symbols",
                status=ItemStatus.NOT_ATTEMPTED,
                error="shard withdrawn; symbol left in the queue",
            )
            for symbol in symbols
        ],
    )


@dataclass
class RunTally:
    """The single source of truth for a run's result: sync_run_items aggregation."""

    run_id: int
    symbol_count: int
    dataset_count: int
    resolved_symbols: int
    counts: dict[str, int] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)

    @property
    def failed(self) -> int:
        return self.counts.get(ItemStatus.FAILED.value, 0)

    @property
    def not_attempted(self) -> int:
        return self.counts.get(ItemStatus.NOT_ATTEMPTED.value, 0)

    @property
    def cells(self) -> int:
        """Cells excluding unknown_symbol, not_attempted, and out_of_scope.

        Never-attempted cells must not count toward "everything attempted failed".
        """
        excluded = {
            ItemStatus.UNKNOWN_SYMBOL.value,
            ItemStatus.NOT_ATTEMPTED.value,
            ItemStatus.OUT_OF_SCOPE.value,
        }
        return sum(v for k, v in self.counts.items() if k not in excluded)

    def exit_code(self) -> int:
        """No failures -> 0 (ok/empty/skipped are all fine).

        The exit code can never be 0 while an unprocessed symbol remains.
        """
        if self.symbol_count and self.resolved_symbols == 0:
            return EXIT_NO_SYMBOL_RESOLVED
        if self.failed == 0:
            return EXIT_PARTIAL if self.not_attempted else EXIT_OK
        if self.cells and self.failed == self.cells:
            return EXIT_ALL_FAILED
        return EXIT_PARTIAL


_STATUS_TO_RUN = {
    EXIT_OK: RunStatus.OK,
    EXIT_PARTIAL: RunStatus.PARTIAL,
    EXIT_ALL_FAILED: RunStatus.FAILED,
    EXIT_NO_SYMBOL_RESOLVED: RunStatus.FAILED,
}


def finalize_run(
    factory: sessionmaker[Session],
    run_id: int,
    *,
    symbol_count: int,
    dataset_count: int,
) -> RunTally:
    """Computes totals and the exit code from sync_run_items, closes out sync_runs.

    Totals never come from the result queue, so they cannot diverge from
    what the children wrote. The caller must have joined all children.
    """
    with factory() as session:
        rows = session.execute(
            select(
                SyncRunItem.status,
                func.count(),
                func.coalesce(func.sum(SyncRunItem.rows_fetched), 0),
                func.coalesce(func.sum(SyncRunItem.rows_written), 0),
                func.coalesce(func.sum(SyncRunItem.rows_verified), 0),
                func.coalesce(func.sum(SyncRunItem.rows_skipped), 0),
            )
            .where(SyncRunItem.run_id == run_id)
            .group_by(SyncRunItem.status)
        ).all()

        counts: dict[str, int] = {}
        totals = {
            "rows_fetched": 0,
            "rows_written": 0,
            "rows_verified": 0,
            "rows_skipped": 0,
        }
        for status, count, fetched, written, verified, skipped in rows:
            counts[ItemStatus(status).value] = int(count)
            totals["rows_fetched"] += int(fetched)
            totals["rows_written"] += int(written)
            totals["rows_verified"] += int(verified)
            totals["rows_skipped"] += int(skipped)

        # A resolved symbol is one with no unknown_symbol row at all.
        unresolved_flag = case((SyncRunItem.status == ItemStatus.UNKNOWN_SYMBOL, 1), else_=0)
        resolved_subq = (
            select(SyncRunItem.symbol)
            .where(SyncRunItem.run_id == run_id)
            .group_by(SyncRunItem.symbol)
            .having(func.sum(unresolved_flag) == 0)
            .subquery()
        )
        resolved = int(
            session.execute(select(func.count()).select_from(resolved_subq)).scalar_one()
        )

        # A symbol with no audit row at all was pulled off the queue by a
        # shard that crashed or timed out; `shard.py` only drains what is
        # still queued. Counting the gap keeps the exit code non-zero.
        # symbol_count=0 for market runs, so this is a no-op there.
        covered = int(
            session.execute(
                select(func.count(func.distinct(SyncRunItem.symbol))).where(
                    SyncRunItem.run_id == run_id
                )
            ).scalar_one()
        )
        missing = max(0, symbol_count - covered)
        if missing:
            log.error(
                "run reconciliation: symbols missing from audit",
                run_id=run_id,
                expected=symbol_count,
                covered=covered,
                missing=missing,
            )
            counts[ItemStatus.NOT_ATTEMPTED.value] = (
                counts.get(ItemStatus.NOT_ATTEMPTED.value, 0) + missing
            )

        tally = RunTally(
            run_id=run_id,
            symbol_count=symbol_count,
            dataset_count=dataset_count,
            resolved_symbols=resolved,
            counts=counts,
            totals=totals,
        )
        session.execute(
            update(SyncRun)
            .where(SyncRun.id == run_id)
            .values(
                finished_at=datetime.now(UTC),
                status=_STATUS_TO_RUN[tally.exit_code()],
                **totals,
            )
        )
        session.commit()
    return tally

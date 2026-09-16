"""The transaction boundary for one symbol, and the retry around it.

Audit rows are written separately: in the same transaction a rollback
would erase the record of the failure.
"""

from __future__ import annotations

import random
import time

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from yfin.core import metrics, tracing
from yfin.core.logging_setup import get_logger
from yfin.models import Symbol
from yfin.pipeline.audit import ItemRecord, channel_records, failed_records, record_items
from yfin.pipeline.payload import SymbolPayload
from yfin.storage.changes import ChangeCollector
from yfin.storage.persistence import PostgresRowWriter
from yfin.storage.rescale import apply_pending

log = get_logger(__name__)

def persist_symbol(
    session: Session,
    payload: SymbolPayload,
    collector: ChangeCollector | None = None,
) -> list[ItemRecord]:
    """One transaction per symbol: written as a whole or not at all.

    The collector is flushed as the LAST statement before the commit; the
    outbox relay waits on the gap between flush and commit.
    """
    records: list[ItemRecord] = []
    writer = PostgresRowWriter(session, collector=collector)
    # One span per symbol: this is the transaction boundary. `symbol` is a
    # span attribute and forbidden as a metric label (a series is kept forever).
    tracing_span = tracing.span(
        "sync.symbol",
        symbol=payload.symbol,
        dataset_count=len(payload.results),
    )
    # Rescale runs before price_bars is written, in the same transaction:
    # bars written in this run are already at Yahoo's current scale, and
    # persist_with_retry replays the whole block on a lock conflict.
    with tracing_span as current:
        rescale_before_bars(session, payload, collector)
        for dataset, result, fetched, duration in payload.results:
            if collector is not None:
                # Labels the events that follow. The dataset is the answer to
                # "which fetch produced this", which the table alone cannot give:
                # six datasets write `symbols`.
                collector.enter_dataset(dataset.name)
            stats = dataset.upsert(writer, result, full_refresh=payload.full_refresh)
            records.extend(
                record_items(dataset, payload.symbol, stats, fetched, duration)
            )
        if collector is not None:
            collector.flush(session)
        records.extend(channel_records(payload))
        # At the end, because the number does not exist until now -- which
        # is the whole reason the span is around the loop rather than
        # inside it.
        tracing.set_attributes(
            current, rows_written=sum(record.rows_written for record in records)
        )
    return records


def rescale_before_bars(
    session: Session,
    payload: SymbolPayload,
    collector: ChangeCollector | None = None,
) -> None:
    """Applies pending splits if any dataset writes to price_bars.

    Runs before the dataset loop, so the collector's `dataset` label is
    still None: a rescale is not something a dataset did.
    """
    writes_bars = any(
        "price_bars" in dataset.produces for dataset, _result, _f, _d in payload.results
    )
    if not writes_bars:
        return
    try:
        apply_pending(session, payload.symbol, collector=collector)
    except Exception as exc:  # noqa: BLE001 - a hook failure must not drop the symbol
        # Swallowing this is dangerous since we're in the same transaction:
        # a broken rescale would silently stick. Re-raise instead and let
        # persist_with_retry and the caller handle it.
        log.error("rescale hook failed", symbol=payload.symbol, error=str(exc))
        raise


def mark_unknown(session: Session, symbol: str, threshold: int) -> None:
    """A symbol unknown for 5 consecutive runs gets is_active=0. Data is not deleted."""
    row = session.get(Symbol, symbol)
    if row is None:
        # The universe is managed by hand; a symbol not on record has no
        # streak counter. Log it instead of staying silent.
        log.info("unknown symbol not tracked (not in symbols table)", symbol=symbol)
        return
    streak = (row.unknown_streak or 0) + 1
    session.execute(
        update(Symbol)
        .where(Symbol.symbol == symbol)
        .values(unknown_streak=streak, is_active=streak < threshold)
    )


# Shards can write the same news / news_symbols row concurrently, so
# serialization_failure (40001) and deadlock_detected (40P01) are retried.
# 55P03 is not listed: NOWAIT / SKIP LOCKED are not used on this path.
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})


def is_lock_conflict(exc: BaseException) -> bool:
    """Checks SQLSTATE, not the error message, which varies by locale and driver.

    An exception without `orig` (a programming error) yields None and skips the retry.
    """
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate in _RETRYABLE_SQLSTATES


def persist_with_retry(
    factory: sessionmaker[Session], payload: SymbolPayload, attempts: int
) -> list[ItemRecord]:
    """Symbol transaction with jittered retry on a lock conflict.

    Safe to replay because the transaction is symbol-scoped and idempotent.
    PostgreSQL requires the rollback before the retry.
    """
    last_error = ""
    for attempt in range(1, attempts + 1):
        with factory() as session:
            try:
                # One collector PER ATTEMPT. A replayed transaction re-does
                # the writes, so reusing the first attempt's collector would
                # publish the rolled-back attempt's events as well as the
                # committed one's.
                collector = (
                    ChangeCollector(payload.changes) if payload.changes else None
                )
                records = persist_symbol(session, payload, collector)
                session.commit()
                return records
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts and is_lock_conflict(exc):
                    metrics.inc("yfin_sync_retries_total", kind="lock_conflict")
                    delay = 0.05 * attempt + random.uniform(0, 0.05)
                    log.warning(
                        "lock conflict; retrying",
                        symbol=payload.symbol,
                        attempt=attempt,
                        error=last_error,
                    )
                    time.sleep(delay)
                    continue
                log.error("symbol transaction failed", symbol=payload.symbol, error=last_error)
                break
    records = [
        record
        for dataset, _, _, _ in payload.results
        # `write`, not a classified kind: `classify_error` reads upstream
        # failures, and this one is the transaction. Calling it here would
        # file a lock conflict under a Yahoo error class.
        for record in failed_records(payload.symbol, dataset.name, last_error, kind="write")
    ]
    # The other channels never depend on the write layer; dropping them
    # would leave no row at all for those cells.
    records.extend(channel_records(payload))
    return records

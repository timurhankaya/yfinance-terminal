"""The transaction boundary for one symbol, and the retry around it.

One symbol is one transaction: a failure rolls back that symbol and
nothing else. The audit rows are written separately, on purpose -- in the
same transaction a rollback would erase the record of the failure exactly
when it matters most.
"""

from __future__ import annotations

import random
import time

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

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

    The collector, when there is one, is filled here and flushed as the LAST
    statement before the caller commits. That ordering is what keeps the
    outbox window small: the pipeline relay cannot pass an open writing
    transaction, so the gap between the flush and the commit is the gap it
    waits on.
    """
    records: list[ItemRecord] = []
    writer = PostgresRowWriter(session, collector=collector)
    # Rescale hook runs before price_bars is written, in the same
    # transaction. In the reverse order, bars written in this run (already
    # at Yahoo's current scale) would get split again. A separate
    # transaction doesn't work either: persist_with_retry replays the
    # whole block on a lock conflict, and a rescale that already committed
    # would muddy the accounting even if not reapplied.
    rescale_before_bars(session, payload, collector)
    for dataset, result, fetched, duration in payload.results:
        if collector is not None:
            # Labels the events that follow. The dataset is the answer to
            # "which fetch produced this", which the table alone cannot give:
            # six datasets write `symbols`.
            collector.enter_dataset(dataset.name)
        stats = dataset.upsert(writer, result, full_refresh=payload.full_refresh)
        records.extend(record_items(dataset, payload.symbol, stats, fetched, duration))
    if collector is not None:
        collector.flush(session)
    records.extend(channel_records(payload))
    return records


def rescale_before_bars(
    session: Session,
    payload: SymbolPayload,
    collector: ChangeCollector | None = None,
) -> None:
    """Applies pending splits if any dataset writes to price_bars.

    Only runs when price_bars will actually be written, so a run like
    `--datasets info` doesn't needlessly query splits/bar_rescales.

    It runs BEFORE the dataset loop, so the collector already exists here
    and its `dataset` label is still None -- which is right: a rescale is
    not something a dataset did.
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


# PostgreSQL SQLSTATEs. Symbols are spread across shards, so two processes
# can write the same news / news_symbols row; a single process had no such
# risk.
#   40001 serialization_failure
#   40P01 deadlock_detected
#
# 55P03 (lock_not_available) is deliberately not listed: it can't occur on
# this code path since NOWAIT / SKIP LOCKED are not used. Retrying an
# unjustified SQLSTATE would silently legitimize the wrong behavior if
# NOWAIT is ever added.
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})


def is_lock_conflict(exc: BaseException) -> bool:
    """Checks SQLSTATE, not the error message.

    Text matching is affected by localized messages and driver formatting
    changes; SQLSTATE is structural and stable. psycopg3 exceptions carry
    `sqlstate`, and SQLAlchemy exposes it under `DBAPIError.orig`. An
    exception without `orig` (a programming error) makes the getattr
    chain return None and correctly skips the retry.
    """
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate in _RETRYABLE_SQLSTATES


def persist_with_retry(
    factory: sessionmaker[Session], payload: SymbolPayload, attempts: int
) -> list[ItemRecord]:
    """Symbol transaction with jittered retry on a lock conflict.

    Safe to replay because the transaction is symbol-scoped and
    idempotent. In PostgreSQL a failed transaction always enters aborted
    state and accepts nothing but ROLLBACK, so the rollback before retry
    is not optional -- the engine enforces it.
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
        for record in failed_records(payload.symbol, dataset.name, last_error)
    ]
    # The other three channels stay in the audit too. They don't depend on
    # the write layer: `failures` blew up during fetch, `skipped` and
    # `out_of_scope` never hit the network at all. Dropping them would
    # leave no row for those cells -- not even `failed`, just absence --
    # and silently mislead "when was this dataset last attempted".
    records.extend(channel_records(payload))
    return records

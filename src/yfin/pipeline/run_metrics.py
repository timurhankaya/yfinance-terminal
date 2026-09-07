"""Getting a shard's counters into `run_metrics`.

A shard is a short-lived process: it exits long before any scrape could
reach it, so its counters go into memory and are written here on the way
out. The exporter reads the table and turns it into gauges.

Written in its OWN short transaction, after the symbol transactions are
done. A metrics failure must not roll back data, and it must not be able to
fail a run either: `flush` swallows its own errors and says so in the log.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.logging_setup import get_logger
from yfin.core.metrics import Accumulator, current_accumulator

log = get_logger(__name__)


def flush(
    factory: sessionmaker[Session],
    run_id: int,
    shard_index: int,
    accumulator: Accumulator | None = None,
) -> int:
    """Writes this process's counters. Returns the number of rows.

    `ON CONFLICT DO UPDATE` rather than plain INSERT: a shard that somehow
    flushed twice should leave the value it last held, not fail the process
    on a primary-key violation at the very end of a successful run.
    """
    accumulator = accumulator or current_accumulator()
    if accumulator is None:
        return 0
    rows = accumulator.rows()
    if not rows:
        return 0

    try:
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO run_metrics (run_id, shard_index, name, labels, value) "
                    "VALUES (:run_id, :shard_index, :name, :labels, :value) "
                    "ON CONFLICT (run_id, shard_index, name, labels) "
                    "DO UPDATE SET value = excluded.value"
                ),
                [
                    {
                        "run_id": run_id,
                        "shard_index": shard_index,
                        "name": row.name,
                        "labels": row.labels,
                        "value": row.value,
                    }
                    for row in rows
                ],
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001 - observability never fails a run
        log.warning(
            "run metrics not written",
            run_id=run_id,
            shard=shard_index,
            rows=len(rows),
            error=str(exc),
        )
        return 0
    return len(rows)


__all__ = ["flush"]

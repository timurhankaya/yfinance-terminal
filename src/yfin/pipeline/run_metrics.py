"""Getting a shard's counters into `run_metrics` for the exporter.

Written in its own short transaction after the symbol transactions: a
metrics failure must neither roll back data nor fail the run.
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

    `ON CONFLICT DO UPDATE`: a second flush must not fail the run at its end.
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

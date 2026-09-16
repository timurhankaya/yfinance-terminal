"""Capturing pipeline writes as change events.

One envelope per changed row, queued into `pipeline_outbox` by the same
transaction that wrote the data. Routing lives in `routing.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from yfin.core.families import DataFamily
from yfin.core.normalize import canonical_json
from yfin.storage.copy import copy_body
from yfin.storage.routing import INFRASTRUCTURE_TABLES, ROUTES

#: Bumped when a field is removed or renamed. Adding one is compatible:
#: consumers ignore what they do not know.
ENVELOPE_VERSION = 1

#: What an event says happened.
ChangeOp = Literal["insert", "update", "delete", "range", "rescale"]

#: Column order for the COPY body. `id` and `xid` are omitted on purpose:
#: the first is an identity, the second defaults to `pg_current_xact_id()`,
#: which the data writes ahead of the flush have already assigned.
OUTBOX_COLUMNS: tuple[str, ...] = ("created_at", "family", "partition_key", "payload")

#: A rescale rewrites `price_bars` and nothing else.
RESCALE_TABLE = "price_bars"

#: The key column a range event's span is measured over, per bars table.
#: A consumer re-reads the span by this field, so it is written out, not guessed.
BARS_TIME_COLUMN: Mapping[str, str] = {
    "price_bars": "ts_utc",
    "periodic_bars": "ts_utc",
    "price_history": "session_date",
    "dividends": "ex_date",
    "splits": "split_date",
    "capital_gains": "gain_date",
}

#: Bars tables whose rows are also keyed by an interval.
BARS_INTERVAL_TABLES: frozenset[str] = frozenset({"price_bars", "periodic_bars"})

#: `_append`'s default for `dataset`, meaning "whatever `enter_dataset` last
#: set". `None` cannot carry that meaning: it is the real answer for `purge`
#: and `rescale`, which run outside any dataset, so the two cases need
#: separate values.
_CURRENT_DATASET: Any = object()


@dataclass(frozen=True)
class ChangeContext:
    """What a collector needs that only the caller knows.

    The collector is built from it per attempt, so a replayed transaction
    produces one set of events rather than two.
    """

    #: `sync_runs.id`, or None outside a sync. A scheduler run is reachable
    #: through `sync_runs.job_run_id` and is not repeated in the envelope.
    run_id: int | None

    #: Above this many inserted rows, a write to a bars-family table becomes
    #: one range event instead of one event per bar.
    range_threshold: int


def context_for(
    *, enabled: bool, run_id: int | None, range_threshold: int
) -> ChangeContext | None:
    """A context, or None when change publishing is off.

    None reaches `PostgresRowWriter`, which then emits no `RETURNING *` and
    no outbox row. Takes values, not `Settings`: this module must not import config.
    """
    if not enabled:
        return None
    return ChangeContext(run_id=run_id, range_threshold=range_threshold)


@dataclass(frozen=True)
class ChangeEvent:
    """One outbox row, rendered but not yet written."""

    family: str
    partition_key: str
    payload: str


class ChangeCollector:
    """Accumulates the events of one transaction, then renders them.

    Holds no session: `flush` takes one, so it can be filled before a transaction opens.
    """

    def __init__(self, ctx: ChangeContext) -> None:
        self._ctx = ctx
        self._dataset: str | None = None
        self._events: list[ChangeEvent] = []

    @property
    def pending(self) -> list[ChangeEvent]:
        """The events recorded so far, in write order."""
        return self._events

    @property
    def range_threshold(self) -> int:
        return self._ctx.range_threshold

    def enter_dataset(self, name: str | None) -> None:
        """Labels everything recorded from here on.

        `None` for `purge`, `prune` and `bars rescale`, which run outside any dataset.
        """
        self._dataset = name

    # --- recording ---------------------------------------------------------

    def record(
        self,
        table: str,
        op: ChangeOp,
        key: Mapping[str, Any],
        row: Mapping[str, Any] | None,
    ) -> None:
        """One row-level event, unless the table is infrastructure.

        `row` is the row as `RETURNING *` gave it back, so the consumer sees
        merged columns and server defaults. `None` for a delete.
        """
        if table in INFRASTRUCTURE_TABLES:
            return
        self._append(table, op, key, row)

    def record_range(
        self,
        table: str,
        symbol: str,
        *,
        kind: Literal["write", "delete"],
        bar_interval: str | None,
        ts_column: str,
        ts_from: datetime | date | None,
        ts_to: datetime | date | None,
        rows: int,
    ) -> None:
        """One event standing for a bulk write or delete on a bars table.

        The consumer re-reads the span over `ts_column`; `bar_interval` is
        null for date-keyed tables. A purge leaves the span null.
        """
        route = ROUTES[table]
        if route.family is not DataFamily.BARS:
            raise ValueError(
                f"{table} is not in the bars family; range coalescing exists because "
                "bar writes are enormous, and using it elsewhere would hide changes "
                "the consumer could have applied directly"
            )
        self._append(
            table,
            "range",
            {"symbol": symbol},
            {
                "kind": kind,
                "bar_interval": bar_interval,
                "ts_column": ts_column,
                "ts_from": ts_from,
                "ts_to": ts_to,
                "rows": rows,
            },
        )

    def record_rescale(
        self,
        symbol: str,
        split_date: date,
        factor: Decimal,
        applied_before: datetime,
    ) -> None:
        """A split applied to the stored history; one event, since it rewrites every bar.

        `dataset` is null: `storage/rescale.py` runs outside the dataset loop.
        """
        self._append(
            RESCALE_TABLE,
            "rescale",
            {"symbol": symbol, "split_date": split_date},
            {"factor": factor, "applied_before": applied_before},
            dataset=None,
        )

    def _append(
        self,
        table: str,
        op: ChangeOp,
        key: Mapping[str, Any],
        row: Mapping[str, Any] | None,
        *,
        dataset: str | None = _CURRENT_DATASET,
    ) -> None:
        route = ROUTES[table]
        self._events.append(
            ChangeEvent(
                family=route.family.value,
                partition_key=str(key[route.partition_column]),
                payload=canonical_json(
                    {
                        "v": ENVELOPE_VERSION,
                        "op": op,
                        "family": route.family.value,
                        "dataset": (
                            self._dataset if dataset is _CURRENT_DATASET else dataset
                        ),
                        "table": table,
                        "key": dict(key),
                        "row": None if row is None else dict(row),
                        "run_id": self._ctx.run_id,
                        # Replaced with the flush timestamp; see `copy_body`.
                        "occurred_at": None,
                    }
                ),
            )
        )

    # --- writing -----------------------------------------------------------

    def copy_body(self, occurred_at: datetime) -> str:
        """The COPY payload for every pending event.

        Separate from `flush` so the rendering can be read without a
        database, which is what the unit tests do.
        """
        rows = [
            {
                "created_at": occurred_at,
                "family": event.family,
                "partition_key": event.partition_key,
                "payload": _with_occurred_at(event.payload, occurred_at),
            }
            for event in self._events
        ]
        return copy_body(rows, OUTBOX_COLUMNS)

    def flush(self, session: Session) -> int:
        """Writes the events into `pipeline_outbox` and returns the count.

        One server-side `clock_timestamp()` (not `now()`, the transaction
        start) orders events across hosts. Must be the last statement before commit.
        """
        if not self._events:
            return 0
        occurred_at = session.execute(text("SELECT clock_timestamp()")).scalar_one()
        body = self.copy_body(occurred_at)
        columns = ", ".join(OUTBOX_COLUMNS)
        raw = session.connection().connection.driver_connection
        with raw.cursor().copy(  # type: ignore[union-attr]
            f"COPY pipeline_outbox ({columns}) FROM STDIN"
        ) as copy:
            copy.write(body)
        written = len(self._events)
        self._events.clear()
        return written


def _with_occurred_at(payload: str, occurred_at: datetime) -> str:
    """Stamps the flush timestamp into an already-rendered envelope.

    Rendering happens at record time to keep `canonical_json` out of the
    window between the timestamp and the COPY; `_append` writes the field as null.
    """
    return payload.replace('"occurred_at":null', f'"occurred_at":"{occurred_at.isoformat()}"')

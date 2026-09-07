"""Capturing pipeline writes as change events.

The writer knows which rows it wrote; nothing downstream does. This is where
that knowledge is turned into something that survives the transaction: one
envelope per inserted, updated or deleted row, queued into `pipeline_outbox`
by the same transaction that wrote the data. A failed commit loses both, a
successful one guarantees eventual publication.

Two things are deliberately NOT here. Routing -- which family a table
belongs to and which column keys it -- lives in `routing.py`, because it is a
property of the table and several datasets write the same one. Rendering
goes through `core/normalize.canonical_json`, because a second JSON encoder
would be a second answer to "how does a Decimal cross the wire", and the
answer this project already gives (as text, never a float) is the whole
reason `f32_decimal` exists.

The collector holds events in memory for the length of one transaction. That
is bounded by what one symbol's sync writes -- tens of thousands of rows at
most, and bar writes coalesce into range events before they get here.
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
#:
#: Written out rather than derived, because "the date-ish key column" is a
#: guess and this is the field a consumer uses to re-read the span. Two of
#: the six also carry `bar_interval`, which is why a range event names both:
#: one span per (symbol, interval) there, one per symbol on the rest.
#: `tests/unit/test_routing.py` holds the map to the schema.
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

    Created by whoever knows the run -- the runners for a sync, the CLI for
    `purge` and `bars rescale` -- and carried to the writer. The COLLECTOR
    is built from it per attempt, so a transaction replayed after a
    serialisation failure produces one set of events rather than two.
    """

    #: `sync_runs.id`, or None outside a sync. A scheduler run is reachable
    #: through `sync_runs.job_run_id` and is not repeated in the envelope.
    run_id: int | None

    #: Above this many inserted rows, a write to a bars-family table becomes
    #: one range event instead of one event per bar.
    range_threshold: int


@dataclass(frozen=True)
class ChangeEvent:
    """One outbox row, rendered but not yet written."""

    family: str
    partition_key: str
    payload: str


class ChangeCollector:
    """Accumulates the events of one transaction, then renders them.

    Built per attempt rather than per symbol, and handed to the writer. It
    holds no session: `flush` takes one, so the collector can be filled by
    code that is not inside a transaction yet.
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

        `None` is the honest answer for `purge`, `prune` and `bars rescale`:
        they run outside any dataset, and inventing one would put a name in
        the envelope that resolves to nothing.
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

        `row` is the row as the database returned it -- `RETURNING *` -- so
        columns outside the update map, `GREATEST`-merged columns and server
        defaults are what the consumer sees, not what the pipeline proposed.
        `None` for a delete, which has no row left to return.
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

        A first sync writes ~20,000 bars per symbol; across a 4,500-symbol
        universe that is on the order of 10^8 row events to say "the history
        is here". The consumer re-reads the span from the endpoint that
        serves the table.

        `bar_interval` is set for `price_bars` and `periodic_bars` and null
        for the bars-family tables keyed by a date, which is why `ts_column`
        has to name the key column the span is over. A purge leaves the span
        null: returning millions of bar keys from a DELETE is the cost this
        event exists to avoid.
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
        """A split applied to the stored history.

        Named rather than enumerated for the same reason as a range event:
        a rescale rewrites every bar the symbol has. `dataset` is null --
        `storage/rescale.py` runs before the dataset loop and from the CLI.
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

        One `clock_timestamp()` for the whole batch, read from the database
        rather than from this process: it is both the outbox row's
        `created_at` and the envelope's `occurred_at`, and taking it from
        the server means the database clock orders events across shard
        processes and hosts.

        `clock_timestamp()`, not `now()`: `now()` is the transaction start
        time, so every event of a long symbol transaction would claim to
        have happened before the writes it describes.

        The caller commits. This is the last statement before that commit,
        which is what keeps the outbox window -- and the relay's wait on
        open writers -- small.
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

    Rendering at record time and patching here, rather than rendering at
    flush time, keeps the expensive part -- `canonical_json` over a wide row
    -- out of the window between the timestamp and the COPY. The field is
    written as `null` by `_append` and is the only one that changes, so the
    substitution is unambiguous.
    """
    return payload.replace('"occurred_at":null', f'"occurred_at":"{occurred_at.isoformat()}"')


__all__ = [
    "ENVELOPE_VERSION",
    "OUTBOX_COLUMNS",
    "ChangeCollector",
    "ChangeContext",
    "ChangeEvent",
    "ChangeOp",
    "BARS_INTERVAL_TABLES",
    "BARS_TIME_COLUMN",
]

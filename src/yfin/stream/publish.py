"""Committed ticks, fanned out to open browser tabs over Redis pub/sub.

This is not a second archive and not the Kafka path. Kafka is durable,
transactional and consumed by other systems; this is at-most-once
fan-out whose whole audience is a web page that is currently open. The
archive is `session.commit()`, and it has already happened by the time
anything here runs -- so every failure below is swallowed, counted, and
logged once. A browser that misses a tick repaints on the next one; a
writer that dies over a publish loses the batch it was writing.

The field table is the contract with the page, and it lives here once.
`scripts/dump_tick_fields.py` writes it to `web/src/live/tick-fields.json`
and CI checks the committed copy is current, the same way `openapi.json`
is checked -- so a renamed key breaks the build rather than the chart.

Why the keys are one and two letters: a tick body is sent per tick per
subscriber, and `previous_close` costs seven times what `pc` does on
every one of them. The table below is where they are spelled out.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from yfin.core import metrics
from yfin.core.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    import redis

log = get_logger(__name__)

#: Short socket timeouts, for the same reason the API's Redis client has
#: them: this runs on the writer thread between a commit and the next
#: batch, and a hung socket there stalls the archive to deliver a price.
CONNECT_TIMEOUT_SECONDS: Final = 1.0
OPERATION_TIMEOUT_SECONDS: Final = 1.0

class FieldKind(StrEnum):
    """How a value reaches the wire.

    `Ms` rather than an ISO string because `canonical_json` renders a
    datetime as isoformat and the page would have to parse it; epoch
    milliseconds is what `new Date(t)` already takes. `Dec` stays a
    STRING: these are NUMERIC(28,12) in the database and a JSON number
    would round them in the browser's binary64 on the way in.

    A `StrEnum` rather than a `Literal` so the four values are named
    once and referenced, never retyped at a comparison. The member's
    value is what reaches `tick-fields.json`, so the wire shape is
    unchanged by the enum.
    """

    Str = "str"
    Int = "int"
    Ms = "ms"
    Dec = "dec"


#: `(wire key, column, kind, required)`. The four required fields are the
#: ones a price cannot be drawn without: which symbol, when, how much, and
#: whether the market was open -- `mh` is what lets the chart drop
#: extended-hours ticks from a `session=regular` series.
TICK_FIELDS: Final[tuple[tuple[str, str, FieldKind, bool], ...]] = (
    ("s", "symbol", FieldKind.Str, True),
    ("t", "ts_utc", FieldKind.Ms, True),
    ("p", "price", FieldKind.Dec, True),
    ("mh", "market_hours_code", FieldKind.Int, True),
    ("c", "change", FieldKind.Dec, False),
    ("cp", "change_percent", FieldKind.Dec, False),
    ("h", "day_high", FieldKind.Dec, False),
    ("l", "day_low", FieldKind.Dec, False),
    ("o", "open_price", FieldKind.Dec, False),
    ("pc", "previous_close", FieldKind.Dec, False),
    ("b", "bid", FieldKind.Dec, False),
    ("a", "ask", FieldKind.Dec, False),
    ("v", "day_volume", FieldKind.Int, False),
    ("ls", "last_size", FieldKind.Int, False),
    ("bs", "bid_size", FieldKind.Int, False),
    ("as", "ask_size", FieldKind.Int, False),
)

CHANNEL_PREFIX: Final = "yfin:tick:"


def channel(symbol: str) -> str:
    """One channel per symbol: a subscriber pays only for what it watches.

    A single channel with the symbol in the body would send every tick of
    the universe to every open tab and make it filter, which is the whole
    cost of the stream moved into the browser.
    """
    return f"{CHANNEL_PREFIX}{symbol}"


def _epoch_ms(value: datetime) -> int:
    """Milliseconds since the epoch, UTC.

    A naive datetime is read as UTC rather than as local time. Every
    timestamp in this codebase is UTC (`TsType` is timestamptz), but a
    row that arrived through a driver or a fixture without a tzinfo would
    otherwise be shifted by the writer host's offset -- a silent error
    that only shows up on a machine that is not on UTC.
    """
    stamped = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return int(stamped.timestamp() * 1000)


def _encode(value: object, kind: FieldKind) -> str | int:
    if kind is FieldKind.Ms:
        return _epoch_ms(value) if isinstance(value, datetime) else int(str(value))
    if kind is FieldKind.Int:
        return value if isinstance(value, int) else int(str(value))
    if kind is FieldKind.Dec and isinstance(value, Decimal):
        # Two corrections, both of them about what the page ends up
        # showing and sending.
        #
        # `normalize()` first: these columns are NUMERIC(28,12), so a
        # price read back from `live_quotes` is `232.500000000000` and
        # every tick would carry twelve zeros that say nothing -- on the
        # wire and in the time-and-sales list.
        #
        # `format(..., "f")` second: `normalize()` renders 100 as `1E+2`
        # and `str(Decimal)` switches to exponents below the
        # coefficient's scale. JS parses those, but the page SHOWS this
        # string, and a crypto price would read as `1E-12` there.
        return format(value.normalize(), "f")
    # A string, not a JSON number: a JSON number would round
    # NUMERIC(28,12) in the browser's binary64 on the way in.
    return str(value)


def _read(row: object, column: str) -> object:
    """One accessor for both shapes a tick arrives in.

    The writer holds row dicts; `live_quotes` snapshots are ORM objects.
    Both produce byte-identical bodies from the one table above -- a
    `snap` that disagreed with the `tick`s following it would show as a
    jump on every chart at subscribe time.
    """
    if isinstance(row, Mapping):
        return row.get(column)
    return getattr(row, column, None)


def tick_body(row: object) -> dict[str, str | int] | None:
    """One tick as the page reads it, or None when it cannot be drawn.

    None means a required field was absent. In practice that is `price`:
    the column is nullable, and a tick with no price has nothing to put on
    a chart or in a time-and-sales list. Publishing it would make the page
    handle a case it can do nothing with.
    """
    body: dict[str, str | int] = {}
    for key, column, kind, required in TICK_FIELDS:
        value = _read(row, column)
        if value is None:
            if required:
                return None
            continue
        body[key] = _encode(value, kind)
    return body


class TickPublisher:
    """Fan-out for one writer process. Fail-open, always.

    The client is built lazily and rebuilt after a failure: `from_url`
    does not connect, so a URL pointing at a Redis that is down costs
    nothing until the first batch, and a Redis that comes back is picked
    up without restarting the stream.

    Off is a first-class state. With `enabled` false or no URL, `publish`
    counts `disabled` and returns -- no client, no import, no socket.
    """

    def __init__(self, *, enabled: bool, url: str) -> None:
        self._on = enabled and bool(url)
        self._url = url
        self._client: redis.Redis | None = None
        #: Whether the last batch got through. Only a CHANGE is logged: a
        #: Redis that is down for an hour is 14,000 batches, and a warning
        #: per batch would bury the log line that says the stream itself
        #: is fine.
        self._healthy = True

    @property
    def enabled(self) -> bool:
        return self._on

    def _connect(self) -> redis.Redis:
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(
                self._url,
                socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
                socket_timeout=OPERATION_TIMEOUT_SECONDS,
                decode_responses=True,
            )
        return self._client

    def publish(self, rows: Sequence[object]) -> None:
        """Publishes one committed batch. Never raises.

        Counted per batch rather than per tick: the question is whether
        the fan-out is working, and a batch of 500 that failed is one
        failure, not 500.
        """
        if not rows:
            return
        if not self._on:
            metrics.inc("yfin_stream_publish_total", result="disabled")
            return
        try:
            client = self._connect()
            # One round trip for the batch. The alternative -- one array
            # per symbol -- is left to measurement (spec, "Canlı veri
            # yolu"); what is NOT an option is publishing before the
            # commit, which would show the page a tick the archive does
            # not have.
            pipeline = client.pipeline(transaction=False)
            for row in rows:
                body = tick_body(row)
                if body is None:
                    continue
                pipeline.publish(
                    channel(str(body["s"])), json.dumps(body, separators=(",", ":"))
                )
            pipeline.execute()
        except Exception as error:  # noqa: BLE001 - fail-open is the point
            metrics.inc("yfin_stream_publish_total", result="failed")
            if self._healthy:
                self._healthy = False
                log.warning(
                    "tick publish failing; the archive is unaffected",
                    error=str(error),
                    error_type=type(error).__name__,
                )
            # Drop the client so the next batch dials again: a connection
            # that broke stays broken in the pool otherwise.
            self._client = None
            return
        metrics.inc("yfin_stream_publish_total", result="ok")
        if not self._healthy:
            self._healthy = True
            log.info("tick publish recovered")

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with suppress(Exception):
                client.close()

"""Committed ticks, fanned out to open browser tabs over Redis pub/sub.

At-most-once, after the commit: every failure is swallowed, counted, and
logged once. TICK_FIELDS is the page contract (`tick-fields.json`, CI-checked).
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

    `Ms` is epoch milliseconds, what `new Date(t)` takes. `Dec` stays a
    string: a JSON number would round NUMERIC(28,12) in binary64.
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
    """One channel per symbol, so a subscriber pays only for what it watches."""
    return f"{CHANNEL_PREFIX}{symbol}"


def _epoch_ms(value: datetime) -> int:
    """Milliseconds since the epoch, UTC.

    A naive datetime is read as UTC, not local time, or a fixture row
    without tzinfo would shift by the writer host's offset.
    """
    stamped = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return int(stamped.timestamp() * 1000)


def _encode(value: object, kind: FieldKind) -> str | int:
    if kind is FieldKind.Ms:
        return _epoch_ms(value) if isinstance(value, datetime) else int(str(value))
    if kind is FieldKind.Int:
        return value if isinstance(value, int) else int(str(value))
    if kind is FieldKind.Dec and isinstance(value, Decimal):
        # `normalize()` strips NUMERIC(28,12)'s trailing zeros; `format("f")`
        # keeps the result out of exponent notation, which the page shows as-is.
        return format(value.normalize(), "f")
    # A string, not a JSON number: a JSON number would round
    # NUMERIC(28,12) in the browser's binary64 on the way in.
    return str(value)


def _read(row: object, column: str) -> object:
    """One accessor for writer row dicts and `live_quotes` ORM objects.

    Both must produce identical bodies, or a `snap` would disagree with
    the `tick`s that follow it.
    """
    if isinstance(row, Mapping):
        return row.get(column)
    return getattr(row, column, None)


def tick_body(row: object) -> dict[str, str | int] | None:
    """One tick as the page reads it, or None when a required field is absent."""
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

    The client is built lazily and rebuilt after a failure, so a Redis
    that comes back is picked up without restarting the stream.
    """

    def __init__(self, *, enabled: bool, url: str) -> None:
        self._on = enabled and bool(url)
        self._url = url
        self._client: redis.Redis | None = None
        #: Whether the last batch got through. Only a change is logged,
        #: so a Redis outage does not produce a warning per batch.
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
        """Publishes one committed batch. Never raises. Metrics count per batch, not per tick."""
        if not rows:
            return
        if not self._on:
            metrics.inc("yfin_stream_publish_total", result="disabled")
            return
        try:
            client = self._connect()
            # One round trip per batch, and only after the commit: the
            # page must never see a tick the archive does not have.
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

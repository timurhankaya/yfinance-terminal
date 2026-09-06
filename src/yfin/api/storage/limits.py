"""Bounds on what a single request may ask the database to do.

Under the synchronous design each request holds a thread for as long as
its query runs, and the pool is fixed. Without these bounds one request
is enough to matter and a handful are enough to stop the API:
`interval=1m&from=1980-01-01` scans a hypertable with millions of rows,
holds a worker for minutes, and costs the caller a single request -- well
inside any per-second rate limit, because a rate limit bounds frequency,
not cost.

So the cost is bounded directly: how much time a query may span, how long
it may run, and how much it may return.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from yfin.models import DAILY_INTERVAL, INTRADAY_INTERVALS

#: Per-request statement timeout. Above any healthy query and far below
#: the point where a stuck one would starve the pool.
STATEMENT_TIMEOUT_MS = 10_000

#: Maximum span of a single bars request, by interval class. Intraday is
#: the tight one: a month of 1m bars is already ~8,000 rows per symbol.
MAX_SPAN = {
    "intraday": timedelta(days=31),
    "daily": timedelta(days=3653),  # ~10 years
    "periodic": timedelta(days=18262),  # ~50 years
}

#: Query parameter sizes. A public API should not accept a megabyte of
#: filter text and find out what it costs later.
MAX_PARAM_LENGTH = 256
MIN_PREFIX_LENGTH = 2
MAX_PREFIX_LENGTH = 32


def span_class(interval: str) -> str:
    if interval in INTRADAY_INTERVALS:
        return "intraday"
    if interval == DAILY_INTERVAL:
        return "daily"
    return "periodic"


def max_span(interval: str) -> timedelta:
    return MAX_SPAN[span_class(interval)]


def apply_statement_timeout(session: Session) -> None:
    """Bounds the current transaction, not the connection.

    `SET LOCAL` so the value dies with the transaction: a pooled
    connection must not carry one request's timeout into the next.
    """
    session.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))


def resolve_window(
    *,
    interval: str,
    start: datetime | None,
    end: datetime | None,
    now: datetime,
) -> tuple[datetime, datetime]:
    """Fills in and bounds the [from, to) window.

    Half-open on purpose: `from` inclusive, `to` exclusive. A closed
    interval makes consecutive pages overlap by exactly one row at the
    boundary, which clients then have to de-duplicate.
    """
    span = max_span(interval)
    if start is not None and end is not None:
        return start, end
    if start is not None:
        return start, start + span
    if end is not None:
        return end - span, end
    return now - span, now


def window_error(interval: str, start: datetime, end: datetime) -> str | None:
    """The reason the window is unacceptable, or None."""
    if start >= end:
        return "'from' must be earlier than 'to'"
    span = max_span(interval)
    if end - start > span:
        return (
            f"the requested range exceeds the maximum of {span.days} days "
            f"for interval {interval}"
        )
    return None


def escape_prefix(value: str) -> str:
    """Makes a user string safe as a LIKE prefix.

    `%` and `_` are wildcards; a bare `%` would turn a prefix lookup into
    a full scan of the symbol table -- cheap to send, expensive to serve.
    """
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped


def to_utc(value: datetime | date | None) -> datetime | None:
    """Normalises a client-supplied bound to an aware UTC datetime.

    A bare date means UTC midnight, and a datetime without an offset is
    read as UTC rather than as local time. Leaving it naive would make it
    incomparable with the aware timestamps in the columns -- and, worse,
    psycopg would interpret it in the connection's timezone, so the same
    request would mean different things on different hosts.
    """
    if value is None:
        return None
    if not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

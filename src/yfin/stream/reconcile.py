"""Closing bar_gaps from the tick archive.

The one place the live stream is allowed to write into `price_bars`, and
it is deliberately narrow: only minutes that have no bar at all, only
inside a gap that is still open, and only for symbols whose timezone is
known.

Why it exists: Yahoo drops 1-minute data after 29 days. A window missed
inside that period can never be fetched again -- so a `retention_expired`
gap is permanent, and the tick archive is the only thing that can still
answer what happened there. Those gaps are the point of this module. A
`fetch_failed` gap is also filled, but only because it is cheaper than a
refetch; that one was never lost.

What this module refuses to do is as important as what it does:

  * it never overwrites an existing bar. A derived bar has no volume, and
    a blanket upsert would blank the volume of real bars that happen to
    sit inside the same gap window;
  * it never invents volume. `day_volume` is cumulative and the stream is
    a ~1s snapshot, not a tick feed, so per-minute volume cannot be
    recovered from it;
  * it never guesses a local date. `price_bars.local_date` is the
    exchange's calendar day, and deriving it from UTC shifts it a day for
    positive-offset exchanges.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.logging_setup import get_logger
from yfin.models.bars import RESOLVED_BY_TICKS
from yfin.storage.contracts import TableWrite, WriteStats, apply_write
from yfin.storage.persistence import PostgresRowWriter
from yfin.stream.protocol import MARKET_HOURS_REGULAR

log = get_logger(__name__)

MINUTE = timedelta(minutes=1)


@dataclass
class ReconcileStats:
    """What a reconciliation pass did, and what it could not do."""

    gaps_examined: int = 0
    gaps_closed: int = 0
    bars_written: int = 0
    minutes_skipped_existing: int = 0
    minutes_skipped_no_price: int = 0
    gaps_without_timezone: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"{self.gaps_closed}/{self.gaps_examined} gap(s) closed, "
            f"{self.bars_written} bar(s) written"
        )


@dataclass(frozen=True)
class OpenGap:
    symbol: str
    gap_start_utc: datetime
    gap_end_utc: datetime
    reason: str
    timezone: str | None


def local_date_for(ts_utc: datetime, timezone_name: str) -> Any:
    """The exchange's calendar day for a UTC instant.

    Converting to UTC and taking the date is what must NOT happen: for a
    positive-offset exchange (BIST, Tokyo) it lands a day early. The
    conversion goes the other way -- into the exchange's zone, then take
    the date.
    """
    return ts_utc.astimezone(ZoneInfo(timezone_name)).date()


class GapReconciler:
    """Fills open 1m gaps from `live_ticks`."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def run(self, *, dry_run: bool = False, limit: int | None = None) -> ReconcileStats:
        stats = ReconcileStats()
        for gap in self._open_gaps(limit):
            stats.gaps_examined += 1
            if gap.timezone is None:
                # Refused rather than guessed: local_date is NOT NULL and
                # a wrong one silently files the bar under the wrong day.
                stats.gaps_without_timezone.append(gap.symbol)
                continue
            self._close(gap, stats, dry_run=dry_run)
        return stats

    # --- reading -----------------------------------------------------------

    def _open_gaps(self, limit: int | None) -> list[OpenGap]:
        """Open 1m gaps, `retention_expired` included.

        Including them is the whole point. An earlier draft filtered them
        out, which left only the gaps Yahoo can still serve -- that is,
        the reconciliation skipped exactly the windows that need it and
        did the ones that did not.
        """
        sql = (
            "SELECT g.symbol, g.gap_start_utc, g.gap_end_utc, g.reason, s.timezone "
            "  FROM bar_gaps g "
            "  JOIN symbols s ON s.symbol = g.symbol "
            " WHERE g.resolved_at IS NULL AND g.bar_interval = '1m' "
            " ORDER BY g.gap_start_utc"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._session_factory() as session:
            rows = session.execute(text(sql)).all()
        return [
            OpenGap(
                symbol=r[0], gap_start_utc=r[1], gap_end_utc=r[2], reason=r[3], timezone=r[4]
            )
            for r in rows
        ]

    def _existing_minutes(self, session: Session, gap: OpenGap) -> set[datetime]:
        """Minutes inside the window that already have a bar.

        `fetch_failed` gaps are recorded at day granularity, so a single
        gap row can span days and contain plenty of real bars. Writing
        over those would replace a correct volume with NULL.
        """
        rows = session.execute(
            text(
                "SELECT ts_utc FROM price_bars "
                " WHERE symbol = :symbol AND bar_interval = '1m' "
                "   AND ts_utc >= :start AND ts_utc < :end"
            ),
            {"symbol": gap.symbol, "start": gap.gap_start_utc, "end": gap.gap_end_utc},
        ).scalars()
        return set(rows)

    def _ticks(self, session: Session, gap: OpenGap) -> list[tuple[datetime, Decimal, int]]:
        """Ticks in the window that carry a usable price.

        `price IS NOT NULL` is a real filter, not defensive noise: a tick
        can legitimately arrive with only bid/ask set, and
        `price_bars.close` is NOT NULL.
        """
        rows = session.execute(
            text(
                "SELECT ts_utc, price, market_hours_code FROM live_ticks "
                " WHERE symbol = :symbol AND ts_utc >= :start AND ts_utc < :end "
                "   AND price IS NOT NULL "
                " ORDER BY ts_utc"
            ),
            {"symbol": gap.symbol, "start": gap.gap_start_utc, "end": gap.gap_end_utc},
        ).all()
        return [(r[0], r[1], r[2]) for r in rows]

    # --- writing -----------------------------------------------------------

    def _close(self, gap: OpenGap, stats: ReconcileStats, *, dry_run: bool) -> None:
        with self._session_factory() as session:
            existing = self._existing_minutes(session, gap)
            ticks = self._ticks(session, gap)
            if not ticks:
                return

            bars = self._derive(gap, ticks, existing, stats)
            if not bars:
                return
            stats.bars_written += len(bars)
            if dry_run:
                return

            apply_write(
                PostgresRowWriter(session),
                TableWrite(
                    table="price_bars",
                    rows=bars,
                    key_columns=("symbol", "bar_interval", "ts_utc"),
                    # Nothing is updated: these minutes had no bar, and a
                    # later real fetch must be free to replace them
                    # through the normal path rather than being blocked
                    # or clobbered here.
                    update_columns=(),
                ),
                WriteStats(),
            )
            session.execute(
                text(
                    "UPDATE bar_gaps SET resolved_at = :ts, resolved_by = :by "
                    " WHERE symbol = :symbol AND bar_interval = '1m' "
                    "   AND gap_start_utc = :start"
                ),
                {
                    "ts": datetime.now(UTC),
                    "by": RESOLVED_BY_TICKS,
                    "symbol": gap.symbol,
                    "start": gap.gap_start_utc,
                },
            )
            session.commit()
            stats.gaps_closed += 1
            log.info(
                "gap closed from ticks",
                symbol=gap.symbol,
                reason=gap.reason,
                bars=len(bars),
            )

    def _derive(
        self,
        gap: OpenGap,
        ticks: Sequence[tuple[datetime, Decimal, int]],
        existing: set[datetime],
        stats: ReconcileStats,
    ) -> list[dict[str, Any]]:
        """Groups ticks into one OHLC bar per minute.

        The window is walked minute by minute rather than treated as a
        single bar: a gap row can cover days.
        """
        assert gap.timezone is not None
        buckets: dict[datetime, list[tuple[datetime, Decimal, int]]] = {}
        for ts, price, hours in ticks:
            minute = ts.replace(second=0, microsecond=0)
            buckets.setdefault(minute, []).append((ts, price, hours))

        bars: list[dict[str, Any]] = []
        for minute in sorted(buckets):
            if minute in existing:
                stats.minutes_skipped_existing += 1
                continue
            group = buckets[minute]
            if not group:
                stats.minutes_skipped_no_price += 1
                continue
            prices = [price for _, price, _ in group]
            bars.append(
                {
                    "symbol": gap.symbol,
                    "bar_interval": "1m",
                    "ts_utc": minute,
                    "local_date": local_date_for(minute, gap.timezone),
                    "open": prices[0],
                    "high": max(prices),
                    "low": min(prices),
                    "close": prices[-1],
                    # Never derived. day_volume is cumulative and the feed
                    # is a ~1s snapshot, so a per-minute figure would be a
                    # guess. NULL says "unknown", which is true.
                    "volume": None,
                    # Anything outside the regular session counts as
                    # extended. This is why market_hours_code is NOT NULL
                    # and exempt from the presence rule: PRE_MARKET is 0.
                    "is_extended": group[-1][2] != MARKET_HOURS_REGULAR,
                }
            )
        return bars


def reconcile_gaps(
    session_factory: sessionmaker[Session], *, dry_run: bool = False, limit: int | None = None
) -> ReconcileStats:
    return GapReconciler(session_factory).run(dry_run=dry_run, limit=limit)


def known_timezone(name: str | None) -> bool:
    """Whether a timezone name can actually be resolved.

    `symbols.timezone` comes from Yahoo, so it is not guaranteed to be a
    name the platform's tz database has.
    """
    if not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True

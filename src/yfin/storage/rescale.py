"""Retroactive rescaling of the archive on a split.

Yahoo bakes split adjustment into OHLC, and old intraday bars cannot be
refetched, so the archive's scale must be kept consistent here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from yfin.core.logging_setup import get_logger
from yfin.models import INTRADAY_INTERVALS, Base
from yfin.storage.changes import ChangeCollector
from yfin.storage.db import rowcount

log = get_logger(__name__)


class RescaleSkipped(Exception):
    """This split cannot be applied; no record is written either.

    Writing one would count the split as "applied", and the correct data
    would never be rescaled again -- a silent, permanent corruption.
    """


def rescale_factors(ratio: Decimal) -> tuple[Decimal, Decimal]:
    """(price factor, volume factor), in Yahoo's direction: price divided, volume multiplied.

    A reverse split (ratio < 1) uses the same formula.
    """
    if ratio <= 0:
        raise RescaleSkipped(f"invalid split ratio: {ratio}")
    return Decimal(1) / ratio, ratio


def split_boundary_utc(split_day: date, timezone_name: str | None) -> datetime:
    """UTC equivalent of local midnight on the split day.

    Raw UTC midnight would put bars on the wrong side of the boundary. An
    unknown tz is never assumed to be UTC: a wrong rescale cannot be undone.
    """
    if not timezone_name:
        raise RescaleSkipped("the symbol's IANA timezone name is unknown")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # The `timezone` column can carry abbreviations like "EDT"/"TRT";
        # if one lands here it's explicitly rejected.
        raise RescaleSkipped(f"invalid timezone name: {timezone_name}") from exc
    local_midnight = datetime.combine(split_day, datetime.min.time(), tzinfo=zone)
    # Returned UTC-aware: `price_bars.ts_utc` is timestamptz, and the
    # comparison must be at the same awareness level.
    return local_midnight.astimezone(UTC)


def _symbol_timezone(session: Session, symbol: str) -> str | None:
    table = Base.metadata.tables["history_metadata"]
    return session.execute(
        select(table.c["exchange_timezone_name"]).where(table.c["symbol"] == symbol)
    ).scalar_one_or_none()


def pending_splits(session: Session, symbol: str) -> list[tuple[date, Decimal]]:
    """Applicable split rows: not yet recorded AND newer than the archive.

    Both gates are required: `bar_rescales` gives idempotency, and the
    earliest-bar gate keeps an unseeded install from re-dividing bars.
    """
    splits = Base.metadata.tables["splits"]
    applied = Base.metadata.tables["bar_rescales"]
    bars = Base.metadata.tables["price_bars"]
    earliest = (
        select(func.min(bars.c["local_date"])).where(bars.c["symbol"] == symbol).scalar_subquery()
    )
    stmt = (
        select(splits.c["split_date"], splits.c["ratio"])
        .outerjoin(
            applied,
            (applied.c["symbol"] == splits.c["symbol"])
            & (applied.c["split_date"] == splits.c["split_date"]),
        )
        .where(
            splits.c["symbol"] == symbol,
            applied.c["symbol"].is_(None),
            # If earliest is NULL (symbol has no bars), the condition is
            # NULL and the row is excluded -- correct, since there's no
            # archive to rescale.
            splits.c["split_date"] > earliest,
        )
        .order_by(splits.c["split_date"])
    )
    return [(row[0], row[1]) for row in session.execute(stmt)]


def seed_baseline(session: Session) -> int:
    """`yfin rescale --seed`: baseline record for every existing split.

    Idempotent. Must run before `bars_*` runs for the first time, or every
    historical split would be applied to bars already at the current scale.
    """
    now = datetime.now(UTC)
    result = session.execute(
        text(
            "INSERT INTO bar_rescales (symbol, split_date, ratio, applied_at, rows_affected) "
            "SELECT s.symbol, s.split_date, 1, :now, 0 FROM splits s "
            "LEFT JOIN bar_rescales r "
            "  ON r.symbol = s.symbol AND r.split_date = s.split_date "
            "WHERE r.symbol IS NULL"
        ),
        {"now": now},
    )
    seeded = rowcount(result)
    log.info("rescale baseline seeded", rows=seeded)
    return seeded


def apply_pending(
    session: Session, symbol: str, *, collector: ChangeCollector | None = None
) -> int:
    """Apply this symbol's pending splits; returns the count applied.

    Must run inside the symbol's write transaction before `bars_*` is
    written. Publishes one `rescale` event per split this session applied.
    """
    pending = pending_splits(session, symbol)
    if not pending:
        return 0

    timezone_name = _symbol_timezone(session, symbol)
    applied = 0
    for split_day, ratio in pending:
        try:
            price_factor, volume_factor = rescale_factors(ratio)
            boundary = split_boundary_utc(split_day, timezone_name)
        except RescaleSkipped as exc:
            # No record written: writing one would count the split as
            # "applied", and the correct data would never be rescaled again.
            log.error("rescale skipped", symbol=symbol, split_date=str(split_day), reason=str(exc))
            continue
        one = _apply_one(
            session, symbol, split_day, ratio, price_factor, volume_factor, boundary
        )
        applied += one
        if one and collector is not None:
            collector.record_rescale(symbol, split_day, price_factor, boundary)
    return applied


def _apply_one(
    session: Session,
    symbol: str,
    split_day: date,
    ratio: Decimal,
    price_factor: Decimal,
    volume_factor: Decimal,
    boundary: datetime,
) -> int:
    """One split: claim the slot with `INSERT ... ON CONFLICT DO NOTHING`, then UPDATE.

    `SELECT ... FOR UPDATE` on a missing PK does not serialize two sessions.
    rowcount 0 means another session claimed it first; the UPDATE is skipped.
    """
    now = datetime.now(UTC)
    claim = session.execute(
        text(
            "INSERT INTO bar_rescales (symbol, split_date, ratio, applied_at, rows_affected) "
            "VALUES (:symbol, :split_date, :ratio, :now, 0) "
            "ON CONFLICT (symbol, split_date) DO NOTHING"
        ),
        {"symbol": symbol, "split_date": split_day, "ratio": ratio, "now": now},
    )
    if not rowcount(claim):
        return 0  # another session claimed it first

    # Intraday intervals only: 1wk/1mo are refetched from scratch every run
    # and are always at Yahoo's current scale; rescaling them would double-adjust.
    placeholders = ", ".join(f":iv{i}" for i in range(len(INTRADAY_INTERVALS)))
    params: dict[str, object] = {
        "symbol": symbol,
        "boundary": boundary,
        "price_factor": price_factor,
        "volume_factor": volume_factor,
    }
    params.update({f"iv{i}": iv for i, iv in enumerate(INTRADAY_INTERVALS)})
    updated = session.execute(
        text(
            "UPDATE price_bars SET "
            "  open = open * :price_factor, "
            "  high = high * :price_factor, "
            "  low = low * :price_factor, "
            "  close = close * :price_factor, "
            # FLOOR: a 3:2 split makes volume fractional, and numeric ->
            # bigint assignment would round. `ts_utc < :boundary` limits the
            # hypertable chunks touched; compression stays off for this write.
            "  volume = FLOOR(volume * :volume_factor) "
            "WHERE symbol = :symbol AND ts_utc < :boundary "
            f"  AND bar_interval IN ({placeholders})"
        ),
        params,
    )
    rows = rowcount(updated)
    session.execute(
        text(
            "UPDATE bar_rescales SET rows_affected = :rows "
            "WHERE symbol = :symbol AND split_date = :split_date"
        ),
        {"rows": rows, "symbol": symbol, "split_date": split_day},
    )
    log.info(
        "rescale applied",
        symbol=symbol,
        split_date=str(split_day),
        ratio=str(ratio),
        rows=rows,
    )
    return 1


def unseeded_historic_splits(session: Session) -> int:
    """Count of unseeded HISTORICAL splits (a maintenance-hygiene warning).

    A split older than price_bars's earliest bar with no matching
    `bar_rescales` record is a sign that `--seed` was skipped.
    """
    splits = Base.metadata.tables["splits"]
    applied = Base.metadata.tables["bar_rescales"]
    bars = Base.metadata.tables["price_bars"]
    earliest = select(func.min(bars.c["local_date"])).scalar_subquery()
    stmt = (
        select(func.count())
        .select_from(
            splits.outerjoin(
                applied,
                (applied.c["symbol"] == splits.c["symbol"])
                & (applied.c["split_date"] == splits.c["split_date"]),
            )
        )
        .where(applied.c["symbol"].is_(None), splits.c["split_date"] < earliest)
    )
    return int(session.execute(stmt).scalar_one())

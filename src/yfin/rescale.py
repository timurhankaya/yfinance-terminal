"""Retroactive rescaling of the archive on a split.

The most critical part of the design, and the easiest to get wrong.

PROBLEM: Yahoo does not give raw prices. `auto_adjust=False` only splits the
DIVIDEND adjustment into `Adj Close`; SPLIT adjustment is already baked into
OHLC. Measured (NVDA, 2024-06-10, 10:1):

    2024-06-05  Close=122.44  Volume=528.402.000   <- day BEFORE the split
                (actually ~1224.40 and ~52.84M that day)

price_history doesn't care: it rewrites the whole history every run.
price_bars can't -- a 1m bar over 30 days old cannot be refetched. So keeping
the archive's scale consistent is on us; otherwise the table ends up mixed-
scale and the split day shows a fake 10x jump.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from yfin.logging_setup import get_logger
from yfin.models import INTRADAY_INTERVALS, Base

log = get_logger(__name__)


def _rowcount(result: Any) -> int:
    """Rows affected.

    `Session.execute` is statically typed to return `Result`, and `rowcount`
    is only defined on `CursorResult`; read here once instead of casting
    everywhere.
    """
    return int(getattr(result, "rowcount", 0) or 0)


class RescaleSkipped(Exception):
    """This split cannot be applied; no record is written either.

    Writing one would count the split as "applied", and the correct data
    would never be rescaled again -- a silent, permanent corruption.
    """


def rescale_factors(ratio: Decimal) -> tuple[Decimal, Decimal]:
    """(price factor, volume factor).

    Yahoo divides historical price and multiplies volume after a split; the
    UPDATE applies the same direction and aligns the archive to Yahoo's
    current scale. A reverse split (ratio < 1) works correctly with the same
    formula, no special case.
    """
    if ratio <= 0:
        raise RescaleSkipped(f"gecersiz split orani: {ratio}")
    return Decimal(1) / ratio, ratio


def split_boundary_utc(split_day: date, timezone_name: str | None) -> datetime:
    """UTC equivalent of local midnight on the split day (tz-naive).

    Raw UTC midnight can't be used: for positive-offset exchanges (BIST
    +03), local 00:00 is 21:00 UTC the PREVIOUS day; bars in between end up
    on the wrong side and are either never rescaled or rescaled twice.

    An unknown tz is never assumed to be UTC: rescaling against the wrong
    boundary is worse than not rescaling at all, because the result can't be
    undone.
    """
    if not timezone_name:
        raise RescaleSkipped("sembolun IANA tz adi bilinmiyor")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # The `timezone` column can carry abbreviations like "EDT"/"TRT";
        # if one lands here it's explicitly rejected.
        raise RescaleSkipped(f"gecersiz tz adi: {timezone_name}") from exc
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

    Reads from the `splits` table, not from dataset scheduling: this stays
    correct against the DB's current splits even if `history` was never
    selected in a given run.

    TWO gates, both required:

      1. No record in `bar_rescales` -- idempotency (the same split is
         never applied twice).
      2. `split_date` > the symbol's earliest bar date -- a structural
         safeguard.

    Without the second, the design would depend on an operational step
    (`rescale --seed`): on a fresh setup where seeding was skipped, the
    first run would apply every historical split in the `splits` table and
    re-divide bars that Yahoo already delivered at the current scale. An
    old split predating the archive has nothing to do -- those bars already
    arrived at the post-split scale. Seeding still matters (audit trail and
    stated intent), but it's no longer the sole guarantee of correctness.
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

    Skipping this step wipes out the archive. The `splits` table is already
    populated from the existing line (including AAPL's 1987, 2000, 2005,
    2014, 2020 splits). Since the trigger is "every split with no matching
    record", a first run against an empty bar_rescales would apply every
    historical split and divide the AAPL archive by 2*2*2*7*4 = 224, even
    though those bars already came from Yahoo at the current scale.

    Idempotent: existing records are untouched. Must run before `bars_*`
    runs for the first time.
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
    seeded = _rowcount(result)
    log.info("rescale baseline seeded", rows=seeded)
    return seeded


def apply_pending(session: Session, symbol: str) -> int:
    """Apply this symbol's pending splits; returns the count applied.

    Call site: inside the symbol's own write transaction, before `bars_*` is
    written. In the reverse order, new bars written in the same run
    (already at the new scale) would be divided a second time.
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
            log.error("rescale atlandi", symbol=symbol, split_date=str(split_day), reason=str(exc))
            continue
        applied += _apply_one(
            session, symbol, split_day, ratio, price_factor, volume_factor, boundary
        )
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
    """One split: claim the slot first, then UPDATE.

    The slot is claimed with `INSERT ... ON CONFLICT DO NOTHING`, not a
    lock. An earlier design used `SELECT ... FOR UPDATE`; testing showed it
    doesn't work: `FOR UPDATE` on a non-existent PK only takes a gap lock,
    gap locks are compatible with each other, so two sessions both see "no
    row, I'll apply it" and the conflict surfaces as a uniqueness violation
    (23505) at INSERT time instead.

    rowcount 1 means we own the slot; 0 means another session claimed it
    first and the UPDATE is skipped.
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
    if not _rowcount(claim):
        return 0  # another session claimed it first

    # 1wk/1mo are out of scope: those two intervals are refetched from
    # scratch every run with period="max", so they're always at Yahoo's
    # current scale. Rescaling them, followed by a dropped fetch that run,
    # would leave rows double-adjusted -- and since bar_rescales already
    # counts the split as "applied", it would never self-correct.
    #
    # This filter is now structurally redundant -- `price_bars` only holds
    # intraday data, 1wk/1mo live in `periodic_bars`. Kept anyway: the rule
    # stays visible in the code and won't silently break if the tables are
    # ever merged back. Cost is one IN clause.
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
            # FLOOR is required: a 3:2 split makes volume*1.5 fractional.
            # Without FLOOR, assigning a `numeric` value to a `bigint`
            # column rounds; FLOOR makes the truncation explicit in code.
            #
            # This UPDATE now hits a hypertable; the `ts_utc < :boundary`
            # condition means it only touches the relevant chunks.
            # Compression was left off specifically because of this
            # backfill write: an UPDATE on a compressed chunk requires
            # decompressing it, and a single split can touch the entire
            # historical archive.
            "  volume = FLOOR(volume * :volume_factor) "
            "WHERE symbol = :symbol AND ts_utc < :boundary "
            f"  AND bar_interval IN ({placeholders})"
        ),
        params,
    )
    rows = _rowcount(updated)
    session.execute(
        text(
            "UPDATE bar_rescales SET rows_affected = :rows "
            "WHERE symbol = :symbol AND split_date = :split_date"
        ),
        {"rows": rows, "symbol": symbol, "split_date": split_day},
    )
    log.info(
        "rescale uygulandi",
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

"""Read routes that exist only for the browser terminal.

Three reads the public surface does not offer, for three different
reasons.

`news` because `/v1` deliberately does not join: "news about AAPL" is two
calls there (`news_symbols` for the ids, then `news`). The page wants one
list, newest first.

`ticks` because the tick archive has no `/v1` route at all -- `QR` opens
on the last few hundred rows and then follows the socket, and paging a
hypertable through a cursor is not that question.

`gaps` because `bar_gaps` is in `NEVER_EXPOSED` (`tests/unit/test_api_contract.py`):
it is operational bookkeeping, not market data, and the intraday chart is
the one reader that needs it -- an hour with no candles means either a
closed market or a missed fetch, and only this table can tell them apart.

`sparklines` because a watchlist draws one line per row and `/v1` has no
batch: 200 rows through `/v1/symbols/{s}/bars` is 200 requests to show
200 tiny lines. One statement over `price_history` answers all of them.

All four sit outside the OpenAPI document and outside the metered
surface -- reachable by anyone, like the rest of the terminal, with
`RequestBrake` the only thing in front of them. Promoting any of them to
`/v1` is a separate decision (spec, "Kararlar" 8).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from yfin.api.core.errors import TYPE_INVALID_PARAMETER, TYPE_NOT_FOUND, ApiProblem
from yfin.api.routers.v1.paging import to_number
from yfin.api.schemas.common import Collection, Resource
from yfin.api.schemas.market import SymbolSummary
from yfin.api.storage import limits, reads
from yfin.api.storage.session import session_scope
from yfin.core.normalize import normalize_symbol
from yfin.models import ReadableInterval
from yfin.models.bars import BarGap
from yfin.models.discovery import Screen, ScreenMember, ScreenRun, screen_quotes
from yfin.models.news import News, NewsSymbol
from yfin.models.prices import PriceHistory
from yfin.models.stream import LiveQuote, LiveTick
from yfin.stream.publish import tick_body

router = APIRouter(prefix="/ui/api", include_in_schema=False)

#: Same shape market.py and datasets.py declare for themselves; the
#: storage module exports only the generator.
SessionDep = Annotated[Session, Depends(session_scope)]

NEWS_DEFAULT_LIMIT = 50
NEWS_MAX_LIMIT = 200

#: `QR` opens on this many rows and then follows the socket. The ceiling
#: is what one screenful of scrollback is worth: a tick body is ~150
#: bytes, so 2,000 rows is a 300 kB response, and past that the page is
#: paying to hold history nobody scrolls to.
TICKS_DEFAULT_LIMIT = 500
TICKS_MAX_LIMIT = 2000

#: One gap per missed fetch window. A chart's window holds a handful even
#: on a badly-behaved symbol; the cap exists so a pathological one cannot
#: return the whole table.
GAPS_MAX_LIMIT = 500


class NewsOut(BaseModel):
    news_id: str
    title: str
    summary: str | None
    pub_date: datetime
    provider_name: str | None
    #: canonical_url when the source gives one, else the click-through.
    link: str | None
    thumbnail_url: str | None


def list_news(session: Session, symbol: str, limit: int) -> list[NewsOut]:
    stmt = (
        select(News)
        .join(NewsSymbol, NewsSymbol.news_id == News.news_id)
        .where(NewsSymbol.symbol == symbol)
        .order_by(News.pub_date.desc(), News.news_id.desc())
        .limit(limit)
    )
    return [
        NewsOut(
            news_id=row.news_id,
            title=row.title,
            summary=row.summary,
            pub_date=row.pub_date,
            provider_name=row.provider_name,
            link=row.canonical_url or row.click_through_url,
            thumbnail_url=row.thumbnail_url,
        )
        for row in session.scalars(stmt)
    ]


@router.get("/symbols/{symbol}/news", response_model=Collection[NewsOut])
def symbol_news(
    session: SessionDep,
    symbol: str,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Collection[NewsOut]:
    size = limit if limit is not None else NEWS_DEFAULT_LIMIT
    if size > NEWS_MAX_LIMIT:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Page size above the maximum",
            detail=f"limit must not exceed {NEWS_MAX_LIMIT}",
        )
    limits.apply_statement_timeout(session)
    rows = list_news(session, normalize_symbol(symbol), size)
    # as_of stays None: the news table keeps no fetch timestamp on the row.
    return Collection[NewsOut](data=rows, next_cursor=None, as_of=None)


# --- the live tick archive --------------------------------------------------


def list_quotes(session: Session, symbols: Sequence[str]) -> list[dict[str, Any]]:
    """The latest tick per symbol, as the socket's `snap` frames.

    `live_quotes` rather than the newest `live_ticks` row: it exists so a
    reader can answer "what is the price now" without scanning a
    hypertable, and it is fed from the supervisor's last-value box, so it
    stays current even when the writer queue overflows.
    """
    if not symbols:
        return []
    stmt = select(LiveQuote).where(LiveQuote.symbol.in_(sorted(set(symbols))))
    bodies = (tick_body(row) for row in session.scalars(stmt))
    return [body for body in bodies if body is not None]


def list_ticks(session: Session, symbol: str, limit: int) -> list[dict[str, Any]]:
    """The newest ticks for one symbol, NEWEST FIRST.

    The same body the socket sends, from the same field table, so `QR`
    has one row shape rather than two: the opening page and everything
    that arrives afterwards are indistinguishable once rendered.
    """
    stmt = (
        select(LiveTick)
        .where(LiveTick.symbol == symbol)
        .order_by(LiveTick.ts_utc.desc(), LiveTick.payload_hash.desc())
        .limit(limit)
    )
    bodies = (tick_body(row) for row in session.scalars(stmt))
    return [body for body in bodies if body is not None]


@router.get("/symbols/{symbol}/ticks", response_model=Collection[dict[str, Any]])
def symbol_ticks(
    session: SessionDep,
    symbol: str,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Collection[dict[str, Any]]:
    size = limit if limit is not None else TICKS_DEFAULT_LIMIT
    if size > TICKS_MAX_LIMIT:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Page size above the maximum",
            detail=f"limit must not exceed {TICKS_MAX_LIMIT}",
        )
    limits.apply_statement_timeout(session)
    rows = list_ticks(session, normalize_symbol(symbol), size)
    # as_of stays None for the same reason the bar routes leave it None:
    # a tick's own ts_utc is when it happened, and there is no separate
    # "when was this verified" to report.
    return Collection[dict[str, Any]](data=rows, next_cursor=None, as_of=None)


# --- sparklines -------------------------------------------------------------
#
# The batch `/v1` deliberately does not offer. Every other read here exists
# because the public surface does not join; this one exists because it does
# not BATCH -- and the arithmetic is what makes that a route rather than a
# loop in the browser: a 200-symbol watchlist is 200 requests for 200 lines
# of thirty numbers each.

#: The same ceiling one live socket connection may subscribe to
#: (`ui/live.py`, MAX_SYMBOLS), for the same reason: this is the other half
#: of a watchlist row, so whatever a page can watch it can also draw.
SPARKLINE_MAX_SYMBOLS = 200

#: POINTS, NOT DAYS: trading sessions, i.e. rows. Thirty calendar days
#: would be ~21 closes, and a sparkline whose length varied with the
#: holidays of its exchange would be comparing shapes of different spans.
SPARKLINE_DEFAULT_POINTS = 30
SPARKLINE_MIN_POINTS = 5
SPARKLINE_MAX_POINTS = 90

#: Calendar days read per point asked for. Sessions are ~5/7 of the
#: calendar, so 2x carries a long weekend and a holiday week and still
#: leaves the tail to be cut in the process.
SPARKLINE_CALENDAR_FACTOR = 2


class SparklineSeries(BaseModel):
    """One symbol's closes, oldest first.

    The dates bracket the series rather than labelling each point: a
    sparkline has no axis, and what a reader needs to know is which window
    the shape covers.
    """

    symbol: str
    closes: list[str]
    first_date: date
    last_date: date


class SparklineSet(BaseModel):
    points: int
    series: list[SparklineSeries]
    #: Symbols with no bars in the window. Named rather than omitted: the
    #: panel draws "no data" in that cell, and a silently short list would
    #: leave a row looking like a symbol that never moved.
    missing: list[str]


def parse_sparkline_symbols(value: str) -> list[str]:
    """The `symbols` parameter as a list, in the order it was asked for.

    Deduplicated because a repeated symbol is one series, and normalised
    through the same function every other route uses so `aapl` and `AAPL`
    are not two queries.
    """
    seen: dict[str, None] = {}
    for token in value.split(","):
        stripped = token.strip()
        if stripped:
            seen.setdefault(normalize_symbol(stripped), None)
    return list(seen)


def read_sparklines(session: Session, symbols: Sequence[str], points: int) -> SparklineSet:
    """The last `points` daily closes for each symbol.

    `price_history`, not `price_bars`: the daily close of a session lives
    in the former and the latter is the intraday archive
    (`models/bars.py:99-102`). The primary key is `(symbol, session_date)`,
    so the filter below is a prefix scan of it.

    The window is found from the archive's own latest session rather than
    from today: an archive that has not synced since Friday should draw
    Friday's month, not four empty days.
    """
    latest = session.scalar(
        select(func.max(PriceHistory.session_date)).where(PriceHistory.symbol.in_(symbols))
    )
    if latest is None:
        return SparklineSet(points=points, series=[], missing=list(symbols))

    since = latest - timedelta(days=points * SPARKLINE_CALENDAR_FACTOR)
    stmt = (
        select(PriceHistory.symbol, PriceHistory.session_date, PriceHistory.close)
        .where(PriceHistory.symbol.in_(symbols), PriceHistory.session_date >= since)
        .order_by(PriceHistory.symbol, PriceHistory.session_date)
    )
    closes: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    for symbol, session_date, close in session.execute(stmt):
        closes[symbol].append((session_date, close))

    series: list[SparklineSeries] = []
    missing: list[str] = []
    for symbol in symbols:
        # Cut in the process, not in SQL: a per-symbol LIMIT is a lateral
        # join or a window function over the whole range, and the range is
        # already bounded to a couple of calendar months.
        rows = closes.get(symbol, [])[-points:]
        if not rows:
            missing.append(symbol)
            continue
        series.append(
            SparklineSeries(
                symbol=symbol,
                # `to_number` like every other row of the API: a NUMERIC
                # serialised as a JSON number would stop being exact at
                # the boundary where it is least visible.
                closes=[to_number(close) or "0" for _, close in rows],
                first_date=rows[0][0],
                last_date=rows[-1][0],
            )
        )
    return SparklineSet(points=points, series=series, missing=missing)


@router.get("/sparklines", response_model=Resource[SparklineSet])
def sparklines(
    session: SessionDep,
    symbols: Annotated[str, Query()],
    points: Annotated[int | None, Query()] = None,
    interval: Annotated[str | None, Query()] = None,
) -> Resource[SparklineSet]:
    # Declared only to be refused. FastAPI ignores a query parameter no
    # route declares, so without this `?interval=5m` would come back as a
    # month of daily closes -- the wrong data under the caller's own label.
    if interval is not None:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Sparklines are daily closes",
            detail="interval is not accepted; intraday bars are /v1/symbols/{symbol}/bars",
        )
    size = points if points is not None else SPARKLINE_DEFAULT_POINTS
    if size < SPARKLINE_MIN_POINTS or size > SPARKLINE_MAX_POINTS:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Point count outside the window",
            detail=(
                f"points must be between {SPARKLINE_MIN_POINTS} and {SPARKLINE_MAX_POINTS}"
            ),
        )
    wanted = parse_sparkline_symbols(symbols)
    if not wanted:
        raise ApiProblem(
            422, TYPE_INVALID_PARAMETER, "No symbols", detail="symbols must name at least one"
        )
    if len(wanted) > SPARKLINE_MAX_SYMBOLS:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Too many symbols",
            detail=f"symbols must not exceed {SPARKLINE_MAX_SYMBOLS}",
        )
    limits.apply_statement_timeout(session)
    # `as_of` stays None for the same reason the price routes leave it
    # None: when a bar was last verified against the source is a per-row
    # question, and the envelope answers a per-response one.
    return Resource[SparklineSet](data=read_sparklines(session, wanted, size), as_of=None)


# --- symbol search ----------------------------------------------------------

#: A picker's worth. More than this and the reader is reading a table,
#: which is what `DS` and `EQS` are for.
SEARCH_LIMIT = 20
SEARCH_MIN_LENGTH = 2


@router.get("/search", response_model=Collection[SymbolSummary])
def search(
    session: SessionDep,
    q: Annotated[str, Query()],
) -> Collection[SymbolSummary]:
    """Symbols by code OR by name, for the terminal's picker.

    `/v1/symbols?q=` matches the symbol column and says so in its
    published contract. That is right for an API and wrong for a person:
    a reader who knows "Akbank" does not know that Yahoo files it under
    `AKBNK.IS`, and one who types APPLE gets a joke coin whose ticker
    starts that way rather than Apple Inc. This is the terminal's own
    read, like `sparklines` and `news`, and `/v1` does not move.
    """
    query = q.strip()
    if len(query) < SEARCH_MIN_LENGTH:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Query too short",
            detail=f"q must be at least {SEARCH_MIN_LENGTH} characters",
        )
    limits.apply_statement_timeout(session)
    rows = reads.search_symbols(session, query=query, limit=SEARCH_LIMIT)
    return Collection[SymbolSummary](
        data=[SymbolSummary.model_validate(row) for row in rows],
        next_cursor=None,
        as_of=None,
    )


# --- bar gaps ---------------------------------------------------------------


class GapOut(BaseModel):
    """One window the archive knows it is missing.

    `reason` is `fetch_failed` or `retention_expired`; the chart shows
    both the same way, but the distinction is what tells an operator
    whether a refetch can still close it.
    """

    bar_interval: str
    gap_start_utc: datetime
    gap_end_utc: datetime
    reason: str
    detected_at: datetime


def list_gaps(
    session: Session,
    symbol: str,
    interval: str,
    start: datetime | None,
    limit: int,
) -> list[GapOut]:
    """OPEN gaps only, oldest first.

    A resolved gap is a window the archive since filled, so the bars are
    there and shading them would be a lie. `resolved_at IS NULL` is the
    whole filter -- including `retention_expired` rows, which stay open
    until `yfin stream reconcile` closes them from the tick archive.
    """
    stmt = (
        select(BarGap)
        .where(
            BarGap.symbol == symbol,
            BarGap.bar_interval == interval,
            BarGap.resolved_at.is_(None),
        )
        .order_by(BarGap.gap_start_utc)
        .limit(limit)
    )
    if start is not None:
        # Overlap, not containment: a gap that began before the chart's
        # window and runs into it is exactly the one worth shading.
        stmt = stmt.where(BarGap.gap_end_utc > start)
    return [
        GapOut(
            bar_interval=row.bar_interval,
            gap_start_utc=row.gap_start_utc,
            gap_end_utc=row.gap_end_utc,
            reason=row.reason,
            detected_at=row.detected_at,
        )
        for row in session.scalars(stmt)
    ]


@router.get("/symbols/{symbol}/gaps", response_model=Collection[GapOut])
def symbol_gaps(
    session: SessionDep,
    symbol: str,
    interval: Annotated[ReadableInterval, Query()] = "5m",
    start: Annotated[datetime | None, Query(alias="from")] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Collection[GapOut]:
    size = limit if limit is not None else GAPS_MAX_LIMIT
    if size > GAPS_MAX_LIMIT:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Page size above the maximum",
            detail=f"limit must not exceed {GAPS_MAX_LIMIT}",
        )
    limits.apply_statement_timeout(session)
    window_start = start.astimezone(UTC) if start is not None and start.tzinfo else start
    rows = list_gaps(session, normalize_symbol(symbol), interval, window_start, size)
    return Collection[GapOut](data=rows, next_cursor=None, as_of=None)


# --- the screener -----------------------------------------------------------
#
# A fourth read the public surface does not offer, and for the same
# reason as `news`: `/v1` does not join. A screen is four tables --
# `screens` says what it is, `screen_runs` when it last ran,
# `screen_members` who matched and in what order, `screen_quotes` what
# each of them was worth -- and reading a screener through the generic
# surface means four calls plus a client-side join over 107 columns of
# quote data to show twelve of them.


class ScreenSummary(BaseModel):
    """One screen and its most recent run.

    `as_of_date` and the counts are null for a screen that has never
    run: it is enabled and configured, and nothing has fetched it yet.
    Dropping such a screen from the list would hide a misconfiguration
    behind an absence.
    """

    screen_key: str
    title: str
    description: str | None
    kind: str
    quote_type: str
    #: What the roster is ordered by, and which way. The grid draws the
    #: rows in `rank_index` order and this is what that order MEANS --
    #: without it a screener is a list of tickers in an unexplained
    #: sequence.
    sort_field: str
    sort_asc: bool
    as_of_date: date | None
    #: When that run actually fetched. `as_of_date` is the session day
    #: the roster belongs to; this is the instant it was verified against
    #: Yahoo, and the two answer different questions.
    fetched_at: datetime | None
    #: What Yahoo said matched, which can exceed what was fetched.
    total: int | None
    #: Rows actually in the roster. Its gap with `total` is the page cap.
    row_count: int | None


class ScreenRow(BaseModel):
    """One matched symbol, in the screen's own order.

    Twelve columns of the hundred-odd `screen_quotes` holds. The rest
    stay one keystroke away -- `DS screen_quotes symbol=X` -- so nothing
    is hidden; this is the grid a screener is read in.
    """

    rank_index: int
    symbol: str
    #: False when the symbol is outside this deployment's universe. The
    #: roster still lists it: what the screen matched is the data.
    is_known: bool
    short_name: str | None
    currency: str | None
    exchange: str | None
    market_state: str | None
    price: str | None
    change: str | None
    change_percent: str | None
    volume: int | None
    market_cap: str | None
    trailing_pe: str | None
    fifty_two_week_change_percent: str | None


class ScreenDetail(BaseModel):
    screen: ScreenSummary
    rows: list[ScreenRow]
    #: Where `rows` starts in the roster, so the page can say "301-400 of
    #: 1,000" rather than leaving the reader to count.
    offset: int
    #: True when there are more rows after this page.
    truncated: bool


#: Rows per page. A roster is bigger than this by default and by design:
#: `yf_screen_size` is 250 and `yf_screen_max_pages` is 4, so a screen
#: can hold 1,000 members, and `most_shorted_stocks` matched 4,022 when
#: it was measured. Showing the first N and stopping would hide the rest
#: of a list whose length the header states -- hence `offset`.
SCREEN_ROWS_MAX = 250


def _latest_runs(session: Session) -> dict[str, ScreenRun]:
    """The most recent run of each screen, in one query.

    `DISTINCT ON` rather than a correlated subquery per screen: the list
    is drawn on every visit to the panel, and one round trip that reads
    an index is the difference between a page that opens and one that
    thinks about it.
    """
    stmt = (
        select(ScreenRun)
        .distinct(ScreenRun.screen_key)
        .order_by(ScreenRun.screen_key, ScreenRun.as_of_date.desc())
    )
    return {run.screen_key: run for run in session.scalars(stmt)}


def _summary(screen: Screen, run: ScreenRun | None) -> ScreenSummary:
    return ScreenSummary(
        screen_key=screen.screen_key,
        title=screen.title,
        description=screen.description,
        kind=str(screen.kind),
        quote_type=str(screen.quote_type),
        sort_field=screen.sort_field,
        sort_asc=screen.sort_asc,
        as_of_date=None if run is None else run.as_of_date,
        fetched_at=None if run is None else run.fetched_at,
        total=None if run is None else run.total,
        row_count=None if run is None else run.row_count,
    )


def list_screens(session: Session) -> list[ScreenSummary]:
    """Enabled screens, by title, each with its latest run.

    Disabled ones are left out: `screens.is_enabled` is the operator's
    switch (the admin page writes it), and a screen turned off stops
    being fetched, so its roster goes stale from that day on. Listing it
    would offer a page of data with an invisible expiry date.
    """
    runs = _latest_runs(session)
    stmt = select(Screen).where(Screen.is_enabled.is_(True)).order_by(Screen.title)
    return [_summary(screen, runs.get(screen.screen_key)) for screen in session.scalars(stmt)]


def read_screen(
    session: Session, screen_key: str, limit: int, offset: int = 0
) -> ScreenDetail | None:
    """One screen's latest roster, joined to that day's quotes.

    None when there is no such screen. A screen that exists but has
    never run comes back with an empty roster rather than a 404: those
    are different problems and the panel says different things about them.

    The join is an OUTER one on purpose. `screen_quotes` is keyed by
    `(symbol, as_of_date)` and is NOT inside the gate's delete scope, so
    a member can exist without a quote row -- an unknown symbol, or a
    quote that failed to parse. Dropping those rows would quietly
    shorten a roster whose length is itself reported.
    """
    screen = session.get(Screen, screen_key)
    if screen is None:
        return None
    run = session.scalars(
        select(ScreenRun)
        .where(ScreenRun.screen_key == screen_key)
        .order_by(ScreenRun.as_of_date.desc())
        .limit(1)
    ).first()
    summary = _summary(screen, run)
    if run is None:
        return ScreenDetail(screen=summary, rows=[], offset=offset, truncated=False)

    quotes = screen_quotes.c
    stmt = (
        select(ScreenMember, screen_quotes)
        .outerjoin(
            screen_quotes,
            and_(
                quotes.symbol == ScreenMember.symbol,
                quotes.as_of_date == ScreenMember.as_of_date,
            ),
        )
        .where(
            ScreenMember.screen_key == screen_key,
            ScreenMember.as_of_date == run.as_of_date,
        )
        # The screen's own sort order. Sorting by symbol here would throw
        # away the one thing a screener's roster carries beyond a list of
        # tickers.
        .order_by(ScreenMember.rank_index)
        # One past the page, so "is there more" is answered by the query
        # rather than by comparing against a count that was read in a
        # different statement and may have moved.
        .limit(limit + 1)
        .offset(offset)
    )
    rows: list[ScreenRow] = []
    truncated = False
    for member, quote in _member_rows(session, stmt):
        if len(rows) == limit:
            truncated = True
            break
        rows.append(
            ScreenRow(
                rank_index=member.rank_index,
                symbol=member.symbol,
                is_known=member.is_known,
                short_name=quote.get("short_name"),
                currency=quote.get("currency"),
                exchange=quote.get("exchange"),
                market_state=quote.get("market_state"),
                # `to_number`, not the tick path's `normalize()`: this is
                # the same string every other API row carries, and the
                # page formats it. The tick wire normalises because `QR`
                # shows that string raw.
                price=to_number(quote.get("regular_market_price")),
                change=to_number(quote.get("regular_market_change")),
                change_percent=to_number(quote.get("regular_market_change_percent")),
                volume=quote.get("regular_market_volume"),
                market_cap=to_number(quote.get("market_cap")),
                trailing_pe=to_number(quote.get("trailing_pe")),
                fifty_two_week_change_percent=to_number(
                    quote.get("fifty_two_week_change_percent")
                ),
            )
        )
    return ScreenDetail(screen=summary, rows=rows, offset=offset, truncated=truncated)


def _member_rows(
    session: Session, stmt: Select[Any]
) -> Iterator[tuple[ScreenMember, dict[str, Any]]]:
    """The member and its quote columns as a plain mapping.

    A `Table` in the select list comes back as loose columns rather than
    an object, and an outer join makes every one of them nullable, so
    the mapping is built here once instead of at each field below.
    """
    for row in session.execute(stmt):
        member = row[0]
        quote = {column.name: row[index + 1] for index, column in enumerate(screen_quotes.c)}
        yield member, quote


@router.get("/screens", response_model=Collection[ScreenSummary])
def screens_list(session: SessionDep) -> Collection[ScreenSummary]:
    limits.apply_statement_timeout(session)
    return Collection[ScreenSummary](
        data=list_screens(session), next_cursor=None, as_of=None
    )


@router.get("/screens/{screen_key}", response_model=Resource[ScreenDetail])
def screen_detail(
    session: SessionDep,
    screen_key: str,
    limit: Annotated[int | None, Query(ge=1)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Resource[ScreenDetail]:
    size = limit if limit is not None else SCREEN_ROWS_MAX
    if size > SCREEN_ROWS_MAX:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Page size above the maximum",
            detail=f"limit must not exceed {SCREEN_ROWS_MAX}",
        )
    limits.apply_statement_timeout(session)
    detail = read_screen(session, screen_key, size, offset)
    if detail is None:
        raise ApiProblem(404, TYPE_NOT_FOUND, "No such screen")
    # `as_of` is filled in here, unlike the other UI routes: a screen run
    # IS a fetch, so `fetched_at` is exactly what the envelope means by
    # "when this data was last verified against the source".
    return Resource[ScreenDetail](data=detail, as_of=detail.screen.fetched_at)

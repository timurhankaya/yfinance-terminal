"""Discovery tables: Search, Lookup, Screener.

`discovery_asof_state` is separate from `asof_state` because a free-text
term is not in `symbols` and would fail the FK. Symbol columns carry no FK
(discovery returns symbols outside the universe) and are indexed instead."""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    SYMBOL_LENGTH,
    AsciiKeyType,
    Base,
    HashType,
    PriceType,
    RawJsonType,
    SymbolType,
    TsType,
)
from yfin.models.columns import make_column
from yfin.models.domains import REPORT_ID_LENGTH
from yfin.models.fields import SCREENER_QUOTE_FIELDS

# Free-text search term. Must equal `SYMBOL_LENGTH`: the term is also
# written to `sync_run_items.symbol` (`SymbolType()`) as an audit record,
# and a wider term overflows there after the data is already written.
QUERY_TERM_LENGTH = SYMBOL_LENGTH

# `slug` for `ALGO_WATCHLIST`, `canonicalName` for `PREDEFINED_SCREENER`.
LIST_KEY_LENGTH = 128

# `lookupTotals` reports nine types; the `LOOKUP_TYPES` constant lists
# eight -- `privateCompany` is not among them (lookup.py:31). Free text
# rather than an ENUM, so a new type reported by the source needs no
# schema change.
LOOKUP_TYPE_LENGTH = 24

# The screen key is also written to `sync_run_items.symbol` as a scope
# label, so it is bound by the same limit.
SCREEN_KEY_LENGTH = SYMBOL_LENGTH


def _query_term_column(**kwargs: object) -> Mapped[str]:
    return mapped_column(AsciiKeyType(QUERY_TERM_LENGTH), **kwargs)  # type: ignore[arg-type]


class ScreenKind(enum.StrEnum):
    PREDEFINED = "predefined"
    CUSTOM = "custom"


class ScreenQuoteType(enum.StrEnum):
    """The `quoteType` written into `yf.screen`'s POST body.

    Three values, three query classes: EquityQuery / FundQuery / ETFQuery.
    """

    EQUITY = "EQUITY"
    MUTUALFUND = "MUTUALFUND"
    ETF = "ETF"


def _enum(kind: type[enum.StrEnum], name: str) -> Enum:
    """`name` is given explicitly: on PostgreSQL an ENUM name is
    permanent, and SQLAlchemy's class-derived name (`screenkind`) breaks
    the project's snake_case convention."""
    return Enum(kind, values_callable=lambda e: [m.value for m in e], name=name)


# --- 1. gate -----------------------------------------------------------


class DiscoveryAsOfState(Base):
    """Search / Lookup gate.

    Has no FK on `query_term`; that is this table's reason to exist.
    """

    __tablename__ = "discovery_asof_state"
    __table_args__ = (
        Index("ix_discovery_asof_dataset_date", "dataset", "as_of_date"),
    )

    query_term: Mapped[str] = _query_term_column(primary_key=True)
    dataset: Mapped[str] = mapped_column(AsciiKeyType(32), primary_key=True)
    # As-of date of the last change.
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    content_hash: Mapped[str] = mapped_column(HashType(), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Written on first INSERT, never updated again.
    first_seen_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    # Last verification time: updated even when the hash is unchanged.
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


# --- 2. search / lookup --------------------------------------------------


class SearchQuote(Base):
    """`Search.quotes` -- symbols returned by a term.

    Rows without a symbol never enter this table: `include_cb=True` also
    returns Crunchbase private-company records, which normalize drops."""

    __tablename__ = "search_quotes"
    __table_args__ = (Index("ix_search_quotes_symbol", "symbol"),)

    query_term: Mapped[str] = _query_term_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    # 0-based order in the response. The source's own ordering is a
    # score; rows without a symbol are numbered after being filtered out.
    rank_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    score: Mapped[Decimal | None] = mapped_column(PriceType())
    quote_type: Mapped[str | None] = mapped_column(String(32, collation="C"))
    type_disp: Mapped[str | None] = mapped_column(String(64, collation="C"))
    exchange: Mapped[str | None] = mapped_column(String(32, collation="C"))
    exch_disp: Mapped[str | None] = mapped_column(String(64, collation="C"))
    short_name: Mapped[str | None] = mapped_column(String(128, collation="C"))
    long_name: Mapped[str | None] = mapped_column(String(255, collation="C"))
    # Sector/industry family only appears on EQUITY rows; absence is not
    # an error, just NULL.
    sector: Mapped[str | None] = mapped_column(String(64, collation="C"))
    sector_disp: Mapped[str | None] = mapped_column(String(64, collation="C"))
    industry: Mapped[str | None] = mapped_column(String(128, collation="C"))
    industry_disp: Mapped[str | None] = mapped_column(String(128, collation="C"))
    disp_sec_ind_flag: Mapped[bool | None] = mapped_column(Boolean)
    is_yahoo_finance: Mapped[bool | None] = mapped_column(Boolean)
    prev_name: Mapped[str | None] = mapped_column(String(255, collation="C"))
    name_change_date: Mapped[datetime | None] = mapped_column(TsType())
    # Whether the symbol could be included in the symbols write.
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)


class SearchList(Base):
    """`Search.lists` -- a block with two shapes.

    `list_type` discriminates `ALGO_WATCHLIST` (`slug`+`pfId`) from
    `PREDEFINED_SCREENER` (`canonicalName`+`total`). No membership row:
    resolving membership would cost a second request."""

    __tablename__ = "search_lists"

    query_term: Mapped[str] = _query_term_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    list_key: Mapped[str] = mapped_column(AsciiKeyType(LIST_KEY_LENGTH), primary_key=True)

    rank_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    list_type: Mapped[str | None] = mapped_column(String(32, collation="C"))
    # `name` for ALGO_WATCHLIST, `title` for PREDEFINED_SCREENER.
    name: Mapped[str | None] = mapped_column(String(255, collation="C"))
    score: Mapped[Decimal | None] = mapped_column(PriceType())
    icon_url: Mapped[str | None] = mapped_column(Text)
    # --- ALGO_WATCHLIST only ---
    brand_slug: Mapped[str | None] = mapped_column(String(64, collation="C"))
    pf_id: Mapped[str | None] = mapped_column(String(128, collation="C"))
    user_id: Mapped[str | None] = mapped_column(String(64, collation="C"))
    symbol_count: Mapped[int | None] = mapped_column(Integer)
    daily_percent_gain: Mapped[Decimal | None] = mapped_column(PriceType())
    follower_count: Mapped[int | None] = mapped_column(Integer)
    # --- PREDEFINED_SCREENER only ---
    yahoo_id: Mapped[str | None] = mapped_column(String(64, collation="C"))
    total: Mapped[int | None] = mapped_column(Integer)
    is_premium: Mapped[bool | None] = mapped_column(Boolean)

    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)


class SearchReportHit(Base):
    """Term <-> report link.

    Sibling of `domain_report_links`. Carries an FK, unlike symbol columns:
    `report_id` is not a symbol and the parent row is written first."""

    __tablename__ = "search_report_hits"

    query_term: Mapped[str] = _query_term_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    report_id: Mapped[str] = mapped_column(
        AsciiKeyType(REPORT_ID_LENGTH),
        ForeignKey("research_reports.report_id", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
    )
    rank_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class LookupResult(Base):
    """`Lookup` documents."""

    __tablename__ = "lookup_results"
    __table_args__ = (Index("ix_lookup_results_symbol", "symbol"),)

    query_term: Mapped[str] = _query_term_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    # 0-based order in the response.
    rank_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # The source's own `rank_index` field -- not an order but Yahoo's
    # ranking score. A shared name would collide with `rank_index` above.
    source_rank: Mapped[int | None] = mapped_column(Integer)
    # Which call this came from. Required because of the adaptive call
    # strategy: the same symbol can be returned by both `equity` and
    # `etf` calls, and since this is not in the PK, the last write wins
    # -- so which call wrote it must be auditable.
    lookup_type: Mapped[str | None] = mapped_column(String(LOOKUP_TYPE_LENGTH, collation="C"))
    quote_type: Mapped[str | None] = mapped_column(String(32, collation="C"))
    exchange: Mapped[str | None] = mapped_column(String(32, collation="C"))
    short_name: Mapped[str | None] = mapped_column(String(128, collation="C"))
    # Populated only on `equity` documents.
    industry_name: Mapped[str | None] = mapped_column(String(128, collation="C"))
    industry_link: Mapped[str | None] = mapped_column(Text)
    fullday_price: Mapped[Decimal | None] = mapped_column(PriceType())
    fullday_change: Mapped[Decimal | None] = mapped_column(PriceType())
    fullday_change_percent: Mapped[Decimal | None] = mapped_column(PriceType())
    regular_market_price: Mapped[Decimal | None] = mapped_column(PriceType())
    regular_market_change: Mapped[Decimal | None] = mapped_column(PriceType())
    regular_market_percent_change: Mapped[Decimal | None] = mapped_column(PriceType())
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)


class LookupTotal(Base):
    """`lookupTotals` -- carries the completeness evidence.

    The gap between `total` and the actual document count documents
    truncation and triggers the adaptive call branch."""

    __tablename__ = "lookup_totals"

    query_term: Mapped[str] = _query_term_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    # Not normalized: the value is exactly Yahoo's response dict key
    # (`privateCompany` is camelCase). A source spelling change creates a
    # second row under "C" and shows up in an audit rather than silently
    # updating the same row.
    lookup_type: Mapped[str] = mapped_column(
        String(LOOKUP_TYPE_LENGTH, collation="C"), primary_key=True
    )
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


# --- 3. screener ---------------------------------------------------------


class Screen(Base):
    """A screen's static identity -- sibling of the `domains` table.

    Seeded from `ScreenDef` in `screens.py`; `is_enabled` here is the
    runtime source, so the file does not re-enable a DB-disabled screen."""

    __tablename__ = "screens"

    screen_key: Mapped[str] = mapped_column(AsciiKeyType(SCREEN_KEY_LENGTH), primary_key=True)
    kind: Mapped[ScreenKind] = mapped_column(_enum(ScreenKind, "screen_kind"), nullable=False)
    quote_type: Mapped[ScreenQuoteType] = mapped_column(
        _enum(ScreenQuoteType, "screen_quote_type"), nullable=False
    )
    # Refreshed from the first GET page for predefined screens; for
    # custom screens it comes from `ScreenDef` and is never refreshed --
    # the POST response carries no metadata.
    title: Mapped[str] = mapped_column(String(255, collation="C"), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # `sortAsc` defaults to descending; without tracking sort order
    # explicitly, page-to-page inconsistency skips symbols.
    sort_field: Mapped[str] = mapped_column(String(64, collation="C"), nullable=False)
    sort_asc: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    definition_json: Mapped[str | None] = mapped_column(RawJsonType())
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class ScreenRun(Base):
    """A screen's daily header -- both gate and data.

    `content_hash` covers only the roster: quote metrics move daily and
    would make the hash never match. `screen_members` is not written at
    all when the hash is unchanged."""

    __tablename__ = "screen_runs"
    __table_args__ = (Index("ix_screen_runs_date", "as_of_date"),)

    screen_key: Mapped[str] = mapped_column(AsciiKeyType(SCREEN_KEY_LENGTH), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)

    # The actual match count reported by Yahoo.
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    # Number of quotes actually returned. Its gap with `total` records,
    # rather than silently hiding, that the screen hit a page limit.
    fetched_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    # Gate-contract column: the row count of `screen_members`.
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    yahoo_id: Mapped[str | None] = mapped_column(String(64, collation="C"))
    version_id: Mapped[int | None] = mapped_column(Integer)
    last_updated: Mapped[datetime | None] = mapped_column(TsType())
    criteria_json: Mapped[str | None] = mapped_column(RawJsonType())
    content_hash: Mapped[str] = mapped_column(HashType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class ScreenMember(Base):
    """A screen's roster for the day -- the gate's child.

    `replace_scope` is `(screen_key, as_of_date)`: the day's last run wins,
    so a symbol that dropped out intraday is not left in the roster."""

    __tablename__ = "screen_members"
    __table_args__ = (Index("ix_screen_members_symbol", "symbol"),)

    screen_key: Mapped[str] = mapped_column(AsciiKeyType(SCREEN_KEY_LENGTH), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    # `offset + within-page 0-based index` = absolute order by the
    # screen's `sort_field`. Part of the hash body: if the roster stays
    # the same but the order changes, that is a real change.
    rank_index: Mapped[int] = mapped_column(Integer, nullable=False)
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


# `screen_quotes` -- independent of any single screen: PK (symbol,
# as_of_date), so a symbol in five screens is written once, and not in the
# gate's delete scope, so leaving a roster does not delete the quote.
# Columns generated from `INFO_FIELDS` keys share names with `ticker_info`.
screen_quotes = Table(
    "screen_quotes",
    Base.metadata,
    Column("symbol", SymbolType(), primary_key=True, nullable=False),
    Column("as_of_date", Date, primary_key=True, nullable=False),
    *(make_column(f, "screen_quotes") for f in SCREENER_QUOTE_FIELDS),
    Column("is_known", Boolean, nullable=False, server_default=text("false")),
    Column("fetched_at", TsType(), nullable=False),
    # `corporateActions` is a list and does not become a column; it stays here.
    Column("raw_json", RawJsonType(), nullable=False)
)

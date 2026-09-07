"""Discovery tables: Search, Lookup, Screener.

Ten tables in three groups:

1. Gate -- `discovery_asof_state`. `asof_state` cannot be reused: its
   `symbol` column is `symbol_fk_column`, carrying an ON DELETE RESTRICT
   FK to `symbols.symbol`. A free-text term (`"Turkish Airlines"`) is
   not in `symbols`, so the gate row would hit an FK violation (23503).
   Domain hit the same wall and opened `domain_asof_state` for it.

2. Search / Lookup -- term-scoped, as-of. Keyed on `query_term`, not
   `symbol`: one search term's result carries multiple symbols.

3. Screener -- `screen_runs` (gate + data), `screen_members` (child),
   and `screen_quotes` (independent of any screen).

Symbol columns carry no FK: discovery datasets return symbols outside
the universe by definition. An FK would roll back a cell's entire data
whenever a `symbols` write failed for any reason -- same reasoning as
news_symbols (models/news.py:45-51). Since there is no FK, every symbol
column gets an explicit index.
"""

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

# Free-text search term. Its length matches `SYMBOL_LENGTH` by necessity,
# not preference: the term is also written to `sync_run_items.symbol`
# (= `SymbolType()`) as an audit record. A wider limit was tried and
# overflowed (22001) -- after the data was already written, at
# `write_items`, the latest possible point in the run.
#
# An earlier design used 64 and assumed the audit record would truncate
# it; truncation was never implemented, and the two constants drifting
# apart hid the bug. One shared constant makes the class of bug
# structurally impossible.
QUERY_TERM_LENGTH = SYMBOL_LENGTH

# `slug` for `ALGO_WATCHLIST`, `canonicalName` for `PREDEFINED_SCREENER`.
# Measured max: `most-bought-by-activist-hedge-funds` = 35.
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

    Rows without a symbol never enter this table: the `include_cb=True`
    default also returns Crunchbase private-company records
    (`{index, name, permalink, isYahooFinance}`), and normalize filters
    them out.
    """

    __tablename__ = "search_quotes"
    __table_args__ = (Index("ix_search_quotes_symbol", "symbol"),)

    query_term: Mapped[str] = _query_term_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    # 0-based order in the response. The source's own ordering is a
    # score; rows without a symbol are numbered after being filtered out.
    rank_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Measured range 12.2 - 16,067,500.0.
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

    `list_type` discriminates: `ALGO_WATCHLIST` (12 keys, `slug`+`pfId`)
    vs. `PREDEFINED_SCREENER` (9 keys, `canonicalName`+`total`). Only four
    fields are shared; separate tables would duplicate those. Follows the
    codebase's own rule for related-but-non-identical shapes (one table +
    an ENUM discriminator), the same pattern as
    institutional_holders+mutualfund_holders.

    No membership row: the block carries no symbol. But it is not "just a
    count" either -- the row carries the identity needed to resolve
    membership (`pfId`+`userId` or `canonicalName`). It is out of scope
    because of cost (a second request), not absence of data.
    """

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

    Sibling of `domain_report_links`. Carries an FK here, unlike symbol
    columns: `report_id` is not a symbol, so there is no out-of-universe
    problem, and the parent row is written in the same transaction,
    before the gated writes.
    """

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
    # ranking score (measured example 30007). A shared name would
    # collide with `rank_index` above.
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

    Comes free in the same response. The gap between `total` and the
    actual document count documents truncation: measured, `GOLD`'s
    `lookupTotals.all` reported 7,273 while `documents` returned 995.
    This same gap is the signal that triggers the adaptive call branch.
    """

    __tablename__ = "lookup_totals"

    query_term: Mapped[str] = _query_term_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    # Deliberately not normalized. COLLATE "C", like every other key
    # column here.
    #
    # The value is exactly Yahoo's response dict key
    # (`raw.totals.items()`: 'equity', 'mutualfund', 'privateCompany').
    # `.lower()` is not applied, for two reasons:
    #   1. `privateCompany` is camelCase; lowercasing it breaks the
    #      source identifier and any code matching on that key.
    #   2. The behavior difference is visible, not silent: a source
    #      reporting 'Equity' one day creates a second row under "C" and
    #      the mismatch shows up in an audit, instead of silently
    #      updating the same row. A loud failure is the intended tradeoff.
    lookup_type: Mapped[str] = mapped_column(
        String(LOOKUP_TYPE_LENGTH, collation="C"), primary_key=True
    )
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


# --- 3. screener ---------------------------------------------------------


class Screen(Base):
    """A screen's static identity -- sibling of the `domains` table.

    Seeded from the `ScreenDef` set in `screens.py`. That file is the
    source of the definition; this table's `is_enabled` column is the
    source of runtime activity -- once an operator disables it in the
    DB, the file does not turn it back on.
    """

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

    `HashGatedDataset`'s `financial_periods` pattern: the gate is itself
    a data table, and the child (`screen_members`) is not written at all
    when `content_hash` is unchanged.

    `content_hash` covers only the roster. If quote metrics entered the
    hash body, daily price movement would mean the hash never matched,
    silently killing the mechanism.
    """

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

    `replace_scope` is `(screen_key, as_of_date)`: when the roster
    changes within a day (measured: `day_gainers` 122 -> 117), the day's
    last run wins. A plain upsert would leave a symbol that appeared in
    the morning and dropped by noon permanently, incorrectly, in that
    day's roster.
    """

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


# `screen_quotes` -- independent of any single screen.
#
# PK (symbol, as_of_date): a symbol appearing in five screens does not
# get its 102 fields written five times. Not covered by the gate's
# delete scope, deliberately -- a symbol's quote does not belong to one
# screen, so dropping out of a roster does not delete its quote.
#
# 75 of the columns are generated from the same source keys as
# `INFO_FIELDS`, so they share column names with `ticker_info` and the
# two tables can be compared without a JOIN.
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

"""Sector / industry (domain) tables.

Sector and industry share column sets, so one table + a discriminator
ENUM, as elsewhere in the codebase. `domain_type` is not in the PK: the
key sets are disjoint, and a two-column PK would ride every `parent_key` JOIN."""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    Base,
    BigNumType,
    HashType,
    PriceType,
    RawJsonType,
    RegionType,
    SymbolType,
    TsType,
    symbol_fk_column,
)

DOMAIN_KEY_LENGTH = 48
REPORT_ID_LENGTH = 64


class DomainType(enum.StrEnum):
    SECTOR = "sector"
    INDUSTRY = "industry"


class FundType(enum.StrEnum):
    ETF = "etf"
    MUTUAL_FUND = "mutual_fund"


class RankType(enum.StrEnum):
    PERFORMING = "performing"
    GROWTH = "growth"


DOMAIN_TYPE_ENUM = Enum(
    DomainType,
    values_callable=lambda e: [m.value for m in e],
    name="domain_type",
    native_enum=True,
)
FUND_TYPE_ENUM = Enum(
    FundType,
    values_callable=lambda e: [m.value for m in e],
    name="domain_fund_type",
    native_enum=True,
)
RANK_TYPE_ENUM = Enum(
    RankType,
    values_callable=lambda e: [m.value for m in e],
    name="domain_rank_type",
    native_enum=True,
)


def domain_key_column(**kwargs: object) -> Mapped[str]:
    """A key column carrying an FK to `domains.domain_key`.

    COLLATE "C": keys are case sensitive at the source, and a
    case-insensitive collation would fold two distinct keys into one row."""
    return mapped_column(
        AsciiKeyType(DOMAIN_KEY_LENGTH),
        ForeignKey("domains.domain_key", onupdate="CASCADE", ondelete="RESTRICT"),
        **kwargs,  # type: ignore[arg-type]
    )


class Domain(Base):
    """Static identity: name, description, symbol, parent. Not as-of -- upsert.

    These fields change a few times a year; a daily snapshot buys nothing.
    """

    __tablename__ = "domains"
    __table_args__ = (
        # Required for an industry's parent; NULL for a sector.
        CheckConstraint(
            "domain_type = 'sector' OR parent_key IS NOT NULL",
            name="ck_domains_parent",
        ),
        Index("ix_domains_type_parent", "domain_type", "parent_key"),
    )

    domain_key: Mapped[str] = mapped_column(AsciiKeyType(DOMAIN_KEY_LENGTH), primary_key=True)
    domain_type: Mapped[DomainType] = mapped_column(DOMAIN_TYPE_ENUM, nullable=False)
    # UNIQUE: each domain's index symbol belongs to exactly one domain.
    symbol: Mapped[str] = symbol_fk_column(nullable=False, unique=True)
    # Self-FK. ON UPDATE RESTRICT, not CASCADE as in `symbol_fk_column`:
    # `domain_key` is Yahoo's fixed slug and a rename should fail loudly.
    # ON DELETE RESTRICT: deleting a sector cannot orphan its industries.
    parent_key: Mapped[str | None] = mapped_column(
        AsciiKeyType(DOMAIN_KEY_LENGTH),
        ForeignKey("domains.domain_key", onupdate="RESTRICT", ondelete="RESTRICT"),
    )
    name: Mapped[str] = mapped_column(String(64, collation="C"), nullable=False)
    # Written by bootstrap for sectors; the `industries[]` block lacks it, so
    # `industry_profile` writes it for industries -- NULL until the first run.
    description: Mapped[str | None] = mapped_column(Text)
    message_board_id: Mapped[str | None] = mapped_column(String(32, collation="C"))
    # Written on first INSERT, never updated again: kept out of
    # `update_columns` scope.
    first_seen_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    # Last verification time.
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainMetric(Base):
    """as-of, region-free: `overview` + `performance` + benchmark in one row.
    No region column: these blocks are identical across regions."""

    __tablename__ = "domain_metrics"
    __table_args__ = (
        Index("ix_domain_metrics_date", "as_of_date"),
    )

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)

    companies_count: Mapped[int | None] = mapped_column(Integer)
    # Absent from the industry `overview` raw JSON; yfinance's `.get()`
    # turns it into None.
    industries_count: Mapped[int | None] = mapped_column(Integer)
    market_cap: Mapped[Decimal | None] = mapped_column(BigNumType())
    market_weight: Mapped[Decimal | None] = mapped_column(PriceType())
    employee_count: Mapped[int | None] = mapped_column(
        BigInteger,
        CheckConstraint(
            '"employee_count" >= 0', name="ck_domain_metrics_employee_count_nonneg"
        ),
    )

    # `performance` block -- yfinance exposes it through no property.
    ytd_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    reg_market_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    one_year_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    three_year_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    five_year_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())

    # `performanceOverviewBenchmark` block -- also absent from yfinance.
    benchmark_name: Mapped[str | None] = mapped_column(String(64, collation="C"))
    benchmark_ytd_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    benchmark_reg_market_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    benchmark_one_year_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    benchmark_three_year_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    benchmark_five_year_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())

    # The non-list part of the response. List blocks are excluded because
    # `canonical_json` preserves list order, and `topCompanies` order
    # changes between runs, which would reopen the gate on every run.
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainTopCompany(Base):
    """A domain's largest companies. Written by two datasets (sector + industry).

    Order (`position`) is not stored: it is derivable from `market_weight`,
    and it changes between runs, which would change the hash every run."""

    __tablename__ = "domain_top_companies"
    __table_args__ = (
        # This is the 4th PK column, so "which sectors have this company
        # in their top 50" would otherwise require a full scan.
        Index("ix_domain_top_companies_symbol", "symbol"),
    )

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    region: Mapped[str] = mapped_column(RegionType(), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    # No FK: symbols here can be outside the universe, and an FK would fail
    # the whole domain's transaction over one foreign symbol. `is_known` is
    # populated from the DB instead.
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    name: Mapped[str | None] = mapped_column(String(255, collation="C"))
    # Not an ENUM -- no evidence the list of values is closed.
    rating: Mapped[str | None] = mapped_column(String(32, collation="C"))
    market_weight: Mapped[Decimal | None] = mapped_column(PriceType())
    market_cap: Mapped[Decimal | None] = mapped_column(BigNumType())
    last_price: Mapped[Decimal | None] = mapped_column(PriceType())
    target_price: Mapped[Decimal | None] = mapped_column(PriceType())
    ytd_return: Mapped[Decimal | None] = mapped_column(PriceType())
    reg_market_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainTopFund(Base):
    """topETFs + topMutualFunds -- sector only (the industry response has
    no such blocks). `fund_type` is in the PK: the symbol sets are disjoint
    today, but nothing guarantees they stay so."""

    __tablename__ = "domain_top_funds"

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    region: Mapped[str] = mapped_column(RegionType(), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    fund_type: Mapped[FundType] = mapped_column(FUND_TYPE_ENUM, primary_key=True)
    # May not be a ticker: mutual funds can carry a Morningstar id.
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    name: Mapped[str | None] = mapped_column(String(255, collation="C"))
    net_assets: Mapped[Decimal | None] = mapped_column(BigNumType())
    expense_ratio: Mapped[Decimal | None] = mapped_column(PriceType())
    last_price: Mapped[Decimal | None] = mapped_column(PriceType())
    ytd_return: Mapped[Decimal | None] = mapped_column(PriceType())
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainTopMover(Base):
    """topPerformingCompanies + topGrowthCompanies -- industry only.

    `rank_type` in the PK prevents data loss: a symbol shared by both
    lists can report different `ytdReturn` values."""

    __tablename__ = "domain_top_movers"

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    region: Mapped[str] = mapped_column(RegionType(), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    rank_type: Mapped[RankType] = mapped_column(RANK_TYPE_ENUM, primary_key=True)
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    name: Mapped[str | None] = mapped_column(String(255, collation="C"))
    # Sentinel-looking values (9999.0) are written as-is, not converted to NULL.
    ytd_return: Mapped[Decimal | None] = mapped_column(PriceType())
    last_price: Mapped[Decimal | None] = mapped_column(PriceType())
    # Only in the `performing` list.
    target_price: Mapped[Decimal | None] = mapped_column(PriceType())
    # Only in the `growth` list.
    growth_estimate: Mapped[Decimal | None] = mapped_column(PriceType())
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class ResearchReport(Base):
    """Analyst report -- a shared entity from two sources.

    The same report id appears in several domains and in `Search.research`,
    hence one report table plus link tables. The two sources populate
    different columns and never overwrite each other's."""

    __tablename__ = "research_reports"
    __table_args__ = (
        Index("ix_research_reports_date", "as_of_date"),
    )

    report_id: Mapped[str] = mapped_column(AsciiKeyType(REPORT_ID_LENGTH), primary_key=True)
    # Not in the PK: "last seen on this day"; per-domain days live in
    # `domain_report_links`. Still required because `AsOfGate` reads
    # `as_of_date` from the first row of `writes`, which can be this table.
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64, collation="C"))
    report_type: Mapped[str | None] = mapped_column(String(64, collation="C"))
    head_html: Mapped[str | None] = mapped_column(String(255, collation="C"))
    # Unbounded: titles run far past any sensible VARCHAR width.
    report_title: Mapped[str | None] = mapped_column(Text)
    # Arrives as a bare float (unlike topCompanies[].targetPrice, which is wrapped).
    target_price: Mapped[Decimal | None] = mapped_column(PriceType())
    target_price_status: Mapped[str | None] = mapped_column(String(32, collation="C"))
    investment_rating: Mapped[str | None] = mapped_column(String(32, collation="C"))
    # ISO text on the domain path, epoch milliseconds on the search path.
    # A shared converter would silently NULL one of them; each dataset
    # applies its own.
    report_ts_utc: Mapped[datetime | None] = mapped_column(TsType())
    # --- populated only on the search path ---
    # The domain response carries no author field.
    author: Mapped[str | None] = mapped_column(String(128, collation="C"))
    # `Search.research` -> `reportHeadline`. Stays NULL on the domain
    # path: its title lives in `head_html` / `report_title`, and the two
    # are not the same thing.
    report_headline: Mapped[str | None] = mapped_column(String(512, collation="C"))
    # Kept out of `update_columns`.
    first_seen_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainReportLink(Base):
    """Report <-> domain link, per day.

    No region column: report ids returned in the identical order across
    all 5 regions.
    """

    __tablename__ = "domain_report_links"

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    report_id: Mapped[str] = mapped_column(
        AsciiKeyType(REPORT_ID_LENGTH),
        ForeignKey("research_reports.report_id", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
    )
    # List order.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainAsOfState(Base):
    """The as-of gate for the domain side.

    `asof_state` is keyed on (symbol, dataset) and reads `row["symbol"]`,
    which on domain tables is the company's symbol, not the gate entity;
    it also has no region axis."""

    __tablename__ = "domain_asof_state"
    __table_args__ = (
        Index("ix_domain_asof_dataset_date", "dataset", "as_of_date"),
    )

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    dataset: Mapped[str] = mapped_column(AsciiKeyType(32), primary_key=True)
    # Region-free datasets write '*' (the market_runner.GLOBAL_SCOPE_MARKER
    # pattern).
    region: Mapped[str] = mapped_column(RegionType(), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    content_hash: Mapped[str] = mapped_column(HashType(), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)

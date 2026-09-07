"""Sector / industry (domain) tables.

Unified taxonomy, by measurement: sector and industry `overview` column
sets are identical, and `topCompanies` column sets measured identical.
The codebase's own rule -- identical column set -> one table + a
discriminator ENUM (institutional_holders+mutualfund_holders,
earnings_estimate+revenue_estimate) -- applies here too.

`domain_type` is not in the PK: the 11 sector and 145 industry keys
measured disjoint (intersection = empty), as do their symbols. Putting
it in the PK would make the `parent_key` self-FK two columns, carried
into every JOIN.
"""

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

# `domain_key` measured max 37 (`utilities-independent-power-producers`);
# 48 leaves headroom.
DOMAIN_KEY_LENGTH = 48
# `report_id` measured max 50.
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

    COLLATE "C": `TECHNOLOGY` returned 404 live -- keys are case
    sensitive, and a case-insensitive collation would fold two distinct
    keys into one row.
    """
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
    # UNIQUE: `^YH311` belongs to exactly one domain. FK -> symbols: 156
    # rows are written by `domain_taxonomy`.
    symbol: Mapped[str] = symbol_fk_column(nullable=False, unique=True)
    # Self-FK. ON DELETE RESTRICT applies here too: deleting a sector
    # cannot orphan 145 industries.
    #
    # ON UPDATE RESTRICT, not CASCADE -- a deliberate deviation from the
    # project's `symbol_fk_column` pattern.
    #
    # Not an engine constraint (it was one under MySQL: a referential
    # action was disallowed on a column used in a CHECK; PostgreSQL has
    # no such restriction). The decision is data-driven: `domain_key` is
    # Yahoo's fixed slug ('technology', 'software-infrastructure') and is
    # not expected to be renamed. If a rename is attempted unexpectedly,
    # RESTRICT gives a visible error instead of a silent corruption.
    #
    # ON DELETE RESTRICT also prevents deleting a sector from orphaning
    # its 145 industries.
    parent_key: Mapped[str | None] = mapped_column(
        AsciiKeyType(DOMAIN_KEY_LENGTH),
        ForeignKey("domains.domain_key", onupdate="RESTRICT", ondelete="RESTRICT"),
    )
    # Measured max 40 chars.
    name: Mapped[str] = mapped_column(String(64, collation="C"), nullable=False)
    # Measured max 446 chars, 156/156 populated. Written by bootstrap for
    # sectors; for industries the `industries[]` block does not include
    # this field, so `industry_profile` writes it instead -- NULL until
    # the first profile run.
    description: Mapped[str | None] = mapped_column(Text)
    # Measured max 15 chars.
    message_board_id: Mapped[str | None] = mapped_column(String(32, collation="C"))
    # Written on first INSERT, never updated again: kept out of
    # `update_columns` scope.
    first_seen_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    # Last verification time.
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainMetric(Base):
    """as-of, region-free: `overview` + `performance` + benchmark in one row.

    No region column: these five blocks measured identical across
    US/GB/DE/JP/TR.
    """

    __tablename__ = "domain_metrics"
    __table_args__ = (
        Index("ix_domain_metrics_date", "as_of_date"),
    )

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)

    # Measured max 1517 (financial-services).
    companies_count: Mapped[int | None] = mapped_column(Integer)
    # This key is absent from the raw JSON in industry `overview` entirely
    # (145/145); yfinance's `.get()` call turns it into None -> nullable.
    industries_count: Mapped[int | None] = mapped_column(Integer)
    # Raw `int`; 1.23e8 ... 2.88e13.
    market_cap: Mapped[Decimal | None] = mapped_column(BigNumType())
    # 1.32e-5 ... 0.746.
    market_weight: Mapped[Decimal | None] = mapped_column(PriceType())
    # 18 ... 11,895,040.
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

    # The non-list part of the response: key, name, symbol, sectorKey,
    # sectorName, overview, performance, performanceOverviewBenchmark.
    # List blocks are excluded: `canonical_json` preserves list order, and
    # storing the full envelope would let `topCompanies` order (changed
    # in 8 of 11 sectors within 15 minutes) reopen the gate on every run
    # -- silently disabling the as-of mechanism entirely.
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainTopCompany(Base):
    """A domain's largest companies. Written by two datasets (sector + industry).

    Order (`position`) is not stored: the ranking criterion,
    `market_weight`, is already a column, and order is derivable from it;
    storing it would change the hash on every run (order changed in 8 of
    11 sectors within 15 minutes).
    """

    __tablename__ = "domain_top_companies"
    __table_args__ = (
        # This is the 4th PK column, so "which sectors have this company
        # in their top 50" would otherwise require a full scan.
        Index("ix_domain_top_companies_symbol", "symbol"),
    )

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    region: Mapped[str] = mapped_column(RegionType(), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    # No FK: SGE.L, 285A.T, ODINE.IS are outside the universe. An FK
    # would fail the whole domain's transaction over one foreign symbol
    # (same reasoning as news_symbols). Instead, `is_known` is populated
    # from the DB.
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    name: Mapped[str | None] = mapped_column(String(255, collation="C"))
    # Measured: Strong Buy / Buy / Hold / Underperform / Sell. Not an
    # ENUM -- no evidence the list is closed (same decision as `action`).
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
    """topETFs + topMutualFunds -- sector only.

    The industry response does not contain these two blocks at all
    (confirmed against its top-level key list); an absence measured, not
    silent data loss.

    `fund_type` is in the PK: identical column sets, symbol sets disjoint
    today -- but nothing proves they stay disjoint tomorrow.
    """

    __tablename__ = "domain_top_funds"

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    region: Mapped[str] = mapped_column(RegionType(), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    fund_type: Mapped[FundType] = mapped_column(FUND_TYPE_ENUM, primary_key=True)
    # May not be a ticker: `0P0001WO1I` is a Morningstar id (measured in
    # healthcare topMutualFunds).
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    # Absent in 7 of 220 daily fund rows; all seven on the mutual fund side.
    name: Mapped[str | None] = mapped_column(String(255, collation="C"))
    net_assets: Mapped[Decimal | None] = mapped_column(BigNumType())
    expense_ratio: Mapped[Decimal | None] = mapped_column(PriceType())
    last_price: Mapped[Decimal | None] = mapped_column(PriceType())
    ytd_return: Mapped[Decimal | None] = mapped_column(PriceType())
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainTopMover(Base):
    """topPerformingCompanies + topGrowthCompanies -- industry only.

    `rank_type` in the PK prevents data loss, not just collisions: a
    full-list measurement across 24 industries found 8 of 50 shared
    symbols reporting two different `ytdReturn` values. Without it in the
    PK, one would silently disappear (same reasoning as
    `fund_metrics.section`).
    """

    __tablename__ = "domain_top_movers"

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    region: Mapped[str] = mapped_column(RegionType(), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    rank_type: Mapped[RankType] = mapped_column(RANK_TYPE_ENUM, primary_key=True)
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    name: Mapped[str | None] = mapped_column(String(255, collation="C"))
    # ELOX (biotechnology) measured 9999.0; not treated as a sentinel,
    # written as-is (same principle as not converting
    # `currentPriceTarget = 0.0` to NULL).
    ytd_return: Mapped[Decimal | None] = mapped_column(PriceType())
    last_price: Mapped[Decimal | None] = mapped_column(PriceType())
    # Only in the `performing` list.
    target_price: Mapped[Decimal | None] = mapped_column(PriceType())
    # Only in the `growth` list; RELL 81.5, low end -9.999999999999998.
    growth_estimate: Mapped[Decimal | None] = mapped_column(PriceType())
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class ResearchReport(Base):
    """Analyst report -- a shared entity from two sources.

    624 report rows are produced per day but only 516 are unique; all 37
    unique sector reports also appear in an industry (100% overlap). A
    single table would violate uniqueness, hence report + link table.

    Not named `domain_research_reports`: `Search.research` returns the
    same reports from the same identity space -- format
    `<PROVIDER>_<SOURCE_ID>_<Type>_<epoch_ms>`, e.g.
    `ARGUS_48138_TechnicalAnalysis_1788520901000` (Sector) and
    `ARGUS_2660_AnalystReport_1785496444000` (Search). Two separate
    tables would store the same report twice, with different column subsets.

    The two sources populate different columns and do not overwrite each
    other: domain -> `head_html`, `report_title`, `report_type`, target
    price/rating; search -> `author`, `report_headline`. Shared:
    `provider` and `report_ts_utc`.
    """

    __tablename__ = "research_reports"
    __table_args__ = (
        Index("ix_research_reports_date", "as_of_date"),
    )

    report_id: Mapped[str] = mapped_column(AsciiKeyType(REPORT_ID_LENGTH), primary_key=True)
    # Not part of the PK -- "last seen on this day". Report content does
    # not change; which day it appeared in which domain is carried by
    # `domain_report_links`. The column's existence is still required:
    # `AsOfGate` reads its gate row's `as_of_date` from the first row of
    # `writes`, and this table can come first -- without the column that
    # would raise KeyError.
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64, collation="C"))
    report_type: Mapped[str | None] = mapped_column(String(64, collation="C"))
    # Measured max 59 chars.
    head_html: Mapped[str | None] = mapped_column(String(255, collation="C"))
    # Must have no length limit: measured max 23,570 characters (104
    # reports, median 281). PostgreSQL `text` has no limit.
    report_title: Mapped[str | None] = mapped_column(Text)
    # Absent entirely in 17 of 104 reports. Also arrives as a bare float
    # (unlike topCompanies[].targetPrice, which is wrapped).
    target_price: Mapped[Decimal | None] = mapped_column(PriceType())
    # Measured: Maintained / Increased / Decreased / absent.
    target_price_status: Mapped[str | None] = mapped_column(String(32, collation="C"))
    # Measured: Bullish / Neutral / Bearish / absent.
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

    Why `asof_state` is not reused: its key is (symbol, dataset), and the
    gate symbol is read from `row["symbol"]` -- but `symbol` on domain
    tables is the company's symbol, so the gate would write against the
    wrong entity. The region axis also has no room there. Same reasoning
    as why `asof_base.py`'s `SnapshotDataset` / `HashGatedDataset` are
    not used here.
    """

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

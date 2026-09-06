"""All models. Importing this module populates Base.metadata."""

from __future__ import annotations

from yfin.models.analysis import (
    METRIC_ENUM,
    AnalystEpsRevision,
    AnalystEpsTrend,
    AnalystEstimate,
    AnalystGradeChange,
    AnalystGrowthEstimate,
    AnalystPriceTarget,
    AnalystRecommendation,
    EarningsHistoryRow,
    EstimateMetric,
)
from yfin.models.asof import AsOfState
from yfin.models.bars import (
    BAR_INTERVALS,
    DAILY_INTERVAL,
    GAP_FETCH_FAILED,
    GAP_RETENTION_EXPIRED,
    INTRADAY_INTERVALS,
    PERIODIC_INTERVALS,
    READABLE_INTERVALS,
    BarGap,
    BarRescale,
    IntradayScope,
    PeriodicBar,
    PriceBar,
    bars_table_for,
    timescale_ddl,
)
from yfin.models.base import Base
from yfin.models.discovery import (
    DiscoveryAsOfState,
    LookupResult,
    LookupTotal,
    Screen,
    ScreenKind,
    ScreenMember,
    ScreenQuoteType,
    ScreenRun,
    SearchList,
    SearchQuote,
    SearchReportHit,
    screen_quotes,
)
from yfin.models.domains import (
    DOMAIN_KEY_LENGTH,
    DOMAIN_TYPE_ENUM,
    FUND_TYPE_ENUM,
    RANK_TYPE_ENUM,
    REPORT_ID_LENGTH,
    Domain,
    DomainAsOfState,
    DomainMetric,
    DomainReportLink,
    DomainTopCompany,
    DomainTopFund,
    DomainTopMover,
    DomainType,
    FundType,
    RankType,
    ResearchReport,
)
from yfin.models.financials import (
    API_FREQ,
    EarningsDate,
    FinancialFact,
    FinancialPeriod,
    SecFiling,
    SecFilingExhibit,
    StatementFreq,
    StatementKind,
    ticker_calendar,
    ticker_calendar_history,
)
from yfin.models.funds import (
    FundMetric,
    FundProfile,
    FundSection,
    FundTopHolding,
    FundWeighting,
    WeightCategory,
)
from yfin.models.holders import (
    HolderBreakdown,
    HolderType,
    InsiderActivity,
    InsiderRosterHolder,
    InsiderTransaction,
    InstitutionalHolder,
)
from yfin.models.market import (
    CalendarEarnings,
    CalendarEconomic,
    CalendarIpo,
    CalendarSplits,
    market_status,
    market_status_history,
    market_summary,
    market_summary_history,
)
from yfin.models.news import News, NewsSymbol
from yfin.models.officers import CompanyOfficer
from yfin.models.prices import CapitalGain, Dividend, PriceHistory, SharesFull, Split
from yfin.models.proxies import Proxy, ProxyHealth, ProxyScheme
from yfin.models.settings import SETTING_KEY_LENGTH, SettingRow
from yfin.models.snapshots import (
    history_metadata,
    ticker_fast_info,
    ticker_fast_info_history,
    ticker_info,
    ticker_info_history,
)
from yfin.models.symbols import Symbol
from yfin.models.sync import ItemStatus, RunScope, RunStatus, SyncRun, SyncRunItem
from yfin.models.views import (
    V_ACTIONS_CREATE,
    V_ACTIONS_DROP,
    V_PRICE_BARS_REGULAR_CREATE,
    V_PRICE_BARS_REGULAR_DROP,
)


def symbol_scoped_tables() -> list[str]:
    """Tables scoped to symbols.symbol, in deletion order.

    Derived entirely from the FK edges in metadata: a new symbol-scoped
    table is automatically covered by `yfin symbols purge` (otherwise it
    would raise ON DELETE RESTRICT).

    Blind spot of this derivation: a symbol-scoped table with no FK to
    `symbols` is invisible here. Such a table existed under MySQL
    (`price_bars`, which could not carry an FK due to partitioning) and
    needed a manually maintained list; a TimescaleDB hypertable can be
    the referencing side, so that exception is gone and the list was
    removed. If a table that cannot carry an FK is added again, the list
    must come back.
    """
    names: list[str] = []
    for table in reversed(Base.metadata.sorted_tables):
        if table.name == "symbols":
            continue
        for fk in table.foreign_keys:
            if fk.column.table.name == "symbols":
                names.append(table.name)
                break
    return names


__all__ = [
    "API_FREQ",
    "DOMAIN_KEY_LENGTH",
    "DOMAIN_TYPE_ENUM",
    "FUND_TYPE_ENUM",
    "RANK_TYPE_ENUM",
    "REPORT_ID_LENGTH",
    "Domain",
    "DomainAsOfState",
    "DomainMetric",
    "DomainReportLink",
    "DiscoveryAsOfState",
    "LookupResult",
    "LookupTotal",
    "Screen",
    "ScreenKind",
    "ScreenMember",
    "ScreenQuoteType",
    "SETTING_KEY_LENGTH",
    "ScreenRun",
    "SearchList",
    "SearchQuote",
    "SettingRow",
    "SearchReportHit",
    "screen_quotes",
    "ResearchReport",
    "DomainTopCompany",
    "DomainTopFund",
    "DomainTopMover",
    "DomainType",
    "FundType",
    "RankType",
    "AnalystEpsRevision",
    "AnalystEpsTrend",
    "AnalystEstimate",
    "AnalystGradeChange",
    "AnalystGrowthEstimate",
    "AnalystPriceTarget",
    "AnalystRecommendation",
    "AsOfState",
    "BAR_INTERVALS",
    "BarGap",
    "BarRescale",
    "Base",
    "CalendarEarnings",
    "CalendarEconomic",
    "CalendarIpo",
    "CalendarSplits",
    "CapitalGain",
    "CompanyOfficer",
    "Dividend",
    "EarningsDate",
    "EarningsHistoryRow",
    "EstimateMetric",
    "FinancialFact",
    "FinancialPeriod",
    "FundMetric",
    "FundProfile",
    "FundSection",
    "FundTopHolding",
    "FundWeighting",
    "GAP_FETCH_FAILED",
    "GAP_RETENTION_EXPIRED",
    "HolderBreakdown",
    "HolderType",
    "INTRADAY_INTERVALS",
    "PERIODIC_INTERVALS",
    "InsiderActivity",
    "InsiderRosterHolder",
    "InsiderTransaction",
    "InstitutionalHolder",
    "IntradayScope",
    "ItemStatus",
    "METRIC_ENUM",
    "News",
    "NewsSymbol",
    "PeriodicBar",
    "PriceBar",
    "PriceHistory",
    "Proxy",
    "ProxyHealth",
    "ProxyScheme",
    "RunScope",
    "RunStatus",
    "SecFiling",
    "SecFilingExhibit",
    "SharesFull",
    "Split",
    "StatementFreq",
    "StatementKind",
    "Symbol",
    "SyncRun",
    "SyncRunItem",
    "V_ACTIONS_CREATE",
    "V_ACTIONS_DROP",
    "V_PRICE_BARS_REGULAR_CREATE",
    "V_PRICE_BARS_REGULAR_DROP",
    "WeightCategory",
    "history_metadata",
    "market_status",
    "market_status_history",
    "market_summary",
    "market_summary_history",
    "DAILY_INTERVAL",
    "READABLE_INTERVALS",
    "bars_table_for",
    "timescale_ddl",
    "ticker_calendar",
    "ticker_calendar_history",
    "ticker_fast_info",
    "ticker_fast_info_history",
    "ticker_info",
    "ticker_info_history",
]

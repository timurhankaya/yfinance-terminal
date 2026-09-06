"""Tum modeller. Import edilmesi Base.metadata'yi doldurur."""

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
    GAP_FETCH_FAILED,
    GAP_RETENTION_EXPIRED,
    INTRADAY_INTERVALS,
    BarGap,
    BarRescale,
    IntradayScope,
    PriceBar,
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

# FK TASIMAYAN ama sembol kapsamli tablolar. Turetme FK kenarlarina
# dayandigi icin bunlar kendiliginden BULUNAMAZ; liste elle tutulur.
#
# LISTE ARTIK BOS. `price_bars` MySQL'de partition'li oldugu icin FK
# tasiyamiyordu (ERROR 1506) ve tek elemandi; TimescaleDB hypertable'i
# referencing taraf olabildigi icin artik FK TASIYOR (PG S7.2) ve
# turetme onu kendiliginden buluyor. Elle listede birakilsaydi purge onu
# IKI KEZ silmeye calisirdi.
#
# Liste yapisi KORUNUYOR: ileride FK tasiyamayan sembol kapsamli bir
# tablo eklenirse (ornegin baska bir motor ozelligi yuzunden) tek
# degisiklik burasi olur ve test_registry.py'deki iki yonlu invaryant
# hatayi yakalar.
_FK_LESS_SYMBOL_TABLES: tuple[str, ...] = ()


def symbol_scoped_tables() -> list[str]:
    """symbols.symbol'a bagli tablolar, SILME sirasina gore.

    Cogu metadata'daki FK kenarlarindan turetilir: yeni bir sembol-kapsamli
    tablo eklendiginde `yfin symbols purge` kendiliginden onu da kapsar
    (aksi halde ON DELETE RESTRICT hatasi verirdi). FK tasiyamayan
    tablolar `_FK_LESS_SYMBOL_TABLES`ten gelir ve BASA konur.
    """
    names: list[str] = [n for n in _FK_LESS_SYMBOL_TABLES if n in Base.metadata.tables]
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
    "ScreenRun",
    "SearchList",
    "SearchQuote",
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
    "InsiderActivity",
    "InsiderRosterHolder",
    "InsiderTransaction",
    "InstitutionalHolder",
    "IntradayScope",
    "ItemStatus",
    "METRIC_ENUM",
    "News",
    "NewsSymbol",
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
    "timescale_ddl",
    "ticker_calendar",
    "ticker_calendar_history",
    "ticker_fast_info",
    "ticker_fast_info_history",
    "ticker_info",
    "ticker_info_history",
]

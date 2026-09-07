"""Field definitions: the single source of truth for typed columns.

The same list generates both the SQLAlchemy columns and the
normalization converter, removing any chance of them drifting apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal[
    "str16",
    "str32",
    "str64",
    "str128",
    "str255",
    "text",
    "dec",
    "big",
    "int",
    "ubig",
    "bool",
    "epoch_s",
    "epoch_ms",
    "dt",
]


@dataclass(frozen=True, slots=True)
class Field:
    source: str  # Key in the yfinance dict.
    column: str  # SQL column name.
    kind: Kind


def _f(source: str, column: str, kind: Kind) -> Field:
    return Field(source, column, kind)


# --- ticker_info / ticker_info_history ------------------------------------
# Scope chosen from the field intersection of 4 reference symbols. Any
# unmapped key stays in raw_json and triggers the unmapped-key warning.

INFO_FIELDS: tuple[Field, ...] = (
    # identity
    _f("quoteType", "quote_type", "str32"),
    _f("typeDisp", "type_disp", "str64"),
    _f("shortName", "short_name", "str128"),
    _f("longName", "long_name", "str255"),
    _f("displayName", "display_name", "str128"),
    _f("exchange", "exchange", "str32"),
    _f("fullExchangeName", "full_exchange_name", "str64"),
    _f("exchangeTimezoneName", "exchange_timezone_name", "str64"),
    _f("exchangeTimezoneShortName", "exchange_timezone_short_name", "str16"),
    _f("market", "market", "str32"),
    _f("region", "region", "str16"),
    _f("language", "language", "str16"),
    _f("marketState", "market_state", "str32"),
    _f("currency", "currency", "str32"),
    _f("financialCurrency", "financial_currency", "str32"),
    _f("quoteSourceName", "quote_source_name", "str64"),
    _f("messageBoardId", "message_board_id", "str64"),
    _f("priceHint", "price_hint", "int"),
    _f("sourceInterval", "source_interval", "int"),
    _f("exchangeDataDelayedBy", "exchange_data_delayed_by", "int"),
    _f("gmtOffSetMilliseconds", "gmt_offset_milliseconds", "int"),
    # classification
    _f("sector", "sector", "str64"),
    _f("sectorKey", "sector_key", "str64"),
    _f("sectorDisp", "sector_disp", "str64"),
    _f("industry", "industry", "str128"),
    _f("industryKey", "industry_key", "str128"),
    _f("industryDisp", "industry_disp", "str128"),
    _f("category", "category", "str64"),
    _f("fundFamily", "fund_family", "str64"),
    _f("legalType", "legal_type", "str64"),
    _f("recommendationKey", "recommendation_key", "str32"),
    _f("averageAnalystRating", "average_analyst_rating", "str32"),
    # address / free text (TEXT to keep within the row-size budget)
    _f("address1", "address1", "text"),
    _f("address2", "address2", "text"),
    _f("city", "city", "str64"),
    _f("state", "state", "str64"),
    _f("zip", "zip", "str16"),
    _f("country", "country", "str64"),
    _f("phone", "phone", "str32"),
    _f("fax", "fax", "str32"),
    _f("website", "website", "text"),
    _f("irWebsite", "ir_website", "text"),
    _f("longBusinessSummary", "long_business_summary", "text"),
    # price
    _f("regularMarketPrice", "regular_market_price", "dec"),
    _f("regularMarketOpen", "regular_market_open", "dec"),
    _f("regularMarketDayHigh", "regular_market_day_high", "dec"),
    _f("regularMarketDayLow", "regular_market_day_low", "dec"),
    _f("regularMarketPreviousClose", "regular_market_previous_close", "dec"),
    _f("regularMarketChange", "regular_market_change", "dec"),
    _f("regularMarketChangePercent", "regular_market_change_percent", "dec"),
    _f("regularMarketDayRange", "regular_market_day_range", "str64"),
    _f("previousClose", "previous_close", "dec"),
    _f("open", "open", "dec"),
    _f("dayHigh", "day_high", "dec"),
    _f("dayLow", "day_low", "dec"),
    _f("currentPrice", "current_price", "dec"),
    _f("bid", "bid", "dec"),
    _f("ask", "ask", "dec"),
    _f("bidSize", "bid_size", "ubig"),
    _f("askSize", "ask_size", "ubig"),
    _f("preMarketPrice", "pre_market_price", "dec"),
    _f("preMarketChange", "pre_market_change", "dec"),
    _f("preMarketChangePercent", "pre_market_change_percent", "dec"),
    # range / average
    _f("fiftyDayAverage", "fifty_day_average", "dec"),
    _f("twoHundredDayAverage", "two_hundred_day_average", "dec"),
    _f("fiftyTwoWeekHigh", "fifty_two_week_high", "dec"),
    _f("fiftyTwoWeekLow", "fifty_two_week_low", "dec"),
    _f("fiftyTwoWeekRange", "fifty_two_week_range", "str64"),
    _f("fiftyTwoWeekChangePercent", "fifty_two_week_change_percent", "dec"),
    _f("allTimeHigh", "all_time_high", "dec"),
    _f("allTimeLow", "all_time_low", "dec"),
    # volume
    _f("volume", "volume", "ubig"),
    _f("regularMarketVolume", "regular_market_volume", "ubig"),
    _f("averageVolume", "average_volume", "ubig"),
    _f("averageVolume10days", "average_volume_10days", "ubig"),
    _f("averageDailyVolume10Day", "average_daily_volume_10day", "ubig"),
    _f("averageDailyVolume3Month", "average_daily_volume_3month", "ubig"),
    # valuation (large values use DECIMAL(38,0))
    _f("marketCap", "market_cap", "big"),
    _f("nonDilutedMarketCap", "non_diluted_market_cap", "big"),
    _f("enterpriseValue", "enterprise_value", "big"),
    _f("totalRevenue", "total_revenue", "big"),
    _f("totalCash", "total_cash", "big"),
    _f("totalDebt", "total_debt", "big"),
    _f("ebitda", "ebitda", "big"),
    _f("grossProfits", "gross_profits", "big"),
    _f("freeCashflow", "free_cashflow", "big"),
    _f("operatingCashflow", "operating_cashflow", "big"),
    _f("netIncomeToCommon", "net_income_to_common", "big"),
    _f("totalAssets", "total_assets", "big"),
    _f("netAssets", "net_assets", "big"),
    _f("floatShares", "float_shares", "big"),
    _f("sharesOutstanding", "shares_outstanding", "big"),
    _f("impliedSharesOutstanding", "implied_shares_outstanding", "big"),
    _f("sharesShort", "shares_short", "big"),
    _f("sharesShortPriorMonth", "shares_short_prior_month", "big"),
    # ratios
    _f("trailingPE", "trailing_pe", "dec"),
    _f("forwardPE", "forward_pe", "dec"),
    _f("priceToBook", "price_to_book", "dec"),
    _f("priceToSalesTrailing12Months", "price_to_sales_trailing_12_months", "dec"),
    _f("pegRatio", "peg_ratio", "dec"),
    _f("trailingPegRatio", "trailing_peg_ratio", "dec"),
    _f("enterpriseToRevenue", "enterprise_to_revenue", "dec"),
    _f("enterpriseToEbitda", "enterprise_to_ebitda", "dec"),
    _f("bookValue", "book_value", "dec"),
    _f("trailingEps", "trailing_eps", "dec"),
    _f("forwardEps", "forward_eps", "dec"),
    _f("epsTrailingTwelveMonths", "eps_trailing_twelve_months", "dec"),
    _f("epsCurrentYear", "eps_current_year", "dec"),
    _f("epsForward", "eps_forward", "dec"),
    _f("priceEpsCurrentYear", "price_eps_current_year", "dec"),
    _f("revenuePerShare", "revenue_per_share", "dec"),
    _f("totalCashPerShare", "total_cash_per_share", "dec"),
    _f("beta", "beta", "dec"),
    _f("profitMargins", "profit_margins", "dec"),
    _f("grossMargins", "gross_margins", "dec"),
    _f("operatingMargins", "operating_margins", "dec"),
    _f("ebitdaMargins", "ebitda_margins", "dec"),
    _f("returnOnAssets", "return_on_assets", "dec"),
    _f("returnOnEquity", "return_on_equity", "dec"),
    _f("revenueGrowth", "revenue_growth", "dec"),
    _f("earningsGrowth", "earnings_growth", "dec"),
    _f("earningsQuarterlyGrowth", "earnings_quarterly_growth", "dec"),
    _f("currentRatio", "current_ratio", "dec"),
    _f("quickRatio", "quick_ratio", "dec"),
    _f("debtToEquity", "debt_to_equity", "dec"),
    _f("heldPercentInsiders", "held_percent_insiders", "dec"),
    _f("heldPercentInstitutions", "held_percent_institutions", "dec"),
    _f("shortRatio", "short_ratio", "dec"),
    _f("shortPercentOfFloat", "short_percent_of_float", "dec"),
    # dividend
    _f("dividendRate", "dividend_rate", "dec"),
    _f("dividendYield", "dividend_yield", "dec"),
    _f("trailingAnnualDividendRate", "trailing_annual_dividend_rate", "dec"),
    _f("trailingAnnualDividendYield", "trailing_annual_dividend_yield", "dec"),
    _f("fiveYearAvgDividendYield", "five_year_avg_dividend_yield", "dec"),
    _f("payoutRatio", "payout_ratio", "dec"),
    _f("lastDividendValue", "last_dividend_value", "dec"),
    _f("lastSplitFactor", "last_split_factor", "str32"),
    # analyst targets
    _f("targetHighPrice", "target_high_price", "dec"),
    _f("targetLowPrice", "target_low_price", "dec"),
    _f("targetMeanPrice", "target_mean_price", "dec"),
    _f("targetMedianPrice", "target_median_price", "dec"),
    _f("numberOfAnalystOpinions", "number_of_analyst_opinions", "int"),
    _f("recommendationMean", "recommendation_mean", "dec"),
    # fund
    _f("netExpenseRatio", "net_expense_ratio", "dec"),
    _f("ytdReturn", "ytd_return", "dec"),
    _f("threeYearAverageReturn", "three_year_average_return", "dec"),
    _f("fiveYearAverageReturn", "five_year_average_return", "dec"),
    _f("beta3Year", "beta_3_year", "dec"),
    _f("navPrice", "nav_price", "dec"),
    _f("yield", "fund_yield", "dec"),
    # crypto
    _f("fromCurrency", "from_currency", "str32"),
    _f("toCurrency", "to_currency", "str32"),
    _f("lastMarket", "last_market", "str64"),
    _f("circulatingSupply", "circulating_supply", "big"),
    _f("maxSupply", "max_supply", "big"),
    _f("totalSupply", "total_supply", "big"),
    _f("fullyDilutedValue", "fully_diluted_value", "big"),
    _f("volume24Hr", "volume_24hr", "big"),
    _f("volumeAllCurrencies", "volume_all_currencies", "big"),
    # company
    _f("fullTimeEmployees", "full_time_employees", "int"),
    # flags
    _f("tradeable", "tradeable", "bool"),
    _f("triggerable", "triggerable", "bool"),
    _f("cryptoTradeable", "crypto_tradeable", "bool"),
    _f("esgPopulated", "esg_populated", "bool"),
    _f("hasPrePostMarketData", "has_pre_post_market_data", "bool"),
    _f("isEarningsDateEstimate", "is_earnings_date_estimate", "bool"),
    # epoch fields (unit is never guessed)
    _f("firstTradeDateMilliseconds", "first_trade_date", "epoch_ms"),
    _f("regularMarketTime", "regular_market_time", "epoch_s"),
    _f("preMarketTime", "pre_market_time", "epoch_s"),
    _f("exDividendDate", "ex_dividend_date", "epoch_s"),
    _f("dividendDate", "dividend_date", "epoch_s"),
    _f("lastDividendDate", "last_dividend_date", "epoch_s"),
    _f("earningsTimestamp", "earnings_timestamp", "epoch_s"),
    _f("earningsTimestampStart", "earnings_timestamp_start", "epoch_s"),
    _f("earningsTimestampEnd", "earnings_timestamp_end", "epoch_s"),
    _f("earningsCallTimestampStart", "earnings_call_timestamp_start", "epoch_s"),
    _f("earningsCallTimestampEnd", "earnings_call_timestamp_end", "epoch_s"),
    _f("lastFiscalYearEnd", "last_fiscal_year_end", "epoch_s"),
    _f("nextFiscalYearEnd", "next_fiscal_year_end", "epoch_s"),
    _f("mostRecentQuarter", "most_recent_quarter", "epoch_s"),
    _f("lastSplitDate", "last_split_date", "epoch_s"),
    _f("governanceEpochDate", "governance_epoch_date", "epoch_s"),
    _f("compensationAsOfEpochDate", "compensation_as_of_epoch_date", "epoch_s"),
    _f("dateShortInterest", "date_short_interest", "epoch_s"),
    _f("sharesShortPreviousMonthDate", "shares_short_previous_month_date", "epoch_s"),
    _f("fundInceptionDate", "fund_inception_date", "epoch_s"),
    _f("startDate", "start_date", "epoch_s"),
)

# --- ticker_fast_info / _history ------------------------------------------
# Hardcoded 20 keys at the source (quote.py:_public_keys), identical
# across 5 markets. All nullable: marketCap/shares return None for ETFs,
# crypto, FX, and indices.

FAST_INFO_FIELDS: tuple[Field, ...] = (
    _f("currency", "currency", "str32"),
    _f("dayHigh", "day_high", "dec"),
    _f("dayLow", "day_low", "dec"),
    _f("exchange", "exchange", "str32"),
    _f("fiftyDayAverage", "fifty_day_average", "dec"),
    _f("lastPrice", "last_price", "dec"),
    _f("lastVolume", "last_volume", "ubig"),
    _f("marketCap", "market_cap", "big"),
    _f("open", "open", "dec"),
    _f("previousClose", "previous_close", "dec"),
    _f("quoteType", "quote_type", "str32"),
    _f("regularMarketPreviousClose", "regular_market_previous_close", "dec"),
    _f("shares", "shares", "big"),
    _f("tenDayAverageVolume", "ten_day_average_volume", "ubig"),
    _f("threeMonthAverageVolume", "three_month_average_volume", "ubig"),
    _f("timezone", "timezone", "str64"),
    _f("twoHundredDayAverage", "two_hundred_day_average", "dec"),
    _f("yearChange", "year_change", "dec"),
    _f("yearHigh", "year_high", "dec"),
    _f("yearLow", "year_low", "dec"),
)

# --- history_metadata -----------------------------------------------------
# Source has 30-32 keys; keys with a space or question mark, like
# 'YF repair?', are kept only in raw_json.

HISTORY_METADATA_FIELDS: tuple[Field, ...] = (
    _f("currency", "currency", "str32"),
    _f("exchangeName", "exchange_name", "str32"),
    _f("fullExchangeName", "full_exchange_name", "str64"),
    _f("instrumentType", "instrument_type", "str32"),
    # `timezone` is not an IANA name: measured, it returns DST-dependent
    # abbreviations like "EDT"/"TRT" that cannot be passed to ZoneInfo.
    # The IANA name is a separate field and is required for rescale to
    # align a split's day to local 00:00.
    _f("timezone", "timezone", "str64"),
    _f("exchangeTimezoneName", "exchange_timezone_name", "str64"),
    _f("gmtoffset", "gmt_offset", "int"),
    # yfinance already converts these two to a tz-aware Timestamp, not epoch.
    _f("firstTradeDate", "first_trade_date", "dt"),
    _f("regularMarketTime", "regular_market_time", "dt"),
    _f("regularMarketPrice", "regular_market_price", "dec"),
    _f("fiftyTwoWeekHigh", "fifty_two_week_high", "dec"),
    _f("fiftyTwoWeekLow", "fifty_two_week_low", "dec"),
    _f("priceHint", "price_hint", "int"),
    _f("dataGranularity", "data_granularity", "str16"),
    _f("range", "range", "str16"),
    _f("hasPrePostMarketData", "has_pre_post_market_data", "bool"),
    _f("shortName", "short_name", "str128"),
    _f("longName", "long_name", "str255"),
    _f("chartPreviousClose", "chart_previous_close", "dec"),
    _f("regularMarketDayHigh", "regular_market_day_high", "dec"),
    _f("regularMarketDayLow", "regular_market_day_low", "dec"),
    _f("regularMarketVolume", "regular_market_volume", "ubig"),
)

# Nested keys known to be left in raw_json, never promoted to a column.
INFO_NESTED_KEYS: frozenset[str] = frozenset(
    {"companyOfficers", "corporateActions", "executiveTeam"}
)

# --- screen_quotes ---------------------------------------------------------
# 75 of the measured 104 fields carry the same source key as INFO_FIELDS
# and inherit their column name + kind from there. Re-declaring them
# instead of inheriting would let the two tables drift apart, silently
# breaking the JOIN-free comparison between `ticker_info` and
# `screen_quotes`.
#
# Two of the remaining 29 do not become columns: `symbol` is the PK
# (defined separately on the table), and `corporateActions` is a list
# and stays in raw_json.
SCREENER_SHARED_SOURCES: frozenset[str] = frozenset(
    {
        "ask",
        "askSize",
        "averageAnalystRating",
        "averageDailyVolume10Day",
        "averageDailyVolume3Month",
        "bid",
        "bidSize",
        "bookValue",
        "cryptoTradeable",
        "currency",
        "displayName",
        "dividendDate",
        "dividendRate",
        "dividendYield",
        "earningsCallTimestampEnd",
        "earningsCallTimestampStart",
        "earningsTimestamp",
        "earningsTimestampEnd",
        "earningsTimestampStart",
        "epsCurrentYear",
        "epsForward",
        "epsTrailingTwelveMonths",
        "esgPopulated",
        "exchange",
        "exchangeDataDelayedBy",
        "exchangeTimezoneName",
        "exchangeTimezoneShortName",
        "fiftyDayAverage",
        "fiftyTwoWeekChangePercent",
        "fiftyTwoWeekHigh",
        "fiftyTwoWeekLow",
        "fiftyTwoWeekRange",
        "financialCurrency",
        "firstTradeDateMilliseconds",
        "forwardPE",
        "fullExchangeName",
        "gmtOffSetMilliseconds",
        "hasPrePostMarketData",
        "impliedSharesOutstanding",
        "isEarningsDateEstimate",
        "language",
        "longName",
        "market",
        "marketCap",
        "marketState",
        "messageBoardId",
        "netAssets",
        "netExpenseRatio",
        "priceEpsCurrentYear",
        "priceHint",
        "priceToBook",
        "quoteSourceName",
        "quoteType",
        "region",
        "regularMarketChange",
        "regularMarketChangePercent",
        "regularMarketDayHigh",
        "regularMarketDayLow",
        "regularMarketDayRange",
        "regularMarketOpen",
        "regularMarketPreviousClose",
        "regularMarketPrice",
        "regularMarketTime",
        "regularMarketVolume",
        "sharesOutstanding",
        "shortName",
        "sourceInterval",
        "tradeable",
        "trailingAnnualDividendRate",
        "trailingAnnualDividendYield",
        "trailingPE",
        "triggerable",
        "twoHundredDayAverage",
        "typeDisp",
        "ytdReturn",
    }
)

# Two keys that never become columns. Kept as a separate constant
# deliberately: `warn_unmapped` excludes them so it does not warn
# "unmapped" for these, which would otherwise be two false alarms on
# every run.
SCREENER_NON_COLUMN_SOURCES: frozenset[str] = frozenset({"symbol", "corporateActions"})

SCREENER_EXTRA_FIELDS: tuple[Field, ...] = (
    _f("fulldayPrice", "fullday_price", "dec"),
    _f("fulldayChange", "fullday_change", "dec"),
    _f("fulldayChangePercent", "fullday_change_percent", "dec"),
    _f("fiftyDayAverageChange", "fifty_day_average_change", "dec"),
    _f("fiftyDayAverageChangePercent", "fifty_day_average_change_percent", "dec"),
    _f("fiftyTwoWeekHighChange", "fifty_two_week_high_change", "dec"),
    _f("fiftyTwoWeekHighChangePercent", "fifty_two_week_high_change_percent", "dec"),
    _f("fiftyTwoWeekLowChange", "fifty_two_week_low_change", "dec"),
    _f("fiftyTwoWeekLowChangePercent", "fifty_two_week_low_change_percent", "dec"),
    _f("twoHundredDayAverageChange", "two_hundred_day_average_change", "dec"),
    _f("twoHundredDayAverageChangePercent", "two_hundred_day_average_change_percent", "dec"),
    # INFO_FIELDS has preMarket* but not postMarket* -- this surfaced
    _f("postMarketPrice", "post_market_price", "dec"),
    # once after-hours measurement was done. Column names still follow
    _f("postMarketChange", "post_market_change", "dec"),
    # the existing pre_market_* pattern symmetrically.
    _f("postMarketChangePercent", "post_market_change_percent", "dec"),
    _f("postMarketTime", "post_market_time", "epoch_s"),
    _f("peTTM", "pe_ttm", "dec"),
    _f("yieldTTM", "yield_ttm", "dec"),
    _f("trailingThreeMonthReturns", "trailing_three_month_returns", "dec"),
    _f("trailingThreeMonthNavReturns", "trailing_three_month_nav_returns", "dec"),
    _f("annualReturnNavY3", "annual_return_nav_y3", "dec"),
    _f("annualReturnNavY5", "annual_return_nav_y5", "dec"),
    _f("lastClosePriceToNNWCPerShare", "last_close_price_to_nnwc_per_share", "dec"),
    _f("lastCloseTevEbitLtm", "last_close_tev_ebit_ltm", "dec"),
    # Measured value set: HIGH, LOW.
    _f("customPriceAlertConfidence", "custom_price_alert_confidence", "str16"),
    # Measured leading whitespace (' LiveWire Group, Inc.'); nz.to_str trims it.
    _f("prevName", "prev_name", "str255"),
    # ISO date text, not epoch. `epoch_s` here would silently NULL it;
    _f("ipoExpectedDate", "ipo_expected_date", "dt"),
    # the `dt` kind accepts both text and epoch.
    _f("nameChangeDate", "name_change_date", "dt"),
)

# INFO_FIELDS order is preserved: column order matches between the two
# tables, so `SELECT *` output can be read side by side.
SCREENER_QUOTE_FIELDS: tuple[Field, ...] = (
    *(f for f in INFO_FIELDS if f.source in SCREENER_SHARED_SOURCES),
    *SCREENER_EXTRA_FIELDS,
)

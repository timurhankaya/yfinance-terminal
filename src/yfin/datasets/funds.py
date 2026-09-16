"""funds_data dataset.

Equity and bond funds carry different weighting keys, so those go to EAV.
A non-fund symbol triggers no request: the quote type is already in the ctx cache.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from yfinance.exceptions import YFDataException

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.asof_base import AsOfDataset, asof_produces
from yfin.datasets.base import NormalizedResult, SyncContext, mark_known_in
from yfin.datasets.common import key_value, note_unmapped, to_fact_value
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.payloads import FundsPayload
from yfin.datasets.registry import register
from yfin.datasets.symbols import fetch_fast_info, fetch_history_metadata
from yfin.ingest.client import call_optional
from yfin.models.funds import FundSection, WeightCategory
from yfin.storage.contracts import RowWriter, TableWrite, WriteStats

log = get_logger(__name__)

# `holding_rank` is CHECKed to 0..255 (models/funds.py); the limit is the
# column's, not the source's.
MAX_HOLDING_RANK = 255

PROFILE_TABLE = "fund_profile"
METRICS_TABLE = "fund_metrics"
WEIGHTINGS_TABLE = "fund_weightings"
HOLDINGS_TABLE = "fund_top_holdings"

FUND_QUOTE_TYPES = frozenset({"ETF", "MUTUALFUND"})
ITEM_KEY_LENGTH = 32
CATEGORY_AVERAGE = "Category Average"

# fund_operations index label -> (value column, category-average column)
OPERATIONS: dict[str, tuple[str, str]] = {
    "Annual Report Expense Ratio": ("expense_ratio", "expense_ratio_cat"),
    "Annual Holdings Turnover": ("holdings_turnover", "holdings_turnover_cat"),
    "Total Net Assets": ("total_net_assets", "total_net_assets_cat"),
}
ASSET_CLASSES: dict[str, str] = {
    "cashPosition": "cash_position",
    "stockPosition": "stock_position",
    "bondPosition": "bond_position",
    "preferredPosition": "preferred_position",
    "convertiblePosition": "convertible_position",
    "otherPosition": "other_position",
}
# equity/bond_holdings index label -> `fund_metrics.metric`. Cannot be
# derived from the label ('Price/Earnings' -> 'price_to_earnings'), hence a map.
EQUITY_METRICS: dict[str, str] = {
    "Price/Earnings": "price_to_earnings",
    "Price/Book": "price_to_book",
    "Price/Sales": "price_to_sales",
    "Price/Cashflow": "price_to_cashflow",
    "Median Market Cap": "median_market_cap",
    "3 Year Earnings Growth": "three_year_earnings_growth",
}
BOND_METRICS: dict[str, str] = {
    "Duration": "duration",
    "Maturity": "maturity",
    "Credit Quality": "credit_quality",
}

PROFILE_COLUMNS = (
    "quote_type",
    "category_name",
    "family",
    "legal_type",
    "description",
    *(column for pair in OPERATIONS.values() for column in pair),
    *ASSET_CLASSES.values(),
    "raw_json",
)


def _resolve_quote_type(ctx: SyncContext) -> str | None:
    """fast_info['quoteType'], else history_metadata['instrumentType'].

    The key is camelCase; `quote_type` silently returns None.
    """
    fast_info = fetch_fast_info(ctx)
    value = nz.as_mapping(fast_info).get("quoteType") if fast_info is not None else None
    if value is None:
        metadata = fetch_history_metadata(ctx)
        value = nz.as_mapping(metadata).get("instrumentType") if metadata is not None else None
    text = nz.to_str(value, max_len=32)
    return text.strip().upper() if text else None


def _read(funds: Any, name: str) -> Any:
    """Reads a field, accepting both `@property` and plain-method form.

    Upstream mixes the two (`quote_type` is a method), and either blind
    form breaks when they fix it.
    """
    value = getattr(funds, name)
    return value() if callable(value) else value


# Ten fields feeding eight sub-structures; the FIRST access triggers one request.
FUND_FIELDS = (
    "quote_type",
    "description",
    "fund_overview",
    "fund_operations",
    "asset_classes",
    "top_holdings",
    "equity_holdings",
    "bond_holdings",
    "bond_ratings",
    "sector_weightings",
)


def _collect(funds: Any) -> dict[str, Any]:
    return {name: _read(funds, name) for name in FUND_FIELDS}


def _frame_to_dict(frame: Any) -> Any:
    if not isinstance(frame, pd.DataFrame):
        return None
    return {str(col): {str(idx): value for idx, value in frame[col].items()} for col in frame}


def _own_column(frame: pd.DataFrame) -> Any:
    """Column 0; its NAME IS the symbol itself, so it is read BY POSITION."""
    return frame.iloc[:, 0]


def _slug(value: Any) -> str | None:
    text = nz.to_str(value, max_len=None)
    if text is None:
        return None
    return text.strip().lower().replace(" ", "_").replace("-", "_") or None


class FundsDataDataset(AsOfDataset[FundsPayload]):
    name = "funds_data"
    depends_on = ("symbols",)
    produces = asof_produces(PROFILE_TABLE, METRICS_TABLE, WEIGHTINGS_TABLE, HOLDINGS_TABLE)
    # `_profile_write` returns exactly one row whenever `normalize` returns
    # anything; the other three tables can be empty for a fund.
    gate_source_tables = (PROFILE_TABLE,)
    api = (
        ApiExposure(
            name="fund_profile",
            family=DataFamily.HOLDERS,
            table="fund_profile",
            sort_key=("as_of_date",),
            descending=True,
            description="Fund family, category and fee profile.",
        ),
        ApiExposure(
            name="fund_metrics",
            family=DataFamily.HOLDERS,
            table="fund_metrics",
            sort_key=("as_of_date", "section", "metric"),
            descending=True,
            filters=("section",),
            description="Fund performance and risk metrics.",
        ),
        ApiExposure(
            name="fund_weightings",
            family=DataFamily.HOLDERS,
            table="fund_weightings",
            sort_key=("as_of_date", "category", "item_key"),
            descending=True,
            filters=("category",),
            description="Sector, asset-class and bond-rating weightings.",
        ),
        ApiExposure(
            name="fund_top_holdings",
            family=DataFamily.HOLDERS,
            table="fund_top_holdings",
            sort_key=("as_of_date", "holding_symbol"),
            descending=True,
            description="The fund's largest positions.",
        ),
    )

    def fetch(self, ctx: SyncContext) -> FundsPayload:
        quote_type = _resolve_quote_type(ctx)
        if quote_type not in FUND_QUOTE_TYPES:
            # NO request is made; the cell becomes `empty`.
            return FundsPayload(data=None, fetched_at=ctx.fetched_at)
        try:
            data = call_optional(
                lambda: _collect(ctx.ticker.get_funds_data()), what=f"{self.name}:{ctx.symbol}"
            )
        except (YFDataException, KeyError) as exc:
            # Under hide_exceptions=False, the source raises a raw
            # KeyError('topHoldings'), NOT YFDataException (scrapers/funds.py).
            log.debug("no fund data", symbol=ctx.symbol, error=str(exc)[:80])
            return FundsPayload(data=None, fetched_at=ctx.fetched_at)
        return FundsPayload(data=data, fetched_at=ctx.fetched_at)

    def normalize(self, raw: FundsPayload, symbol: str) -> NormalizedResult:
        data = raw.data
        if data is None:
            return NormalizedResult()
        quote_type = nz.to_str(data.get("quote_type"), max_len=16)
        if not quote_type:
            # quote_type is NOT NULL; a partially populated object cannot be
            # trusted (for a non-fund symbol, `description` on a second access
            # returns the company summary instead).
            log.debug("funds data has no quote type", symbol=symbol)
            return NormalizedResult()

        as_of = raw.fetched_at.date()
        scope = {"symbol": symbol, "as_of_date": as_of}

        return NormalizedResult(
            writes=[
                self._profile_write(data, symbol, as_of, quote_type, raw.fetched_at),
                self._metrics_write(data, symbol, as_of, raw.fetched_at, scope),
                self._weightings_write(data, symbol, as_of, raw.fetched_at),
                self._holdings_write(data, symbol, as_of, raw.fetched_at, scope),
            ]
        )

    # --- per-table normalization -------------------------------------

    def _profile_write(
        self,
        data: dict[str, Any],
        symbol: str,
        as_of: Any,
        quote_type: str,
        fetched_at: Any,
    ) -> TableWrite:
        overview = data.get("fund_overview") or {}
        assets = data.get("asset_classes") or {}
        row: dict[str, Any] = {
            "symbol": symbol,
            "as_of_date": as_of,
            "quote_type": quote_type,
            "category_name": nz.to_str(overview.get("categoryName"), max_len=64),
            "family": nz.to_str(overview.get("family"), max_len=128),
            "legal_type": nz.to_str(overview.get("legalType"), max_len=64),
            "description": nz.to_str(data.get("description")),
            **{column: None for pair in OPERATIONS.values() for column in pair},
            **{
                column: nz.to_decimal(assets.get(source))
                for source, column in ASSET_CLASSES.items()
            },
            # The full body is kept because reducing to EAV can lose category
            # info; raw_json exists ONLY on this table.
            "raw_json": nz.canonical_json(
                {
                    key: (_frame_to_dict(value) if isinstance(value, pd.DataFrame) else value)
                    for key, value in data.items()
                }
            ),
            "fetched_at": fetched_at,
        }

        operations = data.get("fund_operations")
        if isinstance(operations, pd.DataFrame) and not operations.empty:
            own = _own_column(operations)
            average = (
                operations[CATEGORY_AVERAGE] if CATEGORY_AVERAGE in operations.columns else None
            )
            for position, label in enumerate(operations.index):
                mapping = OPERATIONS.get(str(label))
                if mapping is None:
                    note_unmapped(self.name, [str(label)], symbol=symbol)
                    continue
                value_column, average_column = mapping
                row[value_column] = nz.to_decimal(own.iloc[position])
                if average is not None:
                    row[average_column] = nz.to_decimal(average.iloc[position])

        return TableWrite(
            table=PROFILE_TABLE,
            rows=[row],
            key_columns=("symbol", "as_of_date"),
            update_columns=(*PROFILE_COLUMNS, "fetched_at"),
        )

    def _metrics_write(
        self,
        data: dict[str, Any],
        symbol: str,
        as_of: Any,
        fetched_at: Any,
        scope: dict[str, Any],
    ) -> TableWrite:
        rows: list[dict[str, Any]] = []
        sections = (
            (FundSection.EQUITY, data.get("equity_holdings"), EQUITY_METRICS),
            (FundSection.BOND, data.get("bond_holdings"), BOND_METRICS),
        )
        for section, frame, metrics in sections:
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                continue
            own = _own_column(frame)
            average = frame[CATEGORY_AVERAGE] if CATEGORY_AVERAGE in frame.columns else None
            for position, label in enumerate(frame.index):
                metric = metrics.get(str(label)) or _slug(label)
                if metric is None or len(metric) > ITEM_KEY_LENGTH:
                    log.debug(
                        "unusable fund metric label",
                        symbol=symbol,
                        section=section.value,
                        label=str(label),
                    )
                    continue
                rows.append(
                    {
                        "symbol": symbol,
                        "as_of_date": as_of,
                        # `section` IS PART OF THE PK: without it, the same
                        # metric name appearing in both sections would raise a
                        # uniqueness violation (23505).
                        "section": section.value,
                        "metric": metric,
                        "value": to_fact_value(own.iloc[position]),
                        "category_average": (
                            None if average is None else to_fact_value(average.iloc[position])
                        ),
                        "fetched_at": fetched_at,
                    }
                )

        return TableWrite(
            table=METRICS_TABLE,
            rows=rows,
            key_columns=("symbol", "as_of_date", "section", "metric"),
            update_columns=("value", "category_average", "fetched_at"),
            mode="replace_scope",
            scope_columns=("symbol", "as_of_date"),
            scope_values=(scope,),
        )

    def _weightings_write(
        self, data: dict[str, Any], symbol: str, as_of: Any, fetched_at: Any
    ) -> TableWrite:
        rows: list[dict[str, Any]] = []
        for category, payload in (
            (WeightCategory.SECTOR, data.get("sector_weightings")),
            (WeightCategory.BOND_RATING, data.get("bond_ratings")),
        ):
            for source, value in (payload or {}).items():
                item_key = key_value(
                    _slug(source),
                    ITEM_KEY_LENGTH,
                    field="item_key",
                    dataset=self.name,
                    symbol=symbol,
                )
                weight = nz.to_decimal(value)
                if item_key is None or weight is None:
                    # weight is NOT NULL.
                    continue
                rows.append(
                    {
                        "symbol": symbol,
                        "as_of_date": as_of,
                        "category": category.value,
                        "item_key": item_key,
                        "weight": weight,
                        "fetched_at": fetched_at,
                    }
                )

        return TableWrite(
            table=WEIGHTINGS_TABLE,
            rows=rows,
            key_columns=("symbol", "as_of_date", "category", "item_key"),
            update_columns=("weight", "fetched_at"),
            mode="replace_scope",
            # Scope ALSO includes category: a bond fund never sends sector
            # keys, and an equity fund's bond_rating is a single row.
            scope_columns=("symbol", "as_of_date", "category"),
            scope_values=tuple(
                {"symbol": symbol, "as_of_date": as_of, "category": category.value}
                for category in WeightCategory
            ),
        )

    def _holdings_write(
        self,
        data: dict[str, Any],
        symbol: str,
        as_of: Any,
        fetched_at: Any,
        scope: dict[str, Any],
    ) -> TableWrite:
        rows: dict[str, dict[str, Any]] = {}
        frame = data.get("top_holdings")
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            for rank, (index, record) in enumerate(frame.iterrows()):
                if rank > MAX_HOLDING_RANK:
                    # `holding_rank` is CHECKed to 0..255; a CHECK violation
                    # would roll back all of the symbol's datasets, so extra
                    # rows are dropped and the cell stays `ok`.
                    log.debug(
                        "top_holdings rank over the limit",
                        dataset=self.name,
                        symbol=symbol,
                        limit=MAX_HOLDING_RANK,
                        rows=len(frame),
                    )
                    break
                holding = key_value(
                    nz.normalize_symbol(str(index)),
                    32,
                    field="holding_symbol",
                    dataset=self.name,
                    symbol=symbol,
                )
                if holding is None:
                    continue
                rows[holding] = {
                    "symbol": symbol,
                    "as_of_date": as_of,
                    "holding_symbol": holding,
                    "holding_name": nz.to_str(record.get("Name"), max_len=128),
                    "holding_percent": nz.to_decimal(record.get("Holding Percent")),
                    # Not `rank`: a reserved word on some engines.
                    "holding_rank": rank,
                    # Filled from the DB during upsert.
                    "is_known": False,
                    "fetched_at": fetched_at,
                }

        return TableWrite(
            table=HOLDINGS_TABLE,
            rows=list(rows.values()),
            key_columns=("symbol", "as_of_date", "holding_symbol"),
            update_columns=(
                "holding_name",
                "holding_percent",
                "holding_rank",
                "is_known",
                "fetched_at",
            ),
            mode="replace_scope",
            scope_columns=("symbol", "as_of_date"),
            scope_values=(scope,),
        )

    # --- write --------------------------------------------------------

    def upsert(
        self, writer: RowWriter, result: NormalizedResult, *, full_refresh: bool = False
    ) -> WriteStats:
        """`is_known` is filled from the DB, THEN the as-of gate runs.

        The flag enters the hash body so a universe change reopens the gate.
        """
        # holding_symbol has NO FK: the source sends symbols outside the
        # universe, and an FK would roll back all of the fund's data.
        marked = mark_known_in(
            writer,
            result,
            select=lambda write: write.table == HOLDINGS_TABLE and bool(write.rows),
            column="holding_symbol",
        )
        return super().upsert(writer, marked, full_refresh=full_refresh)


register(FundsDataDataset())
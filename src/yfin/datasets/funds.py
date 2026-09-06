"""funds_data dataset'i (AH S6.3, S5.3).

HIBRIT SEMA, olcumle zorunlu: hisse fonunda 11 sektor + 1 rating, tahvil
fonunda 0 sektor + 9 rating (BND, TLT, AGG). Sabit kolon seti iki fon tipini
birden tasiyamaz, bu yuzden degisken anahtarli yuzdeler EAV'a
(`fund_weightings`), sabit olculen alanlar tipli kolona gider.
`asset_classes` EAV'a GIRMEZ: alti anahtari 10 fonun 10'unda da sabit.

ON KONTROL: fon olmayan sembolde HIC ISTEK YAPILMAZ. `fast_info['quoteType']`
(yoksa `history_metadata['instrumentType']`) okunur; ikisi de bootstrap
`symbols` dataset'i tarafindan ZATEN cekilip ctx onbellegine konmustur ve 12
sembolde birebir ayni olculdu -- ayrica ayni chart istegiyle beslendikleri
icin ek maliyetleri yoktur. Bu yuzden `depends_on`'a `history_metadata`
EKLENMEZ: eklenseydi `--datasets funds` calistirmasi history_metadata
TABLOSUNA da yazar ve fazladan denetim satiri uretirdi.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from yfinance.exceptions import YFDataException

from yfin import normalize as nz
from yfin.client import call_optional
from yfin.datasets.asof_base import AsOfDataset, asof_produces
from yfin.datasets.base import NormalizedResult, SyncContext, TableWrite, WriteStats
from yfin.datasets.common import key_value, to_fact_value
from yfin.datasets.payloads import FundsPayload
from yfin.datasets.registry import register
from yfin.datasets.symbols import fetch_fast_info, fetch_history_metadata
from yfin.logging_setup import get_logger
from yfin.models.funds import FundSection, WeightCategory
from yfin.persistence import RowWriter

log = get_logger(__name__)

# `holding_rank` TINYINT UNSIGNED tasir (models/funds.py). Olculen en buyuk
# top_holdings 10 satir; sinir kolonun kendisidir, kaynagin degil.
MAX_HOLDING_RANK = 255

PROFILE_TABLE = "fund_profile"
METRICS_TABLE = "fund_metrics"
WEIGHTINGS_TABLE = "fund_weightings"
HOLDINGS_TABLE = "fund_top_holdings"

FUND_QUOTE_TYPES = frozenset({"ETF", "MUTUALFUND"})
ITEM_KEY_LENGTH = 32
CATEGORY_AVERAGE = "Category Average"

# fund_operations index etiketi -> (deger kolonu, kategori ortalamasi kolonu)
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
# equity/bond_holdings index etiketi -> `fund_metrics.metric`. Etiketten
# turetilemez ('Price/Earnings' -> 'price_to_earnings'), bu yuzden harita.
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
    """fast_info['quoteType'], yoksa history_metadata['instrumentType'].

    Anahtar ADI camelCase'tir: `fi.get('quote_type')` her sembolde None
    doner ve on kontrol sessizce her sembol icin fon istegi yapardi.
    """
    fast_info = fetch_fast_info(ctx)
    value = nz.as_mapping(fast_info).get("quoteType") if fast_info is not None else None
    if value is None:
        metadata = fetch_history_metadata(ctx)
        value = nz.as_mapping(metadata).get("instrumentType") if metadata is not None else None
    text = nz.to_str(value, max_len=32)
    return text.strip().upper() if text else None


def _read(funds: Any, name: str) -> Any:
    """Alani okur; hem `@property` hem duz metot bicimini kabul eder.

    CANLI OLCUM (2026-09-04, yfinance 1.7.0): on alanin DOKUZU `@property`
    tasiyor, `quote_type` TASIMIYOR (`scrapers/funds.py:47`) -- yani duz
    erisim bir `bound method` dondurur. Kor bir `funds.quote_type` NOT NULL
    kolona "<bound method ...>" yazardi; kor bir `funds.quote_type()` ise
    ust-akis bu tutarsizligi duzelttigi gun `TypeError` verirdi. Ikisini de
    kabul etmek tek dayanikli okuma bicimidir.
    """
    value = getattr(funds, name)
    return value() if callable(value) else value


# Sekiz alt yapiyi besleyen on alan; ILK erisim tek istegi tetikler.
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
    """0. kolon; ADI SEMBOLUN KENDISIDIR, bu yuzden KONUMDAN okunur."""
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

    def fetch(self, ctx: SyncContext) -> FundsPayload:
        quote_type = _resolve_quote_type(ctx)
        if quote_type not in FUND_QUOTE_TYPES:
            # HIC ISTEK YAPILMAZ; hucre `empty` olur.
            return FundsPayload(data=None, fetched_at=ctx.fetched_at)
        try:
            data = call_optional(
                lambda: _collect(ctx.ticker.get_funds_data()), what=f"{self.name}:{ctx.symbol}"
            )
        except (YFDataException, KeyError) as exc:
            # hide_exceptions=False altinda kaynak YFDataException DEGIL ham
            # KeyError('topHoldings') firlatiyor (scrapers/funds.py:190-194).
            log.info("fon verisi yok", symbol=ctx.symbol, error=str(exc)[:80])
            return FundsPayload(data=None, fetched_at=ctx.fetched_at)
        return FundsPayload(data=data, fetched_at=ctx.fetched_at)

    def normalize(self, raw: FundsPayload, symbol: str) -> NormalizedResult:
        data = raw.data
        if data is None:
            return NormalizedResult()
        quote_type = nz.to_str(data.get("quote_type"), max_len=16)
        if not quote_type:
            # quote_type NOT NULL; kismen dolu nesneye guvenilmez (fon
            # olmayan sembolde `description` IKINCI erisimde sirket ozetini
            # donduruyor -- AAPL'de 1825 karakter olculdu).
            log.warning("funds data has no quote type", symbol=symbol)
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

    # --- tablo basina normalizasyon --------------------------------------

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
            # EAV'a indirgenirken kategori bilgisi kaybolabilecegi icin
            # govde korunur (AH S5.7); yalnizca BU tabloda raw_json vardir.
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
                    log.warning(
                        "unmapped keys", dataset=self.name, symbol=symbol, keys=[str(label)]
                    )
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
                    log.warning(
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
                        # `section` PK'DADIR: disarida birakilsaydi ayni
                        # metric adi iki bolumde geldiginde ERROR 1062.
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
                    # weight NOT NULL; 10 fonun hepsinde dolu olculdu
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
            # Kapsam kategoriyi DE icerir: tahvil fonunda sektor anahtari
            # hic gelmez, hisse fonunda bond_rating tek satirdir.
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
                    # `holding_rank` TINYINT UNSIGNED (models/funds.py).
                    # STRICT sql_mode'da 256. satir ERROR 1264 verir ve
                    # `_persist_with_retry` DataError'da yeniden denemez:
                    # SEMBOLUN TUM dataset'leri rollback olurdu. Kolonu
                    # genisletmek migration ister; o gune kadar fazla satir
                    # WARNING ile dusurulur ve hucre `ok` kalir.
                    log.warning(
                        "top_holdings rank sinirini asti",
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
                    # Ad `rank` OLAMAZ: MySQL 8'de ayrilmis sozcuk.
                    "holding_rank": rank,
                    # upsert sirasinda DB'den doldurulur
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

    # --- yazma ------------------------------------------------------------

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        """`is_known` DB'den doldurulur, SONRA as-of kapisi calisir.

        Sira baglayicidir: bayrak hash govdesine girer, boylece evren
        degistiginde (bilinmeyen bir holding sembolu `symbols`'a eklendiginde)
        kapi acilir ve satirlar guncellenir.
        """
        writes = [
            _mark_known(writer, write) if write.table == HOLDINGS_TABLE and write.rows else write
            for write in result.writes
        ]
        return super().upsert(
            writer, NormalizedResult(writes=writes, skipped=dict(result.skipped))
        )


def _mark_known(writer: RowWriter, write: TableWrite) -> TableWrite:
    """holding_symbol'de FK YOKTUR (AH S5.5): kaynakta evren disi semboller
    geliyor (BRK-B, 2330.TW, 005930.KQ, 0700.HK ve FON sembolleri VRTPX,
    BISXX). FK olsaydi sembol basina tek transaction geregi FONUN TUM VERISI
    rollback olurdu -- news_symbols ile birebir ayni gerekce."""
    candidates = {row["holding_symbol"] for row in write.rows}
    known = writer.known_symbols(candidates)
    rows = [{**row, "is_known": row["holding_symbol"] in known} for row in write.rows]
    return TableWrite(
        table=write.table,
        rows=rows,
        key_columns=write.key_columns,
        update_columns=write.update_columns,
        mode=write.mode,
        scope_columns=write.scope_columns,
        scope_values=write.scope_values,
    )


register(FundsDataDataset())
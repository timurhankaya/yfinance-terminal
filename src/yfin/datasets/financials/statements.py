"""Gelir tablosu / bilanco / nakit akisi dataset'leri (S6.5).

Sekiz kayit tek bir `StatementDataset` sinifinin parametreli ornegidir.
Ayri kayit olmalarinin nedeni HATA IZOLASYONU: her (tablo, frekans) ayri bir
HTTP istegidir, biri patladiginda digerleri yazilmalidir.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from yfin import normalize as nz
from yfin.client import call_optional, call_yahoo
from yfin.datasets.base import NormalizedResult, SyncContext, TableWrite
from yfin.datasets.common import to_fact_value
from yfin.datasets.hash_gated import HashGatedDataset
from yfin.datasets.payloads import StatementPayload
from yfin.datasets.registry import register
from yfin.logging_setup import get_logger
from yfin.models.financials import (
    API_FREQ,
    ITEM_KEY_LENGTH,
    StatementFreq,
    StatementKind,
)

log = get_logger(__name__)

GATE_KEY = ("symbol", "statement", "freq", "period_end")
PERIOD_UPDATE_COLUMNS = (
    "currency",
    "item_count",
    "raw_json",
    "content_hash",
    "fetched_at",
)


def _api_getter(ticker: Any, statement: StatementKind) -> Any:
    return {
        StatementKind.INCOME: ticker.get_income_stmt,
        StatementKind.BALANCE_SHEET: ticker.get_balance_sheet,
        StatementKind.CASH_FLOW: ticker.get_cashflow,
    }[statement]


def financial_currency(ctx: SyncContext) -> str | None:
    """info.financialCurrency; BEST-EFFORT. `valuation` dataset'i de kullanir.

    THYAO.IS tablolari USD, fiyatlari TRY: para birimi saklanmazsa veri
    yanlis okunur. Ancak `currency` NULL kabul eden ikincil bir alandir;
    info cagrisi (uc wire istegi) patlarsa statement hucresi DUSMEZ.
    """
    try:
        info = ctx.cached(
            "info", lambda: call_yahoo(ctx.ticker.get_info, what=f"info:{ctx.symbol}")
        )
    except Exception as exc:  # noqa: BLE001 - ikincil alan, hucreyi dusurmez
        log.warning("financialCurrency unavailable", symbol=ctx.symbol, error=str(exc))
        return None
    if not isinstance(info, dict):
        return None
    return nz.to_str(info.get("financialCurrency"), max_len=8)


class StatementDataset(HashGatedDataset[StatementPayload]):
    produces = ("financial_periods", "financial_facts")
    depends_on = ("symbols",)
    gate_table = "financial_periods"
    child_table = "financial_facts"
    gate_key_columns = GATE_KEY

    def __init__(self, name: str, statement: StatementKind, freq: StatementFreq) -> None:
        self.name = name
        self.statement = statement
        self.freq = freq

    def fetch(self, ctx: SyncContext) -> StatementPayload:
        api_freq = API_FREQ[self.freq]
        # yfinance her (tablo, freq) icin ayri HTTP istegi yapar ve sonucu
        # Ticker ornegi icinde onbellege alir; ctx.cached ayni turda ikinci
        # istegi de onler.
        key = f"stmt:{self.statement.value}:{api_freq}"
        # Sirket olmayan sembolde Yahoo 404 doner, trailing uclarinda ise
        # yfinance bos cerceveyi .iloc ile okuyup IndexError firlatir;
        # ikisi de `empty`tir, `failed` degil (S8.2).
        frame = ctx.cached(
            key,
            lambda: call_optional(
                lambda: _api_getter(ctx.ticker, self.statement)(pretty=False, freq=api_freq),
                what=f"{self.name}:{ctx.symbol}",
            ),
        )
        return StatementPayload(
            frame=frame, currency=financial_currency(ctx), fetched_at=ctx.fetched_at
        )

    def normalize(self, raw: StatementPayload, symbol: str) -> NormalizedResult:
        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)

        period_rows: list[dict[str, Any]] = []
        fact_rows: list[dict[str, Any]] = []

        for column in frame.columns:
            period_end = nz.to_local_date(column)
            if period_end is None:
                log.warning(
                    "statement period has no date",
                    symbol=symbol,
                    dataset=self.name,
                    column=str(column),
                )
                continue

            items: dict[str, Any] = {}
            written = 0
            for label, value in frame[column].items():
                item_key = str(label).strip()
                if not item_key:
                    continue
                # NaN -> raw_json'da null olarak durur, tabloya satir YAZILMAZ:
                # "kalem o donemde yok" bilgisi satirin yoklugudur
                items[item_key] = None if nz.is_missing(value) else float(value)
                if len(item_key) > ITEM_KEY_LENGTH:
                    # Etiket evreni const.fundamentals_keys ile kapali ve max
                    # 60 karakter; bu yol kutuphane yukseltmelerine karsi
                    # emniyet valfidir. Hucre `ok` kalir, veri raw_json'da.
                    log.warning(
                        "item_key too long",
                        symbol=symbol,
                        dataset=self.name,
                        item_key=item_key,
                        length=len(item_key),
                    )
                    continue
                decimal_value = to_fact_value(value)
                if decimal_value is None:
                    continue
                fact_rows.append(
                    {
                        "symbol": symbol,
                        "statement": self.statement,
                        "freq": self.freq,
                        "period_end": period_end,
                        "item_key": item_key,
                        "value": decimal_value,
                    }
                )
                written += 1

            canonical = nz.canonical_json(items)
            period_rows.append(
                {
                    "symbol": symbol,
                    "statement": self.statement,
                    "freq": self.freq,
                    "period_end": period_end,
                    "currency": raw.currency,
                    "item_count": written,
                    "raw_json": canonical,
                    "content_hash": nz.content_hash(canonical=canonical),
                    "fetched_at": raw.fetched_at,
                }
            )

        if not period_rows:
            return NormalizedResult()

        return NormalizedResult(
            writes=[
                TableWrite(
                    table="financial_periods",
                    rows=period_rows,
                    key_columns=GATE_KEY,
                    update_columns=PERIOD_UPDATE_COLUMNS,
                ),
                TableWrite(
                    table="financial_facts",
                    rows=fact_rows,
                    key_columns=(*GATE_KEY, "item_key"),
                    update_columns=("value",),
                    mode="replace_scope",
                    scope_columns=GATE_KEY,
                ),
            ]
        )


_SPECS: tuple[tuple[str, StatementKind, StatementFreq], ...] = (
    ("income_stmt", StatementKind.INCOME, StatementFreq.ANNUAL),
    ("quarterly_income_stmt", StatementKind.INCOME, StatementFreq.QUARTERLY),
    ("ttm_income_stmt", StatementKind.INCOME, StatementFreq.TTM),
    ("balance_sheet", StatementKind.BALANCE_SHEET, StatementFreq.ANNUAL),
    ("quarterly_balance_sheet", StatementKind.BALANCE_SHEET, StatementFreq.QUARTERLY),
    # ttm_balance_sheet YOKTUR: freq="trailing" bilancoda
    # ValueError: Illegal argument firlatir (fundamentals.py:80-82)
    ("cashflow", StatementKind.CASH_FLOW, StatementFreq.ANNUAL),
    ("quarterly_cashflow", StatementKind.CASH_FLOW, StatementFreq.QUARTERLY),
    ("ttm_cashflow", StatementKind.CASH_FLOW, StatementFreq.TTM),
)

for _name, _statement, _freq in _SPECS:
    register(StatementDataset(_name, _statement, _freq))

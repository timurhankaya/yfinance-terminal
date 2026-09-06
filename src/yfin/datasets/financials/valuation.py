"""Degerleme olcutleri dataset'leri (`get_valuation_measures`).

`get_valuation_measures` cercevesi finansal tablolarla AYNI sekildedir:
index kalem etiketi, kolonlar donem. Bu yuzden ne yeni tablo ne yeni
normalize gerekir; `StatementDataset` `statement='valuation'` ile yeniden
kullanilir ve tek fark kolon etiketlerinin ONCE tarihe cevrilmesidir.

Kaynak (yfinance 1.7.0 `scrapers/quote.py:739-830`) veriyi statement'larla
ayni fundamentals-timeseries ucundan alir; degerler ham float'tir (eski
key-statistics kazimasindaki '3.76T' bicimli dizeler DEGIL).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

import pandas as pd

from yfin import normalize as nz
from yfin.client import call_optional, call_yahoo
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.datasets.financials.statements import StatementDataset
from yfin.datasets.payloads import StatementPayload
from yfin.datasets.registry import register
from yfin.logging_setup import get_logger
from yfin.models.financials import API_FREQ, StatementFreq, StatementKind

log = get_logger(__name__)

# Kaynagin donem disi tek kolonu. Kolon etiketlerini kaynak
# `f"{d.month}/{d.day}/{d.year}"` ile uretir (quote.py:815).
CURRENT_COLUMN = "Current"
COLUMN_FORMAT = "%m/%d/%Y"


def period_columns(frame: pd.DataFrame, *, symbol: str, dataset: str) -> pd.DataFrame:
    """'M/D/YYYY' kolonlarini donem sonu damgasina cevirir, digerlerini atar.

    `Current` DUSURULUR: donem sonu tarihi yoktur (PK bileseni bos kalirdi)
    ve degeri cekim anindaki fiyata baglidir -- kalici arsivde bayatlayan
    bir sutun, price-history tarafinda `adj_close`in dislanma gerekcesiyle
    ayni sinifta. Guncel piyasa degeri zaten `info.marketCap` ile gelir.

    Cevrim `pd.Timestamp`e BIRAKILMAZ: '1/2/2026' etiketinde ay/gun sirasi
    pandas'in varsayimina kalirdi. Kaynak bicimi bilindigi icin acikca
    verilir; cozulemeyen etiket WARNING ile dusurulur (kutuphane
    yukseltmelerine karsi emniyet valfi).
    """
    renamed: dict[Any, pd.Timestamp] = {}
    for column in frame.columns:
        label = str(column)
        if label == CURRENT_COLUMN:
            continue
        try:
            period_end = datetime.strptime(label, COLUMN_FORMAT).date()
        except ValueError:
            log.warning(
                "valuation column is not a period",
                symbol=symbol,
                dataset=dataset,
                column=label,
            )
            continue
        renamed[column] = pd.Timestamp(period_end)
    if not renamed:
        return pd.DataFrame()
    return frame[list(renamed)].rename(columns=renamed)


def quote_currency(ctx: SyncContext) -> str | None:
    """info.currency -- KOTASYON para birimi; BEST-EFFORT.

    `statements.financial_currency` (info.financialCurrency) BURADA YANLIS
    OLURDU. Olcum (THYAO.IS): financialCurrency=USD, currency=TRY ve
    valuation 'Market Cap' = 4,14e11 -- `info.marketCap` (4,08e11, TRY) ile
    ayni mertebede, USD karsiliginin ~30 kati. Yani degerleme olcutleri
    borsanin KOTASYON para birimindedir, raporlama para biriminde DEGIL.
    Oranlar (P/E, P/S, PEG) zaten birimsizdir; kolonun anlami buradaki iki
    parasal olcut (Market Cap, Enterprise Value) icindir.

    `info` onbellegi statement'larla PAYLASILIR: ayni ctx icinde ikinci bir
    istek dogurmaz.
    """
    try:
        info = ctx.cached(
            "info", lambda: call_yahoo(ctx.ticker.get_info, what=f"info:{ctx.symbol}")
        )
    except Exception as exc:  # noqa: BLE001 - ikincil alan, hucreyi dusurmez
        log.warning("quote currency unavailable", symbol=ctx.symbol, error=str(exc))
        return None
    if not isinstance(info, dict):
        return None
    return nz.to_str(info.get("currency"), max_len=8)


class ValuationDataset(StatementDataset):
    """`statement='valuation'` olan StatementDataset.

    `produces`, `gate_table`, `gate_key_columns` ve `normalize`in govdesi
    tabandan gelir; hash kapisi da aynen isler.
    """

    def __init__(self, name: str, freq: StatementFreq) -> None:
        super().__init__(name, StatementKind.VALUATION, freq)

    def fetch(self, ctx: SyncContext) -> StatementPayload:
        api_freq = API_FREQ[self.freq]
        # `periods=None`: varsayilan 5, cerceveyi ISTEMCIDE kirpar
        # (quote.py:640-644) -- tum gecmis ZATEN ayni istekle gelir, kirpmak
        # bedava veriyi atmak olurdu. Onbellek anahtari statement'lardan
        # ayridir; ayni Ticker'da iki farkli uc bulunur.
        frame = ctx.cached(
            f"valuation:{api_freq}",
            lambda: call_optional(
                lambda: ctx.ticker.get_valuation_measures(freq=api_freq, periods=None),
                what=f"{self.name}:{ctx.symbol}",
            ),
        )
        return StatementPayload(
            frame=frame, currency=quote_currency(ctx), fetched_at=ctx.fetched_at
        )

    def normalize(self, raw: StatementPayload, symbol: str) -> NormalizedResult:
        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)
        dated = period_columns(frame, symbol=symbol, dataset=self.name)
        if dated.empty:
            # Yalnizca 'Current' geldi: yazilacak donem yok, hata da yok
            return NormalizedResult()
        return super().normalize(replace(raw, frame=dated), symbol)


# 'trailing' KAYIT DEGILDIR. Olcum (AAPL, freq='trailing'): 13 kolon ve
# tarihleri DUZENSIZ (9/2, 9/1, 8/27, 8/11, 8/10, 7/31/2026, 10/3/2025);
# ustelik ayni kolonda olcutlerin bir kismi NaN -- Market Cap 9/2'de,
# Trailing P/E 9/1'de dolu. Bunlar donem sonu DEGIL anlik gozlem
# damgalaridir: her kosu yeni `period_end` satirlari uretir ve EAV'nin
# (sembol, tablo, frekans, donem) tanesini anlamsizlastirirdi.
# 'monthly' de KAYIT DEGILDIR: StatementFreq'e yeni bir uye eklemek iki
# tabloda PAYLASILAN native ENUM'u genisletir; ihtiyac dogarsa MONTHLY
# eklenip bu spec listesine bir satir yeter.
_SPECS: tuple[tuple[str, StatementFreq], ...] = (
    ("valuation_measures", StatementFreq.ANNUAL),
    ("quarterly_valuation_measures", StatementFreq.QUARTERLY),
)

for _name, _freq in _SPECS:
    register(ValuationDataset(_name, _freq))

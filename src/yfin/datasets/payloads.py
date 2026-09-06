"""fetch -> normalize arasindaki tipli sozlesmeler.

Bu tipler olmadan iki adim arasinda ad-hoc sozlukler dolasir
({"info": ..., "fetched_at": ...}) ve `mypy --strict` sinirda hicbir sey
dogrulayamaz. `Dataset[RawT]` generic'i sayesinde fetch'in dondurdugu tip
ile normalize'in bekledigi tip artik derleme zamaninda eslesir.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd


@dataclass(frozen=True, slots=True)
class SymbolsPayload:
    """fast_info + history_metadata; ikisi de ctx.cached uzerinden gelir."""

    fast_info: Any
    metadata: Any
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class MetadataPayload:
    metadata: Any
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class InfoPayload:
    info: Mapping[str, Any] | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class FastInfoPayload:
    fast_info: Any
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class StatementPayload:
    """Finansal tablo: index=kalem etiketi, kolonlar=donem sonu.

    Sirket olmayan sembolde (0,0) bos DataFrame doner, exception degil.
    currency info.financialCurrency'dir ve BEST-EFFORT gelir.
    """

    frame: pd.DataFrame | None
    currency: str | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class CalendarPayload:
    """get_calendar(): 9 anahtarlik dict; sembole gore eksik anahtar olur."""

    calendar: Mapping[str, Any] | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class EarningsDatesPayload:
    """Sayfalanmis kazanc tarihleri; her sayfa TAZE bir Ticker ile cekilir."""

    frame: pd.DataFrame | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class SecFilingsPayload:
    """ABD disinda kaynak list degil {} (dict) doner (S8.3)."""

    filings: list[dict[str, Any]]
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class MarketStatusPayload:
    """Market(region).status; US disindaki 7 bolgede None doner."""

    region: str
    status: Mapping[str, Any] | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class MarketSummaryPayload:
    """Market(region).summary; board kodu -> quote sozlugu."""

    region: str
    summary: Mapping[str, Any] | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class CalendarFramePayload:
    """Sayfalanmis takvim cercevesi; tukendiginde None."""

    frame: pd.DataFrame | None
    fetched_at: datetime


# Tarih indeksli tek sutunlu seri; None de gelebilir (S8.3)
SeriesPayload = pd.Series | None
FramePayload = pd.DataFrame
NewsPayload = list[dict[str, Any]]


# --- AH: analiz / sahiplik / fon ------------------------------------------


@dataclass(frozen=True, slots=True)
class AsOfFramePayload:
    """Tarih tasimayan cerceve; anlami ancak `fetched_at` ile tamamlanir.

    `fetched_at` payload'da TASINIR cunku `normalize(raw, symbol)` imzasi
    ctx'i gormez ve as_of_date bu damgadan turer (AH S6.1).
    """

    frame: pd.DataFrame | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class AsOfMappingPayload:
    """get_analyst_price_targets(): 5 anahtarlik dict."""

    payload: Mapping[str, Any] | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class FundsPayload:
    """get_funds_data(); fon olmayan sembolde `data=None` (istek YAPILMAZ)."""

    data: Any | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class RangedFramePayload:
    """Kaynak tarihini TASIYAN cerceve + `--start/--end` araligi.

    Aralik payload'da tasinir cunku `date_range="filter"` elemesi
    `normalize` icinde yapilir ve normalize ctx'i GORMEZ. Kaynak sabit bir
    pencere donduruyor (upgrades_downgrades ~1000 satir, insider
    transactions 150 satir); aralik daha fazla veri GETIRMEZ, kapsami
    daraltir (AH S4.5, S7.3).
    """

    frame: pd.DataFrame | None
    fetched_at: datetime
    start: date | None = None
    end: date | None = None

"""earnings_dates dataset'i (S6.5, S7.4).

Sayfalama TAZE Ticker ister: `TickerBase._earnings_dates` sozlugu yalnizca
`limit`'e anahtarlidir (`base.py:637`), `offset` onbellek anahtarina
GIRMEZ. Ayni Ticker ile offset=100 istenirse birinci sayfanin ta kendisi
doner (`a is b` -> True) ve sayfalama sessizce no-op olur. Bu, ctx.cached
ilkesinin `news`'ten sonraki ikinci istisnasidir.
"""

from __future__ import annotations

from functools import partial
from typing import Any

import pandas as pd

from yfin import normalize as nz
from yfin.client import call_yahoo, make_ticker
from yfin.config import get_settings
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext, TableWrite
from yfin.datasets.payloads import EarningsDatesPayload
from yfin.datasets.registry import register
from yfin.logging_setup import get_logger

log = get_logger(__name__)

# Yahoo limit'i 100'de sabitliyor (base.py:634: ValueError)
PAGE_LIMIT = 100
KEY_COLUMNS = ("symbol", "earnings_ts_utc", "fact_hash")
UPDATE_COLUMNS = (
    "earnings_date_local",
    "tz_name",
    "eps_estimate",
    "reported_eps",
    "surprise_pct",
    "fetched_at",
)


def _fetch_pages(symbol: str, max_pages: int) -> pd.DataFrame | None:
    frames: list[pd.DataFrame] = []
    for page in range(max_pages):
        offset = page * PAGE_LIMIT
        # Her sayfa icin TAZE Ticker; aksi halde onbellek offset'i yok sayar
        ticker = make_ticker(symbol)
        frame = call_yahoo(
            partial(ticker.get_earnings_dates, limit=PAGE_LIMIT, offset=offset),
            what=f"earnings_dates:{symbol}:{offset}",
        )
        # Durma kosulu BOS SAYFA'dir; len(page) < limit degil
        if nz.is_empty_result(frame):
            break
        frames.append(frame)
    if not frames:
        return None
    return pd.concat(frames)


class MissingTimezoneError(ValueError):
    """Kaynak tz'siz bir index dondurdu; damga yorumlanamaz."""


def _index_tz(index: Any) -> str:
    """Index tz'si; object dtype index'te (fixture) ilk elemandan okunur.

    tz YOKSA "UTC" VARSAYILMAZ. Olcumde 100% sembol tz-AWARE ve
    `America/New_York` donuyor (THYAO.IS ve 7203.T dahil); tz'siz bir index
    kutuphanenin davranisinin degistigi anlamina gelir. "UTC" varsaymak
    `tz_name`i de `earnings_ts_utc`yi de 4-5 saat KAYDIRIR ve yanlis veriyi
    dogru gibi yazar -- sessiz bozulma. Bunun yerine hucre `failed` olur
    (S8.2: yanlis veri, veri yoklugundan kotudur).
    """
    tz = getattr(index, "tz", None)
    if tz is None and len(index):
        tz = getattr(index[0], "tz", None)
    if tz is None:
        raise MissingTimezoneError(
            "earnings_dates index'i tz tasimiyor; damga yorumlanamaz "
            "(olcumde tum semboller America/New_York donuyordu)"
        )
    return str(tz)


class EarningsDatesDataset(Dataset[EarningsDatesPayload]):
    name = "earnings_dates"
    depends_on = ("symbols",)
    produces = ("earnings_dates",)

    def fetch(self, ctx: SyncContext) -> EarningsDatesPayload:
        max_pages = get_settings().yf_earnings_dates_max_pages
        return EarningsDatesPayload(
            frame=_fetch_pages(ctx.symbol, max_pages), fetched_at=ctx.fetched_at
        )

    def normalize(self, raw: EarningsDatesPayload, symbol: str) -> NormalizedResult:
        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)

        tz_name = _index_tz(frame.index)
        rows: dict[tuple[Any, ...], dict[str, Any]] = {}

        for index, record in frame.iterrows():
            ts_utc = nz.to_datetime_utc(index)
            local_date = nz.to_local_date(index)
            if ts_utc is None or local_date is None:
                log.warning("earnings date row has no timestamp", symbol=symbol)
                continue

            values = {
                "eps_estimate": nz.to_decimal(record.get("EPS Estimate")),
                "reported_eps": nz.to_decimal(record.get("Reported EPS")),
                "surprise_pct": nz.to_decimal(record.get("Surprise(%)")),
            }
            # fact_hash PK'ya girer: AAPL 2002-07-16 damgasinda iki satirin
            # TEK farki Surprise(%); EPS alanlarinin ikisi de NaN. Kanonik
            # JSON NaN'i null'a cevirir, aksi halde iki satir ayni hash'i
            # alir ve biri sessizce kaybolur.
            digest = nz.content_hash(
                {k: (str(v) if v is not None else None) for k, v in values.items()}
            )
            row = {
                "symbol": symbol,
                "earnings_ts_utc": ts_utc,
                "fact_hash": digest[:16],
                "earnings_date_local": local_date,
                "tz_name": tz_name,
                **values,
                "fetched_at": raw.fetched_at,
            }
            # Sayfalar arasi ortusme olculdu (1 satir): PK uzerinden
            # tekillestirilmezse rows_verified < rows_attempted yanlis
            # `failed` uretir.
            rows[(ts_utc, row["fact_hash"])] = row

        if not rows:
            return NormalizedResult()
        return NormalizedResult(
            writes=[
                TableWrite(
                    table="earnings_dates",
                    rows=list(rows.values()),
                    key_columns=KEY_COLUMNS,
                    update_columns=UPDATE_COLUMNS,
                )
            ]
        )


register(EarningsDatesDataset())

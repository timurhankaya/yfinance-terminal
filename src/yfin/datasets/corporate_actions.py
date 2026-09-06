"""dividends, splits, capital_gains dataset'leri (S6.3 #4-#6).

Ucu de ONARILMIS history cercevesinin bir KOLONUNDAN beslenir; kendi
get_dividends/get_splits/get_capital_gains cagrilarini YAPMAZLAR.

Gerekce (kaynaktan dogrulandi): `base.py:479-486` `repair` parametresini
`PriceHistory.get_dividends`'e forward ETMEZ ve `history.py:646` onbellek
anahtari `(interval, period, repair)`'dir. Yani `ticker.dividends` hem
ONARIMSIZ veri doner hem de IKINCI bir tam history() ag cagrisi yapar.
Bu tasarimda `price_history.dividend` (onarilmis) ile `dividends` tablosu
(onarimsiz) CELISIRDI - ustelik otorite olan taraf `dividends`'tir.

Uc kazanc: otorite tablolar onarilmis veri alir; iki kaynak arasindaki
celiski kalkar; sembol basina UC ag cagrisi kaybolur.

'actions' bunlarin alias'idir; v_actions view'i yalniz bu uc tabloyu okur.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from yfin import normalize as nz
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext, TableWrite
from yfin.datasets.history import fetch_history_frame
from yfin.datasets.payloads import FramePayload
from yfin.datasets.registry import register


class _SeriesDataset(Dataset[FramePayload]):
    # Ucu de PAYLASILAN history cercevesinden beslenir; aralik o cagriya
    # gecer, satir elemesi degildir (AH S6.2).
    date_range = "api"
    # depends_on ("history",) YAPILMAZ: o zaman `--datasets dividends`
    # calistirmasi price_history'ye de yazardi. Cerceve dogrudan
    # cagrilir; history de seciliyse ctx.cached ayni cagriyi paylasir.
    depends_on = ("symbols",)
    table: str
    date_column: str
    value_column: str
    frame_column: str

    def fetch(self, ctx: SyncContext) -> FramePayload:
        return fetch_history_frame(ctx)

    def normalize(self, raw: FramePayload, symbol: str) -> NormalizedResult:
        # None de gelebilir; '.empty' tek basina yetmez (S8.3)
        if nz.is_empty_result(raw):
            return NormalizedResult()

        frame: pd.DataFrame = raw
        if self.frame_column not in frame.columns:
            # Kolon sembole gore DEGISIR (fon olmayan sembolde 'Capital
            # Gains' yoktur). Bos sonuc 'empty'dir, 'failed' degil (S8.2).
            return NormalizedResult()
        series = frame[self.frame_column]
        # Anahtar bazinda dedupe: kaynakta ayni YEREL tarihe dusen iki kayit
        # gelirse (farkli saatlerdeki iki damga) attempted=2 / verified=1
        # olur ve hucre yanlislikla 'failed' isaretlenirdi (S8.6). Son kayit
        # kazanir - shares_full'daki desenin aynisi.
        by_date: dict[Any, dict[str, Any]] = {}
        for index, value in series.items():
            # Cerceve olaysiz gunlerde 0 tasir; yalnizca gercek olaylar
            # tabloya girer.
            if value is None or float(value) == 0.0:
                continue
            # ex_date de YEREL tarihtir; dividends/splits index'i history'den
            # farkli saatte gelir (THYAO: 09:30 vs 00:00) - S8.3
            when = nz.to_local_date(index)
            amount = nz.to_decimal(value)
            if when is None or amount is None:
                continue
            by_date[when] = {
                "symbol": symbol,
                self.date_column: when,
                self.value_column: amount,
            }

        rows: list[dict[str, Any]] = list(by_date.values())
        if not rows:
            return NormalizedResult()

        return NormalizedResult(
            writes=[
                TableWrite(
                    table=self.table,
                    rows=rows,
                    key_columns=("symbol", self.date_column),
                    update_columns=(self.value_column,),
                )
            ]
        )


class DividendsDataset(_SeriesDataset):
    name = "dividends"
    produces = ("dividends",)
    table = "dividends"
    date_column = "ex_date"
    value_column = "amount"
    frame_column = "Dividends"


class SplitsDataset(_SeriesDataset):
    name = "splits"
    produces = ("splits",)
    table = "splits"
    date_column = "split_date"
    value_column = "ratio"
    frame_column = "Stock Splits"


class CapitalGainsDataset(_SeriesDataset):
    """Hicbir sembolde dolu gelmiyor - test edilen 7 fon/ETF dahil (S8.2).

    Bos sonuc 'empty'dir, 'failed' degil. Boslugu hata sayan sistem her
    calistirmada yanlis alarm verir. 'Capital Gains' kolonu yalnizca
    fonlarda gelir; yoksa normalize bos sonuc doner.
    """

    name = "capital_gains"
    produces = ("capital_gains",)
    table = "capital_gains"
    date_column = "gain_date"
    value_column = "amount"
    frame_column = "Capital Gains"


register(DividendsDataset())
register(SplitsDataset())
register(CapitalGainsDataset())

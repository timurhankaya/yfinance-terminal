"""history dataset'i (S6.3 #2) -> price_history."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from yfin import normalize as nz
from yfin.client import call_yahoo
from yfin.config import get_settings
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext, TableWrite
from yfin.datasets.common import date_range_kwargs
from yfin.datasets.payloads import FramePayload
from yfin.datasets.registry import register
from yfin.logging_setup import get_logger

CACHE_HISTORY = "history_df"

log = get_logger(__name__)

# Bir kez olculur; process omru boyunca degismez.
_REPAIR_AVAILABLE: bool | None = None

_ZERO = Decimal(0)

_COLUMN_MAP: dict[str, str] = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Adj Close": "adj_close",
    "Volume": "volume",
    "Dividends": "dividend",
    "Stock Splits": "split_ratio",
    "Capital Gains": "capital_gain",
}

# Paylasilan cerceveyi tuketen (dataset -> tablo, tarih kolonu) esleme.
# `start` bunlarin watermark'larinin MINIMUMUDUR: price_history guncel ama
# dividends bossa dar bir artimli pencere eski temettuleri kacirirdi.
FRAME_CONSUMERS: dict[str, tuple[str, str]] = {
    "history": ("price_history", "session_date"),
    "dividends": ("dividends", "ex_date"),
    "splits": ("splits", "split_date"),
    "capital_gains": ("capital_gains", "gain_date"),
}

UPDATE_COLUMNS = (
    "ts_utc",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "dividend",
    "split_ratio",
    "capital_gain",
    "is_repaired",
)

# is_repaired GERI YAZILMAZ, yalnizca 0 -> 1 yonunde ilerler (S6.2).
MONOTONIC_COLUMNS = ("is_repaired",)


def repair_enabled() -> bool:
    """Onarim istendi VE [repair] ekstrasi kurulu mu?

    yfinance onarim heuristiklerinde scipy.ndimage ve
    sklearn.cluster.DBSCAN'i TEMBEL import eder (scrapers/history.py:820,
    1338). Ekstra eksikse cagri ModuleNotFoundError ile duser ve bu, her
    sembolde `history` + `dividends` + `splits` + `capital_gains`
    hucrelerini birden basarisiz yapar - yani price_history HIC yazilmaz.
    Toplam veri kesintisi yerine onarimsiz ama calisan bir kosu tercih
    edilir; eksiklik bir kez GORUNUR sekilde loglanir.
    """
    if not get_settings().yf_history_repair:
        return False
    global _REPAIR_AVAILABLE
    if _REPAIR_AVAILABLE is None:
        try:
            import scipy.ndimage  # noqa: F401
            import sklearn.cluster  # noqa: F401

            _REPAIR_AVAILABLE = True
        except ImportError:
            _REPAIR_AVAILABLE = False
            log.error(
                "history repair devre disi: yfinance[repair] ekstrasi kurulu degil "
                "(pip install 'yfinance[repair]'); scipy ve scikit-learn gerekir"
            )
    return _REPAIR_AVAILABLE


def _shared_watermark(ctx: SyncContext) -> date | datetime | None:
    """Cerceveyi tuketen SECILI tablolarin watermark minimumu.

    Herhangi biri bossa None doner (period="max"): o tablo icin gecmisin
    tamami cekilmelidir.
    """
    names = ctx.selected if ctx.selected is not None else set(FRAME_CONSUMERS)
    marks: list[date | datetime] = []
    for name, (table, column) in FRAME_CONSUMERS.items():
        if name not in names:
            continue
        mark = ctx.watermark(table, column)
        if mark is None:
            return None
        marks.append(mark)
    if not marks:
        # Hicbir tuketici secili degil (dogrudan cagri); price_history'ye
        # duser, boylece davranis eskisiyle ayni kalir.
        return ctx.watermark("price_history", "session_date")
    return min(marks, key=_as_date)


def fetch_history_frame(ctx: SyncContext) -> pd.DataFrame:
    """Tek gunluk seri. capital_gains AYRI BIR AG CAGRISI DEGILDIR -
    ayni onbellekten gelir (history.py:723), ctx.cached bunu paylasir."""

    def _call() -> pd.DataFrame:
        kwargs: dict[str, Any] = {
            "interval": "1d",
            "auto_adjust": False,  # 'Adj Close' ayri kolon olarak gelsin
            # actions=True SART: dividends/splits/capital_gains bu
            # kolonlardan beslenir (history.py:619-620 aksi halde duser)
            "actions": True,
            # Yahoo'nun bilinen veri hatalarini duzeltir (eksik bolunme/
            # temettu duzeltmesi, 100x kur hatalari, mukerrer temettu).
            # Yalniz 1g interval'de guvenilirdir - kullandigimiz interval.
            "repair": repair_enabled(),
        }
        # --start/--end WATERMARK'I GECERSIZ KILAR (AH S7.3): kullanici
        # acikca bir aralik istediginde artimli pencere onu daraltamaz.
        if ctx.start is not None or ctx.end is not None:
            kwargs.update(date_range_kwargs(ctx.start, ctx.end))
        else:
            watermark = _shared_watermark(ctx)
            if watermark is None:
                kwargs["period"] = "max"
            else:
                overlap = get_settings().yf_incremental_overlap_days
                start = _as_date(watermark) - timedelta(days=overlap)
                kwargs["start"] = start.isoformat()
        return call_yahoo(lambda: ctx.ticker.history(**kwargs), what=f"history:{ctx.symbol}")

    return ctx.cached(CACHE_HISTORY, _call)


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


class HistoryDataset(Dataset[FramePayload]):
    name = "history"
    depends_on = ("symbols",)
    produces = ("price_history",)
    # Aralik yfinance CAGRISINA gecer -> GERCEK geriye donuk cekim; bu
    # "satir eleme" degildir (AH S6.2).
    date_range = "api"

    def fetch(self, ctx: SyncContext) -> FramePayload:
        return fetch_history_frame(ctx)

    def normalize(self, raw: FramePayload, symbol: str) -> NormalizedResult:
        if nz.is_empty_result(raw):
            return NormalizedResult()

        frame: pd.DataFrame = raw
        # Kolon seti sembole gore DEGISIR (fon/ETF'te 'Capital Gains'
        # eklenir); sabit siraya veya varliga guvenilmez (S8.3)
        present = {src: dst for src, dst in _COLUMN_MAP.items() if src in frame.columns}

        rows: list[dict[str, Any]] = []
        for index, record in zip(frame.index, frame.to_dict("records"), strict=True):
            session_date = nz.to_local_date(index)
            ts_utc = nz.to_datetime_utc(index)
            if session_date is None or ts_utc is None:
                continue
            close = nz.to_decimal(record.get("Close"))
            if close is None:
                # close NOT NULL; kapanissiz satir anlamsizdir
                continue

            row: dict[str, Any] = {
                "symbol": symbol,
                "session_date": session_date,
                "ts_utc": ts_utc,
                "close": close,
                "dividend": _ZERO,
                "split_ratio": _ZERO,
                "capital_gain": _ZERO,
                # SABIT sozlukte tutulur, _COLUMN_MAP'e KONMAZ: jenerik
                # dongu onu `else` dalinda to_decimal ile isler ve BOOLEAN
                # kolona Decimal yazardi. Ayrica burada her satirda mevcut
                # oldugu icin `present` kesisimine hic girmez; girseydi
                # kolon gelmedigi kosuda ON DUPLICATE KEY UPDATE
                # kapsamindan da duserdi.
                "is_repaired": bool(nz.to_bool(record.get("Repaired?"))),
            }
            for src, dst in present.items():
                if dst == "close":
                    continue
                value = record.get(src)
                if dst == "volume":
                    volume = nz.to_int(value)
                    row[dst] = None if volume is None or volume < 0 else volume
                elif dst in ("dividend", "split_ratio", "capital_gain"):
                    row[dst] = nz.to_decimal(value) or _ZERO
                else:
                    row[dst] = nz.to_decimal(value)
            rows.append(row)

        if not rows:
            return NormalizedResult()

        return NormalizedResult(
            writes=[
                TableWrite(
                    table="price_history",
                    rows=rows,
                    key_columns=("symbol", "session_date"),
                    update_columns=UPDATE_COLUMNS,
                    monotonic_columns=MONOTONIC_COLUMNS,
                )
            ]
        )


register(HistoryDataset())

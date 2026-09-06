"""Cok interval'li bar cekimi -> price_bars (PB S6).

Bu modul su an yalniz PENCERE PLANLAYICISINI icerir; dataset sinifi ve
normalize sonraki adimlarda eklenir (PB S10).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from yfin import normalize as nz
from yfin.client import call_yahoo
from yfin.config import get_settings
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext, TableWrite
from yfin.datasets.registry import register
from yfin.errors import DatasetOutOfScope
from yfin.logging_setup import get_logger
from yfin.models.bars import (
    BAR_INTERVALS,
    GAP_FETCH_FAILED,
    GAP_RETENTION_EXPIRED,
    INTRADAY_INTERVALS,
)

# (istek basina azami gun, geriye azami derinlik gun) - PB S4.3'te olculdu.
#
# BU DEGERLER YAHOO'NUN ILAN ETTIGI SINIR DEGIL, OLCUMDE KABUL EDILEN
# degerlerdir: Yahoo'nun mesajlari "60 gun" / "730 gun" der ama 60 ve 730
# REDDEDILDI, 59 ve 729 kabul edildi. Sinirlar gun degil SANIYE bazlidir
# ve "su ana" gorelidir, bu yuzden tam sinir her zaman risklidir. Tampon
# zaten buradadir; plan_windows UZERINE IKINCI bir tampon uygulamaz.
#
# None = sinirsiz: 1wk/1mo icin ilk dolum period="max" ile yapilir.
log = get_logger(__name__)

BAR_LIMITS: dict[str, tuple[int | None, int | None]] = {
    # 1m derinligi 29'dur, 30 DEGIL: Yahoo'nun mesaji "within the last 30
    # days" der ama tam 30 gun REDDEDILIR (olculdu: -30g RED, -29g 1950
    # bar). 5m'de 60'in, 60m'de 730'un reddedilmesiyle ayni desen.
    # Bu deger ilk yazimda 30 birakilmisti ve ILK CANLI KOSUDA yakalandi:
    # planlayici sinirda bir istek uretti, YFPricesMissingError aldi ve
    # AAPL'in tum 1m ilk dolumu dustu.
    "1m": (8, 29),
    "5m": (59, 59),
    "15m": (59, 59),
    "60m": (729, 729),
    "1wk": (None, None),
    "1mo": (None, None),
}


@dataclass(frozen=True)
class FetchPlan:
    """Bir interval icin tek kosuluk cekim plani.

    `windows` bos VE `gap` None ise: sinirsiz interval'in ilk dolumu,
    cagiran period="max" kullanir.
    """

    # [start, end) yari acik araliklar; yfinance'in `end` parametresi de
    # DISLAYICIDIR, yani ardisik dilimler ust uste binmez.
    windows: tuple[tuple[date, date], ...] = ()
    # Yahoo penceresi gectigi icin ARTIK CEKILEMEYEN aralik. bar_gaps'e
    # 'retention_expired' olarak yazilir (PB S6.2/4).
    gap: tuple[datetime, datetime] | None = None


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _slice(start: date, end: date, per_request: int | None) -> list[tuple[date, date]]:
    """[start, end) araligini istek penceresine boler."""
    if end <= start:
        return []
    if per_request is None:
        return [(start, end)]
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + timedelta(days=per_request), end)
        out.append((cursor, nxt))
        cursor = nxt
    return out


def plan_windows(
    interval: str,
    watermark: datetime | None,
    now: datetime,
    *,
    start: date | None = None,
    end: date | None = None,
    overlap_days: int = 2,
    open_gaps: Sequence[tuple[datetime, datetime]] = (),
) -> FetchPlan:
    """Bir interval icin cekilecek pencereleri ve kurtarilamayan boslugu hesaplar.

    Bes senaryo (PB S6.2):
      1. Ilk dolum (watermark yok): derinlik kadar geriye, pencereye bolunur.
      2. Normal artimli: watermark - overlap, tek dilim.
      3. Duraklama: pencereyi asan aralik BIRDEN COK dilime bolunur - naif
         tek dilim Yahoo tarafindan reddedilir ve TUM veri kaybedilirdi.
      4. Derinlik asimi: cekilebilen kisim dilimlenir, gerisi `gap` olur.
      5. Acik bosluklar: hala pencere icinde olanlar yeniden denenir;
         bu geri besleme olmadan bar_gaps yalnizca bir mezar tasi olurdu.

    `start`/`end` verilirse (--start/--end, date_range="api") watermark ve
    `open_gaps` YOK SAYILIR ve derinlik asimi icin gap URETILMEZ: elle
    istenen bir geriye donuk cekimin basarisiz olmasi bir kacirma degil,
    kullanici hatasidir.
    """
    if interval not in BAR_LIMITS:
        raise ValueError(f"bilinmeyen interval: {interval}; gecerli: {', '.join(BAR_INTERVALS)}")

    per_request, depth = BAR_LIMITS[interval]
    today = _as_date(now)
    earliest = today - timedelta(days=depth) if depth is not None else None

    # --- elle aralik: watermark ve acik bosluklar devre disi -------------
    if start is not None or end is not None:
        window_start = start if start is not None else today - timedelta(days=overlap_days)
        window_end = end if end is not None else today
        return FetchPlan(windows=tuple(_slice(window_start, window_end, per_request)))

    # --- ilk dolum --------------------------------------------------------
    if watermark is None:
        if earliest is None:
            # Sinirsiz interval: period="max" (dilim yok)
            return FetchPlan()
        return FetchPlan(windows=tuple(_slice(earliest, today, per_request)))

    # --- artimli ----------------------------------------------------------
    wanted_start = _as_date(watermark) - timedelta(days=overlap_days)
    gap: tuple[datetime, datetime] | None = None
    if earliest is not None and wanted_start < earliest:
        # Yahoo penceresi gecmis: kurtarilamayan kisim KAYDEDILIR.
        # gap'in bitisi ilk dilimin baslangicina esittir; aksi halde arada
        # ne cekilen ne kaydedilen bir gun kalirdi.
        gap = (watermark, datetime.combine(earliest, datetime.min.time(), tzinfo=watermark.tzinfo))
        wanted_start = earliest

    # Watermark araligi + hala pencere icinde kalan acik bosluklar
    # (PB S6.2/5). Bosluklar AYRI aralik olarak eklenir: min(hepsi)'nden
    # bugune tek bir acik aralik dilimlemek, 25 gun onceki bir bosluk icin
    # aradaki TUM gunleri yeniden cektirirdi.
    ranges: list[tuple[date, date]] = [(wanted_start, today)]
    for gap_start, gap_end_ts in open_gaps:
        gap_from = _as_date(gap_start)
        if earliest is not None and gap_from < earliest:
            continue  # penceresi kapanmis; bosuna istek uretme
        # Bosluk ucu DAHIL edilsin diye bir gun genisletilir: yfinance'in
        # `end` parametresi dislayicidir.
        gap_to = _as_date(gap_end_ts) + timedelta(days=1)
        ranges.append((gap_from, min(gap_to, today)))

    windows: list[tuple[date, date]] = []
    for range_start, range_end in _merge(ranges):
        windows.extend(_slice(range_start, range_end, per_request))
    return FetchPlan(windows=tuple(windows), gap=gap)


def _merge(ranges: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """Ustuste binen/bitisik araliklari birlestirir.

    Bitisik olanlar da birlestirilir (`<=`): bosluk watermark penceresinin
    hemen oncesine dayaniyorsa iki ayri istek yerine tek istek yeterlidir.
    """
    ordered = sorted(r for r in ranges if r[1] > r[0])
    if not ordered:
        return []
    merged = [ordered[0]]
    for current_start, current_end in ordered[1:]:
        last_start, last_end = merged[-1]
        if current_start <= last_end:
            merged[-1] = (last_start, max(last_end, current_end))
        else:
            merged.append((current_start, current_end))
    return merged


@dataclass(frozen=True)
class BarPayload:
    """fetch'in dondurdugu ham veri (PB S6.4).

    `has_prepost` alani BILEREK YOKTUR. Ilk tasarimda
    has_pre_post_market_data bir erken cikis kapisiydi; olcum onu curuttu:
    SHEL.L ve VWCE.DE bu alani False bildirdikleri halde 5 ve 8 seans disi
    bar donduruyor. O kapi onlari NORMAL SEANS sayar ve
    v_price_bars_regular'a sokardi - yani view'in onlemek icin var oldugu
    bozulmanin ta kendisi. Tek dogruluk kaynagi tradingPeriods'in
    start/end araligidir.
    """

    frame: pd.DataFrame
    trading_periods: pd.DataFrame | None
    interval: str
    # plan_windows'un bildirdigi KURTARILAMAYAN bosluk; normalize onu
    # bar_gaps'e 'retention_expired' olarak yazar (PB S8.4).
    gap: tuple[datetime, datetime] | None = None
    # Istegi DUSEN dilimler. Pencere hala acik olabilecegi icin
    # 'fetch_failed' olarak yazilirlar ve planlayici bir sonraki kosuda
    # onlari YENIDEN DENER (PB S6.2/5).
    failed_windows: tuple[tuple[date, date], ...] = ()
    # Basariyla cekilen dilimler. Icinde kalan ACIK bosluklar kapatilir
    # (resolved_at); aksi halde bir kez yazilan bosluk sonsuza kadar acik
    # kalir ve her kosuda bosuna yeniden cekilir.
    fetched_windows: tuple[tuple[date, date], ...] = ()
    # Bu kosuda DB'den okunan acik bosluklar; hangilerinin kapandigini
    # normalize bunlarla hesaplar.
    open_gaps: tuple[tuple[datetime, datetime], ...] = ()


UPDATE_COLUMNS = (
    "local_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "is_extended",
)

_COLUMN_MAP: dict[str, str] = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
}


def _session_bounds(periods: pd.DataFrame | None) -> dict[date, tuple[pd.Timestamp, pd.Timestamp]]:
    """gun -> (regular seans baslangici, bitisi).

    YALNIZ `start`/`end` okunur. `pre_*`/`post_*` kolonlari iki nedenle
    KULLANILMAZ: (1) dejenere olabilirler - THYAO'da pre=09:30-09:30 ve
    post=18:00-18:00 iken reg=09:30-18:00 saglamdir; (2) prepost=False ile
    yapilan bir cagrida HIC GELMEZLER ve onlara erisen kod KeyError
    verirdi (PB S4.5/3).
    """
    if periods is None or periods.empty:
        return {}
    if "start" not in periods.columns or "end" not in periods.columns:
        return {}
    bounds: dict[date, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for index, row in zip(periods.index, periods.to_dict("records"), strict=True):
        day = nz.to_local_date(index)
        start_ts, end_ts = row.get("start"), row.get("end")
        if day is None or nz.is_missing(start_ts) or nz.is_missing(end_ts):
            continue
        bounds[day] = (pd.Timestamp(start_ts), pd.Timestamp(end_ts))
    return bounds


def is_extended_bar(
    ts: pd.Timestamp,
    local_day: date,
    bounds: dict[date, tuple[pd.Timestamp, pd.Timestamp]],
    interval: str,
) -> bool:
    """Bar seans disi mi (PB S6.4).

    Dort adim:
      1. interval gun ici degilse (1wk/1mo)      -> False (kavram anlamsiz)
      2. o gunun seans siniri bilinmiyorsa        -> False (guvenli varsayilan)
      3. ts < start veya ts >= end                -> True
      4. aksi halde                               -> False

    2. adimin varsayilani bilincli olarak False'tur: bilinmeyen bir bari
    seans disi saymak onu v_price_bars_regular'dan GIZLERDI; ters hata
    (seans disini normal saymak) daha gorunurdur ve denetimde yakalanir.
    """
    if interval not in INTRADAY_INTERVALS:
        return False
    window = bounds.get(local_day)
    if window is None:
        return False
    start_ts, end_ts = window
    return bool(ts < start_ts or ts >= end_ts)


def normalize_bars(raw: BarPayload, symbol: str) -> NormalizedResult:
    """Ham cerceveyi price_bars satirlarina cevirir (PB S8.3)."""
    if raw.interval not in BAR_LIMITS:
        raise ValueError(f"bilinmeyen interval: {raw.interval}")
    if nz.is_empty_result(raw.frame):
        # ERKEN DONUS YOK: bosluk kayitlari cerceveden BAGIMSIZDIR. Bir
        # dilim dusup hic bar gelmediginde tam da o boslugun yazilmasi
        # gerekir; erken donmek onu sessizce yutardi.
        return NormalizedResult(writes=_gap_writes(raw, symbol))

    frame = raw.frame
    bounds = _session_bounds(raw.trading_periods)
    # Kolon seti sembole gore DEGISIR (ETF'te 'Capital Gains' eklenir);
    # sabit siraya veya varliga guvenilmez (S8.3). Bu uc kolon zaten
    # price_bars'a YAZILMAZ, yalnizca yok sayilir (PB K3).
    present = {src: dst for src, dst in _COLUMN_MAP.items() if src in frame.columns}

    rows: list[dict[str, Any]] = []
    for index, record in zip(frame.index, frame.to_dict("records"), strict=True):
        # local_date UTC'den TURETILMEZ: pozitif ofsetli borsalarda tz
        # cevrimi tarihi bir gun geri kaydirir (S5.4). Kaynak index zaten
        # yerel tz tasir.
        local_day = nz.to_local_date(index)
        ts_utc = nz.to_datetime_utc(index)
        if local_day is None or ts_utc is None:
            continue
        close = nz.to_decimal(record.get("Close"))
        if close is None:
            continue  # close NOT NULL; kapanissiz bar anlamsizdir

        row: dict[str, Any] = {
            "symbol": symbol,
            "bar_interval": raw.interval,
            "ts_utc": ts_utc,
            "local_date": local_day,
            "close": close,
            "is_extended": is_extended_bar(pd.Timestamp(index), local_day, bounds, raw.interval),
        }
        for src, dst in present.items():
            if dst == "close":
                continue
            value = record.get(src)
            if dst == "volume":
                volume = nz.to_int(value)
                row[dst] = None if volume is None or volume < 0 else volume
            else:
                row[dst] = nz.to_decimal(value)
        rows.append(row)

    writes = []
    if rows:
        writes.append(
            TableWrite(
                table="price_bars",
                rows=rows,
                key_columns=("symbol", "bar_interval", "ts_utc"),
                update_columns=UPDATE_COLUMNS,
            )
        )
    writes.extend(_gap_writes(raw, symbol))
    return NormalizedResult(writes=writes)


def _gap_writes(raw: BarPayload, symbol: str) -> list[TableWrite]:
    """bar_gaps satirlari: kayip kaydi, gorev kaydi ve gorev kapatma.

    Uc ayri kaynak, tek tablo:
      retention_expired  planlayici hesapladi, veri KALICI kayip
      fetch_failed       dilim dustu; pencere hala aciksa yeniden denenir
      resolved_at        basarili dilim, icindeki acik boslugu kapatir

    Sonuncusu olmadan bar_gaps tek yonlu bir liste olurdu: bir kez yazilan
    bosluk sonsuza kadar acik kalir ve HER kosuda bosuna yeniden cekilirdi.
    """
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []

    if raw.gap is not None:
        gap_start, gap_end = raw.gap
        rows.append(
            {
                "symbol": symbol,
                "bar_interval": raw.interval,
                "gap_start_utc": nz.to_datetime_utc(gap_start),
                "gap_end_utc": nz.to_datetime_utc(gap_end),
                "detected_at": now,
                "reason": GAP_RETENTION_EXPIRED,
                # DAIMA NULL: bu bir KAYIP KAYDIDIR, gorev degil.
                "resolved_at": None,
            }
        )

    for window_start, window_end in raw.failed_windows:
        rows.append(
            {
                "symbol": symbol,
                "bar_interval": raw.interval,
                "gap_start_utc": datetime.combine(window_start, datetime.min.time()),
                "gap_end_utc": datetime.combine(window_end, datetime.min.time()),
                "detected_at": now,
                "reason": GAP_FETCH_FAILED,
                "resolved_at": None,
            }
        )

    writes: list[TableWrite] = []
    if rows:
        writes.append(
            TableWrite(
                table="bar_gaps",
                rows=rows,
                key_columns=("symbol", "bar_interval", "gap_start_utc"),
                # resolved_at GUNCELLENMEZ: ayni anahtar daha once
                # cozulmus olabilir ve onu yeniden acmak, kapanmis bir
                # boslugu her kosuda yeniden cektirirdi.
                update_columns=("gap_end_utc", "detected_at", "reason"),
            )
        )
    writes.extend(_resolve_writes(raw, symbol, now))
    return writes


def _resolve_writes(raw: BarPayload, symbol: str, now: datetime) -> list[TableWrite]:
    """Basarili dilimlerin icinde kalan ACIK bosluklari kapatir.

    `replace_scope` DEGIL, hedefli bir upsert: kapsam silme burada yanlis
    olurdu cunku ayni sembolun BASKA aralikta duran acik bosluklari
    silinirdi.
    """
    if not raw.fetched_windows or not raw.open_gaps:
        return []
    rows: list[dict[str, Any]] = []
    for gap_start, gap_end in raw.open_gaps:
        # Pencere sinirlari da UTC-aware kurulur: aware ve naive
        # datetime karsilastirmasi TypeError verir.
        covered = any(
            datetime.combine(w_start, datetime.min.time(), tzinfo=UTC)
            <= _aware(gap_start)
            < datetime.combine(w_end, datetime.min.time(), tzinfo=UTC)
            for w_start, w_end in raw.fetched_windows
        )
        if not covered:
            continue
        rows.append(
            {
                "symbol": symbol,
                "bar_interval": raw.interval,
                "gap_start_utc": _aware(gap_start),
                "gap_end_utc": _aware(gap_end),
                "detected_at": now,
                "reason": GAP_FETCH_FAILED,
                "resolved_at": now,
            }
        )
    if not rows:
        return []
    return [
        TableWrite(
            table="bar_gaps",
            rows=rows,
            key_columns=("symbol", "bar_interval", "gap_start_utc"),
            update_columns=("resolved_at",),
        )
    ]


def _aware(value: datetime) -> datetime:
    """UTC-aware'e cevirir; naive gelen deger UTC KABUL EDILIR.

    Eskiden tersini yapiyordu (`_naive`): `bar_gaps.gap_start_utc` MySQL
    DATETIME oldugu icin tz dusuruluyordu. Kolon artik `timestamptz`tir
    (PG S2.3) ve naive deger yazmak, psycopg'nin baglanti TZ'sine gore
    yorumlamasina birakmak demekti -- sonuc dogru cikar ama karsilastirma
    ve depolama farkli farkindalik duzeylerinde kalirdi.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class IntervalBarDataset(Dataset[BarPayload]):
    """Tek bir interval'in price_bars'a yazilmasi. Ad: bars_<interval>.

    Alti interval icin alti sinif yazmak ayni govdenin alti kopyasidir
    (PB K9); interval bir ORNEKLEME PARAMETRESIDIR.

    `name` bu yuzden INSTANCE attribute'tur. Taban sinifta sinif
    attribute'u olarak taniml; Registrable protokolu `name: str` istedigi
    icin ikisi de gecerlidir ve sozlesme DARALMAZ (S6.1/6'daki `produces`
    dersinden farki budur - orada tip degisiyordu).
    """

    produces = ("price_bars", "bar_gaps")
    # SADECE "symbols". Once ("symbols", "splits") yazilmisti; iki ayri
    # nedenle YANLISTI:
    #   1. "splits" bir TABLO adidir, dataset adi degil - Registry
    #      cozumlemeyi dataset adlariyla yapar ve cozumleme patlardi.
    #   2. Dataset adi olsaydi bile SplitsDataset._SeriesDataset.fetch ->
    #      fetch_history_frame, yani TAM bir 1d history() cagrisi.
    #      `--datasets bars_1m` bile 1d cekerdi. Proje bu sekle acikca
    #      karsi: corporate_actions.py ayni gerekceyle
    #      `depends_on = ("history",)` YAPMIYOR.
    # Rescale kancasi (PB S6.6) dataset bagimliligiyla degil, `splits`
    # TABLOSUNU okuyarak calisir.
    depends_on = ("symbols",)
    # Aralik yfinance CAGRISINA gecer -> GERCEK geriye donuk cekim.
    date_range = "api"

    def __init__(self, interval: str) -> None:
        if interval not in BAR_INTERVALS:
            raise ValueError(f"bilinmeyen interval: {interval}")
        self.interval = interval
        self.name = f"bars_{interval}"

    def fetch(self, ctx: SyncContext) -> BarPayload:
        # Kapsam kapisi AG CAGRISINDAN ONCE (PB S6.5): kapsam disi sembol
        # icin tek bir istek bile yapilmaz.
        if not ctx.in_scope(self.interval):
            raise DatasetOutOfScope(self.interval)

        watermark = ctx.watermark("price_bars", "ts_utc", where={"bar_interval": self.interval})
        open_gaps = ctx.open_gaps(self.interval)
        plan = plan_windows(
            self.interval,
            _as_datetime(watermark),
            ctx.fetched_at,
            start=ctx.start,
            end=ctx.end,
            overlap_days=get_settings().yf_bar_overlap_days,
            open_gaps=open_gaps,
        )

        windows: list[tuple[date, date] | None] = list(plan.windows)
        if not plan.windows and plan.gap is None:
            # Sinirsiz interval'in ilk dolumu (1wk/1mo): tek cagri,
            # period="max"
            windows.append(None)

        # DILIM BASINA HATA IZOLASYONU (PB S8.1). Tum fetch'i tek bir
        # atomik birim saymak, tek bir ag hatasinda 29 gunluk ilk dolumun
        # TAMAMINI kaybettirirdi. Dusen dilim bar_gaps'e yazilir ve
        # penceresi hala aciksa bir sonraki kosuda yeniden denenir.
        frames: list[pd.DataFrame] = []
        fetched: list[tuple[date, date]] = []
        failed: list[tuple[date, date]] = []
        errors: list[Exception] = []
        for window in windows:
            try:
                frames.append(self._fetch_window(ctx, window))
            except Exception as exc:  # noqa: BLE001 - dilim hata siniri
                errors.append(exc)
                if window is not None:
                    failed.append(window)
                log.warning(
                    "bar dilimi dustu",
                    symbol=ctx.symbol,
                    interval=self.interval,
                    window=str(window),
                    error=str(exc),
                )
                continue
            if window is not None:
                fetched.append(window)

        if errors and not frames:
            # HICBIR dilim gelmediyse bu gercek bir hatadir: sessizce bos
            # donmek `empty` ile `failed`i karistirir ve proxy saglik
            # muhasebesi de bozulurdu (PB S8.2).
            raise errors[0]

        non_empty = [f for f in frames if not nz.is_empty_result(f)]
        frame = pd.concat(non_empty) if non_empty else pd.DataFrame()
        if not frame.empty:
            # Ortusen dilimler ayni bari iki kez getirebilir; upsert bunu
            # zaten tolere eder ama gereksiz satir uretmemek daha ucuz.
            frame = frame[~frame.index.duplicated(keep="last")]

        return BarPayload(
            frame=frame,
            trading_periods=self._trading_periods(ctx),
            interval=self.interval,
            gap=plan.gap,
            failed_windows=tuple(failed),
            fetched_windows=tuple(fetched),
            open_gaps=tuple(open_gaps),
        )

    def _fetch_window(self, ctx: SyncContext, window: tuple[date, date] | None) -> pd.DataFrame:
        kwargs: dict[str, Any] = {
            "interval": self.interval,
            "auto_adjust": False,
            "actions": True,
            # Seans disi barlar YAZILIR ve is_extended ile isaretlenir:
            # kacirilan intraday bar bir daha cekilemez (PB K7).
            "prepost": get_settings().yf_bar_prepost,
            # repair KAPALI (PB K8): 5m'de 1->6, 15m'de 1->8 istek eder ve
            # 1wk/1mo'yu 1d'den resample edip satir anahtarlarini kaydirir.
            "repair": False,
        }
        if window is None:
            kwargs["period"] = "max"
        else:
            kwargs["start"] = window[0].isoformat()
            kwargs["end"] = window[1].isoformat()
        what = f"bars_{self.interval}:{ctx.symbol}"
        return call_yahoo(lambda: ctx.ticker.history(**kwargs), what=what)

    def _trading_periods(self, ctx: SyncContext) -> pd.DataFrame | None:
        """history() cagrisindan SONRA metadata'yi okur.

        EK AG ISTEGI DEGILDIR: history() metadata'yi zaten doldurur ve
        history_metadata dataset'i de ayni onbellegi paylasir (PB S4.1/7).
        """
        metadata = ctx.ticker.get_history_metadata()
        periods = nz.as_mapping(metadata).get("tradingPeriods")
        return periods if isinstance(periods, pd.DataFrame) else None

    def normalize(self, raw: BarPayload, symbol: str) -> NormalizedResult:
        return normalize_bars(raw, symbol)


def _as_datetime(value: date | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.combine(value, datetime.min.time())


for _interval in BAR_INTERVALS:
    register(IntervalBarDataset(_interval))

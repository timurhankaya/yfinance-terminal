"""Gercek API'den bir kez veri cekip fixture olarak kaydeder (S9.1).

Kullanim:  python scripts/capture_fixtures.py [SEMBOL ...]
           python scripts/capture_fixtures.py --bars [SEMBOL ...]
           python scripts/capture_fixtures.py --domain
           python scripts/capture_fixtures.py _market
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yfinance as yf

from yfin import normalize as nz
from yfin.config import get_settings
from yfin.datasets.funds import _collect

# Kapsam S4.3 matrisine gore secildi. MSFT ZORUNLUDUR: Haziran mali yili
# (donem sonunun takvim yilina sabit olmadigini kanitlar) ve 60 karakterlik
# item_key (FinancialAssetsDesignatedasFairValueThroughProfitorLossTotal)
# yalnizca onda var.
# AH S9.1 alti sembol daha ekler; her biri TEK BASINA bir kenar durumun
# kanitidir: PFE dokuz kolonda ozdes iki insider satiri, XOM `Ownership='D/I'`,
# NVDA 11 kolonlu insider_roster + float epoch tarih, WMT 56 karakterlik
# position ve 150 satirlik pencere, KO negatif net_shares, BND tahvil fonu
# (0 sektor + 9 rating, top_holdings BOS), ^GSPC 16 dataset'in tamami bos.
REFERENCE_SYMBOLS = (
    "AAPL",
    "MSFT",
    "THYAO.IS",
    "SPY",
    "BTC-USD",
    "PFE",
    "XOM",
    "NVDA",
    "WMT",
    "KO",
    "BND",
    "^GSPC",
)

# Tek `Ticker` uzerinden YEDI istekle beslenen 16 dataset (AH S4.4).
# `sustainability` BILEREK YOKTUR: 19 sembolde de 404 doner.
ANALYSIS_GETTERS = (
    "get_recommendations",
    "get_upgrades_downgrades",
    "get_analyst_price_targets",
    "get_earnings_estimate",
    "get_revenue_estimate",
    "get_eps_trend",
    "get_eps_revisions",
    "get_earnings_history",
    "get_growth_estimates",
)
HOLDERS_GETTERS = (
    "get_major_holders",
    "get_institutional_holders",
    "get_mutualfund_holders",
    "get_insider_purchases",
    "get_insider_transactions",
    "get_insider_roster_holders",
)

# (dataset adi, statement metodu, freq)
STATEMENT_SPECS = (
    ("income_stmt", "get_income_stmt", "yearly"),
    ("quarterly_income_stmt", "get_income_stmt", "quarterly"),
    ("ttm_income_stmt", "get_income_stmt", "trailing"),
    ("balance_sheet", "get_balance_sheet", "yearly"),
    ("quarterly_balance_sheet", "get_balance_sheet", "quarterly"),
    ("cashflow", "get_cashflow", "yearly"),
    ("quarterly_cashflow", "get_cashflow", "quarterly"),
    ("ttm_cashflow", "get_cashflow", "trailing"),
)

# (dataset adi, freq). 'trailing' ve 'monthly' YOKTUR; gerekce
# `datasets/financials/valuation.py` sonundaki nottadir.
VALUATION_SPECS = (
    ("valuation_measures", "yearly"),
    ("quarterly_valuation_measures", "quarterly"),
)

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# price_bars fixture sembolleri (PB S9.1). Her biri TEK BASINA bir kenar
# durumun kanitidir:
#   AAPL     hasPrePost=True, start=09:30/end=16:00 -> 04:00 ve 16:00
#            sonrasi barlar is_extended=1
#   SHEL.L   REGRESYON: hasPrePost=FALSE bildirdigi halde 16:30/16:35
#            barlari donuyor. Silinen "kapi 1" bunlari normal seans
#            sayardi; bu fixture onun geri gelmesini engeller.
#   VWCE.DE  ayni desen, 17:30/17:35
#   THYAO.IS start=09:30/end=18:00; dejenere olan pre_*/post_* kolonlari
#   BTC-USD  7/24, start=00:00/end=23:59 -> hepsi 0
#   GC=F     bar 18:10'da acilir; local_date seans gunu DEGILDIR
BAR_FIXTURE_SYMBOLS = ("AAPL", "SHEL.L", "VWCE.DE", "THYAO.IS", "BTC-USD", "GC=F")


def _frame_records(obj: Any) -> Any:
    import pandas as pd

    if isinstance(obj, pd.DataFrame):
        # Index'i to_json'a birakmak tz bilgisini UTC'ye duzler ve yerel
        # seans tarihi testi anlamini yitirir; bu yuzden ayri yazilir.
        records = []
        for idx, row in zip(obj.index, obj.to_dict("records"), strict=True):
            entry: dict[str, Any] = {"index": str(idx)}
            entry.update({k: (None if nz.is_missing(v) else v) for k, v in row.items()})
            records.append(entry)
        return records
    if isinstance(obj, pd.Series):
        return [
            {"index": str(idx), "value": None if nz.is_missing(val) else val}
            for idx, val in obj.items()
        ]
    return obj


def _capture_optional(ticker: Any, getter: str) -> Any:
    """404 ve ayristirma hatasi VERI YOKLUGUDUR; fixture `null` tasir.

    Yakalama betigi tek bir egzotik sembolde patlamamalidir: ^GSPC'de 16
    dataset'in TAMAMI 404 doner ve bu beklenen durumdur (AH S8.2).
    """
    try:
        return _frame_records(getattr(ticker, getter)())
    except Exception as exc:  # noqa: BLE001 - yakalama betigi, hat degil
        print(f"  {getter}: veri yok ({type(exc).__name__})")
        return None


def _capture_funds(ticker: Any) -> Any:
    """Fon olmayan sembolde ham `KeyError('topHoldings')` firlar."""
    try:
        # `_read` ZORUNLUDUR: `quote_type` yfinance 1.7.0'da @property
        # tasimiyor, kardes dokuz alan tasiyor (canli olculdu).
        collected = _collect(ticker.get_funds_data())
        return {
            key: (_frame_records(value) if hasattr(value, "columns") else value)
            for key, value in collected.items()
        }
    except Exception as exc:  # noqa: BLE001
        print(f"  funds_data: fon degil ({type(exc).__name__})")
        return None


def capture(symbol: str) -> dict[str, Any]:
    ticker = yf.Ticker(symbol)
    out: dict[str, Any] = {}

    out["isin"] = ticker.get_isin()
    out["info"] = dict(ticker.get_info())
    fast = ticker.get_fast_info()
    out["fast_info"] = {k: fast[k] for k in fast}
    out["history_metadata"] = dict(ticker.get_history_metadata())
    # Fixture boyutunu makul tutmak icin son 400 seans
    out["history"] = _frame_records(
        ticker.history(period="2y", interval="1d", auto_adjust=False, actions=True)
    )
    out["dividends"] = _frame_records(ticker.get_dividends(period="max"))
    out["splits"] = _frame_records(ticker.get_splits(period="max"))
    out["capital_gains"] = _frame_records(ticker.get_capital_gains(period="max"))
    shares = ticker.get_shares_full(start="1970-01-01")
    out["shares_full"] = None if shares is None else _frame_records(shares)
    out["news"] = yf.Ticker(symbol).get_news(
        count=get_settings().yf_news_count, tab=get_settings().yf_news_tab
    )

    # --- financials ---
    for name, method, freq in STATEMENT_SPECS:
        frame = getattr(ticker, method)(pretty=False, freq=freq)
        out[name] = _frame_records(frame)
    # Kolon etiketleri ('Current', 'M/D/YYYY') fixture'da HAM birakilir:
    # tarihe cevrim dataset'in isidir ve test onu dogrulamalidir.
    for name, freq in VALUATION_SPECS:
        out[name] = _frame_records(ticker.get_valuation_measures(freq=freq, periods=None))
    out["calendar"] = dict(ticker.get_calendar() or {})
    # Sayfalama TAZE Ticker ister (base.py:637 onbellegi offset'i yok sayar)
    earnings = yf.Ticker(symbol).get_earnings_dates(limit=100, offset=0)
    # ISO string yalnizca OFSETI korur; tz ADI ayrica saklanir, aksi halde
    # "THYAO'da bile America/New_York" bulgusu fixture'da kaybolur
    out["earnings_dates"] = {
        "tz": str(getattr(earnings, "index", None).tz) if earnings is not None else None,
        "records": _frame_records(earnings),
    }
    sec = ticker.get_sec_filings()
    out["sec_filings"] = sec if isinstance(sec, list) else []

    # AH: analist + sahiplik. AYNI Ticker kullanilir; taze bir Ticker
    # kurmak maliyeti 7 istekten 16'ya cikarirdi (AH S4.4).
    for getter in (*ANALYSIS_GETTERS, *HOLDERS_GETTERS):
        name = getter.removeprefix("get_")
        out[name] = _capture_optional(ticker, getter)
    out["funds_data"] = _capture_funds(ticker)
    return out


def capture_bars(symbol: str) -> dict[str, Any]:
    """price_bars fixture'i: cerceve + tradingPeriods (PB S9.1).

    1m FIXTURE'I 30 GUN SONRA YENIDEN YAKALANAMAZ; yakalananlar repoya
    girer. Burada 5m kullanilir cunku ayni is_extended mantigini test eder
    ve penceresi 59 gundur.
    """
    ticker = yf.Ticker(symbol)
    frame = ticker.history(
        period="5d",
        interval="5m",
        auto_adjust=False,
        actions=True,
        prepost=True,
        repair=False,
    )
    metadata = ticker.get_history_metadata()
    periods = metadata.get("tradingPeriods")
    return {
        "bars_5m": {
            "frame": _frame_records(frame),
            "trading_periods": _frame_records(periods),
            # Kurala GIRMEZ; SHEL.L/VWCE.DE'de False oldugu halde ek bar
            # geldigini BELGELEMEK icin saklanir (PB S4.5/1).
            "has_pre_post_market_data": metadata.get("hasPrePostMarketData"),
            "exchange_timezone": metadata.get("exchangeTimezoneName"),
        }
    }


def capture_weekly(symbol: str) -> dict[str, Any]:
    """1wk/1mo fixture'i: is_extended DAIMA 0 olmali (PB S6.4 kural 1)."""
    ticker = yf.Ticker(symbol)
    out: dict[str, Any] = {}
    for name, interval in (("bars_1wk", "1wk"), ("bars_1mo", "1mo")):
        frame = ticker.history(
            period="2y", interval=interval, auto_adjust=False, actions=True, repair=False
        )
        out[name] = {
            "frame": _frame_records(frame),
            "trading_periods": None,
            "has_pre_post_market_data": None,
            "exchange_timezone": str(getattr(frame.index, "tz", None)),
        }
    return out


def capture_market() -> dict[str, Any]:
    """Piyasa fixture'lari: US disi 7 bolgede status None beklenir."""
    from yfinance import Calendars, Market

    out: dict[str, Any] = {}
    market = Market("US")
    out["market_status"] = dict(market.status or {})
    out["market_summary"] = {k: dict(v) for k, v in (market.summary or {}).items()}
    calendars = Calendars()
    out["earnings_calendar"] = _frame_records(
        calendars.get_earnings_calendar(limit=100, offset=0, filter_most_active=False)
    )
    out["economic_calendar"] = _frame_records(calendars.get_economic_events_calendar(limit=100))
    out["ipo_calendar"] = _frame_records(calendars.get_ipo_info_calendar(limit=100))
    out["splits_calendar"] = _frame_records(calendars.get_splits_calendar(limit=100))
    return out


# --- domain (sektor / endustri) fixture'lari (SI S9.1) --------------------
#
# Her referans anahtar TEK BASINA bir kenar durumun kanitidir:
DOMAIN_SECTOR_FIXTURES: tuple[tuple[str, str], ...] = (
    # Tam yanit: 12 endustri, 10 ETF + 10 fon, 4 rapor, performance + benchmark
    ("technology", "US"),
    # 6/6 endustri anahtari kutuphane sabitinden FARKLI -- anahtar kaynagi
    # kuralinin cekirdek kaniti
    ("utilities", "US"),
    # Fon sembolu 0P0001WO1I (Morningstar kimligi, ticker degil) -> is_known=0
    ("healthcare", "US"),
    # companiesCount ust siniri (1517) -- INTEGER yeterliliginin kaniti
    ("financial-services", "US"),
    # Bolge kapsami: topETFs BOS, topCompanies tamamen farkli;
    # overview/performance US ile birebir ayni
    ("technology", "GB"),
)
DOMAIN_INDUSTRY_FIXTURES: tuple[tuple[str, str], ...] = (
    # Iki mover listesi dolu, sectorKey bagi, industriesCount YOK
    ("semiconductors", "US"),
    # UC liste blogunun HICBIRI yok (companiesCount=1)
    ("infrastructure-operations", "US"),
    # ytdReturn = 9999.0 sentinel degil
    ("biotechnology", "US"),
    # Ayni sembol iki listede birden; growthEstimate / name eksikleri
    ("pharmaceutical-retailers", "US"),
    # growthEstimate ust ucu
    ("electronic-components", "US"),
    # topPerforming'te name eksik
    ("gold", "US"),
)


def capture_domain() -> None:
    """Ham JSON zarflarini ({"data": {...}}) fixture olarak kaydeder.

    Zarf OLDUGU GIBI saklanir: `fetch_domain` `payload["data"]` okur ve
    testler ayni yoldan gecmelidir -- fixture zaten acilmis olsaydi
    `KeyError('data')` -> DATA -> failed yolu hic sinanamazdi.
    """
    from yfin.datasets.domain.common import _QUERY

    root = FIXTURE_ROOT / "_domain"
    specs = (
        ("sector", "sectors", DOMAIN_SECTOR_FIXTURES),
        ("industry", "industries", DOMAIN_INDUSTRY_FIXTURES),
    )
    for kind, path, entries in specs:
        target = root / kind
        target.mkdir(parents=True, exist_ok=True)
        for key, region in entries:
            payload = _yf_data().get_raw_json(
                f"{_QUERY}/{path}/{key}",
                params={
                    "formatted": "true",
                    "withReturns": "true",
                    "lang": "en-US",
                    "region": region,
                },
            )
            suffix = "" if region == "US" else f".{region}"
            file = target / f"{key}{suffix}.json"
            file.write_text(nz.canonical_json(payload), encoding="utf-8")
            print(f"_domain/{kind}/{file.name}  ({file.stat().st_size} byte)")


# --- SQ S10.1: kesif ve ekran fixture'lari -------------------------------
# Her giris TEK BASINA bir kenar durumun kanitidir; listeden bir sey
# silinirse o durumun testi sessizce ornekten yoksun kalir.
DISCOVERY_SEARCH_FIXTURES = (
    # sembollu + Crunchbase SEMBOLSUZ satir bir arada (SQ S4.1/3, K14)
    ("AAPL", "search_AAPL"),
    # `lists` dolu; ayni EQUITY tipinde 12 ve 16 anahtarli iki satir;
    # prevName + nameChangeDate (SQ S4.1/2)
    ("GC=F", "search_GC=F"),
    # 0 quote ama dolu haber + rapor: `search_quotes` bos kalirken hucre
    # `ok` (SQ S4.1/5, S9.2)
    ("Turkish Airlines", "search_Turkish-Airlines"),
    # tum bloklar bos -> kapi satiri YAZILMAZ (SQ S9.2)
    ("zzzqqxnope", "search_zzzqqxnope"),
    # CRYPTOCURRENCY: dar alan seti, sektor/endustri YOK
    ("BTC-USD", "search_BTC-USD"),
    # `lists` IKI SEKILLI: ALGO_WATCHLIST + PREDEFINED_SCREENER (SQ S4.1/6)
    ("gold", "search_lists_two_shapes"),
)

DISCOVERY_LOOKUP_FIXTURES = (
    # DAR terim: `all` tam kumeyi verir, 9 tipli lookupTotals,
    # privateCompany dahil (SQ S4.1/8, S4.1/10)
    ("BTC", "all", "lookup_BTC_all"),
    # GENIS terim: `all` ~1.000'de KIRPILIR (996 belge / 7.261 total).
    # K6'nin adaptif dalinin kaniti.
    ("GOLD", "all", "lookup_GOLD_all"),
    # Ayni terimin tipli cagrisi: industryLink/industryName YALNIZ burada
    # (SQ S4.1/9)
    ("GOLD", "equity", "lookup_GOLD_equity"),
    # total=0, hatasiz -> `empty`
    ("zzzqqxnope", "all", "lookup_zzzqqxnope"),
)

# (ekran, offset, size, dosya). Sayfa boyutu KUCUK tutulur: normalize
# testleri icin yapi yeterlidir, 250 satirlik sayfa fixture'i megabaytlik
# olurdu. `total` sayfa boyutundan BAGIMSIZ gelir, bu yuzden durma kosulu
# yine gercek degerle sinanir.
SCREEN_FIXTURES = (
    # predefined GET: 17 anahtar, metadata dolu (SQ S4.1/13)
    ("day_gainers", None, 25, "day_gainers_p0"),
    ("top_mutual_funds", None, 25, "top_mutual_funds_p0"),
    # POST: 5 anahtar, metadata YOK
    ("top_mutual_funds", 25, 25, "top_mutual_funds_p1"),
    # son sayfa: offset + len < total kosulunun bittigi yer
    ("top_mutual_funds", 1750, 250, "top_mutual_funds_last"),
    # karisik quoteType (EQUITY + ETF) tek ekranda
    ("bond_etfs", None, 25, "bond_etfs_p0"),
)


def capture_discovery() -> None:
    """`Search` ve `Lookup` ham govdelerini kaydeder (SQ S10.1).

    `include_research=True` ve `include_nav_links=True` ACIKCA verilir:
    ikisinin de varsayilani False'tur (search.py:32-34) ve verilmezse
    `researchReports` HIC gelmez -- fixture'lar sessizce eksik kalirdi
    (SQ S4.1/5).
    """
    from yfin.screens import SCREEN_KEY_MAX_LENGTH  # noqa: F401  (import kontrolu)

    target = FIXTURE_ROOT / "_discovery"
    target.mkdir(parents=True, exist_ok=True)

    for query, name in DISCOVERY_SEARCH_FIXTURES:
        search = yf.Search(
            query,
            max_results=10,
            news_count=5,
            lists_count=10,
            include_research=True,
            include_nav_links=True,
        )
        file = target / f"{name}.json"
        file.write_text(nz.canonical_json(search.response), encoding="utf-8")
        print(f"_discovery/{file.name}  ({file.stat().st_size} byte)")

    for query, lookup_type, name in DISCOVERY_LOOKUP_FIXTURES:
        # `_fetch_lookup` sarmalayicinin KENDI metodudur (K7 korunur) ve
        # `_parse_response`in attigi `lookupTotals` + `total` alanlarini
        # tasiyan tek yoldur (lookup.py:96-104).
        payload = yf.Lookup(query)._fetch_lookup(lookup_type, 1000)
        file = target / f"{name}.json"
        file.write_text(nz.canonical_json(payload), encoding="utf-8")
        print(f"_discovery/{file.name}  ({file.stat().st_size} byte)")


def capture_screen() -> None:
    """`yf.screen` yanitlarini kaydeder (SQ S10.1).

    SQ K12: ILK sayfa `count`, sonrakiler `size` ile istenir. `offset` ile
    `count` gonderilseydi Yahoo onu SESSIZCE yok sayip 25 satir dondururdu.
    """
    from yfin.screens import screen_by_key

    target = FIXTURE_ROOT / "_screen"
    target.mkdir(parents=True, exist_ok=True)

    for key, offset, size, name in SCREEN_FIXTURES:
        spec = screen_by_key(key)
        if offset is None:
            payload = yf.screen(
                key, count=size, sortField=spec.sort_field, sortAsc=spec.sort_asc
            )
        else:
            payload = yf.screen(
                key, offset=offset, size=size, sortField=spec.sort_field, sortAsc=spec.sort_asc
            )
        file = target / f"{name}.json"
        file.write_text(nz.canonical_json(payload), encoding="utf-8")
        print(f"_screen/{file.name}  ({file.stat().st_size} byte)")

    # Custom ekran: ILK sayfa da POST'tur ve metadata GELMEZ (SQ S4.1/13).
    spec = screen_by_key("tr_equity")
    assert spec.query is not None
    payload = yf.screen(
        spec.query, size=25, sortField=spec.sort_field, sortAsc=spec.sort_asc
    )
    file = target / "tr_equity_p0.json"
    file.write_text(nz.canonical_json(payload), encoding="utf-8")
    print(f"_screen/{file.name}  ({file.stat().st_size} byte)")


def _yf_data() -> Any:
    from yfinance.data import YfData

    return YfData()


def _write(target: Path, data: dict[str, Any]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for dataset, payload in data.items():
        path = target / f"{dataset}.json"
        path.write_text(nz.canonical_json(payload), encoding="utf-8")
        print(f"{target.name}/{dataset}.json  ({path.stat().st_size} byte)")


def main(symbols: list[str]) -> None:
    if symbols == ["_market"]:
        _write(FIXTURE_ROOT / "_market", capture_market())
        return
    if symbols and symbols[0] == "--domain":
        capture_domain()
        return
    if symbols and symbols[0] == "--discovery":
        capture_discovery()
        return
    if symbols and symbols[0] == "--screen":
        capture_screen()
        return
    if symbols and symbols[0] == "--bars":
        # Yalniz bar fixture'lari: tum dataset'leri yeniden cekmeden
        # (12 sembol x 7 istek) 7 istekle isi bitirir.
        for symbol in symbols[1:] or list(BAR_FIXTURE_SYMBOLS):
            _write(FIXTURE_ROOT / symbol, capture_bars(symbol))
        _write(FIXTURE_ROOT / "AAPL", capture_weekly("AAPL"))
        return
    for symbol in symbols or list(REFERENCE_SYMBOLS):
        _write(FIXTURE_ROOT / symbol, capture(symbol))


if __name__ == "__main__":
    main(sys.argv[1:])

"""price_bars normalizasyonu ve is_extended (PB S6.4, S8.3).

Gercek fixture'larla kosar; agsiz ve DB'siz. Her sembol tek basina bir
kenar durumun kanitidir - fixture'lardaki olculmus dagilim:

    AAPL      849 bar, 522'si seans disi   (hasPrePost=True)
    SHEL.L    503 bar,   5'i seans disi    (hasPrePost=FALSE)
    VWCE.DE   504 bar,   8'i seans disi    (hasPrePost=FALSE)
    THYAO.IS  479 bar,   0                 (pre/post dejenere)
    BTC-USD  1322 bar,   0                 (7/24)
    GC=F     1232 bar,   0                 (bar 18:10'da acilir)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from helpers import as_frame, load_fixture
from yfin.datasets.bars import BarPayload, normalize_bars


def _payload(symbol: str, dataset: str = "bars_5m", interval: str = "5m") -> BarPayload:
    raw = load_fixture(symbol, dataset)
    periods = raw.get("trading_periods")
    return BarPayload(
        frame=as_frame(raw["frame"]),
        trading_periods=as_frame(periods) if periods else None,
        interval=interval,
    )


def _rows(symbol: str, dataset: str = "bars_5m", interval: str = "5m") -> list[dict[str, Any]]:
    result = normalize_bars(_payload(symbol, dataset, interval), symbol)
    assert len(result.writes) >= 1
    return result.writes[0].rows


# --- is_extended ----------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "expected_extended"),
    [("AAPL", 522), ("SHEL.L", 5), ("VWCE.DE", 8), ("THYAO.IS", 0), ("BTC-USD", 0), ("GC=F", 0)],
)
def test_extended_bar_counts_match_the_measured_fixtures(
    symbol: str, expected_extended: int
) -> None:
    rows = _rows(symbol)
    assert sum(1 for r in rows if r["is_extended"]) == expected_extended


def test_shell_l_regression_extended_bars_despite_has_prepost_false() -> None:
    """SILINEN "kapi 1"in geri gelmesini engelleyen test.

    SHEL.L hasPrePostMarketData=False bildirir ama 16:30/16:35 barlari
    doner (regular seans 08:00-16:30). has_pre_post_market_data'ya dayanan
    bir erken cikis bunlari NORMAL SEANS sayar ve v_price_bars_regular'a
    sokar - yani view'in onlemek icin var oldugu bozulmanin ta kendisi.
    """
    rows = _rows("SHEL.L")
    extended = [r for r in rows if r["is_extended"]]

    assert extended, "hasPrePost=False diye erken cikilmis olabilir"
    assert all(r["ts_utc"].time() >= datetime(2026, 1, 1, 15, 30).time() for r in extended)


def test_thyao_degenerate_pre_post_columns_do_not_mark_everything_extended() -> None:
    """THYAO'da dejenere olan pre_*/post_* kolonlaridir, start/end DEGIL.

    tradingPeriods: pre=09:30-09:30, reg=09:30-18:00, post=18:00-18:00.
    Kural yalniz start/end'e baktigi icin tum barlar normal seanstir.
    """
    rows = _rows("THYAO.IS")

    assert not any(r["is_extended"] for r in rows)
    assert len(rows) == 479


def test_multiday_intervals_carry_no_extended_column() -> None:
    """1wk/1mo'da kavram anlamsizdir (PB S6.4 kural 1).

    Eskiden bu satirlar `is_extended=False` tasiyordu. Artik KOLONU HIC
    TASIMIYORLAR: gun ustu barlar `periodic_bars`a gider ve o tabloda
    boyle bir kolon YOKTUR (PG S7.1). "Anlamsiz alani False ile
    doldurmak" yerine "alani hic olusturmamak" -- kavram semada da yok.
    """
    for dataset, interval in (("bars_1wk", "1wk"), ("bars_1mo", "1mo")):
        rows = _rows("AAPL", dataset, interval)
        assert rows
        assert all("is_extended" not in r for r in rows)


def test_missing_trading_periods_defaults_to_not_extended() -> None:
    """Guvenli varsayilan 0'dir: bilinmeyen bari seans disi saymak onu
    v_price_bars_regular'dan GIZLERDI; ters hata daha gorunurdur."""
    payload = BarPayload(frame=_payload("AAPL").frame, trading_periods=None, interval="5m")

    rows = normalize_bars(payload, "AAPL").writes[0].rows

    assert not any(r["is_extended"] for r in rows)


def test_trading_periods_without_pre_post_columns_is_accepted() -> None:
    """prepost=False ile cekilen tradingPeriods yalniz start/end tasir;
    pre_start'a erisen kod KeyError verirdi (PB S4.5/3)."""
    payload = _payload("SHEL.L")
    assert payload.trading_periods is not None
    trimmed = payload.trading_periods[["start", "end"]]

    rows = (
        normalize_bars(
            BarPayload(frame=payload.frame, trading_periods=trimmed, interval="5m"), "SHEL.L"
        )
        .writes[0]
        .rows
    )

    assert sum(1 for r in rows if r["is_extended"]) == 5


# --- normalizasyon --------------------------------------------------------


def test_local_date_is_the_local_calendar_day_not_the_utc_day() -> None:
    """GC=F seansi aksam acilir ve UTC gece yarisini GECER.

    America/New_York -04:00 oldugu icin yerel 20:00 bari UTC'de ERTESI
    GUNE duser. local_date'i ts_utc'den turetmek o barin gunu bir ileri
    kaydirirdi; kaynak index zaten yerel tz tasidigi icin tarih ondan
    alinir (S5.4'un ayni dersi, ters yonde).
    """
    rows = _rows("GC=F")
    shifted = [r for r in rows if r["local_date"] != r["ts_utc"].date()]

    assert shifted, "UTC gunune tasan bar bulunamadi - fixture beklenmedik"
    for row in shifted:
        # UTC gunu yerel gunun BIR ILERISI olmali
        assert (row["ts_utc"].date() - row["local_date"]).days == 1
        assert row["ts_utc"].hour < 5


def test_ts_utc_is_naive_utc() -> None:
    """Kolon timestamptz(6); cevrim normalize'da yapilir ve UTC-aware doner."""
    rows = _rows("AAPL")

    assert all(r["ts_utc"].tzinfo is UTC for r in rows)
    assert isinstance(rows[0]["local_date"], date)


def test_write_targets_price_bars_with_the_three_column_key() -> None:
    result = normalize_bars(_payload("AAPL"), "AAPL")
    write = result.writes[0]

    assert write.table == "price_bars"
    assert write.key_columns == ("symbol", "bar_interval", "ts_utc")
    assert "is_extended" in write.update_columns
    assert write.monotonic_columns == ()


def test_derived_action_columns_are_not_written() -> None:
    """dividend/split_ratio/capital_gain TUREVDIR; otorite dividends,
    splits, capital_gains tablolaridir (PB K3). adj_close da yazilmaz:
    kalici arsivde bayatlar."""
    rows = _rows("AAPL")

    for banned in ("dividend", "split_ratio", "capital_gain", "adj_close", "is_repaired"):
        assert banned not in rows[0], f"{banned} price_bars'a yazilmamali"


def test_rows_carry_the_interval() -> None:
    rows = _rows("BTC-USD")

    assert {r["bar_interval"] for r in rows} == {"5m"}
    assert {r["symbol"] for r in rows} == {"BTC-USD"}


def test_row_without_close_is_dropped() -> None:
    """close NOT NULL; kapanissiz bar anlamsizdir."""
    payload = _payload("AAPL")
    frame = payload.frame.copy()
    frame.iloc[0, frame.columns.get_loc("Close")] = None

    rows = (
        normalize_bars(
            BarPayload(frame=frame, trading_periods=payload.trading_periods, interval="5m"), "AAPL"
        )
        .writes[0]
        .rows
    )

    assert len(rows) == len(payload.frame) - 1


def test_negative_volume_becomes_null() -> None:
    payload = _payload("AAPL")
    frame = payload.frame.copy()
    frame.iloc[0, frame.columns.get_loc("Volume")] = -5

    rows = (
        normalize_bars(
            BarPayload(frame=frame, trading_periods=payload.trading_periods, interval="5m"), "AAPL"
        )
        .writes[0]
        .rows
    )

    assert rows[0]["volume"] is None


def test_prices_are_decimal_not_float() -> None:
    rows = _rows("AAPL")

    assert isinstance(rows[0]["close"], Decimal)


def test_empty_frame_yields_empty_result() -> None:
    payload = BarPayload(frame=pd.DataFrame(), trading_periods=None, interval="5m")

    assert normalize_bars(payload, "AAPL").is_empty


def test_unknown_interval_is_rejected() -> None:
    payload = _payload("AAPL")

    with pytest.raises(ValueError, match="bilinmeyen interval"):
        normalize_bars(
            BarPayload(frame=payload.frame, trading_periods=None, interval="3mo"), "AAPL"
        )

"""Pencere planlayici (PB S6.2).

Bu fonksiyon tasarimin veri kaybina karsi ILK savunma hattidir; agsiz ve
DB'siz oldugu icin de en ucuz test edilen parcasidir.

Olculmus sinirlar (PB S4.3): 1m istek basina 8 gun / 30 gun derinlik,
5m-15m 59, 60m 729. `BAR_LIMITS` bu KABUL EDILMIS degerleri tasir - 9, 60
ve 730 reddedildi - yani tampon zaten sabitlerin icindedir ve planlayici
uzerine IKINCI bir tampon uygulamaz.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yfin.datasets.bars import BAR_LIMITS, plan_windows

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _span_days(window: tuple[object, object]) -> int:
    start, end = window
    return (end - start).days  # type: ignore[operator]


def test_first_fill_1m_is_four_slices_none_exceeding_the_request_limit() -> None:
    """30 gun derinlik / 8 gun pencere -> 4 dilim."""
    plan = plan_windows("1m", None, NOW)

    assert len(plan.windows) == 4
    assert all(_span_days(w) <= 8 for w in plan.windows)
    assert plan.gap is None
    # Dilimler bitisik ve derinligi tam kapsamali
    assert plan.windows[0][0] == (NOW - timedelta(days=29)).date()
    assert plan.windows[-1][1] == NOW.date()
    for earlier, later in zip(plan.windows, plan.windows[1:], strict=False):
        assert earlier[1] == later[0], "dilimler bitisik degil - arada gun kayboluyor"


@pytest.mark.parametrize("interval", ["5m", "15m"])
def test_first_fill_59_day_intervals_are_single_slice(interval: str) -> None:
    plan = plan_windows(interval, None, NOW)

    assert len(plan.windows) == 1
    assert _span_days(plan.windows[0]) == 59
    assert plan.gap is None


def test_first_fill_60m_is_single_729_day_slice() -> None:
    plan = plan_windows("60m", None, NOW)

    assert len(plan.windows) == 1
    assert _span_days(plan.windows[0]) == 729


@pytest.mark.parametrize("interval", ["1wk", "1mo"])
def test_first_fill_multiday_intervals_have_no_windows(interval: str) -> None:
    """Sinirsiz interval'lerde ilk dolum period='max' ile yapilir."""
    plan = plan_windows(interval, None, NOW)

    assert plan.windows == ()
    assert plan.gap is None


def test_normal_incremental_is_a_single_slice() -> None:
    watermark = NOW - timedelta(days=1)

    plan = plan_windows("1m", watermark, NOW, overlap_days=2)

    assert len(plan.windows) == 1
    assert plan.windows[0][0] == (watermark - timedelta(days=2)).date()
    assert plan.gap is None


def test_ten_day_pause_is_split_into_two_slices() -> None:
    """Bu testin varlik sebebi olculmus bir hatadir.

    Naif `start = watermark - overlap` 12 gunluk bir pencere ister; Yahoo
    bunu 8 gun sinirini astigi icin YFPricesMissingError ile reddeder ve
    TUM dilim kaybedilir. Yani duraklama, sessizce veri kaybina donusur.
    """
    watermark = NOW - timedelta(days=10)

    plan = plan_windows("1m", watermark, NOW, overlap_days=2)

    assert len(plan.windows) == 2
    assert all(_span_days(w) <= 8 for w in plan.windows)
    assert plan.gap is None


def test_pause_beyond_retention_records_an_unrecoverable_gap() -> None:
    """35 gun duraklama: ilk 5 gun Yahoo'da ARTIK YOK."""
    watermark = NOW - timedelta(days=35)

    plan = plan_windows("1m", watermark, NOW)

    assert plan.gap is not None
    gap_start, gap_end = plan.gap
    assert gap_start == watermark
    # Bosluk, cekilebilen en eski noktaya kadar uzanir; arada
    # KAYDEDILMEYEN gun kalmamalidir
    assert gap_end.date() == plan.windows[0][0]
    assert all(_span_days(w) <= 8 for w in plan.windows)


def test_open_gaps_are_retried_while_still_inside_the_window() -> None:
    """bar_gaps yalnizca bir mezar tasi degil, GOREV LISTESIDIR.

    Watermark bosluğun otesine gectiginde planlayici onu bir daha hic
    istemezdi; acik bosluk yeniden dilimlenmeli.
    """
    watermark = NOW - timedelta(days=1)
    gap = (NOW - timedelta(days=12), NOW - timedelta(days=11))

    plan = plan_windows("1m", watermark, NOW, overlap_days=2, open_gaps=[gap])

    covered = [w for w in plan.windows if w[0] <= gap[0].date() < w[1]]
    assert covered, f"acik bosluk yeniden denenmedi: {plan.windows}"


def test_open_gap_outside_retention_is_not_retried() -> None:
    """Penceresi kapanmis bosluk bosuna istek uretmemeli."""
    watermark = NOW - timedelta(days=1)
    gap = (NOW - timedelta(days=40), NOW - timedelta(days=39))

    plan = plan_windows("1m", watermark, NOW, overlap_days=2, open_gaps=[gap])

    assert all(w[0] >= (NOW - timedelta(days=29)).date() for w in plan.windows)


def test_no_window_ever_starts_at_the_retention_boundary() -> None:
    """CANLI KOSUDA YAKALANAN HATANIN REGRESYONU.

    BAR_LIMITS["1m"] onceden (8, 30) idi; planlayici ilk dilimi TAM
    sinirda baslatti ve Yahoo YFPricesMissingError dondurdu - AAPL'in
    tum 1m ilk dolumu dustu. Olculdu: -30g RED, -29g 1950 bar.

    Bu test her interval icin en eski dilimin, Yahoo'nun ILAN ETTIGI
    sinirdan en az bir gun iceride kaldigini dogrular.
    """
    announced = {"1m": 30, "5m": 60, "15m": 60, "60m": 730}
    for interval, limit in announced.items():
        plan = plan_windows(interval, None, NOW)
        oldest = plan.windows[0][0]
        assert oldest > (NOW - timedelta(days=limit)).date(), (
            f"{interval}: dilim tam sinirda basliyor, Yahoo reddedecek"
        )


def test_explicit_range_overrides_watermark_and_produces_no_gap() -> None:
    """--start/--end elle geriye donuk cekimdir; derinlik asimi bir
    'kacirma' DEGILDIR, kullanici hatasidir ve gap uretmez (PB S6.2)."""
    watermark = NOW - timedelta(days=1)
    start = (NOW - timedelta(days=20)).date()
    end = (NOW - timedelta(days=5)).date()

    plan = plan_windows("1m", watermark, NOW, start=start, end=end)

    assert plan.gap is None
    assert plan.windows[0][0] == start
    assert plan.windows[-1][1] == end
    assert all(_span_days(w) <= 8 for w in plan.windows)


def test_explicit_range_ignores_open_gaps() -> None:
    watermark = NOW - timedelta(days=1)
    gap = (NOW - timedelta(days=12), NOW - timedelta(days=11))

    plan = plan_windows(
        "1m",
        watermark,
        NOW,
        start=(NOW - timedelta(days=3)).date(),
        end=NOW.date(),
        open_gaps=[gap],
    )

    assert len(plan.windows) == 1
    assert plan.windows[0][0] == (NOW - timedelta(days=3)).date()


def test_up_to_date_watermark_still_asks_for_the_overlap() -> None:
    """Ortusme idempotenttir (PB S4.1/5) ve seans sinirindaki barin
    kacmasini onler; watermark 'simdi' olsa bile pencere bos kalmamali."""
    plan = plan_windows("5m", NOW, NOW, overlap_days=2)

    assert len(plan.windows) == 1
    assert plan.windows[0][0] == (NOW - timedelta(days=2)).date()


def test_unknown_interval_is_rejected() -> None:
    with pytest.raises(ValueError, match="bilinmeyen interval"):
        plan_windows("3mo", None, NOW)


def test_bar_limits_match_the_measured_values() -> None:
    """Sabitler PB S4.3'teki OLCULEN kabul degerleridir."""
    # 29, 30 DEGIL: tam 30 gun canli olcumde REDDEDILDI (bkz. bars.py).
    assert BAR_LIMITS["1m"] == (8, 29)
    assert BAR_LIMITS["5m"] == (59, 59)
    assert BAR_LIMITS["15m"] == (59, 59)
    assert BAR_LIMITS["60m"] == (729, 729)
    assert BAR_LIMITS["1wk"] == (None, None)
    assert BAR_LIMITS["1mo"] == (None, None)


def test_distant_open_gap_becomes_its_own_slice_not_a_giant_span() -> None:
    """Uzak bir acik bosluk, aradaki TUM gunleri yeniden cektirmemeli.

    Naif cozum min(watermark, gap)'ten bugune kadar dilimler: 25 gun
    onceki bir bosluk icin 4 istek uretir ve bunlarin ucu zaten yazilmis
    veriyi tekrar ceker. Bosluk ayri bir dilim olmali.
    """
    watermark = NOW - timedelta(days=1)
    gap = (NOW - timedelta(days=25), NOW - timedelta(days=24))

    plan = plan_windows("1m", watermark, NOW, overlap_days=2, open_gaps=[gap])

    assert len(plan.windows) == 2, f"bosluk ayri dilim olmali: {plan.windows}"
    assert plan.windows[0][0] <= gap[0].date() < plan.windows[0][1]
    assert plan.windows[1][0] == (watermark - timedelta(days=2)).date()


def test_overlapping_gap_and_watermark_are_merged_into_one_slice() -> None:
    """Bosluk watermark penceresine bitisikse ikiye bolunmemeli."""
    watermark = NOW - timedelta(days=3)
    gap = (NOW - timedelta(days=4), NOW - timedelta(days=3))

    plan = plan_windows("1m", watermark, NOW, overlap_days=2, open_gaps=[gap])

    assert len(plan.windows) == 1

"""Geriye donuk yeniden olcekleme - saf hesap (PB S6.6).

SQL'siz: yon, tip ve sinir kurallari DB'ye dokunmadan dogrulanir.
Idempotency ve atomiklik repo testlerindedir.

Formulun YONU olculmus bir gercege dayanir: Yahoo, split sonrasi TUM
gecmisi yeniden olcekler - fiyati boler, hacmi carpar (NVDA 2024-06-10
10:1; 2024-06-05 Close=122.44 / Volume=528.402.000, gerceginde ~1224.40
ve ~52,84 M). Arsivimizdeki eski satirlar split ONCESI olcektedir; UPDATE
onlari Yahoo'nun guncel olcegine hizalar.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from yfin.rescale import RescaleSkipped, rescale_factors, split_boundary_utc


def test_forward_split_divides_price_and_multiplies_volume() -> None:
    """NVDA 10:1 - olculmus yon."""
    price_factor, volume_factor = rescale_factors(Decimal(10))

    assert Decimal("1224.40") * price_factor == Decimal("122.440")
    assert 52_840_210 * volume_factor == Decimal("528402100")


def test_reverse_split_uses_the_same_formula() -> None:
    """ratio < 1'de bolme carpmaya doner; ozel dal YOKTUR."""
    price_factor, volume_factor = rescale_factors(Decimal("0.1"))

    assert Decimal("12.244") * price_factor == Decimal("122.44")
    assert Decimal("528402100") * volume_factor == Decimal("52840210.0")


def test_fractional_ratio_stays_decimal() -> None:
    """3:2 split ratio=1.5; float bolme milyonlarca satirda birikimli
    sapma uretir (PB K4)."""
    price_factor, _ = rescale_factors(Decimal("1.5"))
    result = Decimal("150") * price_factor

    assert isinstance(result, Decimal)
    assert result == Decimal(100)


@pytest.mark.parametrize("bad", [Decimal(0), Decimal("-2")])
def test_non_positive_ratio_is_rejected(bad: Decimal) -> None:
    """splits'te bozuk bir 0 satiri ERROR_FOR_DIVISION_BY_ZERO uretir ve
    TUM sembolu dusururdu (PB S6.6/3)."""
    with pytest.raises(RescaleSkipped):
        rescale_factors(bad)


# --- split siniri ---------------------------------------------------------


def test_split_boundary_is_local_midnight_in_utc() -> None:
    """AAPL: America/New_York -04:00 -> yerel 00:00 = UTC 04:00."""
    boundary = split_boundary_utc(date(2026, 6, 10), "America/New_York")

    assert boundary == datetime(2026, 6, 10, 4, 0)


def test_positive_offset_exchange_shifts_the_other_way() -> None:
    """BIST +03: yerel 00:00 = ONCEKI gunun UTC 21:00'i.

    Ham UTC gece yarisi alinsaydi split gunu 00:00-03:00 arasindaki
    barlar (BIST'te seans yok ama kripto/vadelide var) yanlis tarafta
    kalirdi.
    """
    boundary = split_boundary_utc(date(2026, 6, 10), "Europe/Istanbul")

    assert boundary == datetime(2026, 6, 9, 21, 0)


def test_dst_transition_is_handled_by_zoneinfo() -> None:
    """Kis saatinde ofset -05:00'e doner."""
    assert split_boundary_utc(date(2026, 1, 15), "America/New_York") == datetime(2026, 1, 15, 5, 0)


def test_unknown_timezone_is_skipped_not_guessed() -> None:
    """tz bilinmiyorsa UTC VARSAYILMAZ: yanlis sinirla olceklemek,
    hic olceklememekten daha kotudur - sonucu geri alinamaz."""
    with pytest.raises(RescaleSkipped):
        split_boundary_utc(date(2026, 6, 10), None)
    with pytest.raises(RescaleSkipped):
        split_boundary_utc(date(2026, 6, 10), "EDT")


def test_zoneinfo_accepts_the_iana_names_we_store() -> None:
    """history_metadata.exchange_timezone_name gercekten IANA adi olmali;
    `timezone` kolonu ("EDT"/"TRT") KULLANILAMAZ."""
    for name in ("America/New_York", "Europe/Istanbul", "Europe/London", "UTC"):
        assert ZoneInfo(name) is not None

"""Rescale'in DB davranisi: tohum, idempotency, kapsam (PB S6.6, S9.2).

Bu dosyadaki ilk test tasarimin EN KRITIK regresyonudur: `--seed`
atlandiginda arsivin bozuldugunu ve tohumlandiginda bozulmadigini AYNI
senaryoda gosterir.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from yfin.models import PriceBar, Split, Symbol
from yfin.rescale import (
    apply_pending,
    pending_splits,
    seed_baseline,
    unseeded_historic_splits,
)

pytestmark = pytest.mark.repo

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
SPLIT_DAY = date(2026, 6, 10)


def _setup(session: Session, *, interval: str = "5m", ratio: str = "10") -> None:
    """AAPL: bir split + split ONCESI ve SONRASI birer bar."""
    session.add(Symbol(symbol="AAPL", is_active=True))
    session.flush()
    session.execute(
        text(
            "INSERT INTO history_metadata (symbol, exchange_timezone_name, raw_json,"
            " content_hash, fetched_at) VALUES"
            " ('AAPL','America/New_York','{}','h',:now)"
        ),
        {"now": NOW},
    )
    session.add(Split(symbol="AAPL", split_date=SPLIT_DAY, ratio=Decimal(ratio)))
    for ts, close, volume in (
        (datetime(2026, 6, 5, 14, 30), Decimal("1224.40"), 52_840_210),  # split ONCESI
        (datetime(2026, 6, 15, 14, 30), Decimal("130.00"), 500_000),  # split SONRASI
    ):
        session.add(
            PriceBar(
                symbol="AAPL",
                bar_interval=interval,
                ts_utc=ts,
                local_date=ts.date(),
                open=close,
                high=close,
                low=close,
                close=close,
                volume=volume,
                is_extended=False,
            )
        )
    session.flush()


def _bar(session: Session, ts: datetime, interval: str = "5m") -> PriceBar:
    return session.get(PriceBar, ("AAPL", interval, ts))  # type: ignore[return-value]


def test_seed_prevents_the_first_run_from_destroying_the_archive(db_session: Session) -> None:
    """TASARIMIN EN KRITIK REGRESYONU.

    splits tablosu mevcut hat tarafindan zaten doludur; bos bir
    bar_rescales ile yapilan ilk kosu TARIHSEL split'leri uygular ve
    Yahoo'dan zaten guncel olcekte gelmis barlari bir kez daha boler.
    """
    _setup(db_session)

    seeded = seed_baseline(db_session)
    assert seeded == 1
    applied = apply_pending(db_session, "AAPL")

    assert applied == 0, "tohumlanmis split yeniden uygulanmis"
    assert _bar(db_session, datetime(2026, 6, 5, 14, 30)).close == Decimal("1224.40")


def test_split_inside_the_archive_is_applied_even_without_seed(db_session: Session) -> None:
    """Split arsivin ICINDE (en erken bardan SONRA) ise uygulanir.

    Tohum olmasa bile dogru davranis budur: 06-05 bari split'ten once
    yazildi, yani split ONCESI olcektedir ve hizalanmasi gerekir.
    """
    _setup(db_session)

    applied = apply_pending(db_session, "AAPL")

    assert applied == 1
    assert _bar(db_session, datetime(2026, 6, 5, 14, 30)).close == Decimal("122.440000000000")


def test_split_older_than_the_archive_is_never_applied(db_session: Session) -> None:
    """YAPISAL KORUMA: arsivden ESKI split'in yapacak isi yoktur.

    Bu, tasarimi operasyonel bir adima (`rescale --seed`) bagimli
    olmaktan cikarir. Taze bir kurulumda seed atlanirsa mekanizma
    splits'teki TUM tarihsel split'leri (AAPL'de 1987, 2000, 2005, 2014,
    2020) uygular ve Yahoo'dan zaten guncel olcekte gelmis barlari
    yeniden bolerdi - AAPL icin 224 kat.
    """
    _setup(db_session)
    # Arsivden COK ESKI bir split; hicbir bar ondan once yazilmadi
    db_session.add(Split(symbol="AAPL", split_date=date(2014, 6, 9), ratio=Decimal(7)))
    db_session.flush()

    pending = pending_splits(db_session, "AAPL")

    assert date(2014, 6, 9) not in [d for d, _r in pending], "arsivden eski split bekleyenlerde"
    apply_pending(db_session, "AAPL")
    # 2026 barlari yalniz 2026 split'inden etkilendi (10), 2014'ten degil
    assert _bar(db_session, datetime(2026, 6, 5, 14, 30)).close == Decimal("122.440000000000")


def test_symbol_without_any_bars_has_nothing_pending(db_session: Session) -> None:
    """Arsiv bossa olceklenecek bir sey de yoktur (earliest NULL)."""
    db_session.add(Symbol(symbol="MSFT", is_active=True))
    db_session.flush()  # FK: sembol split'ten ONCE var olmali
    db_session.add(Split(symbol="MSFT", split_date=date(2003, 2, 18), ratio=Decimal(2)))
    db_session.flush()

    assert pending_splits(db_session, "MSFT") == []


def test_new_split_after_the_archive_exists_is_applied_once(db_session: Session) -> None:
    """Mekanizmanin ASIL isi: arsive girdikten SONRA olan split."""
    _setup(db_session)
    seed_baseline(db_session)
    # Yeni bir split gelir (tohumdan sonra)
    new_day = date(2026, 8, 1)
    db_session.add(Split(symbol="AAPL", split_date=new_day, ratio=Decimal(2)))
    db_session.flush()

    assert apply_pending(db_session, "AAPL") == 1
    # 06-15 bari yeni split'ten ONCE -> bolunmus olmali
    assert _bar(db_session, datetime(2026, 6, 15, 14, 30)).close == Decimal("65.000000000000")
    # Ikinci kosu hicbir sey yapmamali (idempotency)
    assert apply_pending(db_session, "AAPL") == 0
    assert _bar(db_session, datetime(2026, 6, 15, 14, 30)).close == Decimal("65.000000000000")


def test_volume_is_multiplied_and_floored(db_session: Session) -> None:
    """3:2 split: volume*1.5 kesirli cikar; FLOOR olmadan MySQL onu
    SESSIZCE yuvarlardi (PB S6.6/5)."""
    _setup(db_session, ratio="1.5")
    db_session.execute(
        text("UPDATE price_bars SET volume = 3 WHERE ts_utc = '2026-06-05 14:30:00'")
    )
    db_session.flush()
    db_session.expire_all()

    apply_pending(db_session, "AAPL")

    assert _bar(db_session, datetime(2026, 6, 5, 14, 30)).volume == 4  # 4.5 -> FLOOR


def test_weekly_and_monthly_bars_are_never_rescaled(db_session: Session) -> None:
    """1wk/1mo her kosuda period='max' ile bastan cekilir, yani daima
    guncel olcektedir. Olceklenselerdi ve o kosuda fetch dusseydi
    satirlar CIFT duzeltilmis kalirdi (PB S6.6/1)."""
    _setup(db_session, interval="1wk")

    apply_pending(db_session, "AAPL")

    assert _bar(db_session, datetime(2026, 6, 5, 14, 30), "1wk").close == Decimal("1224.40")


def test_zero_ratio_is_skipped_and_not_recorded(db_session: Session) -> None:
    """Bozuk bir 0 satiri ERROR_FOR_DIVISION_BY_ZERO ile TUM sembolu
    dusururdu. Kayit da yazilmaz: yazilsaydi split 'uygulandi' sayilir ve
    duzeltilmis bir ratio bir daha islenmezdi (PB S6.6/3)."""
    _setup(db_session, ratio="0")

    assert apply_pending(db_session, "AAPL") == 0
    assert _bar(db_session, datetime(2026, 6, 5, 14, 30)).close == Decimal("1224.40")
    assert pending_splits(db_session, "AAPL"), "atlanan split bekleyenlerden dusmemeli"


def test_missing_timezone_skips_rather_than_assuming_utc(db_session: Session) -> None:
    """tz yoksa UTC VARSAYILMAZ: yanlis sinirla olceklemek geri alinamaz."""
    _setup(db_session)
    db_session.execute(text("UPDATE history_metadata SET exchange_timezone_name = NULL"))
    db_session.flush()

    assert apply_pending(db_session, "AAPL") == 0
    assert _bar(db_session, datetime(2026, 6, 5, 14, 30)).close == Decimal("1224.40")


def test_split_boundary_uses_local_midnight(db_session: Session) -> None:
    """Sinir yerel 00:00'in UTC karsiligidir (NY icin 04:00).

    Split gununun 02:00 UTC bari (yerelde ONCEKI gun 22:00) split
    ONCESIDIR ve olceklenmelidir; ham UTC gece yarisi kullanilsaydi
    kacirilirdi.
    """
    _setup(db_session)
    edge = datetime(2026, 6, 10, 2, 0)
    db_session.add(
        PriceBar(
            symbol="AAPL",
            bar_interval="5m",
            ts_utc=edge,
            local_date=date(2026, 6, 9),
            close=Decimal(100),
            volume=1,
            is_extended=False,
        )
    )
    db_session.flush()

    apply_pending(db_session, "AAPL")

    assert _bar(db_session, edge).close == Decimal("10.000000000000")


def test_seed_is_idempotent(db_session: Session) -> None:
    _setup(db_session)

    assert seed_baseline(db_session) == 1
    assert seed_baseline(db_session) == 0


def test_unseeded_historic_split_is_reported(db_session: Session) -> None:
    """Bakim isi uyarisi: price_bars'in en erken barindan ESKI, kaydi
    olmayan split -> --seed atlanmis demektir (PB S10/9a)."""
    _setup(db_session)

    assert unseeded_historic_splits(db_session) == 0  # split, barlardan sonra

    db_session.add(Split(symbol="AAPL", split_date=date(2020, 1, 2), ratio=Decimal(4)))
    db_session.flush()

    assert unseeded_historic_splits(db_session) == 1

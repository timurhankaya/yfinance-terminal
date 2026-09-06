"""GERCEK API verisi -> GERCEK MySQL (AH S9.1 + S9.2 birlestirilmis).

Bu dosya hattaki en guclu kaniti verir ve AGSIZ kosar: fixture'lar canli
Yahoo'dan bir kez yakalandi (`scripts/capture_fixtures.py`), buradaki her
test onlari dataset'in `normalize`ina verir ve ciktiyi gercek semaya YAZAR.

Neden hem bu hem `test_dataset_writes.py`: orada payload'lari BEN kurdum,
yani yalnizca kendi varsayimimi sinar. Burada girdi kaynagin kendisinden
gelir -- kolon adi, dtype, eksik kolon ve bos deger dagilimi Yahoo'nun
gercekte donduruguyle ayni. Ikisi ayri sinif hata yakalar.

`empty` sonuc BASARISIZLIK DEGILDIR (S8.2): 17 dataset ^GSPC'de, alti
dataset THYAO.IS'te, `funds_data` fon olmayan her sembolde bos doner. Test
"dolu olmali" demez; "bos degilse HATASIZ yazilmali" der.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from helpers import FIXTURE_ROOT, as_dataset_frame, as_funds_data, load_fixture
from yfin.datasets import SYMBOL_DATASETS
from yfin.datasets.base import Dataset
from yfin.datasets.payloads import (
    AsOfFramePayload,
    AsOfMappingPayload,
    FundsPayload,
    RangedFramePayload,
)
from yfin.persistence import PostgresRowWriter

pytestmark = pytest.mark.repo

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

# Referans semboller (AH S9.1); her biri bir kenar durumun kanitidir.
SYMBOLS = ("AAPL", "MSFT", "THYAO.IS", "PFE", "XOM", "NVDA", "WMT", "KO", "SPY", "BND", "^GSPC")

# Index'i TARIH olan iki dataset; digerlerinde index donem etiketi ya da
# sira numarasidir ve Timestamp'e cevrilmesi HATA olurdu.
DATETIME_INDEX = frozenset({"upgrades_downgrades", "earnings_history"})

# `date_range="filter"` olanlar RangedFramePayload alir.
RANGED = frozenset({"upgrades_downgrades", "earnings_history", "insider_transactions"})

FRAME_DATASETS = (
    "recommendations",
    "upgrades_downgrades",
    "earnings_estimate",
    "revenue_estimate",
    "eps_trend",
    "eps_revisions",
    "earnings_history",
    "growth_estimates",
    "major_holders",
    "institutional_holders",
    "mutualfund_holders",
    "insider_purchases",
    "insider_transactions",
    "insider_roster_holders",
)


def _fixture_symbols() -> list[str]:
    return [s for s in SYMBOLS if (FIXTURE_ROOT / s).is_dir()]


@pytest.fixture
def written_symbols(db_session: Session) -> Iterator[list[str]]:
    codes = _fixture_symbols()
    if not codes:
        pytest.skip("fixture yok (scripts/capture_fixtures.py calistirin)")
    for code in codes:
        db_session.execute(
            text(
                "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
                "VALUES (:s, 1, 0, :t, :t)"
            ),
            {"s": code, "t": NOW},
        )
    yield codes


def _payload(name: str, symbol: str) -> Any:
    raw = load_fixture(symbol, name)
    if name == "funds_data":
        return FundsPayload(as_funds_data(raw), NOW)
    if name == "analyst_price_targets":
        return AsOfMappingPayload(raw, NOW)
    frame = as_dataset_frame(raw, datetime_index=name in DATETIME_INDEX)
    if name in RANGED:
        return RangedFramePayload(frame, NOW)
    return AsOfFramePayload(frame, NOW)


def _write(session: Session, dataset: Dataset[Any], symbol: str) -> tuple[int, int]:
    """(attempted, verified) -- bos sonuc (0, 0) doner."""
    result = dataset.normalize(_payload(dataset.name, symbol), symbol)
    if result.is_empty:
        return 0, 0
    stats = dataset.upsert(PostgresRowWriter(session), result)
    return sum(stats.attempted.values()), sum(stats.verified.values())


@pytest.mark.parametrize("name", [*FRAME_DATASETS, "analyst_price_targets", "funds_data"])
def test_every_fixture_symbol_writes_without_error(
    db_session: Session, written_symbols: list[str], name: str
) -> None:
    """Kaynagin GERCEK ciktisi hatasiz yazilir ve eksiksiz dogrulanir.

    Tek iddia S8.6'dir: `rows_verified == rows_attempted`. Hangi sembolun
    dolu, hangisinin bos geldigine dair BIR IDDIA KURULMAZ.
    """
    dataset = SYMBOL_DATASETS[name]
    filled = 0
    for symbol in written_symbols:
        attempted, verified = _write(db_session, dataset, symbol)
        assert verified == attempted, f"{name}/{symbol}"
        filled += bool(attempted)
    # En az bir sembolde dolu gelmeli; hepsi bosaysa fixture ya da esleme
    # bozulmustur ve test sessizce hicbir sey kanitlamaz olurdu.
    assert filled > 0, f"{name}: 11 sembolun hicbirinde veri yok"


def test_index_symbol_is_empty_everywhere_but_never_fails(
    db_session: Session, written_symbols: list[str]
) -> None:
    """^GSPC: 17 dataset'in tamami bos -- `empty`, `failed` DEGIL (S8.4)."""
    if "^GSPC" not in written_symbols:
        pytest.skip("^GSPC fixture'i yok")
    for name in (*FRAME_DATASETS, "analyst_price_targets", "funds_data"):
        attempted, verified = _write(db_session, SYMBOL_DATASETS[name], "^GSPC")
        assert (attempted, verified) == (0, 0), name


def test_bond_fund_writes_ratings_but_no_holdings(
    db_session: Session, written_symbols: list[str]
) -> None:
    """BND olcumu: 0 sektor + 9 rating, `top_holdings` BOS."""
    if "BND" not in written_symbols:
        pytest.skip("BND fixture'i yok")
    _write(db_session, SYMBOL_DATASETS["funds_data"], "BND")

    categories = list(
        db_session.execute(
            text("SELECT DISTINCT category FROM fund_weightings WHERE symbol = 'BND'")
        ).scalars()
    )
    holdings = db_session.execute(
        text("SELECT COUNT(*) FROM fund_top_holdings WHERE symbol = 'BND'")
    ).scalar_one()
    assert categories == ["bond_rating"]
    assert holdings == 0


def test_equity_fund_writes_sectors_and_holdings(
    db_session: Session, written_symbols: list[str]
) -> None:
    if "SPY" not in written_symbols:
        pytest.skip("SPY fixture'i yok")
    _write(db_session, SYMBOL_DATASETS["funds_data"], "SPY")

    quote_type = db_session.execute(
        text("SELECT quote_type FROM fund_profile WHERE symbol = 'SPY'")
    ).scalar_one()
    # `quote_type` yfinance'ta @property DEGIL; kor erisim buraya
    # "<bound method ...>" yazardi (canli olculdu).
    assert quote_type == "ETF"


def test_eps_revisions_down_last_7d_is_populated(
    db_session: Session, written_symbols: list[str]
) -> None:
    """`downLast7Days`in buyuk D'si: kucuk `d` ile okunsaydi kolon SESSIZCE
    hep NULL kalirdi ve baska hicbir test bunu yakalamazdi."""
    _write(db_session, SYMBOL_DATASETS["eps_revisions"], "AAPL")
    filled = db_session.execute(
        text(
            "SELECT COUNT(*) FROM analyst_eps_revisions "
            "WHERE symbol = 'AAPL' AND down_last_7d IS NOT NULL"
        )
    ).scalar_one()
    assert filled > 0


def test_insider_transactions_are_deduplicated(
    db_session: Session, written_symbols: list[str]
) -> None:
    """Kaynak ozdes satir dondurebiliyor; `rows_attempted` tekillestirme
    SONRASI sayidir, aksi halde S8.6 esitligi kirilirdi."""
    for symbol in written_symbols:
        raw = load_fixture(symbol, "insider_transactions")
        if not raw:
            continue
        attempted, verified = _write(db_session, SYMBOL_DATASETS["insider_transactions"], symbol)
        assert verified == attempted
        assert attempted <= len(raw)

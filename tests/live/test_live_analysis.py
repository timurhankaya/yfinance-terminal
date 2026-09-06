"""Analiz + sahiplik + fon canli entegrasyon testi (AH S9.3).

Varsayilan olarak ATLANIR. Elle calistirmak icin:  pytest -m live
Gercek Yahoo API + gercek PostgreSQL; CI'da calistirilmaz.

Iki iddia BURADA baglanir; ikisi de yalnizca canli kaynakla gorulebilir:

1. ISTEK SAYISI -- 16 dataset tek sembolde YEDI istek yapar (fonlarda
   sekiz). Sayi sessizce artarsa (biri taze bir `Ticker` kurarsa) test
   kirilir; AH S4.4'un varsayimini koda baglayan TEK sey budur.
2. 404 KURALI -- `hide_exceptions=False` altinda ^GSPC'nin butun hucreleri
   `empty` olur, `failed` DEGIL, ve run cikis kodu 0 kalir.

Bos gelmesi beklenen hicbir hucreye "dolu olmali" iddiasi KURULMAZ (S8.2).
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from yfin.client import configure_yfinance
from yfin.datasets import SYMBOL_DATASETS
from yfin.models import Base, ItemStatus, SyncRunItem
from yfin.runner import EXIT_OK, run_sync

pytestmark = pytest.mark.live

# AH S9.1 referans sembolleri; her biri bir kenar durumun kanitidir.
SYMBOLS = ("AAPL", "PFE", "XOM", "NVDA", "KO", "THYAO.IS", "SPY", "BND", "^GSPC")
DATASETS = ("analysis", "holders", "funds")

# Sembol basina beklenen Yahoo modul istegi (AH S4.4): recommendationTrend,
# upgradeDowngradeHistory, financialData, earningsTrend, earningsHistory,
# industry+sector+indexTrend, holders demeti.
EXPECTED_REQUESTS = 7


@pytest.fixture(scope="module", autouse=True)
def configured() -> None:
    """404 KURALI ANCAK BU AYARLA anlamlidir.

    `run_sync` dogrudan cagrildiginda `configure_yfinance`i CAGIRMAZ -- o,
    `shard.run_sharded` ve `market_runner`in isidir. Ayar yapilmazsa
    yfinance varsayilani (`hide_exceptions=True`) gecerli olur, ag hatasi
    sessizce bos sonuca doner ve test "hicbir hucre failed degil" derken
    aslinda VERI KAYBINI onaylardi (AH S8.4).
    """
    configure_yfinance(None, proxy_key="direct")


@pytest.fixture(scope="module")
def live_analysis(test_engine: Engine):  # type: ignore[no-untyped-def]
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session:
        for symbol in SYMBOLS:
            session.execute(
                text(
                    "INSERT INTO symbols (symbol, is_active, unknown_streak, "
                    "created_at, updated_at) VALUES (:s, true, 0, now(), now()) "
                    "ON CONFLICT (symbol) DO NOTHING"
                ),
                {"s": symbol},
            )
        session.commit()
    datasets = SYMBOL_DATASETS.resolve(list(DATASETS))
    return run_sync(test_engine, list(SYMBOLS), datasets)


def _count(session: Session, table: str, symbol: str | None = None) -> int:
    target = Base.metadata.tables[table]
    stmt = select(func.count()).select_from(target)
    if symbol is not None:
        stmt = stmt.where(target.c["symbol"] == symbol)
    return int(session.execute(stmt).scalar_one())


def _statuses(session: Session, run_id: int, symbol: str) -> set[str]:
    rows = session.execute(
        select(SyncRunItem.status)
        .where(SyncRunItem.run_id == run_id)
        .where(SyncRunItem.symbol == symbol)
    ).scalars()
    return {ItemStatus(row).value for row in rows}


# --- run butunlugu ---------------------------------------------------------


def test_run_succeeds_despite_empty_symbols(live_analysis, test_engine: Engine) -> None:  # type: ignore[no-untyped-def]
    """^GSPC ve BND'nin bos hucreleri run'i `partial` YAPMAZ."""
    assert live_analysis.exit_code() == EXIT_OK
    assert live_analysis.failed == 0


def test_index_symbol_produces_only_empty_cells(live_analysis, test_engine: Engine) -> None:  # type: ignore[no-untyped-def]
    """404 KURALI: veri yoklugu `empty`tir, `failed` degil (AH S8.4)."""
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session:
        statuses = _statuses(session, live_analysis.run_id, "^GSPC")
    assert ItemStatus.FAILED.value not in statuses


def test_equity_symbol_fills_analyst_tables(live_analysis, test_engine: Engine) -> None:  # type: ignore[no-untyped-def]
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session:
        assert _count(session, "analyst_recommendations", "AAPL") > 0
        assert _count(session, "analyst_estimates", "AAPL") > 0
        assert _count(session, "analyst_grade_changes", "AAPL") > 0
        assert _count(session, "holder_breakdown", "AAPL") > 0
        assert _count(session, "asof_state", "AAPL") > 0


def test_fund_symbol_fills_fund_tables(live_analysis, test_engine: Engine) -> None:  # type: ignore[no-untyped-def]
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session:
        assert _count(session, "fund_profile", "SPY") == 1
        assert _count(session, "fund_top_holdings", "SPY") > 0
        # BND tahvil fonudur: profil dolar, top_holdings BOS kalir
        assert _count(session, "fund_profile", "BND") == 1
        assert _count(session, "fund_weightings", "BND") > 0


def test_asof_row_count_matches_written_rows(live_analysis, test_engine: Engine) -> None:  # type: ignore[no-untyped-def]
    """`asof_state.row_count` denetim icindir; yazilan satirla tutmali."""
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session:
        written = _count(session, "analyst_recommendations", "AAPL")
        recorded = session.execute(
            text(
                "SELECT row_count FROM asof_state "
                "WHERE symbol = 'AAPL' AND dataset = 'recommendations'"
            )
        ).scalar_one()
    assert int(recorded) == written


# --- istek sayisi ----------------------------------------------------------


def test_symbol_costs_seven_requests(test_engine: Engine) -> None:
    """Sembol basina TEK `Ticker`; 16 dataset YEDI istek paylasir.

    `news`'un taze-Ticker istisnasi (T S6.3) buraya genisletilirse maliyet
    16 istege cikar ve bu test kirilir -- amaci tam olarak budur.
    """
    from yfinance.data import YfData

    from yfin.client import make_ticker
    from yfin.datasets.base import SyncContext

    calls: list[str] = []
    original = YfData.get_raw_json

    def counted(self, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(url)
        return original(self, url, *args, **kwargs)

    YfData.get_raw_json = counted  # type: ignore[method-assign]
    try:
        from datetime import UTC, datetime

        ctx = SyncContext("AAPL", make_ticker("AAPL"), datetime.now(UTC))
        for name in (*SYMBOL_DATASETS.aliases["analysis"], *SYMBOL_DATASETS.aliases["holders"]):
            SYMBOL_DATASETS[name].fetch(ctx)
    finally:
        YfData.get_raw_json = original  # type: ignore[method-assign]

    assert len(calls) == EXPECTED_REQUESTS, calls

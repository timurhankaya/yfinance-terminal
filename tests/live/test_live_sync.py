"""Canli entegrasyon testi (S9.3).

Varsayilan olarak ATLANIR. Elle calistirmak icin:  pytest -m live
Gercek Yahoo API + gercek MySQL; CI'da calistirilmaz.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from yfin.datasets import SYMBOL_DATASETS
from yfin.models import Base, ItemStatus
from yfin.runner import EXIT_OK, run_sync

pytestmark = pytest.mark.live

SYMBOLS = ("AAPL", "THYAO.IS", "SPY", "BTC-USD")


@pytest.fixture(scope="module")
def live_run(test_engine: Engine):  # type: ignore[no-untyped-def]
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session:
        for symbol in SYMBOLS:
            session.execute(
                text(
                    "INSERT INTO symbols (symbol, is_active, unknown_streak, "
                    "created_at, updated_at) VALUES (:s, 1, 0, NOW(6), NOW(6)) "
                    "ON DUPLICATE KEY UPDATE symbol = symbol"
                ),
                {"s": symbol},
            )
        session.commit()
    # OPT-IN dataset'ler ACIKCA eklenir (SQ K11). `resolve(None)` onlari
    # bilincli olarak DISLAR -- ciplak `yfin sync`e sembol basina iki istek
    # eklememek icin. Canli kapsam testi ise KAYITLI HER dataset'i
    # gormelidir; ikisi arasindaki dogru koprü, iddiayi zayiflatmak degil
    # kapsami genisletmektir.
    # `"all"` YALNIZCA tek basinayken sihirlidir (registry.py) ve bu dogru
    # davranistir; burada KAYITLI HER AD acikca verilir.
    summary = run_sync(test_engine, list(SYMBOLS), SYMBOL_DATASETS.resolve(list(SYMBOL_DATASETS)))
    return summary


def _count(session: Session, table: str, symbol: str | None = None) -> int:
    target = Base.metadata.tables[table]
    stmt = select(func.count()).select_from(target)
    if symbol is not None:
        stmt = stmt.where(target.c["symbol"] == symbol)
    return int(session.execute(stmt).scalar_one())


def test_run_succeeds(test_engine: Engine, live_run) -> None:  # type: ignore[no-untyped-def]
    # RunTally ozeti sync_run_items'tan AGREGE edilir; hucre kirilimi icin
    # denetim tablosu okunur (tek dogruluk kaynagi orasidir)
    with Session(test_engine) as session:
        failures = session.execute(
            text(
                "SELECT symbol, dataset, LEFT(COALESCE(error,''), 120) FROM sync_run_items "
                "WHERE run_id = :r AND status = 'failed'"
            ),
            {"r": live_run.run_id},
        ).all()
    assert not failures, [tuple(r) for r in failures]
    assert live_run.exit_code() == EXIT_OK


def test_every_registered_dataset_is_covered(test_engine: Engine, live_run) -> None:  # type: ignore[no-untyped-def]
    """Kapsam SABIT bir sayiya degil REGISTRY'ye baglanir.

    Onceki hali 11'i sabit yaziyordu ve financials/market eklenince
    kirildi; registry buyudukce testin de buyumesi gerekir.
    """
    with Session(test_engine) as session:
        datasets = set(
            session.execute(
                text("SELECT DISTINCT dataset FROM sync_run_items WHERE run_id = :r"),
                {"r": live_run.run_id},
            ).scalars()
        )
    assert datasets == set(SYMBOL_DATASETS)


def test_row_counts(test_engine: Engine) -> None:
    with Session(test_engine) as session:
        assert _count(session, "symbols") >= 4
        for symbol in SYMBOLS:
            assert _count(session, "price_history", symbol) > 1000, symbol
        assert _count(session, "dividends", "AAPL") > 80
        assert _count(session, "splits", "AAPL") >= 5
        assert _count(session, "news") > 0


def test_capital_gains_empty_is_expected(test_engine: Engine, live_run) -> None:  # type: ignore[no-untyped-def]
    """'dolu gelmeli' beklentisi KURULMAZ - empty beklenen sonuctur (S9.3)."""
    with Session(test_engine) as session:
        statuses = set(
            session.execute(
                text(
                    "SELECT DISTINCT status FROM sync_run_items "
                    "WHERE run_id = :r AND dataset = 'capital_gains'"
                ),
                {"r": live_run.run_id},
            ).scalars()
        )
    assert statuses
    assert statuses <= {ItemStatus.EMPTY.value, ItemStatus.OK.value}


def test_shares_full_expectations(test_engine: Engine) -> None:
    with Session(test_engine) as session:
        assert _count(session, "shares_full", "AAPL") > 0
        assert _count(session, "shares_full", "THYAO.IS") > 0
        # Kripto icin veri gelmez
        assert _count(session, "shares_full", "BTC-USD") == 0


def test_not_null_columns_are_populated(test_engine: Engine) -> None:
    """S5.2'de NOT NULL isaretli alanlarin dolu oldugu."""
    with Session(test_engine) as session:
        for table, column in (
            ("price_history", "close"),
            ("price_history", "ts_utc"),
            ("dividends", "amount"),
            ("news", "pub_date"),
            ("news", "title"),
            ("ticker_info", "raw_json"),
            ("ticker_info", "content_hash"),
        ):
            target = Base.metadata.tables[table]
            nulls = session.execute(
                select(func.count()).select_from(target).where(target.c[column].is_(None))
            ).scalar_one()
            assert nulls == 0, f"{table}.{column}"


def test_foreign_key_integrity(test_engine: Engine) -> None:
    with Session(test_engine) as session:
        for table in (
            "price_history",
            "dividends",
            "splits",
            "capital_gains",
            "shares_full",
            "ticker_info",
            "ticker_fast_info",
            "company_officers",
            "history_metadata",
        ):
            orphans = session.execute(
                text(
                    f"SELECT COUNT(*) FROM {table} t "
                    "LEFT JOIN symbols s ON s.symbol = t.symbol WHERE s.symbol IS NULL"
                )
            ).scalar_one()
            assert orphans == 0, table


def test_v_actions_consistency(test_engine: Engine) -> None:
    with Session(test_engine) as session:
        view_total = session.execute(text("SELECT COUNT(*) FROM v_actions")).scalar_one()
        base_total = sum(
            session.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()
            for t in ("dividends", "splits", "capital_gains")
        )
        assert view_total == base_total


def test_raw_json_hash_verifiable_from_db(test_engine: Engine) -> None:
    with Session(test_engine) as session:
        for table in ("ticker_info", "ticker_fast_info", "history_metadata"):
            mismatches = session.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE SHA2(raw_json,256) <> content_hash")
            ).scalar_one()
            assert mismatches == 0, table


def test_second_run_is_idempotent(test_engine: Engine) -> None:
    with Session(test_engine) as session:
        before = _count(session, "price_history")
    summary = run_sync(
        test_engine, list(SYMBOLS), SYMBOL_DATASETS.resolve(["history", "dividends", "splits"])
    )
    with Session(test_engine) as session:
        after = _count(session, "price_history")
    assert after == before
    assert summary.exit_code() == EXIT_OK

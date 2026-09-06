"""AsOfDataset'in GERCEK MySQL uzerindeki davranisi (AH S9.2).

`tests/unit/test_asof_base.py` kapinin karar mantigini sahte bir writer ile
baglar; burada ayni akis `PostgresRowWriter` ile kosar. Ikisi ayri sorulari
yanitlar: karar dogru mu (unit) ve MySQL o karari GERCEKTEN uyguluyor mu
(repo) -- upsert kapsamı, replace_scope silme ve `first_seen_at`in
korunmasi yalnizca burada gorulur.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from yfin.datasets import SYMBOL_DATASETS
from yfin.datasets.base import NormalizedResult, TableWrite
from yfin.datasets.payloads import AsOfFramePayload
from yfin.models import ItemStatus
from yfin.persistence import PostgresRowWriter
from yfin.runner import _record_items

pytestmark = pytest.mark.repo

AS_OF = date(2026, 9, 4)
NEXT_DAY = date(2026, 9, 5)
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=6)
TOMORROW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)

DATASET = SYMBOL_DATASETS["institutional_holders"]


@pytest.fixture
def symbol(db_session: Session) -> Iterator[str]:
    code = "ZZASOF"
    db_session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
            "VALUES (:s, true, 0, :t, :t)"
        ),
        {"s": code, "t": NOW},
    )
    yield code


def _frame(holders: list[str]) -> Any:
    import pandas as pd

    return pd.DataFrame(
        {
            "Date Reported": [pd.Timestamp("2026-06-30")] * len(holders),
            "Holder": holders,
            "pctHeld": [0.05] * len(holders),
            "Shares": [1000] * len(holders),
            "Value": [2000] * len(holders),
            "pctChange": [0.0] * len(holders),
        }
    )


def _run(
    session: Session, symbol: str, holders: list[str], *, fetched_at: datetime
) -> Any:
    payload = AsOfFramePayload(frame=_frame(holders), fetched_at=fetched_at)
    result = DATASET.normalize(payload, symbol)
    return DATASET.upsert(PostgresRowWriter(session), result)


def _gate(session: Session, symbol: str) -> Any:
    return session.execute(
        text(
            "SELECT as_of_date, content_hash, row_count, first_seen_at, fetched_at "
            "FROM asof_state WHERE symbol = :s AND dataset = :d"
        ),
        {"s": symbol, "d": DATASET.name},
    ).one_or_none()


def _holders(session: Session, symbol: str) -> list[str]:
    return list(
        session.execute(
            text("SELECT holder FROM institutional_holders WHERE symbol = :s ORDER BY holder"),
            {"s": symbol},
        ).scalars()
    )


# --- ilk calistirma --------------------------------------------------------


def test_first_run_writes_data_and_opens_the_gate(db_session: Session, symbol: str) -> None:
    stats = _run(db_session, symbol, ["Vanguard", "Blackrock"], fetched_at=NOW)

    assert _holders(db_session, symbol) == ["Blackrock", "Vanguard"]
    gate = _gate(db_session, symbol)
    assert gate is not None
    assert gate.row_count == 2
    assert stats.verified["institutional_holders"] == 2


# --- ikinci calistirma, ayni icerik ----------------------------------------


def test_unchanged_content_skips_data_but_refreshes_verification_time(
    db_session: Session, symbol: str
) -> None:
    """Kapi satiri HER DURUMDA yazilir; hash esitse yalnizca `fetched_at`.

    Sapilsaydi "bu sembol en son ne zaman KONTROL EDILDI" sorusu cevapsiz
    kalirdi (hash_gated.py'nin kurdugu ilke).
    """
    _run(db_session, symbol, ["Vanguard"], fetched_at=NOW)
    stats = _run(db_session, symbol, ["Vanguard"], fetched_at=LATER)

    gate = _gate(db_session, symbol)
    assert gate.fetched_at == LATER
    assert stats.skipped["institutional_holders"] == 1
    assert stats.attempted.get("institutional_holders", 0) == 0


def test_first_seen_at_is_never_overwritten(db_session: Session, symbol: str) -> None:
    """`first_seen_at` `update_columns` KAPSAMI DISINDADIR; girseydi
    ON DUPLICATE KEY UPDATE "ilk INSERT'te yazilir" kuralini bozardi."""
    _run(db_session, symbol, ["Vanguard"], fetched_at=NOW)
    # Icerik degisir -> kapi TAM yazilir; first_seen_at yine de korunmali
    _run(db_session, symbol, ["Vanguard", "Blackrock"], fetched_at=LATER)

    gate = _gate(db_session, symbol)
    assert gate.first_seen_at == NOW
    assert gate.fetched_at == LATER


def test_hash_survives_a_new_fetched_at(db_session: Session, symbol: str) -> None:
    """Hash `as_of_date`/`fetched_at`ten BAGIMSIZDIR. Bu iddia olmadan
    VOLATILE_COLUMNS bir gun sessizce daralir ve mekanizma hic
    calismaz hale gelirdi."""
    _run(db_session, symbol, ["Vanguard"], fetched_at=NOW)
    before = _gate(db_session, symbol).content_hash
    _run(db_session, symbol, ["Vanguard"], fetched_at=LATER)
    assert _gate(db_session, symbol).content_hash == before


# --- icerik degisimi -------------------------------------------------------


def test_shrinking_source_deletes_stale_rows_in_the_same_day(
    db_session: Session, symbol: str
) -> None:
    """`replace_scope` + ACIK `scope_values`: liste kuculdugunde eski satir
    AYNI as-of gununde kalmaz."""
    _run(db_session, symbol, ["Vanguard", "Blackrock"], fetched_at=NOW)
    _run(db_session, symbol, ["Vanguard"], fetched_at=LATER)

    assert _holders(db_session, symbol) == ["Vanguard"]


def test_sibling_holder_type_is_untouched(db_session: Session, symbol: str) -> None:
    """Iki dataset ayni tabloda yasar; kapsam `holder_type` ile ayrisir."""
    mutualfund = SYMBOL_DATASETS["mutualfund_holders"]
    writer = PostgresRowWriter(db_session)
    mutualfund.upsert(
        writer, mutualfund.normalize(AsOfFramePayload(_frame(["VFIAX"]), NOW), symbol)
    )
    _run(db_session, symbol, ["Vanguard", "Blackrock"], fetched_at=NOW)
    _run(db_session, symbol, ["Vanguard"], fetched_at=LATER)

    rows = db_session.execute(
        text(
            "SELECT holder_type, holder FROM institutional_holders "
            "WHERE symbol = :s ORDER BY holder_type, holder"
        ),
        {"s": symbol},
    ).all()
    assert rows == [("institution", "Vanguard"), ("mutualfund", "VFIAX")]


def test_next_day_creates_a_second_as_of_row(db_session: Session, symbol: str) -> None:
    """Gecmis ILERIYE dogru birikir: dunun satiri korunur."""
    _run(db_session, symbol, ["Vanguard"], fetched_at=NOW)
    _run(db_session, symbol, ["Vanguard", "Blackrock"], fetched_at=TOMORROW)

    dates = list(
        db_session.execute(
            text(
                "SELECT DISTINCT as_of_date FROM institutional_holders "
                "WHERE symbol = :s ORDER BY as_of_date"
            ),
            {"s": symbol},
        ).scalars()
    )
    assert dates == [AS_OF, NEXT_DAY]
    assert _gate(db_session, symbol).as_of_date == NEXT_DAY


# --- bos sonuc -------------------------------------------------------------


def test_empty_source_never_opens_the_gate(db_session: Session, symbol: str) -> None:
    """Aksi halde her fon-olmayan sembol icin olu bir kapi satiri birikir ve
    `first_seen_at` "ilk kez BOS donuldu" anlamina kayardi (AH S6.1/3)."""
    import pandas as pd

    stats = DATASET.upsert(
        PostgresRowWriter(db_session),
        DATASET.normalize(AsOfFramePayload(pd.DataFrame(), NOW), symbol),
    )

    assert _gate(db_session, symbol) is None
    assert stats.tables() == []


# --- denetim kaydi ---------------------------------------------------------


def test_audit_cell_is_skipped_when_content_is_unchanged(
    db_session: Session, symbol: str
) -> None:
    """`runner._record_items` bunu `skipped` sayar: attempted=0, skipped>0."""
    _run(db_session, symbol, ["Vanguard"], fetched_at=NOW)
    stats = _run(db_session, symbol, ["Vanguard"], fetched_at=LATER)

    records = {r.table_name: r for r in _record_items(DATASET, symbol, stats, 1, 0)}
    assert records["institutional_holders"].status is ItemStatus.SKIPPED
    # Kapi satiri her calistirmada YAZILIR -> kendi hucresi `ok` kalir
    assert records["asof_state"].status is ItemStatus.OK


def test_multi_table_dataset_can_be_empty_and_skipped_at_once(
    db_session: Session, symbol: str
) -> None:
    """BND'de `fund_top_holdings` bostur, kardes tablolar `skipped` olur --
    tek dataset ayni calistirmada IKI farkli durum uretebilir (AH S7.2)."""
    funds = SYMBOL_DATASETS["funds_data"]
    writer = PostgresRowWriter(db_session)
    result = NormalizedResult(
        writes=[
            TableWrite(
                table="fund_profile",
                rows=[
                    {
                        "symbol": symbol,
                        "as_of_date": AS_OF,
                        "quote_type": "ETF",
                        "raw_json": "{}",
                        "expense_ratio": Decimal("0.01"),
                        "fetched_at": NOW,
                    }
                ],
                key_columns=("symbol", "as_of_date"),
                update_columns=("quote_type", "raw_json", "expense_ratio", "fetched_at"),
            ),
            TableWrite(
                table="fund_top_holdings",
                rows=[],
                key_columns=("symbol", "as_of_date", "holding_symbol"),
                update_columns=("holding_name", "fetched_at"),
                mode="replace_scope",
                scope_columns=("symbol", "as_of_date"),
                scope_values=({"symbol": symbol, "as_of_date": AS_OF},),
            ),
        ]
    )
    funds.upsert(writer, result)
    stats = funds.upsert(writer, result)

    records = {r.table_name: r for r in _record_items(funds, symbol, stats, 1, 0)}
    assert records["fund_profile"].status is ItemStatus.SKIPPED
    assert records["fund_top_holdings"].status is ItemStatus.EMPTY

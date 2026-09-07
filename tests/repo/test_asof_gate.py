"""AsOfDataset's actual behavior against PostgreSQL.

`tests/unit/test_asof_base.py` pins the gate's decision logic with a fake
writer; here the same flow runs through `PostgresRowWriter`. Separate
questions: is the decision correct (unit) vs. does the database actually
apply it (repo) -- upsert scope, replace_scope deletion, and `first_seen_at`
preservation only show up here.
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
from yfin.datasets.base import NormalizedResult
from yfin.datasets.payloads import AsOfFramePayload
from yfin.models import ItemStatus
from yfin.pipeline.audit import record_items
from yfin.storage.contracts import TableWrite
from yfin.storage.persistence import PostgresRowWriter

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


# --- first run --------------------------------------------------------


def test_first_run_writes_data_and_opens_the_gate(db_session: Session, symbol: str) -> None:
    stats = _run(db_session, symbol, ["Vanguard", "Blackrock"], fetched_at=NOW)

    assert _holders(db_session, symbol) == ["Blackrock", "Vanguard"]
    gate = _gate(db_session, symbol)
    assert gate is not None
    assert gate.row_count == 2
    assert stats.verified["institutional_holders"] == 2


# --- second run, same content ----------------------------------------


def test_unchanged_content_skips_data_but_refreshes_verification_time(
    db_session: Session, symbol: str
) -> None:
    """The gate row is always written; on a matching hash only `fetched_at` moves.

    Otherwise "when was this symbol last checked" would be unanswerable --
    the invariant hash_gated.py establishes.
    """
    _run(db_session, symbol, ["Vanguard"], fetched_at=NOW)
    stats = _run(db_session, symbol, ["Vanguard"], fetched_at=LATER)

    gate = _gate(db_session, symbol)
    assert gate.fetched_at == LATER
    assert stats.skipped["institutional_holders"] == 1
    assert stats.attempted.get("institutional_holders", 0) == 0


def test_first_seen_at_is_never_overwritten(db_session: Session, symbol: str) -> None:
    """`first_seen_at` is outside `update_columns`; including it would break the
    rule that it is written only on the first INSERT."""
    _run(db_session, symbol, ["Vanguard"], fetched_at=NOW)
    # Content changes -> gate is fully rewritten; first_seen_at must still be kept
    _run(db_session, symbol, ["Vanguard", "Blackrock"], fetched_at=LATER)

    gate = _gate(db_session, symbol)
    assert gate.first_seen_at == NOW
    assert gate.fetched_at == LATER


def test_hash_survives_a_new_fetched_at(db_session: Session, symbol: str) -> None:
    """The hash is independent of `as_of_date`/`fetched_at`. Without this
    invariant, VOLATILE_COLUMNS could silently shrink one day and disable
    the whole mechanism."""
    _run(db_session, symbol, ["Vanguard"], fetched_at=NOW)
    before = _gate(db_session, symbol).content_hash
    _run(db_session, symbol, ["Vanguard"], fetched_at=LATER)
    assert _gate(db_session, symbol).content_hash == before


# --- content change -------------------------------------------------------


def test_shrinking_source_deletes_stale_rows_in_the_same_day(
    db_session: Session, symbol: str
) -> None:
    """`replace_scope` + explicit `scope_values`: a shrinking list drops the
    stale row within the same as-of day."""
    _run(db_session, symbol, ["Vanguard", "Blackrock"], fetched_at=NOW)
    _run(db_session, symbol, ["Vanguard"], fetched_at=LATER)

    assert _holders(db_session, symbol) == ["Vanguard"]


def test_sibling_holder_type_is_untouched(db_session: Session, symbol: str) -> None:
    """Two datasets share one table; `holder_type` separates their scope."""
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
    """History accumulates forward: yesterday's row is kept."""
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


# --- empty result -------------------------------------------------------------


def test_empty_source_never_opens_the_gate(db_session: Session, symbol: str) -> None:
    """Otherwise every non-fund symbol would accumulate a dead gate row, and
    `first_seen_at` would come to mean "first time an empty result came back"."""
    import pandas as pd

    stats = DATASET.upsert(
        PostgresRowWriter(db_session),
        DATASET.normalize(AsOfFramePayload(pd.DataFrame(), NOW), symbol),
    )

    assert _gate(db_session, symbol) is None
    assert stats.tables() == []


# --- audit record ---------------------------------------------------------


def test_audit_cell_is_skipped_when_content_is_unchanged(
    db_session: Session, symbol: str
) -> None:
    """`runner.record_items` counts this as `skipped`: attempted=0, skipped>0."""
    _run(db_session, symbol, ["Vanguard"], fetched_at=NOW)
    stats = _run(db_session, symbol, ["Vanguard"], fetched_at=LATER)

    records = {r.table_name: r for r in record_items(DATASET, symbol, stats, 1, 0)}
    assert records["institutional_holders"].status is ItemStatus.SKIPPED
    # The gate row is written on every run -> its own cell stays `ok`
    assert records["asof_state"].status is ItemStatus.OK


def test_multi_table_dataset_can_be_empty_and_skipped_at_once(
    db_session: Session, symbol: str
) -> None:
    """On BND `fund_top_holdings` is empty and the sibling table is `skipped` --
    one dataset can produce two different statuses in the same run."""
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

    records = {r.table_name: r for r in record_items(funds, symbol, stats, 1, 0)}
    assert records["fund_profile"].status is ItemStatus.SKIPPED
    assert records["fund_top_holdings"].status is ItemStatus.EMPTY

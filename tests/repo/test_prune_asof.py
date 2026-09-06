"""As-of pruning. Real PostgreSQL.

The one special rule here is "keep the latest day", for a mechanical reason:
even if the data row is deleted, the `asof_state` gate row stays, so the next
run finds the hash unchanged, the dataset says `skipped`, and nothing gets
written. If the latest day were deleted, the loss would be permanent even
while the source still has the data. The last test in this file runs exactly
that scenario.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from yfin.datasets import SYMBOL_DATASETS
from yfin.datasets.payloads import AsOfFramePayload
from yfin.persistence import PostgresRowWriter
from yfin.prune import PruneDisabledError, asof_tables, prune_asof, run_prune

pytestmark = pytest.mark.repo

SYMBOL = "ZZPRUNE"
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
DATASET = SYMBOL_DATASETS["institutional_holders"]
TABLE = "institutional_holders"


@pytest.fixture
def symbol(db_session: Session) -> Iterator[str]:
    db_session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
            "VALUES (:s, true, 0, :t, :t)"
        ),
        {"s": SYMBOL, "t": NOW},
    )
    yield SYMBOL


def _write_day(session: Session, day: datetime, holders: list[str]) -> None:
    frame = pd.DataFrame(
        {
            "Date Reported": [pd.Timestamp("2026-06-30")] * len(holders),
            "Holder": holders,
            "pctHeld": [0.05] * len(holders),
            "Shares": [1000] * len(holders),
            "Value": [2000] * len(holders),
            "pctChange": [0.0] * len(holders),
        }
    )
    result = DATASET.normalize(AsOfFramePayload(frame, day), SYMBOL)
    DATASET.upsert(PostgresRowWriter(session), result)


def _days(session: Session, symbol: str) -> list[date]:
    return list(
        session.execute(
            text(
                f"SELECT DISTINCT as_of_date FROM {TABLE} "
                "WHERE symbol = :s ORDER BY as_of_date"
            ),
            {"s": symbol},
        ).scalars()
    )


# --- scope -------------------------------------------------------------


def test_asof_tables_are_derived_from_dataset_base_not_column_name() -> None:
    """`shares_full` also carries `as_of_date` in its PK but is not as-of:
    it is the source's own date and gets re-fetched via a watermark."""
    tables = asof_tables()
    assert "shares_full" not in tables
    assert TABLE in tables
    assert "fund_top_holdings" in tables


def test_gate_table_is_never_pruned() -> None:
    """If the gate row were deleted, `first_seen_at` would be lost and the
    whole history rewritten on the next run."""
    assert "asof_state" not in asof_tables()


def test_asof_table_count_matches_the_fourteen_as_of_tables() -> None:
    """Discovery tables have their own gate family, separate from this count.

    `asof_table_datasets` filters by gate table; `search`/`lookup` are also
    `AsOfGate` but do not count here. Without the filter this count would
    jump from 14 to 24, and `prune_asof(asof_state)` would try to prune
    discovery tables using the wrong gate.
    """
    assert len(asof_tables()) == 14


# --- pruning -------------------------------------------------------------


def test_old_days_are_removed(db_session: Session, symbol: str) -> None:
    for offset, holders in ((0, ["A"]), (5, ["A", "B"]), (10, ["A", "B", "C"])):
        _write_day(db_session, NOW + timedelta(days=offset), holders)
    assert len(_days(db_session, symbol)) == 3

    removed = prune_asof(db_session, NOW.date() + timedelta(days=8))

    assert removed[TABLE] == 3  # 1 (day 0) + 2 (day 5)
    assert _days(db_session, symbol) == [NOW.date() + timedelta(days=10)]


def test_latest_day_survives_even_when_entirely_behind_the_cutoff(
    db_session: Session, symbol: str
) -> None:
    """Even when a symbol's entire history is before the cutoff, its latest day survives."""
    _write_day(db_session, NOW, ["A"])
    _write_day(db_session, NOW + timedelta(days=5), ["A", "B"])

    prune_asof(db_session, NOW.date() + timedelta(days=90))

    assert _days(db_session, symbol) == [NOW.date() + timedelta(days=5)]


def test_dry_run_counts_without_deleting(db_session: Session, symbol: str) -> None:
    _write_day(db_session, NOW, ["A"])
    _write_day(db_session, NOW + timedelta(days=5), ["A", "B"])

    counted = prune_asof(db_session, NOW.date() + timedelta(days=3), dry_run=True)

    assert counted[TABLE] == 1
    assert len(_days(db_session, symbol)) == 2


def test_prune_is_disabled_by_default(db_session: Session, symbol: str) -> None:
    with pytest.raises(PruneDisabledError):
        run_prune(db_session, enabled=False, asof_before=NOW, dry_run=True)


def test_report_total_includes_asof(db_session: Session, symbol: str) -> None:
    _write_day(db_session, NOW, ["A"])
    _write_day(db_session, NOW + timedelta(days=5), ["A", "B"])

    report = run_prune(
        db_session,
        enabled=True,
        orphan_news=False,
        asof_before=NOW + timedelta(days=3),
        dry_run=True,
    )
    assert report.asof[TABLE] == 1
    assert report.total == 1


# --- why the rule exists ----------------------------------------------


def test_pruning_the_latest_day_would_be_permanent(db_session: Session, symbol: str) -> None:
    """Why the rule exists: because the gate never reopens, deleted data never comes back.

    The latest day is deleted by hand here and the same content re-synced.
    Since the hash is unchanged, the dataset says `skipped` and the table
    stays empty -- exactly what would happen if `prune_asof` did not
    protect the latest day.
    """
    _write_day(db_session, NOW, ["A", "B"])
    db_session.execute(text(f"DELETE FROM {TABLE} WHERE symbol = :s"), {"s": symbol})

    _write_day(db_session, NOW + timedelta(hours=6), ["A", "B"])

    assert _days(db_session, symbol) == []  # data did not come back
    # The gate row is still in place: that is what drives the "unchanged" decision
    assert (
        db_session.execute(
            text("SELECT COUNT(*) FROM asof_state WHERE symbol = :s"), {"s": symbol}
        ).scalar_one()
        == 1
    )


def test_prune_asof_leaves_the_gate_row_intact(db_session: Session, symbol: str) -> None:
    """The gate row is never pruned; `first_seen_at` stays even as old days go."""
    _write_day(db_session, NOW, ["A"])
    _write_day(db_session, NOW + timedelta(days=5), ["A", "B"])

    prune_asof(db_session, NOW.date() + timedelta(days=3))

    first_seen = db_session.execute(
        text("SELECT first_seen_at FROM asof_state WHERE symbol = :s"), {"s": symbol}
    ).scalar_one()
    assert first_seen == NOW


class TestSharedTableProtection:
    """`institutional_holders` is written by two datasets.

    If `mutualfund_holders`'s latest day is older than `institutional_holders`'s,
    a protection computed via `GROUP BY symbol` alone would leave it unprotected
    and delete its one, most-recent row. The loss is permanent: since the
    `asof_state` gate row is not deleted, the next run finds the hash
    unchanged, says `skipped`, and writes nothing.
    """

    MUTUAL = SYMBOL_DATASETS["mutualfund_holders"]

    def _write_mutual(self, session: Session, day: datetime, holders: list[str]) -> None:
        frame = pd.DataFrame(
            {
                "Date Reported": [pd.Timestamp("2026-06-30")] * len(holders),
                "Holder": holders,
                "pctHeld": [0.05] * len(holders),
                "Shares": [1000] * len(holders),
                "Value": [2000] * len(holders),
                "pctChange": [0.0] * len(holders),
            }
        )
        result = self.MUTUAL.normalize(AsOfFramePayload(frame, day), SYMBOL)
        self.MUTUAL.upsert(PostgresRowWriter(session), result)

    def _rows(self, session: Session) -> set[tuple[str, date]]:
        return {
            (str(holder_type), as_of)
            for holder_type, as_of in session.execute(
                text(
                    "SELECT holder_type, as_of_date FROM institutional_holders "
                    "WHERE symbol = :s"
                ),
                {"s": SYMBOL},
            )
        }

    def test_older_dataset_keeps_its_latest_day(self, db_session: Session, symbol: str) -> None:
        # mutualfund last seen 08-01, institutional last seen 09-04
        self._write_mutual(db_session, datetime(2026, 8, 1, 12, 0), ["Vanguard 500 Index"])
        _write_day(db_session, datetime(2026, 9, 4, 12, 0), ["BlackRock Inc"])
        db_session.flush()
        assert self._rows(db_session) == {
            ("mutualfund", date(2026, 8, 1)),
            ("institution", date(2026, 9, 4)),
        }

        prune_asof(db_session, before=date(2026, 9, 1))
        db_session.flush()

        # mutualfund's single, most-recent row must be protected
        assert self._rows(db_session) == {
            ("mutualfund", date(2026, 8, 1)),
            ("institution", date(2026, 9, 4)),
        }

    def test_genuinely_old_day_is_still_pruned(self, db_session: Session, symbol: str) -> None:
        """Protection only applies to the latest day; an older one is still pruned."""
        self._write_mutual(db_session, datetime(2026, 7, 1, 12, 0), ["Old Fund"])
        self._write_mutual(db_session, datetime(2026, 8, 1, 12, 0), ["Vanguard 500 Index"])
        db_session.flush()

        prune_asof(db_session, before=date(2026, 9, 1))
        db_session.flush()

        assert self._rows(db_session) == {("mutualfund", date(2026, 8, 1))}


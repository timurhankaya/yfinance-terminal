"""As-of budamasi (AH S5.4 uzantisi). Gercek MySQL.

Budamanin buradaki tek ozel kurali "son gunu koru"dur ve gerekcesi
MEKANIKTIR: veri satiri silinse bile `asof_state` kapisi yerinde kalir,
ertesi kosuda hash esitlenir ve dataset `skipped` deyip HICBIR SEY yazmaz.
Son gun silinseydi kayip, kaynak hala veriyi verirken bile kalici olurdu.
Bu dosyanin son testi tam olarak o senaryoyu kosturur.
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
from yfin.persistence import MySQLRowWriter
from yfin.prune import PruneDisabledError, asof_tables, prune_asof, run_prune

pytestmark = pytest.mark.repo

SYMBOL = "ZZPRUNE"
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC).replace(tzinfo=None)
DATASET = SYMBOL_DATASETS["institutional_holders"]
TABLE = "institutional_holders"


@pytest.fixture
def symbol(db_session: Session) -> Iterator[str]:
    db_session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
            "VALUES (:s, 1, 0, :t, :t)"
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
    DATASET.upsert(MySQLRowWriter(session), result)


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


# --- kapsam ----------------------------------------------------------------


def test_asof_tables_are_derived_from_dataset_base_not_column_name() -> None:
    """`shares_full` de `as_of_date`i PK'sinda tasir ama as-of DEGILDIR:
    kaynagin kendi tarihidir ve watermark ile yeniden cekilir."""
    tables = asof_tables()
    assert "shares_full" not in tables
    assert TABLE in tables
    assert "fund_top_holdings" in tables


def test_gate_table_is_never_pruned() -> None:
    """Kapi satiri silinseydi `first_seen_at` kaybolur ve butun gecmis bir
    sonraki kosuda yeniden yazilirdi."""
    assert "asof_state" not in asof_tables()


def test_asof_table_count_matches_the_fourteen_as_of_tables() -> None:
    assert len(asof_tables()) == 14


# --- budama --------------------------------------------------------------


def test_old_days_are_removed(db_session: Session, symbol: str) -> None:
    for offset, holders in ((0, ["A"]), (5, ["A", "B"]), (10, ["A", "B", "C"])):
        _write_day(db_session, NOW + timedelta(days=offset), holders)
    assert len(_days(db_session, symbol)) == 3

    removed = prune_asof(db_session, NOW.date() + timedelta(days=8))

    assert removed[TABLE] == 3  # 1 (gun 0) + 2 (gun 5)
    assert _days(db_session, symbol) == [NOW.date() + timedelta(days=10)]


def test_latest_day_survives_even_when_entirely_behind_the_cutoff(
    db_session: Session, symbol: str
) -> None:
    """SINIRIN TAMAMEN GERISINDE kalan sembolde bile son gun KORUNUR."""
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


# --- kuralin GEREKCESI -----------------------------------------------------


def test_pruning_the_latest_day_would_be_permanent(db_session: Session, symbol: str) -> None:
    """Kuralin var olma nedeni: kapi acilmadigi icin veri GERI GELMEZ.

    Burada son gun ELLE silinir ve ayni icerik yeniden sync edilir. Hash
    degismedigi icin dataset `skipped` der ve tablo BOS kalir -- `prune_asof`
    son gunu korumasaydi olacak sey tam olarak budur.
    """
    _write_day(db_session, NOW, ["A", "B"])
    db_session.execute(text(f"DELETE FROM {TABLE} WHERE symbol = :s"), {"s": symbol})

    _write_day(db_session, NOW + timedelta(hours=6), ["A", "B"])

    assert _days(db_session, symbol) == []  # veri GERI GELMEDI
    # Kapi satiri hala yerinde: "degismedi" karari bundan geliyor
    assert (
        db_session.execute(
            text("SELECT COUNT(*) FROM asof_state WHERE symbol = :s"), {"s": symbol}
        ).scalar_one()
        == 1
    )


def test_prune_asof_leaves_the_gate_row_intact(db_session: Session, symbol: str) -> None:
    """Kapi satiri budanmaz; eski gunler gitse de `first_seen_at` durur."""
    _write_day(db_session, NOW, ["A"])
    _write_day(db_session, NOW + timedelta(days=5), ["A", "B"])

    prune_asof(db_session, NOW.date() + timedelta(days=3))

    first_seen = db_session.execute(
        text("SELECT first_seen_at FROM asof_state WHERE symbol = :s"), {"s": symbol}
    ).scalar_one()
    assert first_seen == NOW


class TestSharedTableProtection:
    """A1 regresyonu: `institutional_holders` tablosunu IKI dataset yazar.

    `mutualfund_holders`in en guncel gunu `institutional_holders`inkinden
    ESKIYSE, `GROUP BY symbol` uzerinden hesaplanan koruma onu korumasiz
    birakir ve TEK+EN GUNCEL satiri silinir. Kayip KALICIDIR: `asof_state`
    kapi satiri silinmedigi icin ertesi kosu hash'i esit bulur, `skipped`
    der ve hicbir sey yazmaz.
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
        self.MUTUAL.upsert(MySQLRowWriter(session), result)

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
        # mutualfund SON kez 08-01'de, institutional 09-04'te goruldu
        self._write_mutual(db_session, datetime(2026, 8, 1, 12, 0), ["Vanguard 500 Index"])
        _write_day(db_session, datetime(2026, 9, 4, 12, 0), ["BlackRock Inc"])
        db_session.flush()
        assert self._rows(db_session) == {
            ("mutualfund", date(2026, 8, 1)),
            ("institution", date(2026, 9, 4)),
        }

        prune_asof(db_session, before=date(2026, 9, 1))
        db_session.flush()

        # mutualfund'in TEK ve EN GUNCEL satiri korunmalidir
        assert self._rows(db_session) == {
            ("mutualfund", date(2026, 8, 1)),
            ("institution", date(2026, 9, 4)),
        }

    def test_genuinely_old_day_is_still_pruned(self, db_session: Session, symbol: str) -> None:
        """Koruma yalnizca EN GUNCEL gune aittir; eskisi yine budanir."""
        self._write_mutual(db_session, datetime(2026, 7, 1, 12, 0), ["Eski Fon"])
        self._write_mutual(db_session, datetime(2026, 8, 1, 12, 0), ["Vanguard 500 Index"])
        db_session.flush()

        prune_asof(db_session, before=date(2026, 9, 1))
        db_session.flush()

        assert self._rows(db_session) == {("mutualfund", date(2026, 8, 1))}


"""Pruning of discovery and screen tables (real PostgreSQL)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from yfin.pipeline.prune import prune_orphan_reports, prune_screens

pytestmark = pytest.mark.repo

OLD = date(2026, 1, 1)
NEW = date(2026, 9, 5)
NOW = datetime(2026, 9, 5, 12, 0)


def _scalar(db_session: Session, sql: str, **params: Any) -> Any:
    return db_session.execute(text(sql), params).scalar()


def _seed_report(db_session: Session, report_id: str) -> None:
    db_session.execute(
        text(
            "INSERT INTO research_reports (report_id, as_of_date, provider, "
            "first_seen_at, fetched_at) VALUES (:i, :d, 'Argus Research', :t, :t)"
        ),
        {"i": report_id, "d": NEW, "t": NOW},
    )


class TestOrphanReports:
    def test_search_linked_report_survives(self, db_session: Session) -> None:
        """A report reachable only via the search path has no domain-side link
        and must not be counted as orphaned.
        """
        _seed_report(db_session, "SEARCH_ONLY")
        db_session.execute(
            text(
                "INSERT INTO search_report_hits (query_term, as_of_date, report_id, "
                "rank_index, fetched_at) VALUES ('AAPL', :d, 'SEARCH_ONLY', 0, :t)"
            ),
            {"d": NEW, "t": NOW},
        )
        db_session.flush()

        removed = prune_orphan_reports(db_session, dry_run=True)
        assert removed == 0
        assert _scalar(
            db_session, "SELECT COUNT(*) FROM research_reports WHERE report_id = 'SEARCH_ONLY'"
        )

    def test_unlinked_report_is_removed(self, db_session: Session) -> None:
        """A report with no match in either link table is removed."""
        _seed_report(db_session, "ORPHAN")
        db_session.flush()
        assert prune_orphan_reports(db_session, dry_run=True) >= 1


class TestPruneScreens:
    def _seed_screen(self, db_session: Session, day: date, symbols: tuple[str, ...]) -> None:
        db_session.execute(
            text(
                "INSERT INTO screen_runs (screen_key, as_of_date, total, fetched_rows, "
                "row_count, page_count, content_hash, fetched_at) "
                "VALUES ('day_gainers', :d, 1, 1, 1, 1, 'h', :t) "
                "ON CONFLICT DO NOTHING"
            ),
            {"d": day, "t": NOW},
        )
        for symbol in symbols:
            db_session.execute(
                text(
                    "INSERT INTO screen_members (screen_key, as_of_date, symbol, rank_index, "
                    "is_known, fetched_at) VALUES ('day_gainers', :d, :s, 0, false, :t)"
                ),
                {"d": day, "s": symbol, "t": NOW},
            )
            db_session.execute(
                text(
                    "INSERT INTO screen_quotes (symbol, as_of_date, is_known, fetched_at, "
                    "raw_json) VALUES (:s, :d, false, :t, '{}')"
                ),
                {"d": day, "s": symbol, "t": NOW},
            )
        db_session.flush()

    def test_last_day_is_protected(self, db_session: Session) -> None:
        """As-of pruning's principle: the latest day is always protected."""
        self._seed_screen(db_session, OLD, ("ZZOLD",))
        self._seed_screen(db_session, NEW, ("ZZNEW",))

        prune_screens(db_session, date(2026, 12, 31))
        db_session.flush()

        assert _scalar(
            db_session, "SELECT COUNT(*) FROM screen_members WHERE as_of_date = :d", d=NEW
        )
        assert (
            _scalar(
                db_session, "SELECT COUNT(*) FROM screen_members WHERE as_of_date = :d", d=OLD
            )
            == 0
        )

    def test_quotes_keep_their_own_last_day(self, db_session: Session) -> None:
        """`screen_quotes` has no `screen_key` column, so the latest day is kept
        per symbol; `prune_asof` would skip it via the `scope_column not in
        table.c` branch.
        """
        self._seed_screen(db_session, OLD, ("ZZQ",))
        self._seed_screen(db_session, NEW, ("ZZQ",))

        prune_screens(db_session, date(2026, 12, 31))
        db_session.flush()

        rows = db_session.execute(
            text("SELECT as_of_date FROM screen_quotes WHERE symbol = 'ZZQ'")
        ).scalars().all()
        assert rows == [NEW]

    def test_dry_run_changes_nothing(self, db_session: Session) -> None:
        self._seed_screen(db_session, OLD, ("ZZDRY",))
        self._seed_screen(db_session, NEW, ("ZZDRY",))
        before = _scalar(db_session, "SELECT COUNT(*) FROM screen_quotes")

        report = prune_screens(db_session, date(2026, 12, 31), dry_run=True)
        db_session.flush()

        assert report
        assert _scalar(db_session, "SELECT COUNT(*) FROM screen_quotes") == before

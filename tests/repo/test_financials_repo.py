"""Financials/market repository tests: real PostgreSQL + TimescaleDB, no network."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from yfin.datasets.base import NormalizedResult
from yfin.datasets.hash_gated import HashGatedDataset
from yfin.models import Base
from yfin.storage.contracts import TableWrite, WriteStats, apply_write
from yfin.storage.persistence import PostgresRowWriter

pytestmark = pytest.mark.repo

GATE_KEY = ("symbol", "statement", "freq", "period_end")
PERIOD_END = date(2025, 9, 30)
FETCHED_AT = datetime(2026, 9, 4, 10, 0, 0, 500000, tzinfo=UTC)
# Real accession-number parsing is covered by a unit test; only key behavior
# matters here, so a plain value is used.
FILING_ID = "acc-test-a1"
# Largest absolute value magnitude measured on 7203.T
HUGE_VALUE = Decimal("1.05522331e14").quantize(Decimal("1E-10"))


def _seed_symbol(session: Session, symbol: str = "AAPL") -> None:
    session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
            "VALUES (:s, true, 0, now(), now()) ON CONFLICT (symbol) DO NOTHING"
        ),
        {"s": symbol},
    )


def _period_row(content_hash: str = "h1", item_count: int = 2, **over: Any) -> dict[str, Any]:
    row = {
        "symbol": "AAPL",
        "statement": "income",
        "freq": "annual",
        "period_end": PERIOD_END,
        "currency": "USD",
        "item_count": item_count,
        "raw_json": '{"TotalRevenue":1}',
        "content_hash": content_hash,
        "fetched_at": FETCHED_AT,
    }
    row.update(over)
    return row


def _fact_row(item_key: str, value: Decimal | str, **over: Any) -> dict[str, Any]:
    row = {
        "symbol": "AAPL",
        "statement": "income",
        "freq": "annual",
        "period_end": PERIOD_END,
        "item_key": item_key,
        "value": Decimal(value),
    }
    row.update(over)
    return row


def _period_write(rows: list[dict[str, Any]]) -> TableWrite:
    return TableWrite(
        table="financial_periods",
        rows=rows,
        key_columns=GATE_KEY,
        update_columns=("currency", "item_count", "raw_json", "content_hash", "fetched_at"),
    )


def _fact_write(rows: list[dict[str, Any]], scope: Any = None) -> TableWrite:
    return TableWrite(
        table="financial_facts",
        rows=rows,
        key_columns=(*GATE_KEY, "item_key"),
        update_columns=("value",),
        mode="replace_scope",
        scope_columns=GATE_KEY,
        scope_values=scope,
    )


def _count(session: Session, table: str) -> int:
    return int(
        session.execute(select(func.count()).select_from(Base.metadata.tables[table])).scalar_one()
    )


class TestFinancialWrites:
    def test_decimal_round_trip_keeps_ratio_and_huge_value(self, db_session: Session) -> None:
        """DECIMAL(38,10) holds a ratio and a huge value in the same column without loss."""
        _seed_symbol(db_session)
        stats = WriteStats()
        writer = PostgresRowWriter(db_session)
        apply_write(writer, _period_write([_period_row()]), stats)
        apply_write(
            writer,
            _fact_write(
                [
                    _fact_row("TaxRateForCalcs", "0.1560000000"),
                    _fact_row("TotalAssets", HUGE_VALUE),
                ]
            ),
            stats,
        )
        rows = dict(
            db_session.execute(
                text("SELECT item_key, value FROM financial_facts WHERE symbol='AAPL'")
            ).all()
        )
        assert rows["TaxRateForCalcs"] == Decimal("0.1560000000")
        assert rows["TotalAssets"] == HUGE_VALUE

    def test_composite_fk_cascades(self, db_session: Session) -> None:
        _seed_symbol(db_session)
        stats = WriteStats()
        writer = PostgresRowWriter(db_session)
        apply_write(writer, _period_write([_period_row()]), stats)
        apply_write(writer, _fact_write([_fact_row("TotalRevenue", "1")]), stats)
        assert _count(db_session, "financial_facts") == 1

        db_session.execute(text("DELETE FROM financial_periods WHERE symbol='AAPL'"))
        assert _count(db_session, "financial_facts") == 0

    def test_orphan_fact_is_rejected(self, db_session: Session) -> None:
        _seed_symbol(db_session)
        stats = WriteStats()
        with pytest.raises(IntegrityError):
            apply_write(PostgresRowWriter(db_session), _fact_write([_fact_row("X", "1")]), stats)
        db_session.rollback()

    def test_symbol_delete_is_restricted(self, db_session: Session) -> None:
        _seed_symbol(db_session)
        apply_write(PostgresRowWriter(db_session), _period_write([_period_row()]), WriteStats())
        with pytest.raises(IntegrityError):
            db_session.execute(text("DELETE FROM symbols WHERE symbol='AAPL'"))
        db_session.rollback()

    def test_replace_scope_only_touches_its_period(self, db_session: Session) -> None:
        """Scope is (symbol, statement, freq, period_end); a neighboring freq is untouched."""
        _seed_symbol(db_session)
        writer = PostgresRowWriter(db_session)
        stats = WriteStats()
        apply_write(
            writer,
            _period_write([_period_row(), _period_row(freq="quarterly", content_hash="h2")]),
            stats,
        )
        apply_write(
            writer,
            _fact_write(
                [
                    _fact_row("A", "1"),
                    _fact_row("B", "2"),
                    _fact_row("A", "3", freq="quarterly"),
                ]
            ),
            stats,
        )
        assert _count(db_session, "financial_facts") == 3

        # Only the annual scope gets rewritten
        apply_write(writer, _fact_write([_fact_row("A", "9")]), WriteStats())
        remaining = db_session.execute(
            text("SELECT freq, item_key, value FROM financial_facts ORDER BY freq, item_key")
        ).all()
        assert [(str(r[0]), r[1]) for r in remaining] == [("annual", "A"), ("quarterly", "A")]
        assert remaining[1][2] == Decimal("3.0000000000")  # quarterly value preserved

    def test_empty_rows_still_delete_scope(self, db_session: Session) -> None:
        """If every item of a period comes back NaN (rows empty), old rows would
        stay forever: an early return would happen before the delete."""
        _seed_symbol(db_session)
        writer = PostgresRowWriter(db_session)
        apply_write(writer, _period_write([_period_row()]), WriteStats())
        apply_write(writer, _fact_write([_fact_row("A", "1")]), WriteStats())
        assert _count(db_session, "financial_facts") == 1

        scope = (
            {
                "symbol": "AAPL",
                "statement": "income",
                "freq": "annual",
                "period_end": PERIOD_END,
            },
        )
        apply_write(writer, _fact_write([], scope=scope), WriteStats())
        assert _count(db_session, "financial_facts") == 0

    def test_verification_counts_composite_keys(self, db_session: Session) -> None:
        _seed_symbol(db_session)
        writer = PostgresRowWriter(db_session)
        apply_write(writer, _period_write([_period_row()]), WriteStats())
        stats = WriteStats()
        apply_write(writer, _fact_write([_fact_row("A", "1"), _fact_row("B", "2")]), stats)
        assert stats.attempted["financial_facts"] == stats.verified["financial_facts"] == 2


class _Gated(HashGatedDataset[None]):
    name = "_repo_gated"
    produces = ("financial_periods", "financial_facts")
    gate_table = "financial_periods"
    child_table = "financial_facts"
    gate_key_columns = GATE_KEY

    def fetch(self, ctx: Any) -> None:  # pragma: no cover
        return None

    def normalize(self, raw: None, symbol: str) -> NormalizedResult:  # pragma: no cover
        return NormalizedResult()


def _gated_result(content_hash: str, fetched_at: datetime) -> NormalizedResult:
    return NormalizedResult(
        writes=[
            _period_write([_period_row(content_hash=content_hash, fetched_at=fetched_at)]),
            _fact_write([_fact_row("A", "1"), _fact_row("B", "2")]),
        ]
    )


class TestHashGateAgainstMySQL:
    def test_second_run_skips_facts_but_advances_fetched_at(self, db_session: Session) -> None:
        writer = PostgresRowWriter(db_session)
        _seed_symbol(db_session)
        dataset = _Gated()

        dataset.upsert(writer, _gated_result("h1", FETCHED_AT))
        assert _count(db_session, "financial_facts") == 2

        later = datetime(2026, 9, 5, 10, 0, 0, 500000, tzinfo=UTC)
        stats = dataset.upsert(writer, _gated_result("h1", later))
        assert stats.skipped["financial_facts"] == 2
        assert _count(db_session, "financial_facts") == 2
        stored = db_session.execute(
            text("SELECT fetched_at, content_hash FROM financial_periods WHERE symbol='AAPL'")
        ).one()
        # fetched_at is "last verified time": it advances even when the hash matches
        assert stored[0] == later
        assert stored[1] == "h1"

    def test_changed_hash_rewrites_facts(self, db_session: Session) -> None:
        writer = PostgresRowWriter(db_session)
        _seed_symbol(db_session)
        dataset = _Gated()
        dataset.upsert(writer, _gated_result("h1", FETCHED_AT))

        changed = NormalizedResult(
            writes=[
                _period_write([_period_row(content_hash="h2")]),
                _fact_write([_fact_row("A", "5")]),
            ]
        )
        stats = dataset.upsert(writer, changed)
        assert stats.attempted["financial_facts"] == 1
        rows = db_session.execute(text("SELECT item_key, value FROM financial_facts")).all()
        # item B dropped out of the source -> replace_scope deleted it too
        assert [(r[0], r[1]) for r in rows] == [("A", Decimal("5.0000000000"))]

    def test_fact_count_matches_item_count_sum(self, db_session: Session) -> None:
        writer = PostgresRowWriter(db_session)
        _seed_symbol(db_session)
        _Gated().upsert(writer, _gated_result("h1", FETCHED_AT))
        facts = _count(db_session, "financial_facts")
        total = db_session.execute(
            text("SELECT SUM(item_count) FROM financial_periods")
        ).scalar_one()
        assert facts == int(total)


class TestSchemaInvariants:
    def test_statement_and_freq_enums_have_single_definition(self, db_session: Session) -> None:
        """If two tables' ENUM definitions diverge, an FK over the ordinal silently
        links to the wrong row; MySQL warns on neither CREATE nor INSERT."""
        rows = db_session.execute(
            text(
                "SELECT column_name, COUNT(DISTINCT data_type) FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND column_name IN ('statement','freq') "
                "GROUP BY COLUMN_NAME"
            )
        ).all()
        assert {r[0] for r in rows} == {"statement", "freq"}
        assert all(r[1] == 1 for r in rows)

    def test_key_text_columns_are_case_and_accent_sensitive(self, db_session: Session) -> None:
        """A case-insensitive collation would treat 'Enflasyon' == 'ENFLASYON'."""
        rows = [
            {
                "region": "TR",
                "event_time_utc": datetime(2026, 1, 5, 7, 0),
                "event_name": name,
                "period_for": "Dec",
                "actual": None,
                "expected": None,
                "last_reported": None,
                "revised": None,
                "fetched_at": FETCHED_AT,
            }
            for name in ("Enflasyon", "ENFLASYON", "Enflâsyon")
        ]
        apply_write(
            PostgresRowWriter(db_session),
            TableWrite(
                table="calendar_economic",
                rows=rows,
                key_columns=("region", "event_time_utc", "event_name"),
                update_columns=("period_for", "fetched_at"),
            ),
            WriteStats(),
        )
        assert _count(db_session, "calendar_economic") == 3

    def test_short_hash_is_case_sensitive(self, db_session: Session) -> None:
        _seed_symbol(db_session)
        rows = [
            {
                "symbol": "AAPL",
                "earnings_ts_utc": datetime(2002, 7, 16, 20, 0),
                "fact_hash": value,
                "earnings_date_local": date(2002, 7, 16),
                "tz_name": "America/New_York",
                "eps_estimate": None,
                "reported_eps": None,
                "surprise_pct": Decimal("2.55"),
                "fetched_at": FETCHED_AT,
            }
            for value in ("abcdef0123456789", "ABCDEF0123456789")
        ]
        apply_write(
            PostgresRowWriter(db_session),
            TableWrite(
                table="earnings_dates",
                rows=rows,
                key_columns=("symbol", "earnings_ts_utc", "fact_hash"),
                update_columns=("surprise_pct", "fetched_at"),
            ),
            WriteStats(),
        )
        assert _count(db_session, "earnings_dates") == 2

    def test_same_filing_two_exhibits_same_type(self, db_session: Session) -> None:
        """Two EX-99.1 exhibits with different URLs in the same filing happen in
        practice; without url_hash in the PK the second would fail as a duplicate."""
        _seed_symbol(db_session)
        writer = PostgresRowWriter(db_session)
        apply_write(
            writer,
            TableWrite(
                table="sec_filings",
                rows=[
                    {
                        "symbol": "AAPL",
                        "filing_id": FILING_ID,
                        "filing_date": date(2026, 9, 1),
                        "filed_ts_utc": datetime(2026, 9, 1),
                        "filing_type": "8-K",
                        "title": "t",
                        "edgar_url": "u",
                        "exhibit_count": 1,
                        "raw_json": "{}",
                        "fetched_at": FETCHED_AT,
                    }
                ],
                key_columns=("symbol", "filing_id"),
                update_columns=("filing_type", "fetched_at"),
            ),
            WriteStats(),
        )
        exhibits = [
            {
                "symbol": "AAPL",
                "filing_id": FILING_ID,
                "exhibit_type": "EX-99.1",
                "url_hash": digest,
                "url": url,
            }
            for digest, url in (("a" * 16, "https://a"), ("b" * 16, "https://b"))
        ]
        apply_write(
            writer,
            TableWrite(
                table="sec_filing_exhibits",
                rows=exhibits,
                key_columns=("symbol", "filing_id", "exhibit_type", "url_hash"),
                update_columns=("url",),
            ),
            WriteStats(),
        )
        assert _count(db_session, "sec_filing_exhibits") == 2

    def test_sync_runs_scope_defaults_to_symbols(self, db_session: Session) -> None:
        db_session.execute(
            text(
                "INSERT INTO sync_runs (started_at, status, symbol_count, dataset_count) "
                "VALUES (now(), 'running', 1, 1)"
            )
        )
        scope = db_session.execute(
            text("SELECT scope FROM sync_runs ORDER BY id DESC LIMIT 1")
        ).scalar_one()
        assert scope == "symbols"

    def test_period_end_index_is_used(self, db_session: Session) -> None:
        """Without an index on (period_end, item_key), financial_facts would full-scan.

        `enable_seqscan = off` is required: on an empty table the planner always
        picks a Seq Scan (correctly -- an index lookup is more expensive there).
        The test checks the index exists and is usable, not the planner's choice
        on an empty table.
        """
        db_session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            db_session.execute(
                text(
                    "EXPLAIN SELECT symbol, item_key, value FROM financial_facts "
                    "WHERE period_end = '2025-09-30'"
                )
            )
            .scalars()
            .all()
        )
        assert "Index" in plan, f"index not used:\n{plan}"


class TestPrune:
    """Pruning is disabled by default; date-bounded deletes need explicit enabling."""

    def _splits_row(self, when: datetime) -> dict[str, Any]:
        return {
            "symbol": "AAPL",
            "payable_on_utc": when,
            "company": "Apple",
            "optionable": True,
            "old_share_worth": 1,
            "share_worth": 4,
            "ratio": Decimal("4"),
            "is_known": True,
            "fetched_at": FETCHED_AT,
        }

    def _seed_calendar(self, session: Session, *whens: datetime) -> None:
        _seed_symbol(session)
        apply_write(
            PostgresRowWriter(session),
            TableWrite(
                table="calendar_splits",
                rows=[self._splits_row(w) for w in whens],
                key_columns=("symbol", "payable_on_utc"),
                update_columns=("company", "fetched_at"),
            ),
            WriteStats(),
        )

    def test_disabled_by_default_refuses_to_delete(self, db_session: Session) -> None:
        from yfin.pipeline.prune import PruneDisabledError, run_prune

        old = datetime(2020, 1, 1, 4, 0)
        self._seed_calendar(db_session, old)
        with pytest.raises(PruneDisabledError, match="YF_PRUNE_ENABLED"):
            run_prune(db_session, enabled=False, calendars_before=datetime(2024, 1, 1))
        assert _count(db_session, "calendar_splits") == 1  # row still there

    def test_default_settings_have_prune_disabled(self) -> None:
        from yfin.core.config import Settings

        assert Settings().yf_prune_enabled is False

    def test_orphan_news_still_runs_when_disabled(self, db_session: Session) -> None:
        """Orphan-news cleanup is not date-bounded; it runs even when disabled."""
        from yfin.pipeline.prune import run_prune

        report = run_prune(db_session, enabled=False)
        assert report.calendars == {}
        assert report.history == {}

    def test_enabled_deletes_only_rows_before_cutoff(self, db_session: Session) -> None:
        from yfin.pipeline.prune import run_prune

        old, new = (
            datetime(2020, 1, 1, 4, 0, tzinfo=UTC),
            datetime(2026, 9, 1, 4, 0, tzinfo=UTC),
        )
        self._seed_calendar(db_session, old, new)
        report = run_prune(
            db_session, enabled=True, orphan_news=False, calendars_before=datetime(2024, 1, 1)
        )
        assert report.calendars["calendar_splits"] == 1
        remaining = (
            db_session.execute(text("SELECT payable_on_utc FROM calendar_splits")).scalars().all()
        )
        assert remaining == [new]

    def test_dry_run_counts_without_deleting(self, db_session: Session) -> None:
        from yfin.pipeline.prune import run_prune

        old = datetime(2020, 1, 1, 4, 0)
        self._seed_calendar(db_session, old)
        report = run_prune(
            db_session,
            enabled=True,
            orphan_news=False,
            calendars_before=datetime(2024, 1, 1),
            dry_run=True,
        )
        assert report.calendars["calendar_splits"] == 1
        assert _count(db_session, "calendar_splits") == 1  # not deleted

    def test_history_tables_are_derived_from_metadata(self) -> None:
        """Derived from metadata, not a hand-written list: a new snapshot pair is
        covered automatically."""
        from yfin.pipeline.prune import history_tables

        derived = set(history_tables())
        assert {
            "ticker_info_history",
            "ticker_fast_info_history",
            "ticker_calendar_history",
            "market_status_history",
            "market_summary_history",
        } <= derived
        assert all(name.endswith("_history") for name in derived)

    def test_history_prune_respects_cutoff(self, db_session: Session) -> None:
        from yfin.pipeline.prune import run_prune

        _seed_symbol(db_session)
        rows = [
            {
                "region": "US",
                "board_code": "CME",
                "fetched_at": when,
                "symbol": None,
                "is_known": False,
                "raw_json": "{}",
                "content_hash": "h",
            }
            for when in (datetime(2020, 1, 1), datetime(2026, 9, 1))
        ]
        apply_write(
            PostgresRowWriter(db_session),
            TableWrite(
                table="market_summary_history",
                rows=rows,
                key_columns=("region", "board_code", "fetched_at"),
                update_columns=("raw_json", "content_hash"),
            ),
            WriteStats(),
        )
        report = run_prune(
            db_session, enabled=True, orphan_news=False, history_before=datetime(2024, 1, 1)
        )
        assert report.history["market_summary_history"] == 1
        assert _count(db_session, "market_summary_history") == 1


class TestValuationStatementKind:
    """The 'valuation' ENUM value and the migration that added it (real PostgreSQL)."""

    def test_valuation_period_and_facts_are_accepted(self, db_session: Session) -> None:
        """The same (symbol, freq, period) can carry both income and valuation;
        `statement` in the PK is what tells them apart."""
        _seed_symbol(db_session)
        stats = WriteStats()
        writer = PostgresRowWriter(db_session)
        apply_write(
            writer, _period_write([_period_row(), _period_row(statement="valuation")]), stats
        )
        apply_write(
            writer,
            _fact_write(
                [
                    _fact_row("TotalRevenue", "1000.0000000000"),
                    _fact_row("Market Cap", HUGE_VALUE, statement="valuation"),
                    _fact_row("PEG Ratio (5yr expected)", "1.9300000000", statement="valuation"),
                ]
            ),
            stats,
        )
        db_session.flush()
        assert _count(db_session, "financial_periods") == 2
        assert _count(db_session, "financial_facts") == 3
        kinds = set(
            db_session.execute(text("SELECT DISTINCT statement FROM financial_facts")).scalars()
        )
        assert kinds == {"income", "valuation"}

    def test_orphan_valuation_fact_is_rejected(self, db_session: Session) -> None:
        """The composite FK is still enforced for the new ENUM value."""
        _seed_symbol(db_session)
        writer = PostgresRowWriter(db_session)
        with pytest.raises(IntegrityError):
            apply_write(
                writer,
                _fact_write([_fact_row("Market Cap", "1.0000000000", statement="valuation")]),
                WriteStats(),
            )
            db_session.flush()

    def test_enum_carries_every_statement_kind(self, db_session: Session) -> None:
        """The ENUM type must carry every member of StatementKind.

        This replaces a former migration test: MySQL had a separate revision
        adding 'valuation', requiring careful downgrade/upgrade around FK
        checks. PostgreSQL's ENUM is its own type object, so adding a value
        never touches the FK (measured: ALTER TYPE ... ADD VALUE leaves a
        row with a composite FK intact). The invariant that matters --
        the schema accepts every value the code knows -- is unchanged.
        """
        from yfin.models import StatementKind

        labels = set(
            db_session.execute(
                text(
                    "SELECT e.enumlabel FROM pg_enum e "
                    "  JOIN pg_type t ON t.oid = e.enumtypid "
                    " WHERE t.typname = 'statement_kind'"
                )
            ).scalars()
        )
        assert labels == {member.value for member in StatementKind}
        assert "valuation" in labels


class TestSchemaIsolation:
    """Per-process schema cleanup."""

    def test_stale_schema_dropped_but_live_and_base_kept(self, settings: Any) -> None:
        """A concurrent run's schema is untouched; only dead PIDs get dropped."""
        import os

        from sqlalchemy import create_engine

        from helpers import drop_stale_schemas

        base = settings.db_test_name
        # Connect to the test database, not the bootstrap one (postgres):
        # information_schema is per-database, so a bootstrap connection
        # cannot see these schemas.
        engine = create_engine(settings.db_url(base), isolation_level="AUTOCOMMIT")
        stale = f"{base}_999999"
        live = f"{base}_{os.getpid()}"
        with engine.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{stale}"'))
            # The base name (no numeric suffix) must survive: cleanup only
            # targets the `<base>_<pid>` pattern.
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{base}"'))

        dropped = drop_stale_schemas(engine, base)

        with engine.connect() as conn:
            names = {
                n
                for n in conn.execute(
                    text("SELECT schema_name FROM information_schema.schemata")
                ).scalars()
                if n.startswith(base)
            }
        engine.dispose()
        assert stale in dropped
        assert stale not in names
        assert live in names  # this run's own schema stays
        assert base in names  # base schema stays

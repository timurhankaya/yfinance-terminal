"""Financials/market repository testleri: gercek MySQL 8.3, agsiz (S9.2)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from yfin.datasets.base import NormalizedResult, TableWrite, WriteStats
from yfin.datasets.hash_gated import HashGatedDataset
from yfin.models import Base
from yfin.persistence import PostgresRowWriter, apply_write

pytestmark = pytest.mark.repo

GATE_KEY = ("symbol", "statement", "freq", "period_end")
PERIOD_END = date(2025, 9, 30)
FETCHED_AT = datetime(2026, 9, 4, 10, 0, 0, 500000)
# Gercek accession numarasi ayristirmasi unit testte dogrulanir; burada
# yalnizca anahtar davranisi onemli oldugu icin sade bir deger kullanilir
FILING_ID = "acc-test-a1"
# 7203.T'de olculen en buyuk mutlak deger mertebesi
HUGE_VALUE = Decimal("1.05522331e14").quantize(Decimal("1E-10"))


def _seed_symbol(session: Session, symbol: str = "AAPL") -> None:
    session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
            "VALUES (:s, 1, 0, NOW(6), NOW(6)) ON DUPLICATE KEY UPDATE symbol = symbol"
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
        """DECIMAL(38,10): oran ve cok buyuk deger ayni kolonda kayipsiz."""
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
        """Kapsam (symbol, statement, freq, period_end); komsu frekans
        dokunulmadan kalir."""
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

        # Yalnizca annual kapsami yeniden yazilir
        apply_write(writer, _fact_write([_fact_row("A", "9")]), WriteStats())
        remaining = db_session.execute(
            text("SELECT freq, item_key, value FROM financial_facts ORDER BY freq, item_key")
        ).all()
        assert [(str(r[0]), r[1]) for r in remaining] == [("annual", "A"), ("quarterly", "A")]
        assert remaining[1][2] == Decimal("3.0000000000")  # quarterly degeri korundu

    def test_empty_rows_still_delete_scope(self, db_session: Session) -> None:
        """Donemin TUM kalemleri NaN geldiginde (rows bos) eski satirlar
        kalici olarak kalirdi: erken cikis silmeden ONCE oldugu icin."""
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

        later = datetime(2026, 9, 5, 10, 0, 0, 500000)
        stats = dataset.upsert(writer, _gated_result("h1", later))
        assert stats.skipped["financial_facts"] == 2
        assert _count(db_session, "financial_facts") == 2
        stored = db_session.execute(
            text("SELECT fetched_at, content_hash FROM financial_periods WHERE symbol='AAPL'")
        ).one()
        # fetched_at "son dogrulama zamani"dir: hash esitse bile ilerler
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
        # B kalemi kaynaktan kalkti -> replace_scope onu da sildi
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
        """Iki tabloda ENUM tanimi ayrisirsa FK ORDINAL uzerinden sessizce
        yanlis satira baglanir; MySQL ne CREATE ne INSERT'te uyarir."""
        rows = db_session.execute(
            text(
                "SELECT COLUMN_NAME, COUNT(DISTINCT COLUMN_TYPE) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND COLUMN_NAME IN ('statement','freq') "
                "GROUP BY COLUMN_NAME"
            )
        ).all()
        assert {r[0] for r in rows} == {"statement", "freq"}
        assert all(r[1] == 1 for r in rows)

    def test_key_text_columns_are_case_and_accent_sensitive(self, db_session: Session) -> None:
        """Varsayilan utf8mb4_0900_ai_ci 'Enflasyon' = 'ENFLASYON' sayar."""
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
        """Ayni dosyalamada iki farkli URL'li EX-99.1 gercekte olur;
        url_hash PK'da olmasaydi ikincisi ERROR 1062 ile duserdi."""
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
                "VALUES (NOW(6), 'running', 1, 1)"
            )
        )
        scope = db_session.execute(
            text("SELECT scope FROM sync_runs ORDER BY id DESC LIMIT 1")
        ).scalar_one()
        assert scope == "symbols"

    def test_period_end_index_is_used(self, db_session: Session) -> None:
        """financial_facts (period_end, item_key) olmadan full scan olurdu."""
        plan = (
            db_session.execute(
                text(
                    "EXPLAIN SELECT symbol, item_key, value FROM financial_facts "
                    "WHERE period_end = '2025-09-30'"
                )
            )
            .mappings()
            .one()
        )
        assert plan["key"] is not None


class TestPrune:
    """Budama VARSAYILAN KAPALI (S7.4); tarih sinirli silme acikca
    etkinlestirilmeden calismaz."""

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
        from yfin.prune import PruneDisabledError, run_prune

        old = datetime(2020, 1, 1, 4, 0)
        self._seed_calendar(db_session, old)
        with pytest.raises(PruneDisabledError, match="YF_PRUNE_ENABLED"):
            run_prune(db_session, enabled=False, calendars_before=datetime(2024, 1, 1))
        assert _count(db_session, "calendar_splits") == 1  # satir DURUYOR

    def test_default_settings_have_prune_disabled(self) -> None:
        from yfin.config import Settings

        assert Settings().yf_prune_enabled is False

    def test_orphan_news_still_runs_when_disabled(self, db_session: Session) -> None:
        """Oksuz haber temizligi tarih sinirli DEGIL; kapali modda da calisir."""
        from yfin.prune import run_prune

        report = run_prune(db_session, enabled=False)
        assert report.calendars == {}
        assert report.history == {}

    def test_enabled_deletes_only_rows_before_cutoff(self, db_session: Session) -> None:
        from yfin.prune import run_prune

        old, new = datetime(2020, 1, 1, 4, 0), datetime(2026, 9, 1, 4, 0)
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
        from yfin.prune import run_prune

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
        assert _count(db_session, "calendar_splits") == 1  # SILINMEDI

    def test_history_tables_are_derived_from_metadata(self) -> None:
        """Elle liste yerine metadata: yeni snapshot cifti otomatik kapsanir."""
        from yfin.prune import history_tables

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
        from yfin.prune import run_prune

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
    """'valuation' ENUM degeri ve onu ekleyen migration (gercek MySQL)."""

    def test_valuation_period_and_facts_are_accepted(self, db_session: Session) -> None:
        """Ayni (sembol, freq, donem) hem income hem valuation tasiyabilir;
        ayrimi PK'daki `statement` yapar."""
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
        """Bilesik FK yeni ENUM degerinde de zorlanir."""
        _seed_symbol(db_session)
        writer = PostgresRowWriter(db_session)
        with pytest.raises(IntegrityError):
            apply_write(
                writer,
                _fact_write([_fact_row("Market Cap", "1.0000000000", statement="valuation")]),
                WriteStats(),
            )
            db_session.flush()

    def test_migration_widens_enum_under_foreign_key(self, db_session: Session) -> None:
        """MySQL FK'li kolonun tipini degistirmeyi reddeder; migration bunu
        FOREIGN_KEY_CHECKS=0 arasinda yapar. Once daraltip sonra genisletmek
        iki yonu de ayni testte kanitlar.

        DDL MySQL'de implicit commit yapar: test transaction'i bu degisikligi
        geri almaz, bu yuzden upgrade() SON adimdir ve sema testin
        basindaki haline doner.
        """
        import importlib.util
        from pathlib import Path

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = next(Path("migrations/versions").glob("*_valuation_statement_kind.py"))
        spec = importlib.util.spec_from_file_location("mig_valuation", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        connection = db_session.connection()
        migration_context = MigrationContext.configure(connection)

        def enum_definition() -> str:
            return str(
                connection.execute(
                    text(
                        "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() "
                        "AND TABLE_NAME = 'financial_facts' AND COLUMN_NAME = 'statement'"
                    )
                ).scalar_one()
            )

        with Operations.context(migration_context):
            module.downgrade()
            assert "valuation" not in enum_definition()
            module.upgrade()
        assert "valuation" in enum_definition()


class TestSchemaIsolation:
    """Surece ozel sema temizligi (S9.2 notu)."""

    def test_stale_schema_dropped_but_live_and_base_kept(self, settings: Any) -> None:
        """Es zamanli kosunun semasina DOKUNULMAZ; yalnizca olu PID silinir."""
        import os

        from sqlalchemy import create_engine

        from helpers import drop_stale_schemas

        base = settings.db_test_name
        bootstrap = create_engine(settings.bootstrap_url())
        stale = f"{base}_999999"
        live = f"{base}_{os.getpid()}"
        with bootstrap.connect() as conn:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{stale}`"))
            conn.commit()

        dropped = drop_stale_schemas(bootstrap, base)

        with bootstrap.connect() as conn:
            names = {
                n
                for n in conn.execute(
                    text("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")
                ).scalars()
                if n.startswith(base)
            }
        bootstrap.dispose()
        assert stale in dropped
        assert stale not in names
        assert live in names  # bu kosunun kendi semasi durur
        assert base in names  # taban sema durur

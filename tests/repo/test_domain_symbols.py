"""`symbols`a yazilan 156 satirin politikasi (SI S9.3, S5.8, S7.8)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from domain_support import NOW, run_taxonomy

pytestmark = pytest.mark.repo


def test_domain_symbols_are_written_inactive(db_session: Session) -> None:
    """Varsayilan `yfin sync` bu sembolleri CEKMEZ."""
    run_taxonomy(db_session, ("technology",))
    rows = list(
        db_session.execute(
            text(
                "SELECT symbol, is_active, quote_type, exchange, currency, timezone "
                "FROM symbols WHERE quote_type = 'INDEX'"
            )
        )
    )
    assert rows
    for row in rows:
        assert row.is_active == 0
        assert row.symbol.startswith("^YH")
        assert (row.exchange, row.currency, row.timezone) == (
            "YHD",
            "USD",
            "America/New_York",
        )


def test_manual_activation_is_not_overwritten(db_session: Session) -> None:
    """`is_active` ve `unknown_streak` `update_columns` DISINDA."""
    run_taxonomy(db_session, ("technology",))
    symbol = db_session.execute(
        text("SELECT symbol FROM domains WHERE domain_key = 'technology'")
    ).scalar_one()
    db_session.execute(
        text("UPDATE symbols SET is_active = 1, unknown_streak = 3 WHERE symbol = :s"),
        {"s": symbol},
    )

    run_taxonomy(db_session, ("technology",), fetched_at=NOW + timedelta(days=1))
    row = db_session.execute(
        text("SELECT is_active, unknown_streak, last_seen_at FROM symbols WHERE symbol = :s"),
        {"s": symbol},
    ).one()
    assert row.is_active == 1
    assert row.unknown_streak == 3
    assert row.last_seen_at == NOW + timedelta(days=1)


def test_short_name_is_refreshed(db_session: Session) -> None:
    run_taxonomy(db_session, ("technology",))
    symbol = db_session.execute(
        text("SELECT symbol FROM domains WHERE domain_key = 'technology'")
    ).scalar_one()
    db_session.execute(
        text("UPDATE symbols SET short_name = 'ESKI' WHERE symbol = :s"), {"s": symbol}
    )
    run_taxonomy(db_session, ("technology",))
    assert (
        db_session.execute(
            text("SELECT short_name FROM symbols WHERE symbol = :s"), {"s": symbol}
        ).scalar_one()
        == "Technology"
    )


def test_symbols_are_included_in_symbol_scoped_tables(db_session: Session) -> None:
    """`domains` FK tasidigi icin `yfin symbols purge` onu KENDILIGINDEN kapsar."""
    from yfin.models import symbol_scoped_tables

    assert "domains" in symbol_scoped_tables()
    # FK TASIMAYAN domain tablolari kapsamda OLMAMALI: oradaki `symbol`
    # sirketin sembolu.
    for table in ("domain_top_companies", "domain_top_funds", "domain_top_movers"):
        assert table not in symbol_scoped_tables(), table

"""Schema decisions for the 18 new tables.

Each test pins one decision that independent review verified against actual
PostgreSQL. All run against the yfinance_test schema and roll back afterward.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from yfin.models import Base

AS_OF = date(2026, 9, 4)
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

AS_OF_TABLES = (
    "analyst_recommendations",
    "analyst_price_targets",
    "analyst_estimates",
    "analyst_eps_trend",
    "analyst_eps_revisions",
    "analyst_growth_estimates",
    "holder_breakdown",
    "institutional_holders",
    "insider_activity",
    "insider_roster",
    "fund_profile",
    "fund_metrics",
    "fund_weightings",
    "fund_top_holdings",
)

NEW_TABLES = (
    *AS_OF_TABLES,
    "analyst_grade_changes",
    "earnings_history",
    "insider_transactions",
    "asof_state",
)


def test_all_eighteen_tables_registered() -> None:
    assert len(NEW_TABLES) == 18
    for name in NEW_TABLES:
        assert name in Base.metadata.tables, name


def test_as_of_tables_have_as_of_date_in_primary_key() -> None:
    """Without as_of_date in the PK, the table cannot keep history."""
    for name in AS_OF_TABLES:
        pk = [c.name for c in Base.metadata.tables[name].primary_key.columns]
        assert pk[:2] == ["symbol", "as_of_date"], f"{name} -> {pk}"


def test_event_tables_have_no_as_of_date() -> None:
    """Tables whose source carries its own date are not as-of tables."""
    for name in ("analyst_grade_changes", "earnings_history", "insider_transactions"):
        assert "as_of_date" not in Base.metadata.tables[name].c, name


def test_fund_metrics_pk_includes_section() -> None:
    """Without section in the PK, the same metric name in two sections would
    collide; sibling table fund_weightings already puts category in its PK."""
    pk = [c.name for c in Base.metadata.tables["fund_metrics"].primary_key.columns]
    assert pk == ["symbol", "as_of_date", "section", "metric"]


def test_holding_rank_is_not_named_rank() -> None:
    """`rank` is a reserved word in MySQL 8: CREATE TABLE ... rank -> ERROR 1064."""
    cols = Base.metadata.tables["fund_top_holdings"].c
    assert "rank" not in cols
    assert "holding_rank" in cols


def test_holding_symbol_has_no_foreign_key_but_has_index() -> None:
    """An FK on an out-of-universe symbol would roll back the fund's whole row set."""
    table = Base.metadata.tables["fund_top_holdings"]
    assert table.c["holding_symbol"].foreign_keys == set()
    assert table.c["symbol"].foreign_keys != set()
    indexed = {tuple(c.name for c in i.columns) for i in table.indexes}
    assert ("holding_symbol",) in indexed


def test_ownership_column_fits_the_measured_value() -> None:
    """Measured 'D/I' on XOM; VARCHAR(2) would truncate it."""
    col = Base.metadata.tables["insider_transactions"].c["ownership"]
    assert col.type.length >= 3


def test_nullable_columns_that_were_never_measured_non_null() -> None:
    """If date_reported or net_trans were NOT NULL/UNSIGNED, one NaT or negative
    value would drop the symbol's entire transaction set."""
    assert Base.metadata.tables["institutional_holders"].c["date_reported"].nullable
    net_trans = Base.metadata.tables["insider_activity"].c["net_trans"]
    assert net_trans.nullable
    assert getattr(net_trans.type, "unsigned", False) is False


def test_every_new_table_has_symbol_fk_to_symbols() -> None:
    for name in NEW_TABLES:
        fks = Base.metadata.tables[name].c["symbol"].foreign_keys
        assert fks, name
        fk = next(iter(fks))
        assert fk.column.table.name == "symbols"
        assert fk.ondelete == "RESTRICT"
        assert fk.onupdate == "CASCADE"


def test_asof_state_first_seen_is_not_nullable() -> None:
    cols = Base.metadata.tables["asof_state"].c
    assert not cols["first_seen_at"].nullable
    assert not cols["fetched_at"].nullable


# --------------------------------------------------------------------------
# Actual PostgreSQL
# --------------------------------------------------------------------------


def _insert(session: Session, table: str, **values: object) -> None:
    # PostgreSQL quotes identifiers with double quotes; backticks are a
    # syntax error.
    cols = ", ".join(f'"{k}"' for k in values)
    binds = ", ".join(f":{k}" for k in values)
    session.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({binds})"), values)


@pytest.fixture
def symbol(db_session: Session) -> str:
    code = "ZZTEST"
    _insert(db_session, "symbols", symbol=code, created_at=NOW, updated_at=NOW)
    return code


def test_fund_metrics_same_name_in_two_sections(db_session: Session, symbol: str) -> None:
    """The same metric name can exist in two sections because section is in the PK."""
    for section in ("equity", "bond"):
        _insert(
            db_session,
            "fund_metrics",
            symbol=symbol,
            as_of_date=AS_OF,
            section=section,
            metric="duration",
            value=Decimal("1.5"),
            fetched_at=NOW,
        )
    count = db_session.execute(
        text("SELECT COUNT(*) FROM fund_metrics WHERE symbol = :s"), {"s": symbol}
    ).scalar_one()
    assert count == 2


def test_fact_value_type_carries_eps_and_revenue_in_one_column(
    db_session: Session, symbol: str
) -> None:
    """1.97656 and 1_285_436_390_920 share a column -- why it is DECIMAL(38,10)."""
    _insert(
        db_session,
        "analyst_estimates",
        symbol=symbol,
        as_of_date=AS_OF,
        metric="eps",
        period="0q",
        avg=Decimal("1.97656"),
        fetched_at=NOW,
    )
    _insert(
        db_session,
        "analyst_estimates",
        symbol=symbol,
        as_of_date=AS_OF,
        metric="revenue",
        period="0q",
        avg=Decimal("1285436390920"),
        fetched_at=NOW,
    )
    rows = dict(
        db_session.execute(
            text("SELECT metric, avg FROM analyst_estimates WHERE symbol = :s"), {"s": symbol}
        ).all()
    )
    assert rows["eps"] == Decimal("1.9765600000")
    assert rows["revenue"] == Decimal("1285436390920.0000000000")


def test_insider_activity_accepts_negative_net_shares(db_session: Session, symbol: str) -> None:
    """Measured net -547806 on KO."""
    _insert(
        db_session,
        "insider_activity",
        symbol=symbol,
        as_of_date=AS_OF,
        period_label="6m",
        net_shares=Decimal("-547806"),
        net_trans=-3,
        fetched_at=NOW,
    )
    row = db_session.execute(
        text("SELECT net_shares, net_trans FROM insider_activity WHERE symbol = :s"),
        {"s": symbol},
    ).one()
    assert row.net_shares == Decimal("-547806")
    assert row.net_trans == -3


def test_ownership_stores_three_character_value(db_session: Session, symbol: str) -> None:
    _insert(
        db_session,
        "insider_transactions",
        symbol=symbol,
        start_date=AS_OF,
        fact_hash="a" * 16,
        ownership="D/I",
        fetched_at=NOW,
    )
    value = db_session.execute(
        text("SELECT ownership FROM insider_transactions WHERE symbol = :s"), {"s": symbol}
    ).scalar_one()
    assert value == "D/I"


def test_holder_scope_delete_leaves_sibling_type(db_session: Session, symbol: str) -> None:
    """replace_scope is (symbol, as_of_date, holder_type) and leaves the
    sibling type untouched -- this is what lets two datasets share one table."""
    for holder_type, holder in (("institution", "Vanguard"), ("mutualfund", "VFIAX")):
        _insert(
            db_session,
            "institutional_holders",
            symbol=symbol,
            as_of_date=AS_OF,
            holder_type=holder_type,
            holder=holder,
            fetched_at=NOW,
        )
    db_session.execute(
        text(
            "DELETE FROM institutional_holders "
            "WHERE symbol = :s AND as_of_date = :d AND holder_type = 'institution'"
        ),
        {"s": symbol, "d": AS_OF},
    )
    remaining = db_session.execute(
        text("SELECT holder_type, holder FROM institutional_holders WHERE symbol = :s"),
        {"s": symbol},
    ).all()
    assert remaining == [("mutualfund", "VFIAX")]


def test_as_of_pk_upserts_within_the_same_day(db_session: Session, symbol: str) -> None:
    """A second run on the same day overwrites the row rather than inserting a new one."""
    _insert(
        db_session,
        "analyst_recommendations",
        symbol=symbol,
        as_of_date=AS_OF,
        period="0m",
        strong_buy=1,
        buy=2,
        hold=3,
        sell=4,
        strong_sell=5,
        fetched_at=NOW,
    )
    db_session.execute(
        text(
            "INSERT INTO analyst_recommendations "
            "(symbol, as_of_date, period, strong_buy, buy, hold, sell, strong_sell, fetched_at) "
            "VALUES (:s, :d, '0m', 9, 9, 9, 9, 9, :t) "
            # PostgreSQL equivalent of MySQL's VALUES(...): the conflict target is
            # explicit and the new value is read via `excluded`.
            "ON CONFLICT (symbol, as_of_date, period) "
            "DO UPDATE SET strong_buy = excluded.strong_buy"
        ),
        {"s": symbol, "d": AS_OF, "t": NOW},
    )
    rows = db_session.execute(
        text("SELECT COUNT(*), MAX(strong_buy) FROM analyst_recommendations WHERE symbol = :s"),
        {"s": symbol},
    ).one()
    assert rows == (1, 9)


def test_unknown_symbol_is_rejected_by_fk(db_session: Session) -> None:
    with pytest.raises((IntegrityError, OperationalError)):
        _insert(
            db_session,
            "asof_state",
            symbol="NOSUCH",
            dataset="recommendations",
            as_of_date=AS_OF,
            content_hash="0" * 64,
            row_count=1,
            first_seen_at=NOW,
            fetched_at=NOW,
        )
        db_session.flush()


def test_holding_symbol_accepts_out_of_universe_value(db_session: Session, symbol: str) -> None:
    """No FK means an out-of-universe symbol can be written; is_known flags it."""
    _insert(
        db_session,
        "fund_top_holdings",
        symbol=symbol,
        as_of_date=AS_OF,
        holding_symbol="BRK-B",
        holding_rank=0,
        is_known=False,
        fetched_at=NOW,
    )
    row = db_session.execute(
        text("SELECT holding_symbol, is_known FROM fund_top_holdings WHERE symbol = :s"),
        {"s": symbol},
    ).one()
    assert row.holding_symbol == "BRK-B"
    assert row.is_known == 0

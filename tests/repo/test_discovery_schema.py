"""The actual schema of the discovery tables (real PostgreSQL, no network)."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

pytestmark = pytest.mark.repo

DISCOVERY_TABLES = (
    "discovery_asof_state",
    "search_quotes",
    "search_lists",
    "search_report_hits",
    "lookup_results",
    "lookup_totals",
    "screens",
    "screen_runs",
    "screen_members",
    "screen_quotes",
)


def _inspector(db_session: Session):  # type: ignore[no-untyped-def]
    return inspect(db_session.get_bind())


def test_all_tables_exist(db_session: Session) -> None:
    insp = _inspector(db_session)
    for table in DISCOVERY_TABLES:
        assert insp.has_table(table), table


def test_primary_keys(db_session: Session) -> None:
    insp = _inspector(db_session)
    expected = {
        "discovery_asof_state": ["query_term", "dataset"],
        "search_quotes": ["query_term", "as_of_date", "symbol"],
        "search_lists": ["query_term", "as_of_date", "list_key"],
        "search_report_hits": ["query_term", "as_of_date", "report_id"],
        "lookup_results": ["query_term", "as_of_date", "symbol"],
        "lookup_totals": ["query_term", "as_of_date", "lookup_type"],
        "screens": ["screen_key"],
        "screen_runs": ["screen_key", "as_of_date"],
        "screen_members": ["screen_key", "as_of_date", "symbol"],
        # Independent of the screen. If `screen_key` were in the PK, a symbol
        # appearing in five screens would have its 102 fields written five times.
        "screen_quotes": ["symbol", "as_of_date"],
    }
    for table, cols in expected.items():
        assert insp.get_pk_constraint(table)["constrained_columns"] == cols, table


def test_discovery_gate_has_no_foreign_key(db_session: Session) -> None:
    """This is why the table exists at all.

    `asof_state.symbol` carries an FK to `symbols.symbol`; a free-text search
    term isn't there, so the gate row would fail with `ERROR 1452`. If
    someone adds an FK here later "for consistency", `yfin discover term`
    breaks loudly, not silently -- and this test goes red first.
    """
    assert _inspector(db_session).get_foreign_keys("discovery_asof_state") == []


def test_symbol_columns_have_no_foreign_key(db_session: Session) -> None:
    """Discovery datasets return out-of-universe symbols by definition."""
    insp = _inspector(db_session)
    for table in ("search_quotes", "lookup_results", "screen_members", "screen_quotes"):
        referred = {fk["referred_table"] for fk in insp.get_foreign_keys(table)}
        assert "symbols" not in referred, table


def test_symbol_columns_are_indexed(db_session: Session) -> None:
    """No FK means the index must be defined explicitly.

    An FK'd column can get an index automatically; without one it does not,
    and `WHERE symbol = ...` would be a full table scan.
    """
    insp = _inspector(db_session)
    for table in ("search_quotes", "lookup_results", "screen_members"):
        indexed = {tuple(ix["column_names"]) for ix in insp.get_indexes(table)}
        assert ("symbol",) in indexed, table


def test_report_hits_has_foreign_key_to_reports(db_session: Session) -> None:
    """Unlike the symbol columns, an FK exists here: `report_id` is not a
    symbol, has no out-of-universe problem, and the parent is written in the
    same transaction before the gated writes."""
    fks = _inspector(db_session).get_foreign_keys("search_report_hits")
    assert [(f["referred_table"], f["referred_columns"]) for f in fks] == [
        ("research_reports", ["report_id"])
    ]


def test_research_reports_renamed_and_extended(db_session: Session) -> None:
    """Report ids share one namespace, and now one table."""
    insp = _inspector(db_session)
    assert insp.has_table("research_reports")
    assert not insp.has_table("domain_research_reports")
    cols = {c["name"] for c in insp.get_columns("research_reports")}
    assert {"author", "report_headline"} <= cols
    # The domain side's columns are kept
    assert {"head_html", "report_title", "report_type"} <= cols


def test_domain_report_links_still_points_to_reports(db_session: Session) -> None:
    """The link table keeps its name; only its FK target changes."""
    fks = _inspector(db_session).get_foreign_keys("domain_report_links")
    referred = {f["referred_table"] for f in fks}
    assert "research_reports" in referred


def test_screen_quotes_column_count(db_session: Session) -> None:
    """102 typed fields plus symbol, as_of_date, is_known, fetched_at, raw_json."""
    from yfin.models.fields import SCREENER_QUOTE_FIELDS

    cols = {c["name"] for c in _inspector(db_session).get_columns("screen_quotes")}
    assert {f.column for f in SCREENER_QUOTE_FIELDS} <= cols
    assert {"symbol", "as_of_date", "is_known", "fetched_at", "raw_json"} <= cols


def test_screen_quotes_shares_column_names_with_ticker_info(db_session: Session) -> None:
    """75 shared fields inherit their column names from `ticker_info`.

    Redefining them instead would let the two tables drift apart over time,
    silently breaking any JOIN-free comparison between them.
    """
    insp = _inspector(db_session)
    quotes = {c["name"] for c in insp.get_columns("screen_quotes")}
    info = {c["name"] for c in insp.get_columns("ticker_info")}
    from yfin.models.fields import INFO_FIELDS, SCREENER_SHARED_SOURCES

    shared = {f.column for f in INFO_FIELDS if f.source in SCREENER_SHARED_SOURCES}
    assert len(shared) == 75
    assert shared <= quotes
    assert shared <= info


def test_symbols_discovery_columns(db_session: Session) -> None:
    """Existing rows are retroactively labeled `manual`."""
    cols = {c["name"]: c for c in _inspector(db_session).get_columns("symbols")}
    assert "discovered_by" in cols
    assert "discovered_at" in cols
    assert cols["discovered_by"]["nullable"] is False
    assert "manual" in str(cols["discovered_by"]["default"])


def test_ascii_key_columns_use_binary_collation(db_session: Session) -> None:
    """Key columns must be case-sensitive.

    Under a case-insensitive collation, 'AAPL' = 'aapl' would collapse two
    different symbols into one row. MySQL enforced this with `ascii_bin`;
    the PostgreSQL equivalent is COLLATE "C", applied uniformly to all
    string columns.
    """
    rows = db_session.execute(
        __import__("sqlalchemy").text(
            "SELECT table_name, column_name, collation_name "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name IN ('discovery_asof_state','search_quotes','lookup_results',"
            "'screen_members','screens','screen_runs','search_lists') "
            "AND column_name IN ('query_term','symbol','screen_key','list_key','dataset')"
        )
    ).all()
    assert rows
    for table, column, collation in rows:
        assert collation == "C", f"{table}.{column} = {collation}"

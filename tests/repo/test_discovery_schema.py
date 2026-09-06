"""SQ S5: kesif tablolarinin fiili semasi (gercek MySQL, agsiz)."""

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
        # SQ K5: EKRANDAN BAGIMSIZ. `screen_key` PK'da OLSAYDI bes ekranda
        # gorunen sembolun 102 alani bes kez yazilirdi.
        "screen_quotes": ["symbol", "as_of_date"],
    }
    for table, cols in expected.items():
        assert insp.get_pk_constraint(table)["constrained_columns"] == cols, table


def test_discovery_gate_has_no_foreign_key(db_session: Session) -> None:
    """SQ K3a -- bu tablonun VAROLUS SEBEBI.

    `asof_state.symbol` `symbols.symbol`a FK tasir; serbest arama terimi
    orada olmadigi icin kapi satiri `ERROR 1452` alirdi. Bir gun biri
    "tutarlilik olsun" diye buraya FK eklerse `yfin discover term` sessizce
    degil, GURULTULU bicimde kirilir -- ve bu test once kirmizi olur.
    """
    assert _inspector(db_session).get_foreign_keys("discovery_asof_state") == []


def test_symbol_columns_have_no_foreign_key(db_session: Session) -> None:
    """SQ K9: kesif dataset'leri TANIMI GEREGI evren disi sembol dondurur."""
    insp = _inspector(db_session)
    for table in ("search_quotes", "lookup_results", "screen_members", "screen_quotes"):
        referred = {fk["referred_table"] for fk in insp.get_foreign_keys(table)}
        assert "symbols" not in referred, table


def test_symbol_columns_are_indexed(db_session: Session) -> None:
    """FK olmadigi icin index ACIKCA tanimlanir (S S5.6).

    InnoDB FK'li kolona index'i kendisi kurar; FK yoksa kurmaz ve
    `WHERE symbol = ...` sorgusu tam tarama olurdu.
    """
    insp = _inspector(db_session)
    for table in ("search_quotes", "lookup_results", "screen_members"):
        indexed = {tuple(ix["column_names"]) for ix in insp.get_indexes(table)}
        assert ("symbol",) in indexed, table


def test_report_hits_has_foreign_key_to_reports(db_session: Session) -> None:
    """Sembol kolonlarinin aksine BURADA FK VARDIR: `report_id` sembol
    degildir, evren disilik sorunu yoktur ve ebeveyn ayni transaction'da
    kapili yazimlardan ONCE yazilir (SQ S6.2.1)."""
    fks = _inspector(db_session).get_foreign_keys("search_report_hits")
    assert [(f["referred_table"], f["referred_columns"]) for f in fks] == [
        ("research_reports", ["report_id"])
    ]


def test_research_reports_renamed_and_extended(db_session: Session) -> None:
    """SQ K8: rapor kimlikleri TEK UZAY, tablo da tek."""
    insp = _inspector(db_session)
    assert insp.has_table("research_reports")
    assert not insp.has_table("domain_research_reports")
    cols = {c["name"] for c in insp.get_columns("research_reports")}
    assert {"author", "report_headline"} <= cols
    # Domain tarafinin kolonlari KORUNUR
    assert {"head_html", "report_title", "report_type"} <= cols


def test_domain_report_links_still_points_to_reports(db_session: Session) -> None:
    """Bag tablosu ADINI KORUR; yalniz FK hedefi degisir."""
    fks = _inspector(db_session).get_foreign_keys("domain_report_links")
    referred = {f["referred_table"] for f in fks}
    assert "research_reports" in referred


def test_screen_quotes_column_count(db_session: Session) -> None:
    """SQ S4.5: 102 tipli alan + symbol + as_of_date + is_known +
    fetched_at + raw_json."""
    from yfin.models.fields import SCREENER_QUOTE_FIELDS

    cols = {c["name"] for c in _inspector(db_session).get_columns("screen_quotes")}
    assert {f.column for f in SCREENER_QUOTE_FIELDS} <= cols
    assert {"symbol", "as_of_date", "is_known", "fetched_at", "raw_json"} <= cols


def test_screen_quotes_shares_column_names_with_ticker_info(db_session: Session) -> None:
    """SQ S4.5: 75 ortak alan kolon adini `ticker_info`dan DEVRALIR.

    Devralmak yerine yeniden yazilsaydi iki tablo zamanla ayrisir ve
    JOIN'siz karsilastirma sessizce yanlislanirdi.
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
    """SQ S5.12: mevcut satirlar geriye donuk `manual` etiketlenir."""
    cols = {c["name"]: c for c in _inspector(db_session).get_columns("symbols")}
    assert "discovered_by" in cols
    assert "discovered_at" in cols
    assert cols["discovered_by"]["nullable"] is False
    assert "manual" in str(cols["discovered_by"]["default"])


def test_ascii_key_columns_use_binary_collation(db_session: Session) -> None:
    """Anahtar kolonlar case-SENSITIVE olmalidir.

    Duyarsiz bir collation'da 'AAPL' = 'aapl' -> iki farkli sembol ayni
    satira duser (S S5.1). MySQL'de bu `ascii_bin` ile saglaniyordu;
    PostgreSQL'de karsiligi COLLATE "C"dir (PG S2.5) ve tum string
    kolonlarina tekduze uygulanir.
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

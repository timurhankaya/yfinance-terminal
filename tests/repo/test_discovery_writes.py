"""Kesif yazimlarinin GERCEK MySQL davranisi (SQ S10.3).

Unit testler `normalize`in urettigi satiri dogrular; burada o satirin
gercekten yazilabildigi VE mevcut veriyi bozmadigi dogrulanir. Bu iki sey
farkli hata siniflari yakalar: FK ihlali, NOT NULL, kolon kapsami ve
`ON DUPLICATE KEY UPDATE` semantigi yalnizca gercek bir INSERT'te gorunur.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from yfin.datasets.discovery.lookup import LookupDataset, LookupPayload
from yfin.datasets.discovery.search import SearchDataset, SearchPayload
from yfin.datasets.market.screener import ScreenerDataset, ScreenPayload
from yfin.persistence import MySQLRowWriter

pytestmark = pytest.mark.repo

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC).replace(tzinfo=None)
LATER = datetime(2026, 9, 6, 12, 0, tzinfo=UTC).replace(tzinfo=None)
AS_OF = date(2026, 9, 5)


def _search_payload(fixture: str, term: str, *, fetched_at: datetime = NOW) -> SearchPayload:
    raw = json.loads((FIXTURES / "_discovery" / f"{fixture}.json").read_text(encoding="utf-8"))
    return SearchPayload(
        query_term=term,
        as_of_date=AS_OF,
        fetched_at=fetched_at,
        quotes=raw.get("quotes") or [],
        news=raw.get("news") or [],
        lists=raw.get("lists") or [],
        reports=raw.get("researchReports") or [],
    )


def _screen_payload(fixture: str, key: str, *, quotes: list[dict[str, Any]] | None = None) -> Any:
    raw = json.loads((FIXTURES / "_screen" / f"{fixture}.json").read_text(encoding="utf-8"))
    return ScreenPayload(
        screen_key=key,
        as_of_date=AS_OF,
        fetched_at=NOW,
        quotes=raw["quotes"] if quotes is None else quotes,
        total=raw["total"],
        page_count=1,
        metadata={k: v for k, v in raw.items() if k != "quotes"},
    )


def _write(db_session: Session, dataset: Any, payload: Any, *, symbol: str | None = None) -> Any:
    writer = MySQLRowWriter(db_session)
    result = (
        dataset.normalize(payload) if symbol is None else dataset.normalize(payload, symbol)
    )
    stats = dataset.upsert(writer, result)
    db_session.flush()
    return stats


def _scalar(db_session: Session, sql: str, **params: Any) -> Any:
    return db_session.execute(text(sql), params).scalar()


class TestSearchWrites:
    def test_all_tables_verify(self, db_session: Session) -> None:
        payload = _search_payload("search_AAPL", "AAPL")
        stats = _write(db_session, SearchDataset(), payload, symbol="AAPL")
        assert stats.attempted
        for table, attempted in stats.attempted.items():
            assert stats.verified.get(table, 0) == attempted, table

    def test_free_term_can_write_its_gate_row(self, db_session: Session) -> None:
        """REGRESYON (SQ K3a) -- `discovery_asof_state`in VAROLUS SEBEBI.

        `asof_state.symbol` `symbols.symbol`a FK tasir. Serbest terim
        `symbols`ta YOKTUR; o kapiya yazilsaydi `ERROR 1452` alinirdi.
        """
        _write(
            db_session,
            SearchDataset(),
            _search_payload("search_Turkish-Airlines", "Turkish Airlines"),
            symbol="Turkish Airlines",
        )
        assert (
            _scalar(
                db_session,
                "SELECT COUNT(*) FROM discovery_asof_state "
                "WHERE query_term = :q AND dataset = 'search'",
                q="Turkish Airlines",
            )
            == 1
        )
        assert (
            _scalar(
                db_session, "SELECT COUNT(*) FROM symbols WHERE symbol = :q", q="Turkish Airlines"
            )
            == 0
        )

    def test_quotes_empty_but_news_written(self, db_session: Session) -> None:
        """SQ S9.2: durum TABLO BAZINDA turetilir."""
        _write(
            db_session,
            SearchDataset(),
            _search_payload("search_Turkish-Airlines", "Turkish Airlines"),
            symbol="Turkish Airlines",
        )
        assert _scalar(db_session, "SELECT COUNT(*) FROM search_quotes") == 0
        assert _scalar(db_session, "SELECT COUNT(*) FROM news") > 0
        assert _scalar(db_session, "SELECT COUNT(*) FROM research_reports") > 0

    def test_report_parent_precedes_hit(self, db_session: Session) -> None:
        """FK: `search_report_hits.report_id` -> `research_reports`.

        Ebeveyn ayni transaction'da ONCE yazilmazsa ERROR 1452.
        """
        _write(db_session, SearchDataset(), _search_payload("search_AAPL", "AAPL"), symbol="AAPL")
        assert _scalar(db_session, "SELECT COUNT(*) FROM search_report_hits") > 0

    def test_empty_query_writes_no_gate_row(self, db_session: Session) -> None:
        """Bos sonucta kapi satiri YAZILMAZ; aksi halde her anlamsiz terim
        icin olu satir birikir ve `first_seen_at` "ilk kez BOS donuldu"
        anlamina kayardi."""
        _write(
            db_session,
            SearchDataset(),
            _search_payload("search_zzzqqxnope", "zzzqqxnope"),
            symbol="zzzqqxnope",
        )
        assert _scalar(db_session, "SELECT COUNT(*) FROM discovery_asof_state") == 0

    def test_idempotent_second_run(self, db_session: Session) -> None:
        payload = _search_payload("search_AAPL", "AAPL")
        _write(db_session, SearchDataset(), payload, symbol="AAPL")
        before = _scalar(db_session, "SELECT COUNT(*) FROM search_quotes")
        _write(db_session, SearchDataset(), payload, symbol="AAPL")
        assert _scalar(db_session, "SELECT COUNT(*) FROM search_quotes") == before


class TestNewsSparseUpdate:
    def test_rich_row_survives_a_search_pass(self, db_session: Session) -> None:
        """REGRESYON (SQ S8.5).

        Once `Ticker.news`in zengin govdesi yazilir, sonra ayni `news_id`
        Search yolundan gecer. `summary`/`description`/`canonical_url`
        KORUNMALIDIR -- Search bu alanlari hic tasimaz ve kor bir upsert
        onlari NULL'lardi.
        """
        payload = _search_payload("search_AAPL", "AAPL")
        news_id = payload.news[0]["uuid"]
        db_session.execute(
            text(
                "INSERT INTO news (news_id, title, summary, description, canonical_url, "
                "pub_date, raw_json) VALUES (:i, 'zengin', 'OZET', 'ACIKLAMA', "
                "'https://x/y', :t, '{}')"
            ),
            {"i": news_id, "t": NOW},
        )
        db_session.flush()

        _write(db_session, SearchDataset(), payload, symbol="AAPL")

        row = db_session.execute(
            text(
                "SELECT summary, description, canonical_url, title "
                "FROM news WHERE news_id = :i"
            ),
            {"i": news_id},
        ).one()
        assert row.summary == "OZET"
        assert row.description == "ACIKLAMA"
        assert row.canonical_url == "https://x/y"
        # Search'un DOLDURDUGU kolon guncellenir
        assert row.title != "zengin"


class TestSymbolPromotion:
    def test_activated_symbol_stays_active(self, db_session: Session) -> None:
        """REGRESYON (SQ K10).

        Operator sembolu elle aktiflestirdikten sonra ayni sembol yeniden
        kesfedilirse `is_active` 1 KALMALIDIR. Kapsama girseydi `yfin sync`
        o sembolu SESSIZCE cekmeyi birakirdi ve bu ancak "neden veri
        gelmiyor" diye sorulunca fark edilirdi.
        """
        payload = _search_payload("search_AAPL", "AAPL")
        _write(db_session, SearchDataset(), payload, symbol="AAPL")

        db_session.execute(
            text("UPDATE symbols SET is_active = 1, discovered_by = 'manual' WHERE symbol = 'AAPL'")
        )
        db_session.flush()

        _write(db_session, SearchDataset(), payload, symbol="AAPL")

        row = db_session.execute(
            text("SELECT is_active, discovered_by FROM symbols WHERE symbol = 'AAPL'")
        ).one()
        assert row.is_active == 1
        assert row.discovered_by == "manual"

    def test_new_symbol_is_written_inactive(self, db_session: Session) -> None:
        _write(db_session, SearchDataset(), _search_payload("search_AAPL", "AAPL"), symbol="AAPL")
        row = db_session.execute(
            text("SELECT is_active, discovered_by FROM symbols WHERE symbol = 'AAPL'")
        ).one()
        assert row.is_active == 0
        assert row.discovered_by == "search"

    def test_lookup_does_not_null_search_columns(self, db_session: Session) -> None:
        """REGRESYON (SQ S5.12).

        `lookup` yolu `long_name`/`currency` DONDURMEZ. Ortak bir
        `update_columns` listesi kullanilsaydi her lookup kosusu
        `search`/`screener`in yazdigi bu kolonlari SILERDI.
        """
        _write(db_session, SearchDataset(), _search_payload("search_AAPL", "AAPL"), symbol="AAPL")
        db_session.execute(
            text("UPDATE symbols SET currency = 'USD' WHERE symbol = 'AAPL'")
        )
        db_session.flush()
        before = db_session.execute(
            text("SELECT long_name, currency FROM symbols WHERE symbol = 'AAPL'")
        ).one()
        assert before.long_name

        payload = LookupPayload(
            query_term="AAPL",
            as_of_date=AS_OF,
            fetched_at=LATER,
            documents=[("all", {"symbol": "AAPL", "shortName": "Apple", "exchange": "NMS"})],
            totals={"all": 1},
        )
        _write(db_session, LookupDataset(), payload, symbol="AAPL")

        after = db_session.execute(
            text("SELECT long_name, currency FROM symbols WHERE symbol = 'AAPL'")
        ).one()
        assert after.long_name == before.long_name
        assert after.currency == "USD"


class TestLookupWrites:
    def test_totals_include_private_company(self, db_session: Session) -> None:
        """SQ S4.1/10: kaynak DOKUZ tip bildiriyor; `LOOKUP_TYPES` sabiti
        `privateCompany`yi BILMIYOR."""
        raw = json.loads(
            (FIXTURES / "_discovery" / "lookup_BTC_all.json").read_text(encoding="utf-8")
        )
        block = raw["finance"]["result"][0]
        payload = LookupPayload(
            query_term="BTC",
            as_of_date=AS_OF,
            fetched_at=NOW,
            documents=[("all", d) for d in block["documents"][:20]],
            totals={str(k): int(v) for k, v in block["lookupTotals"].items()},
        )
        _write(db_session, LookupDataset(), payload, symbol="BTC")
        assert (
            _scalar(
                db_session,
                "SELECT total FROM lookup_totals "
                "WHERE query_term = 'BTC' AND lookup_type = 'privateCompany'",
            )
            is not None
        )


class TestScreenerWrites:
    def test_all_tables_verify(self, db_session: Session) -> None:
        payload = _screen_payload("day_gainers_p0", "day_gainers")
        stats = _write(db_session, ScreenerDataset(), payload)
        for table, attempted in stats.attempted.items():
            assert stats.verified.get(table, 0) == attempted, table

    def test_replace_scope_drops_yesterdays_member(self, db_session: Session) -> None:
        """REGRESYON (SQ S8.4).

        Kadro gun icinde degisiyor (olculdu: `day_gainers` 122 -> 117).
        Duz upsert olsaydi sabah cikip oglen dusen sembol o gunun
        kadrosunda KALICI olarak yanlis gorunurdu.
        """
        first = _screen_payload("day_gainers_p0", "day_gainers")
        _write(db_session, ScreenerDataset(), first)
        dropped = first.quotes[0]["symbol"]
        assert _scalar(
            db_session,
            "SELECT COUNT(*) FROM screen_members WHERE symbol = :s",
            s=dropped,
        )

        second = _screen_payload("day_gainers_p0", "day_gainers", quotes=first.quotes[1:])
        _write(db_session, ScreenerDataset(), second)

        assert (
            _scalar(
                db_session,
                "SELECT COUNT(*) FROM screen_members WHERE symbol = :s",
                s=dropped,
            )
            == 0
        )

    def test_quotes_survive_membership_change(self, db_session: Session) -> None:
        """SQ K5: kotasyon EKRANDAN BAGIMSIZDIR; kadrodan cikan sembolun
        kotasyonu SILINMEZ."""
        first = _screen_payload("day_gainers_p0", "day_gainers")
        _write(db_session, ScreenerDataset(), first)
        dropped = first.quotes[0]["symbol"]

        second = _screen_payload("day_gainers_p0", "day_gainers", quotes=first.quotes[1:])
        _write(db_session, ScreenerDataset(), second)

        assert _scalar(
            db_session, "SELECT COUNT(*) FROM screen_quotes WHERE symbol = :s", s=dropped
        )

    def test_gate_skips_children_when_hash_matches(self, db_session: Session) -> None:
        """Kadro degismediginde `screen_members` ATLANIR."""
        payload = _screen_payload("day_gainers_p0", "day_gainers")
        _write(db_session, ScreenerDataset(), payload)
        stats = _write(db_session, ScreenerDataset(), payload)
        assert stats.skipped.get("screen_members", 0) > 0


class TestSharedReport:
    def test_same_report_from_both_paths_is_one_row(self, db_session: Session) -> None:
        """SQ K8: rapor kimlikleri TEK UZAY.

        Once domain yolu gibi bir satir yazilir, sonra Search yolu ayni
        `report_id` ile gecer. Tek satir kalmali ve DOMAIN kolonlari
        korunmalidir.
        """
        payload = _search_payload("search_AAPL", "AAPL")
        report_id = payload.reports[0]["id"]
        db_session.execute(
            text(
                "INSERT INTO research_reports (report_id, as_of_date, provider, report_type, "
                "head_html, first_seen_at, fetched_at) "
                "VALUES (:i, :d, 'Argus Research', 'AnalystReport', 'DOMAIN BASLIK', :t, :t)"
            ),
            {"i": report_id, "d": AS_OF, "t": NOW},
        )
        db_session.flush()

        _write(db_session, SearchDataset(), payload, symbol="AAPL")

        row = db_session.execute(
            text(
                "SELECT COUNT(*) AS n, MAX(head_html) AS head, MAX(author) AS author "
                "FROM research_reports WHERE report_id = :i"
            ),
            {"i": report_id},
        ).one()
        assert row.n == 1
        # Domain'in yazdigi kolon KORUNUR, Search'unki EKLENIR
        assert row.head == "DOMAIN BASLIK"
        assert row.author

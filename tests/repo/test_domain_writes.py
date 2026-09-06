"""Fixture'lar GERCEK MySQL'e yazilir; satir sayilari ve tip round-trip'i (SI S9.3)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from domain_support import AS_OF, FIXTURE_SECTORS, run_dataset, run_taxonomy
from helpers import domain_data

pytestmark = pytest.mark.repo


def _count(session: Session, table: str, where: str = "1=1") -> int:
    return int(session.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}")).scalar_one())


def test_taxonomy_writes_symbols_and_domains(db_session: Session) -> None:
    stats = run_taxonomy(db_session)
    expected_industries = sum(
        len([r for r in domain_data("sector", key)["industries"] if "key" in r])
        for key in FIXTURE_SECTORS
    )
    assert _count(db_session, "domains", "domain_type = 'sector'") == len(FIXTURE_SECTORS)
    assert _count(db_session, "domains", "domain_type = 'industry'") == expected_industries
    # `symbols` satirlari FK'nin ONCESINDE yazilir
    assert _count(db_session, "symbols", "quote_type = 'INDEX'") == len(
        FIXTURE_SECTORS
    ) + expected_industries
    assert stats.verified["domains"] == len(FIXTURE_SECTORS) + expected_industries


def test_industries_count_matches_the_discovered_universe(db_session: Session) -> None:
    """EKSIKSIZLIGIN BEKLENEN DEGERINI API'NIN KENDISI VERIYOR."""
    run_taxonomy(db_session)
    for key in FIXTURE_SECTORS:
        run_dataset(db_session, "sector_profile", "sector", key)
    reported = int(
        db_session.execute(
            text(
                "SELECT SUM(m.industries_count) FROM domain_metrics m "
                "JOIN domains d USING (domain_key) WHERE d.domain_type = 'sector'"
            )
        ).scalar_one()
    )
    assert reported == _count(db_session, "domains", "domain_type = 'industry'")


def test_sector_rankings_write_companies_and_funds(db_session: Session) -> None:
    run_taxonomy(db_session)
    stats = run_dataset(db_session, "sector_rankings", "sector", "technology")
    raw = domain_data("sector", "technology")
    assert stats.verified["domain_top_companies"] == len(raw["topCompanies"])
    assert stats.verified["domain_top_funds"] == len(raw["topETFs"]) + len(raw["topMutualFunds"])
    assert _count(db_session, "domain_top_funds", "fund_type = 'etf'") == len(raw["topETFs"])


def test_industry_rankings_write_movers(db_session: Session) -> None:
    run_taxonomy(db_session)
    stats = run_dataset(db_session, "industry_rankings", "industry", "semiconductors")
    raw = domain_data("industry", "semiconductors")
    assert stats.verified["domain_top_movers"] == len(raw["topPerformingCompanies"]) + len(
        raw["topGrowthCompanies"]
    )
    assert stats.verified["domain_top_companies"] == len(raw["topCompanies"])


def test_numeric_round_trip_is_lossless(db_session: Session) -> None:
    """Ust ve alt uclar KAYIPSIZ geri okunur."""
    run_taxonomy(db_session)
    run_dataset(db_session, "sector_profile", "sector", "technology")
    run_dataset(db_session, "sector_rankings", "sector", "technology")

    raw = domain_data("sector", "technology")
    row = db_session.execute(
        text(
            "SELECT market_cap, market_weight, employee_count, companies_count "
            "FROM domain_metrics WHERE domain_key = 'technology'"
        )
    ).one()
    overview = raw["overview"]
    assert row.market_cap == Decimal(str(overview["marketCap"]["raw"]))
    assert row.market_weight == Decimal(str(overview["marketWeight"]["raw"]))
    assert row.employee_count == overview["employeeCount"]["raw"]
    assert row.companies_count == overview["companiesCount"]

    top = raw["topCompanies"][0]
    stored = db_session.execute(
        text(
            "SELECT market_cap, ytd_return, last_price FROM domain_top_companies "
            "WHERE domain_key = 'technology' AND symbol = :s"
        ),
        {"s": top["symbol"]},
    ).one()
    assert stored.market_cap == Decimal(str(top["marketCap"]["raw"]))
    assert stored.ytd_return == Decimal(str(top["ytdReturn"]["raw"]))


def test_extreme_growth_estimate_round_trips(db_session: Session) -> None:
    """DECIMAL(28,12) OLCEGINDE kayipsiz.

    Kaynak `0.7656249999999998` gibi cift-duyarlikli artiklar dondurebiliyor;
    kolon 12 haneye yuvarlar. Bu bir KAYIP DEGIL, kolonun ilan edilmis
    olcegidir -- `PriceType` kod tabaninin her yerinde ayni sozlesmeyi
    tasir. Test bu yuzden AYNI olcege quantize eder.
    """
    run_taxonomy(db_session)
    run_dataset(db_session, "industry_rankings", "industry", "electronic-components")
    raw = domain_data("industry", "electronic-components")
    scale = Decimal("0.000000000001")
    checked = 0
    for entry in raw["topGrowthCompanies"]:
        expected = entry.get("growthEstimate", {}).get("raw")
        if expected is None:
            continue
        stored = db_session.execute(
            text(
                "SELECT growth_estimate FROM domain_top_movers "
                "WHERE domain_key = 'electronic-components' AND rank_type = 'growth' "
                "AND symbol = :s"
            ),
            {"s": entry["symbol"]},
        ).scalar_one()
        assert stored == Decimal(str(expected)).quantize(scale)
        checked += 1
    assert checked, "growthEstimate tasiyan satir bekleniyordu"


def test_empty_industry_writes_nothing_and_no_gate_row(db_session: Session) -> None:
    run_taxonomy(db_session)
    stats = run_dataset(
        db_session, "industry_rankings", "industry", "infrastructure-operations"
    )
    assert stats.attempted == {}
    assert _count(db_session, "domain_asof_state", "dataset = 'industry_rankings'") == 0


def test_regional_rows_carry_their_region(db_session: Session) -> None:
    run_taxonomy(db_session)
    run_dataset(db_session, "sector_rankings", "sector", "technology", region="US")
    run_dataset(db_session, "sector_rankings", "sector", "technology", region="GB")
    regions = set(
        db_session.execute(
            text("SELECT DISTINCT region FROM domain_top_companies")
        ).scalars()
    )
    assert regions == {"US", "GB"}
    # GB'de topETFs BOS: satir yok, hata degil
    assert _count(db_session, "domain_top_funds", "region = 'GB'") == 0
    assert _count(db_session, "domain_top_funds", "region = 'US'") > 0


def test_domain_metrics_have_no_region_column(db_session: Session) -> None:
    """`overview`/`performance` 5 bolgede BIREBIR ayni olculdu."""
    columns = set(
        db_session.execute(
            text(
                "SELECT COLUMN_NAME FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'domain_metrics'"
            )
        ).scalars()
    )
    assert "region" not in columns


def test_industry_profile_fills_description_without_touching_identity(
    db_session: Session,
) -> None:
    run_taxonomy(db_session)
    before = db_session.execute(
        text(
            "SELECT description, message_board_id, name, symbol, parent_key "
            "FROM domains WHERE domain_key = 'semiconductors'"
        )
    ).one()
    assert before.description is None

    run_dataset(
        db_session, "industry_profile", "industry", "semiconductors", parent=before.parent_key
    )
    after = db_session.execute(
        text(
            "SELECT description, message_board_id, name, symbol, parent_key "
            "FROM domains WHERE domain_key = 'semiconductors'"
        )
    ).one()
    assert after.description
    assert after.message_board_id
    assert (after.name, after.symbol, after.parent_key) == (
        before.name,
        before.symbol,
        before.parent_key,
    )


def test_as_of_date_is_written_on_every_asof_table(db_session: Session) -> None:
    run_taxonomy(db_session)
    run_dataset(db_session, "sector_profile", "sector", "technology")
    for table in ("domain_metrics", "domain_report_links", "research_reports"):
        days = set(db_session.execute(text(f"SELECT DISTINCT as_of_date FROM {table}")).scalars())
        assert days == {AS_OF}, table

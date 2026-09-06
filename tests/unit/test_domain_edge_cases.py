"""SI S10 tablosunun her satiri (SI S9.2).

Her kenar durum bir FIXTURE ile baglanir; hicbiri tahmin degildir.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from helpers import domain_data
from yfin.datasets.domain.payloads import DomainPayload
from yfin.datasets.registry import DOMAIN_DATASETS

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
AS_OF = date(2026, 9, 4)

SECTOR_RANKINGS = DOMAIN_DATASETS["sector_rankings"]
INDUSTRY_RANKINGS = DOMAIN_DATASETS["industry_rankings"]
SECTOR_PROFILE = DOMAIN_DATASETS["sector_profile"]
INDUSTRY_PROFILE = DOMAIN_DATASETS["industry_profile"]


def _payload(kind: str, key: str, region: str = "US") -> DomainPayload:
    return DomainPayload(
        data=domain_data(kind, key, region),
        fetched_at=NOW,
        as_of_date=AS_OF,
        region=region,
        domain_type=kind,
    )


def _rows(dataset, kind: str, key: str, table: str, region: str = "US") -> list[dict]:
    result = dataset.normalize(_payload(kind, key, region), key)
    return next(w.rows for w in result.writes if w.table == table)


def test_infrastructure_operations_produces_an_empty_result() -> None:
    """companiesCount=1; UC liste blogunun hicbirinde satir yok -> `empty`."""
    result = INDUSTRY_RANKINGS.normalize(
        _payload("industry", "infrastructure-operations"), "infrastructure-operations"
    )
    assert result.is_empty
    # Profil tarafi NORMAL calisir: `overview` doludur.
    profile = INDUSTRY_PROFILE.normalize(
        _payload("industry", "infrastructure-operations"), "infrastructure-operations"
    )
    metrics = next(w for w in profile.writes if w.table == "domain_metrics")
    assert metrics.rows[0]["companies_count"] == 1
    assert metrics.rows[0]["employee_count"] == 18


def test_gb_region_has_no_etfs() -> None:
    rows = _rows(SECTOR_RANKINGS, "sector", "technology", "domain_top_funds", "GB")
    assert rows == []
    companies = _rows(SECTOR_RANKINGS, "sector", "technology", "domain_top_companies", "GB")
    assert companies, "GB'de topCompanies dolu olmali"
    assert all(row["region"] == "GB" for row in companies)


def test_gb_companies_do_not_overlap_us() -> None:
    """Prob esiginin (%50) neden guvenli oldugunun veri tabanli kaniti."""
    us = {
        r["symbol"]
        for r in _rows(SECTOR_RANKINGS, "sector", "technology", "domain_top_companies")
    }
    gb = {
        r["symbol"]
        for r in _rows(SECTOR_RANKINGS, "sector", "technology", "domain_top_companies", "GB")
    }
    assert us and gb
    assert us.isdisjoint(gb)


def test_missing_rating_becomes_null() -> None:
    rows = _rows(SECTOR_RANKINGS, "sector", "technology", "domain_top_companies")
    assert any(row["rating"] is None for row in rows) or all(
        row["rating"] for row in rows
    ), "kolon nullable olmali"
    # Kolonun NULL kabul ettigi, en azindan bir kaynakta eksigin
    # dusurulmedigi ile baglanir:
    raw = domain_data("sector", "technology")["topCompanies"]
    for entry in raw:
        if "rating" not in entry:
            match = next(r for r in rows if r["symbol"] == entry["symbol"])
            assert match["rating"] is None


def test_fund_without_a_name_is_kept_with_null_name() -> None:
    rows = _rows(SECTOR_RANKINGS, "sector", "healthcare", "domain_top_funds")
    nameless = [r for r in rows if r["name"] is None]
    assert nameless, "adsiz fon satiri bekleniyordu (220 fonun 7'sinde)"
    assert all(r["fund_type"] == "mutual_fund" for r in nameless)


def test_non_ticker_fund_symbol_is_written_as_is() -> None:
    """`0P0001WO1I` Morningstar kimligi; FK YOK, `is_known` isaretler."""
    rows = _rows(SECTOR_RANKINGS, "sector", "healthcare", "domain_top_funds")
    symbols = {r["symbol"] for r in rows}
    assert any(s.startswith("0P") for s in symbols)
    assert all(r["is_known"] is False for r in rows), "is_known upsert'te doldurulur"


def test_extreme_ytd_return_is_not_a_sentinel() -> None:
    """9999.0 OLDUGU GIBI yazilir (`currentPriceTarget = 0.0` ilkesi)."""
    raw = domain_data("industry", "biotechnology")
    extremes = [
        r
        for block in ("topPerformingCompanies", "topGrowthCompanies")
        for r in raw.get(block) or []
        if (r.get("ytdReturn") or {}).get("raw", 0) > 50
    ]
    rows = _rows(INDUSTRY_RANKINGS, "industry", "biotechnology", "domain_top_movers")
    for entry in extremes:
        match = next(r for r in rows if r["symbol"] == entry["symbol"])
        assert match["ytd_return"] == Decimal(str(entry["ytdReturn"]["raw"]))


def test_same_symbol_in_both_mover_lists_yields_two_rows() -> None:
    """`rank_type` PK'da: iki satir da yazilir, veri KAYBOLMAZ."""
    raw = domain_data("industry", "pharmaceutical-retailers")
    shared = {r["symbol"] for r in raw["topPerformingCompanies"]} & {
        r["symbol"] for r in raw["topGrowthCompanies"]
    }
    assert shared, "iki listede birden gorunen sembol bekleniyordu"
    rows = _rows(
        INDUSTRY_RANKINGS, "industry", "pharmaceutical-retailers", "domain_top_movers"
    )
    for symbol in shared:
        kinds = {r["rank_type"] for r in rows if r["symbol"] == symbol}
        assert kinds == {"performing", "growth"}


def test_mover_without_a_name_is_kept() -> None:
    raw = domain_data("industry", "gold")
    nameless = [r for r in raw["topPerformingCompanies"] if "name" not in r]
    assert nameless, "adsiz mover satiri bekleniyordu"
    rows = _rows(INDUSTRY_RANKINGS, "industry", "gold", "domain_top_movers")
    for entry in nameless:
        match = next(
            r for r in rows if r["symbol"] == entry["symbol"] and r["rank_type"] == "performing"
        )
        assert match["name"] is None
        assert match["ytd_return"] is not None


def test_growth_estimate_only_on_growth_rows() -> None:
    rows = _rows(
        INDUSTRY_RANKINGS, "industry", "electronic-components", "domain_top_movers"
    )
    for row in rows:
        if row["rank_type"] == "performing":
            assert row["growth_estimate"] is None
        else:
            assert row["target_price"] is None


def test_companies_count_upper_bound_fits_in_integer() -> None:
    metrics = _rows(SECTOR_PROFILE, "sector", "financial-services", "domain_metrics")
    assert metrics[0]["companies_count"] > 1000
    assert metrics[0]["companies_count"] < 2**31


def test_industry_metrics_have_null_industries_count() -> None:
    metrics = _rows(INDUSTRY_PROFILE, "industry", "semiconductors", "domain_metrics")
    assert metrics[0]["industries_count"] is None
    assert metrics[0]["companies_count"] is not None


def test_sector_metrics_carry_industries_count() -> None:
    metrics = _rows(SECTOR_PROFILE, "sector", "technology", "domain_metrics")
    assert metrics[0]["industries_count"] == 12


def test_raw_json_excludes_list_blocks() -> None:
    """Tam zarf saklansaydi kapi HER KOSUDA acilirdi (SI S2)."""
    import json

    metrics = _rows(SECTOR_PROFILE, "sector", "technology", "domain_metrics")
    payload = json.loads(metrics[0]["raw_json"])
    assert set(payload) <= {
        "key",
        "name",
        "symbol",
        "sectorKey",
        "sectorName",
        "overview",
        "performance",
        "performanceOverviewBenchmark",
    }
    assert "topCompanies" not in payload
    assert "industries" not in payload


def test_performance_and_benchmark_blocks_are_promoted_to_columns() -> None:
    """yfinance'in HICBIR property ile acmadigi 11 alan (SI S4.3)."""
    metrics = _rows(SECTOR_PROFILE, "sector", "technology", "domain_metrics")[0]
    for column in (
        "ytd_change_pct",
        "reg_market_change_pct",
        "one_year_change_pct",
        "three_year_change_pct",
        "five_year_change_pct",
        "benchmark_ytd_change_pct",
        "benchmark_reg_market_change_pct",
        "benchmark_one_year_change_pct",
        "benchmark_three_year_change_pct",
        "benchmark_five_year_change_pct",
    ):
        assert metrics[column] is not None, column
    assert metrics["benchmark_name"] == "S&P 500"


def test_reports_are_four_per_domain_with_positions() -> None:
    result = SECTOR_PROFILE.normalize(_payload("sector", "technology"), "technology")
    reports = next(w for w in result.writes if w.table == "research_reports")
    links = next(w for w in result.writes if w.table == "domain_report_links")
    assert len(reports.rows) == 4
    assert [r["position"] for r in links.rows] == [0, 1, 2, 3]
    assert {r["report_id"] for r in links.rows} == {r["report_id"] for r in reports.rows}


def test_report_links_carry_no_region() -> None:
    """Rapor kimlikleri 5 bolgede BIREBIR ayni sirayla dondu."""
    result = SECTOR_PROFILE.normalize(_payload("sector", "technology"), "technology")
    links = next(w for w in result.writes if w.table == "domain_report_links")
    assert "region" not in links.rows[0]


def test_industry_profile_writes_description_into_domains() -> None:
    result = INDUSTRY_PROFILE.normalize(_payload("industry", "semiconductors"), "semiconductors")
    domains = next(w for w in result.writes if w.table == "domains")
    assert domains.rows[0]["description"]
    assert domains.rows[0]["message_board_id"]
    assert domains.update_columns == ("description", "message_board_id", "fetched_at")


def test_industry_rankings_scope_includes_domain_key() -> None:
    """Kapsam `domain_key` icermeseydi SEKTOR satirlari silinirdi (SI S5.11)."""
    result = INDUSTRY_RANKINGS.normalize(_payload("industry", "semiconductors"), "semiconductors")
    companies = next(w for w in result.writes if w.table == "domain_top_companies")
    assert companies.mode == "replace_scope"
    assert companies.scope_columns == ("domain_key", "region", "as_of_date")


def test_mover_scope_excludes_rank_type() -> None:
    """Iki liste tek fetch'ten gelir ve BIRLIKTE yazilir."""
    result = INDUSTRY_RANKINGS.normalize(_payload("industry", "semiconductors"), "semiconductors")
    movers = next(w for w in result.writes if w.table == "domain_top_movers")
    assert "rank_type" not in movers.scope_columns
    assert "rank_type" in movers.key_columns

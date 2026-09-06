"""Wrapper asymmetry and field parsing.

Yahoo wraps numeric fields as `{"raw":..., "fmt":...}`, but not
consistently. This file pins all three shapes in the same test; two
separate parsers would let one of them silently write None.
"""

from __future__ import annotations

from decimal import Decimal

from helpers import domain_data
from yfin.datasets.domain.common import dec_of, int_of, text_of, unwrap


def test_unwrap_handles_all_three_shapes() -> None:
    assert unwrap({"raw": 1.5, "fmt": "1.50"}) == 1.5
    assert unwrap(450.0) == 450.0
    assert unwrap(None) is None
    # Wrapped but with no `raw`: the dict is not returned as-is, it becomes
    # None -- writing a dict into a non-NOT-NULL column would raise DataError.
    assert unwrap({"fmt": "1.50"}) is None


def test_target_price_is_wrapped_in_top_companies_but_bare_in_reports() -> None:
    """The same field name, two different shapes -- why a single `unwrap` path exists."""
    data = domain_data("sector", "technology")
    company = data["topCompanies"][0]
    assert isinstance(company["targetPrice"], dict)
    assert "raw" in company["targetPrice"]

    wrapped_reports = [r for r in data["researchReports"] if "targetPrice" in r]
    for report in wrapped_reports:
        assert not isinstance(report["targetPrice"], dict), (
            "researchReports[].targetPrice should be a bare float"
        )

    # Both go through the same path and both produce a Decimal.
    assert isinstance(dec_of(company, "targetPrice"), Decimal)
    for report in wrapped_reports:
        assert isinstance(dec_of(report, "targetPrice"), Decimal)


def test_missing_report_fields_become_null() -> None:
    """17 of 104 reports have no `targetPrice` at all -> row.get() -> NULL."""
    data = domain_data("sector", "technology")
    missing = [r for r in data["researchReports"] if "targetPrice" not in r]
    assert missing, "expected at least one report missing targetPrice"
    for report in missing:
        assert dec_of(report, "targetPrice") is None
        assert text_of(report, "targetPriceStatus", 32) is None


def test_counts_are_bare_integers() -> None:
    """`companiesCount` / `industriesCount` are bare ints."""
    overview = domain_data("sector", "financial-services")["overview"]
    assert isinstance(overview["companiesCount"], int)
    assert isinstance(overview["industriesCount"], int)
    # Proof of the upper bound: INTEGER is enough
    assert int_of(overview, "companiesCount") == overview["companiesCount"]
    assert int_of(overview, "companiesCount") < 2**31


def test_industry_overview_has_no_industries_count_key_at_all() -> None:
    """yfinance's `.get()` call manufactures a 7th field; it's not in the raw JSON."""
    overview = domain_data("industry", "semiconductors")["overview"]
    assert "industriesCount" not in overview
    assert int_of(overview, "industriesCount") is None
    assert len(overview) == 6


def test_empty_string_becomes_null() -> None:
    assert text_of({"name": ""}, "name", 32) is None
    assert text_of({"name": "   "}, "name", 32) is None
    assert text_of({"name": " Technology "}, "name", 32) == "Technology"

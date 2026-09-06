"""Sarmalayici asimetrisi ve alan ayristirmasi (SI S9.2, S4.4).

Yahoo sayisal alanlari `{"raw":..., "fmt":...}` olarak sarar AMA TUTARLI
BICIMDE DEGIL. Bu dosya UC bicimi ayni testte sabitler; iki ayri
ayristirici yazilsaydi biri sessizce None yazardi.
"""

from __future__ import annotations

from decimal import Decimal

from helpers import domain_data
from yfin.datasets.domain.common import dec_of, int_of, text_of, unwrap


def test_unwrap_handles_all_three_shapes() -> None:
    assert unwrap({"raw": 1.5, "fmt": "1.50"}) == 1.5
    assert unwrap(450.0) == 450.0
    assert unwrap(None) is None
    # Sarmali ama `raw` yok: sozluk oldugu gibi geri donmez, None olur --
    # NOT NULL olmayan bir kolona sozluk yazmak DataError verirdi.
    assert unwrap({"fmt": "1.50"}) is None


def test_target_price_is_wrapped_in_top_companies_but_bare_in_reports() -> None:
    """AYNI ALAN ADI, IKI FARKLI BICIM -- tek `unwrap` yolunun gerekcesi."""
    data = domain_data("sector", "technology")
    company = data["topCompanies"][0]
    assert isinstance(company["targetPrice"], dict)
    assert "raw" in company["targetPrice"]

    wrapped_reports = [r for r in data["researchReports"] if "targetPrice" in r]
    for report in wrapped_reports:
        assert not isinstance(report["targetPrice"], dict), (
            "researchReports[].targetPrice CIPLAK float olmali (SI S4.4)"
        )

    # Ikisi de ayni yoldan gecer ve ikisi de Decimal uretir.
    assert isinstance(dec_of(company, "targetPrice"), Decimal)
    for report in wrapped_reports:
        assert isinstance(dec_of(report, "targetPrice"), Decimal)


def test_missing_report_fields_become_null() -> None:
    """104 raporun 17'sinde `targetPrice` HIC YOK -> row.get() -> NULL."""
    data = domain_data("sector", "technology")
    missing = [r for r in data["researchReports"] if "targetPrice" not in r]
    assert missing, "en az bir raporda targetPrice eksik olmali"
    for report in missing:
        assert dec_of(report, "targetPrice") is None
        assert text_of(report, "targetPriceStatus", 32) is None


def test_counts_are_bare_integers() -> None:
    """`companiesCount` / `industriesCount` CIPLAK int (SI S4.4)."""
    overview = domain_data("sector", "financial-services")["overview"]
    assert isinstance(overview["companiesCount"], int)
    assert isinstance(overview["industriesCount"], int)
    # Ust sinir kaniti: INTEGER yeterli
    assert int_of(overview, "companiesCount") == overview["companiesCount"]
    assert int_of(overview, "companiesCount") < 2**31


def test_industry_overview_has_no_industries_count_key_at_all() -> None:
    """yfinance'in `.get()` cagrisi 7. alani URETIYOR; ham JSON'da YOK."""
    overview = domain_data("industry", "semiconductors")["overview"]
    assert "industriesCount" not in overview
    assert int_of(overview, "industriesCount") is None
    assert len(overview) == 6


def test_empty_string_becomes_null() -> None:
    assert text_of({"name": ""}, "name", 32) is None
    assert text_of({"name": "   "}, "name", 32) is None
    assert text_of({"name": " Technology "}, "name", 32) == "Technology"

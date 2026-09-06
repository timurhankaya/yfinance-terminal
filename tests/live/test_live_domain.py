"""Canli Yahoo dogrulamalari (SI S9.4). CI'da KAPALI: `-m live` gerekir."""

from __future__ import annotations

import random

import pytest

from yfin.datasets.domain.common import SECTOR_KEYS, fetch_domain
from yfin.domain_runner import RegionValidationError, domain_regions

pytestmark = pytest.mark.live


def test_every_sector_key_resolves() -> None:
    """`SECTOR_KEYS`in 11'i de 200 donmeli."""
    for key in SECTOR_KEYS:
        data = fetch_domain(key, "sector", "US")
        assert data["key"] == key


def test_industries_count_equals_the_discovered_universe() -> None:
    """EKSIKSIZLIGIN BEKLENEN DEGERINI API'NIN KENDISI VERIYOR (SI S8.3)."""
    total_reported = 0
    total_discovered = 0
    for key in SECTOR_KEYS:
        data = fetch_domain(key, "sector", "US")
        discovered = [row for row in data["industries"] if "key" in row]
        assert data["overview"]["industriesCount"] == len(discovered), key
        total_reported += data["overview"]["industriesCount"]
        total_discovered += len(discovered)
    assert total_reported == total_discovered
    assert total_discovered == 145


def test_a_random_sample_of_discovered_industry_keys_resolves() -> None:
    """Kesfedilen anahtarlar CALISIR; kutuphane sabitininkiler calismiyordu."""
    keys: list[str] = []
    for key in SECTOR_KEYS:
        keys.extend(
            row["key"]
            for row in fetch_domain(key, "sector", "US")["industries"]
            if "key" in row
        )
    for key in random.sample(keys, 5):
        data = fetch_domain(key, "industry", "US")
        assert data["key"] == key
        assert data["sectorKey"]


def test_library_industry_keys_still_return_404() -> None:
    """SI S4.1'in cekirdek bulgusu: `utilities`in 6/6 anahtari 404."""
    from yfinance.const import SECTOR_INDUSTY_MAPPING_LC

    failures = 0
    for key in SECTOR_INDUSTY_MAPPING_LC["utilities"]:
        try:
            fetch_domain(key, "industry", "US")
        except Exception:
            failures += 1
    assert failures == 6


def test_invalid_region_is_rejected_by_the_empirical_probe() -> None:
    from yfin.config import Settings

    settings = Settings(yf_domain_regions="XX", yf_domain_reference_sector="technology")
    with pytest.raises(RegionValidationError):
        domain_regions(settings)


def test_supported_region_passes_the_probe() -> None:
    from yfin.config import Settings

    settings = Settings(yf_domain_regions="US,GB", yf_domain_reference_sector="technology")
    assert domain_regions(settings) == ["US", "GB"]


def test_region_only_affects_the_five_list_blocks() -> None:
    """Bolge YALNIZ liste bloklarini kapsiyor (SI S4.1).

    KESIN ESITLIK ARANMAZ: iki istek arasinda saniyeler geciyor ve piyasa
    hareket ediyor -- `ytdChangePercent` 0.119388185 -> 0.11939399 gibi
    kayiyor. Iddia "bolge bu bloklari DEGISTIRMIYOR"dur, "deger donmus"
    degil; bu yuzden metin alanlarinda esitlik, sayisal alanlarda dar bir
    tolerans kullanilir.
    """
    us = fetch_domain("technology", "sector", "US")
    gb = fetch_domain("technology", "sector", "GB")

    for field in ("description", "messageBoardId", "companiesCount", "industriesCount"):
        assert us["overview"][field] == gb["overview"][field], field
    assert us["performanceOverviewBenchmark"]["name"] == (
        gb["performanceOverviewBenchmark"]["name"]
    )

    for block in ("performance", "performanceOverviewBenchmark"):
        for field, value in us[block].items():
            if not isinstance(value, dict):
                continue
            other = gb[block][field]["raw"]
            assert abs(value["raw"] - other) < 1e-3, f"{block}.{field}"

    assert {c["symbol"] for c in us["topCompanies"]}.isdisjoint(
        {c["symbol"] for c in gb["topCompanies"]}
    )

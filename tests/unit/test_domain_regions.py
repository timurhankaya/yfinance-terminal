"""Region validation. NO network -- `fetch` is injected."""

from __future__ import annotations

from typing import Any

import pytest

from yfin.core.config import Settings
from yfin.pipeline.domain_runner import US, RegionValidationError, domain_regions

_US_TOP = [{"symbol": s} for s in ("NVDA", "AAPL", "MSFT", "AVGO")]
_GB_TOP = [{"symbol": s} for s in ("SGE.L", "AVV.L", "KNOS.L", "SPT.L")]
# A list whose order and SET have both shifted but that does not overlap US
_GB_DRIFTED = [{"symbol": s} for s in ("SPT.L", "KNOS.L", "AVV.L", "DARK.L")]


def _settings(regions: str) -> Settings:
    return Settings(yf_domain_regions=regions, yf_domain_reference_sector="technology")


class Recorder:
    def __init__(self, responses: dict[str, list[dict[str, Any]]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, key: str, domain_type: str, region: str) -> dict[str, Any]:
        self.calls.append((key, domain_type, region))
        return {"topCompanies": self.responses[region]}


def test_us_alone_makes_no_request() -> None:
    """US is Yahoo's FALLBACK region; there is nothing to validate against."""
    fetch = Recorder({US: _US_TOP})
    assert domain_regions(_settings("US"), fetch=fetch) == ["US"]
    assert fetch.calls == []


def test_format_rejection_happens_before_any_request() -> None:
    fetch = Recorder({})
    for value in ("EUROPE", "usa", "U", "1S"):
        with pytest.raises(RegionValidationError, match="ISO 3166-1"):
            domain_regions(_settings(value), fetch=fetch)
    assert fetch.calls == []


def test_empty_configuration_is_rejected() -> None:
    with pytest.raises(RegionValidationError):
        domain_regions(_settings("  ,  "), fetch=Recorder({}))


def test_xx_alone_is_still_probed_against_us() -> None:
    """The BASELINE IS ALWAYS US.

    If the primary region were used as the baseline, `YF_DOMAIN_REGIONS=XX`
    would validate against itself and US data would get written under the
    XX label.
    """
    fetch = Recorder({US: _US_TOP, "XX": _US_TOP})
    with pytest.raises(RegionValidationError, match="is not supported by Yahoo"):
        domain_regions(_settings("XX"), fetch=fetch)
    # The first request is the BASELINE and the baseline's region is always
    # US, regardless of the configured value.
    assert fetch.calls[0] == ("technology", "sector", US)


def test_supported_region_passes() -> None:
    fetch = Recorder({US: _US_TOP, "GB": _GB_TOP})
    assert domain_regions(_settings("US,GB"), fetch=fetch) == ["US", "GB"]
    assert [c[2] for c in fetch.calls] == [US, "GB"]


def test_a_drifted_but_different_list_still_passes() -> None:
    """PROOF THAT EQUALITY IS NOT USED.

    A list whose order and set have both shifted still passes as long as it
    does not overlap US; if equality were used, the natural drift between two
    consecutive requests would wrongly REJECT this region.
    """
    fetch = Recorder({US: _US_TOP, "GB": _GB_DRIFTED})
    assert domain_regions(_settings("US,GB"), fetch=fetch) == ["US", "GB"]


def test_partial_overlap_below_the_threshold_passes() -> None:
    mixed = [{"symbol": s} for s in ("NVDA", "SGE.L", "AVV.L", "KNOS.L")]
    fetch = Recorder({US: _US_TOP, "GB": mixed})
    assert domain_regions(_settings("US,GB"), fetch=fetch) == ["US", "GB"]


def test_overlap_at_the_threshold_is_rejected() -> None:
    half = [{"symbol": s} for s in ("NVDA", "AAPL", "SGE.L", "AVV.L")]
    fetch = Recorder({US: _US_TOP, "GB": half})
    with pytest.raises(RegionValidationError, match="overlaps US by 50%"):
        domain_regions(_settings("US,GB"), fetch=fetch)


def test_base_request_is_cached_for_the_run() -> None:
    """If US is configured, `sector_profile` does NOT re-fetch the baseline."""
    cache: dict[str, Any] = {}
    fetch = Recorder({US: _US_TOP, "GB": _GB_TOP})
    domain_regions(_settings("US,GB"), cache=cache, fetch=fetch)
    assert "raw:technology:US" in cache
    assert "raw:technology:GB" in cache


def test_us_free_configuration_pays_one_extra_base_request() -> None:
    fetch = Recorder({US: _US_TOP, "GB": _GB_TOP, "DE": [{"symbol": "SAP.DE"}]})
    assert domain_regions(_settings("GB,DE"), fetch=fetch) == ["GB", "DE"]
    assert [c[2] for c in fetch.calls] == [US, "GB", "DE"]

"""Ucuncu registry: alias, bootstrap, scope ve regional bayraklari (SI S9.2)."""

from __future__ import annotations

import pytest

from yfin.datasets.registry import DOMAIN_DATASETS, UnknownDatasetError


def test_alias_expansion() -> None:
    assert [d.name for d in DOMAIN_DATASETS.resolve(["sector"])] == [
        "domain_taxonomy",
        "sector_profile",
        "sector_rankings",
    ]
    assert [d.name for d in DOMAIN_DATASETS.resolve(["industry"])] == [
        "domain_taxonomy",
        "industry_profile",
        "industry_rankings",
    ]


def test_all_resolves_to_five_datasets() -> None:
    assert len(DOMAIN_DATASETS.resolve(None)) == 5
    assert len(DOMAIN_DATASETS.resolve(["all"])) == 5


def test_unknown_name_is_rejected() -> None:
    with pytest.raises(UnknownDatasetError):
        DOMAIN_DATASETS.resolve(["sectors"])


def test_bootstrap_is_hidden_from_user_visible_names() -> None:
    assert "domain_taxonomy" not in DOMAIN_DATASETS.user_visible_names()
    assert "sector" in DOMAIN_DATASETS.user_visible_names()


def test_depends_on_pulls_the_bootstrap_even_when_deselected() -> None:
    resolved = [d.name for d in DOMAIN_DATASETS.resolve(["industry_rankings"])]
    assert resolved == ["domain_taxonomy", "industry_rankings"]


def test_scope_and_regional_flags_drive_the_run_shape() -> None:
    """`regional` bolge dongusunu, `scope` anahtar kumesini belirler."""
    expected = {
        "domain_taxonomy": ("sector", False, False),
        "sector_profile": ("sector", False, True),
        "sector_rankings": ("sector", True, True),
        "industry_profile": ("industry", False, True),
        "industry_rankings": ("industry", True, True),
    }
    for name, (scope, regional, per_key) in expected.items():
        dataset = DOMAIN_DATASETS[name]
        assert dataset.scope == scope, name
        assert dataset.regional is regional, name
        assert dataset.per_key is per_key, name


def test_produces_declares_the_gate_table() -> None:
    """Kapi bildirilmezse hata yolunda denetimden duserdi (AH S6.1)."""
    for name in ("sector_profile", "sector_rankings", "industry_profile", "industry_rankings"):
        assert DOMAIN_DATASETS[name].produces[-1] == "domain_asof_state"
    assert "domain_asof_state" not in DOMAIN_DATASETS["domain_taxonomy"].produces


def test_table_counts_match_the_audit_formula() -> None:
    """SI S8.3: 2 / 4 / 3 / 5 / 3 tablo."""
    counts = {name: len(DOMAIN_DATASETS[name].produces) for name in DOMAIN_DATASETS}
    assert counts == {
        "domain_taxonomy": 2,
        "sector_profile": 4,
        "sector_rankings": 3,
        "industry_profile": 5,
        "industry_rankings": 3,
    }


def test_expected_cell_count_matches_the_spec_for_one_region() -> None:
    from yfin.domain_audit import expected_cell_count

    assert expected_cell_count(1) == 1239

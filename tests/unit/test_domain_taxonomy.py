"""domain_taxonomy normalization."""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime

from helpers import domain_data
from yfin.datasets.domain.common import SECTOR_KEYS
from yfin.datasets.domain.payloads import TaxonomyPayload
from yfin.datasets.registry import DOMAIN_DATASETS

FETCHED_AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
AS_OF = date(2026, 9, 4)

DATASET = DOMAIN_DATASETS["domain_taxonomy"]


def _payload(*keys: str) -> TaxonomyPayload:
    return TaxonomyPayload(
        sectors={key: domain_data("sector", key) for key in keys}, fetched_at=FETCHED_AT
    )


def _rows(payload: TaxonomyPayload) -> tuple[list[dict], list[dict]]:
    result = DATASET.normalize(payload, "*")
    tables = {write.table: write for write in result.writes}
    return tables["symbols"].rows, tables["domains"].rows


def test_all_industries_row_is_dropped_by_the_absence_of_key() -> None:
    """12 of 13 rows have a `key`; that one row has neither `key` nor `symbol`."""
    data = domain_data("sector", "technology")
    assert len(data["industries"]) == 13
    _, domains = _rows(_payload("technology"))
    industries = [r for r in domains if r["domain_type"] == "industry"]
    assert len(industries) == 12
    assert data["overview"]["industriesCount"] == 12


def test_renaming_all_industries_still_drops_it() -> None:
    """Proof the code does not look at the name.

    yfinance filters via `i.get('name') != 'All Industries'`, a
    language-dependent rule. If Yahoo changes the label, matching on the
    name would try to insert row 13 into the PK with `key=NULL`.
    """
    data = copy.deepcopy(domain_data("sector", "technology"))
    row = next(r for r in data["industries"] if "key" not in r)
    assert row["name"] == "All Industries"
    row["name"] = "Tum Endustriler"

    payload = TaxonomyPayload(sectors={"technology": data}, fetched_at=FETCHED_AT)
    _, domains = _rows(payload)
    assert len([r for r in domains if r["domain_type"] == "industry"]) == 12


def test_sector_row_precedes_its_industries() -> None:
    """Row order is binding: `parent_key` is a self-FK."""
    _, domains = _rows(_payload("technology", "utilities"))
    seen: set[str] = set()
    for row in domains:
        if row["domain_type"] == "industry":
            assert row["parent_key"] in seen, f"{row['domain_key']} came before its parent"
        seen.add(row["domain_key"])


def test_symbols_write_comes_before_domains_write() -> None:
    """Table order is also binding: `domains.symbol` -> `symbols.symbol` FK."""
    result = DATASET.normalize(_payload("technology"), "*")
    assert [w.table for w in result.writes] == ["symbols", "domains"]


def test_symbol_rows_are_inactive_and_typed() -> None:
    symbols, _ = _rows(_payload("technology"))
    for row in symbols:
        assert row["is_active"] is False
        assert row["quote_type"] == "INDEX"
        assert row["exchange"] == "YHD"
        assert row["currency"] == "USD"
        assert row["timezone"] == "America/New_York"
        assert row["last_seen_at"] == FETCHED_AT


def test_is_active_and_unknown_streak_are_outside_update_columns() -> None:
    """If the user manually activated `^YH311`, sync must not turn it back off."""
    result = DATASET.normalize(_payload("technology"), "*")
    symbols = next(w for w in result.writes if w.table == "symbols")
    assert "is_active" not in symbols.update_columns
    assert "unknown_streak" not in symbols.update_columns


def test_domains_update_columns_are_disjoint_from_industry_profile() -> None:
    """The two writers update disjoint column sets and never overwrite each other."""
    result = DATASET.normalize(_payload("technology"), "*")
    bootstrap = next(w for w in result.writes if w.table == "domains")
    profile = DOMAIN_DATASETS["industry_profile"]
    overlap = set(bootstrap.update_columns) & set(
        # `fetched_at` is present in both and must be: each writer refreshes
        # its own "time of last verification".
        {"description", "message_board_id"}
    )
    assert overlap == set()
    assert profile.name == "industry_profile"
    assert "first_seen_at" not in bootstrap.update_columns


def test_industry_rows_carry_no_description_from_the_industries_block() -> None:
    """The `industries[]` block has neither field; `industry_profile` writes them."""
    _, domains = _rows(_payload("technology"))
    for row in domains:
        if row["domain_type"] != "industry":
            continue
        assert row["description"] is None
        assert row["message_board_id"] is None


def test_sector_rows_get_description_from_their_own_overview() -> None:
    _, domains = _rows(_payload("technology"))
    sector = next(r for r in domains if r["domain_type"] == "sector")
    assert sector["description"]
    assert sector["message_board_id"]
    assert sector["parent_key"] is None


def test_bootstrap_is_prepended_to_every_resolution() -> None:
    for names in (None, ["sector"], ["industry_rankings"]):
        resolved = [d.name for d in DOMAIN_DATASETS.resolve(names)]
        assert resolved[0] == "domain_taxonomy"


def test_sector_keys_are_the_only_universe_source() -> None:
    """Bootstrap only iterates `SECTOR_KEYS`; the key count is 11."""
    assert len(SECTOR_KEYS) == 11
    assert len(set(SECTOR_KEYS)) == 11
    assert AS_OF.isoformat() == "2026-09-04"

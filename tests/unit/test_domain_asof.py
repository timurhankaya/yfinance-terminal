"""Domain as-of kapisi: hash govdesi ve kapi kimligi (SI S9.2, S6.2)."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from helpers import domain_data
from yfin.datasets.asof_base import DOMAIN_GATE_TABLE, GLOBAL_REGION_MARKER
from yfin.datasets.base import NormalizedResult
from yfin.datasets.domain.payloads import DomainPayload
from yfin.datasets.registry import DOMAIN_DATASETS
from yfin.storage.contracts import TableWrite

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=6)
AS_OF = date(2026, 9, 4)

RANKINGS = DOMAIN_DATASETS["sector_rankings"]
PROFILE = DOMAIN_DATASETS["sector_profile"]
INDUSTRY_RANKINGS = DOMAIN_DATASETS["industry_rankings"]


class FakeWriter:
    def __init__(self, known: set[str] | None = None) -> None:
        self.written: list[TableWrite] = []
        self.hashes: dict[tuple[str, str], str] = {}
        self.known = known or set()

    def write(self, write: TableWrite) -> int:
        self.written.append(write)
        return len(write.rows)

    def current_hash(self, table: str, key: Mapping[str, Any]) -> str | None:
        return self.hashes.get((table, "|".join(str(v) for v in key.values())))

    def known_symbols(self, candidates: set[str]) -> set[str]:
        return candidates & self.known

    def write_for(self, table: str) -> TableWrite:
        return next(w for w in self.written if w.table == table)


def _payload(
    kind: str, key: str, region: str = "US", *, fetched_at: datetime = NOW
) -> DomainPayload:
    return DomainPayload(
        data=domain_data(kind, key, region),
        fetched_at=fetched_at,
        as_of_date=AS_OF,
        region=region,
        domain_type=kind,
    )


def test_row_order_does_not_change_the_hash() -> None:
    """`topCompanies` sirasi 15 dakikada 11 sektorun 8'inde degisti."""
    payload = _payload("sector", "technology")
    baseline = RANKINGS.normalize(payload, "technology")

    shuffled = copy.deepcopy(payload.data)
    shuffled["topCompanies"] = list(reversed(shuffled["topCompanies"]))
    other = RANKINGS.normalize(
        DomainPayload(
            data=shuffled,
            fetched_at=payload.fetched_at,
            as_of_date=AS_OF,
            region="US",
            domain_type="sector",
        ),
        "technology",
    )
    assert RANKINGS.content_hash(baseline) == RANKINGS.content_hash(other)


def test_fetched_at_and_as_of_date_are_volatile() -> None:
    first = RANKINGS.normalize(_payload("sector", "technology"), "technology")
    second = RANKINGS.normalize(
        _payload("sector", "technology", fetched_at=LATER), "technology"
    )
    assert RANKINGS.content_hash(first) == RANKINGS.content_hash(second)


def test_first_seen_at_is_volatile_too() -> None:
    """SI S6.2/3: aksi halde kapi ASLA esitlenmezdi.

    `research_reports.first_seen_at` her kosuda degisir; hash
    govdesine girseydi profil dataset'i her gun her satiri yeniden yazar
    ve as-of mekanizmasi sessizce hic calismazdi.
    """
    first = PROFILE.normalize(_payload("sector", "technology"), "technology")
    second = PROFILE.normalize(
        _payload("sector", "technology", fetched_at=LATER), "technology"
    )
    reports = next(w for w in first.writes if w.table == "research_reports")
    assert reports.rows and "first_seen_at" in reports.rows[0]
    assert PROFILE.content_hash(first) == PROFILE.content_hash(second)


def test_is_known_stays_in_the_hash_body() -> None:
    """Bayrak hash'te KALIR: evren degisince kapi acilir (SI S7.4)."""
    result = RANKINGS.normalize(_payload("sector", "technology"), "technology")
    plain = FakeWriter()
    RANKINGS.upsert(plain, result)
    hash_without = plain.write_for(DOMAIN_GATE_TABLE).rows[0]["content_hash"]

    symbol = result.writes[0].rows[0]["symbol"]
    aware = FakeWriter(known={symbol})
    RANKINGS.upsert(aware, RANKINGS.normalize(_payload("sector", "technology"), "technology"))
    hash_with = aware.write_for(DOMAIN_GATE_TABLE).rows[0]["content_hash"]

    assert hash_without != hash_with


def test_a_new_yahoo_field_changes_the_hash() -> None:
    payload = _payload("sector", "technology")
    baseline = RANKINGS.normalize(payload, "technology")
    mutated = copy.deepcopy(payload.data)
    mutated["topCompanies"][0]["rating"] = "Sell"
    other = RANKINGS.normalize(
        DomainPayload(
            data=mutated,
            fetched_at=NOW,
            as_of_date=AS_OF,
            region="US",
            domain_type="sector",
        ),
        "technology",
    )
    assert RANKINGS.content_hash(baseline) != RANKINGS.content_hash(other)


def test_gate_identity_is_domain_key_dataset_region() -> None:
    result = RANKINGS.normalize(_payload("sector", "technology", "GB"), "technology")
    assert RANKINGS.gate_identity(result) == {
        "domain_key": "technology",
        "dataset": "sector_rankings",
        "region": "GB",
    }


def test_regionless_dataset_writes_the_global_marker() -> None:
    """`domain_metrics` satirlarinda `region` kolonu YOK -> '*'."""
    result = PROFILE.normalize(_payload("sector", "technology"), "technology")
    assert "region" not in result.writes[0].rows[0]
    assert PROFILE.gate_identity(result)["region"] == GLOBAL_REGION_MARKER


def test_gate_row_uses_the_domain_gate_table() -> None:
    writer = FakeWriter()
    RANKINGS.upsert(writer, RANKINGS.normalize(_payload("sector", "technology"), "technology"))
    gate = writer.write_for(DOMAIN_GATE_TABLE)
    assert gate.key_columns == ("domain_key", "dataset", "region")
    assert gate.rows[0]["as_of_date"] == AS_OF
    fresh = RANKINGS.normalize(_payload("sector", "technology"), "technology")
    assert gate.rows[0]["row_count"] == sum(len(w.rows) for w in fresh.writes)


def test_unchanged_hash_skips_data_tables_but_still_writes_the_gate() -> None:
    result = RANKINGS.normalize(_payload("sector", "technology"), "technology")
    digest = RANKINGS.content_hash(result)
    writer = FakeWriter()
    writer.hashes[(DOMAIN_GATE_TABLE, "technology|sector_rankings|US")] = digest

    stats = RANKINGS.upsert(writer, result)
    assert [w.table for w in writer.written] == [DOMAIN_GATE_TABLE]
    assert stats.skipped["domain_top_companies"] > 0


def test_empty_result_writes_no_gate_row() -> None:
    """`infrastructure-operations`: uc blogun hicbirinde satir yok."""
    result = INDUSTRY_RANKINGS.normalize(
        _payload("industry", "infrastructure-operations"), "infrastructure-operations"
    )
    assert result.is_empty
    writer = FakeWriter()
    stats = INDUSTRY_RANKINGS.upsert(writer, result)
    assert writer.written == []
    assert stats.attempted == {}


def test_profile_writes_start_with_a_table_that_has_as_of_date() -> None:
    """`domains` EN SONDA: `as_of_date` kolonu yok, basta olsaydi KeyError."""
    industry_profile = DOMAIN_DATASETS["industry_profile"]
    result = industry_profile.normalize(_payload("industry", "semiconductors"), "semiconductors")
    tables = [w.table for w in result.writes]
    assert tables[-1] == "domains"
    for write in result.writes[:-1]:
        assert write.rows == [] or "as_of_date" in write.rows[0]
    assert "as_of_date" not in result.writes[-1].rows[0]
    assert NormalizedResult(writes=result.writes).is_empty is False

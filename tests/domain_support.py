"""Domain repo testleri icin ortak kosum yardimcilari (SI S9.3).

Uretim yolunun AYNISINI kullanir: gercek dataset ornekleri, gercek
`PostgresRowWriter`, gercek `NormalizedResult`. Tek fark cekimin fixture'dan
gelmesidir.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from helpers import domain_data
from yfin.datasets.domain.payloads import DomainPayload, TaxonomyPayload
from yfin.datasets.registry import DOMAIN_DATASETS
from yfin.persistence import PostgresRowWriter

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
AS_OF = date(2026, 9, 4)

# Fixture'i olan sektorler; taksonomi bunlar uzerinden kurulur.
FIXTURE_SECTORS = ("technology", "utilities", "healthcare", "financial-services")


def run_taxonomy(
    session: Session,
    sectors: tuple[str, ...] = FIXTURE_SECTORS,
    *,
    fetched_at: datetime = NOW,
) -> Any:
    dataset = DOMAIN_DATASETS["domain_taxonomy"]
    payload = TaxonomyPayload(
        sectors={key: domain_data("sector", key) for key in sectors}, fetched_at=fetched_at
    )
    result = dataset.normalize(payload, "*")
    return dataset.upsert(PostgresRowWriter(session), result)


def run_dataset(
    session: Session,
    name: str,
    kind: str,
    key: str,
    *,
    region: str = "US",
    fetched_at: datetime = NOW,
    as_of: date = AS_OF,
    parent: str | None = None,
) -> Any:
    dataset = DOMAIN_DATASETS[name]
    payload = DomainPayload(
        data=domain_data(kind, key, region),
        fetched_at=fetched_at,
        as_of_date=as_of,
        # Bolgesiz dataset'ler satirlarina `region` yazmaz; bolgeli olanlar
        # `payload.region`i kullanir.
        region=region,
        domain_type=kind,
        expected_parent=parent,
    )
    result = dataset.normalize(payload, key)
    return dataset.upsert(PostgresRowWriter(session), result)

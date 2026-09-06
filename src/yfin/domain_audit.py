"""`yfin domain audit` -- UC BAGIMSIZ KONTROL (SI S8.3).

Ikinci kontrol bu tasarimin cekirdegidir: beklenen deger BIZIM
LISTEMIZDEN DEGIL, Yahoo'nun `overview.industriesCount` alanindan gelir ve
11/11 sektorde liste uzunluguna esit olculdu (toplam 145). Yahoo yeni bir
endustri eklerse ve kesfimiz onu kacirirsa bu kontrol PATLAR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from yfin.datasets.domain.common import SECTOR_KEYS
from yfin.models import Domain, DomainMetric, DomainType, ItemStatus, SyncRunItem


@dataclass
class DomainAuditReport:
    sector_count: int = 0
    industry_count: int = 0
    expected_industries: int | None = None
    failed_cells: int = 0
    cells_by_status: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def exit_code(self) -> int:
        return 0 if self.ok else 1


def audit_domains(
    session: Session,
    *,
    as_of: date | None = None,
    run_id: int | None = None,
) -> DomainAuditReport:
    report = DomainAuditReport()

    # 1) Sektor sayisi
    report.sector_count = int(
        session.execute(
            select(func.count())
            .select_from(Domain)
            .where(Domain.domain_type == DomainType.SECTOR)
        ).scalar_one()
    )
    if report.sector_count != len(SECTOR_KEYS):
        report.problems.append(
            f"sektor sayisi {report.sector_count}, beklenen {len(SECTOR_KEYS)}"
        )

    report.industry_count = int(
        session.execute(
            select(func.count())
            .select_from(Domain)
            .where(Domain.domain_type == DomainType.INDUSTRY)
        ).scalar_one()
    )

    # 2) Endustri sayisi -- BEKLENEN DEGERI API'NIN KENDISI VERIYOR.
    #
    # DIKKAT: `as_of_date = :as_of` KESIN ESITLIK KULLANILAMAZ. Kapi hash'i
    # esit bulursa o gun HIC `domain_metrics` satiri yazilmaz (`as_of_date`
    # VOLATILE, satir da yeniden yazilmaz) ve audit SAHTE BASARISIZLIK
    # verirdi. Her sektorun `:as_of`a KADARKI EN SON satiri alinir.
    latest = (
        select(
            DomainMetric.domain_key.label("domain_key"),
            DomainMetric.industries_count.label("industries_count"),
            func.row_number()
            .over(
                partition_by=DomainMetric.domain_key,
                order_by=DomainMetric.as_of_date.desc(),
            )
            .label("rn"),
        )
        .join(Domain, Domain.domain_key == DomainMetric.domain_key)
        .where(Domain.domain_type == DomainType.SECTOR)
    )
    if as_of is not None:
        latest = latest.where(DomainMetric.as_of_date <= as_of)
    sub = latest.subquery()
    expected = session.execute(
        select(func.sum(sub.c.industries_count)).where(sub.c.rn == 1)
    ).scalar()
    report.expected_industries = None if expected is None else int(expected)

    if report.expected_industries is None:
        report.problems.append(
            "domain_metrics'te sektor satiri yok: beklenen endustri sayisi hesaplanamiyor"
        )
    elif report.expected_industries != report.industry_count:
        report.problems.append(
            f"endustri sayisi {report.industry_count}, "
            f"API'nin bildirdigi toplam {report.expected_industries}"
        )

    # 3) Hucre kapsami ve hata
    if run_id is not None:
        rows = session.execute(
            select(SyncRunItem.status, func.count())
            .where(SyncRunItem.run_id == run_id)
            .group_by(SyncRunItem.status)
        ).all()
        report.cells_by_status = {ItemStatus(status).value: int(count) for status, count in rows}
        report.failed_cells = report.cells_by_status.get(ItemStatus.FAILED.value, 0)
        if report.failed_cells:
            report.problems.append(f"run #{run_id}: {report.failed_cells} basarisiz hucre")

    return report


def expected_cell_count(region_count: int, industry_count: int = 145) -> int:
    """Beklenen `sync_run_items` hucre sayisi (SI S8.3).

    domain_taxonomy   : 2 tablo x 1 tur
    sector_profile    : 4 tablo x 11 anahtar x 1 bolge
    sector_rankings   : 3 tablo x 11 anahtar x R
    industry_profile  : 5 tablo x N anahtar x 1 bolge
    industry_rankings : 3 tablo x N anahtar x R

    R=1 -> 1239 (spec S8.3 ile ayni). R=5 -> 3111; spec'teki 3143 bir
    ARITMETIK SLIP'tir: 2 + 44 + 33*5 + 725 + 435*5 = 3111. Beklenen deger
    burada FORMULDEN uretilir, elle yazilmis bir sabitten degil.
    """
    sectors = len(SECTOR_KEYS)
    return (
        2
        + 4 * sectors
        + 3 * sectors * region_count
        + 5 * industry_count
        + 3 * industry_count * region_count
    )

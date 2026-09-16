"""`yfin domain audit` -- three independent checks.

The expected industry count comes from Yahoo's `overview.industriesCount`,
not our own list, so an industry our discovery misses fails the audit.
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

    # 1) Sector count
    report.sector_count = int(
        session.execute(
            select(func.count())
            .select_from(Domain)
            .where(Domain.domain_type == DomainType.SECTOR)
        ).scalar_one()
    )
    if report.sector_count != len(SECTOR_KEYS):
        report.problems.append(
            f"sector count {report.sector_count}, expected {len(SECTOR_KEYS)}"
        )

    report.industry_count = int(
        session.execute(
            select(func.count())
            .select_from(Domain)
            .where(Domain.domain_type == DomainType.INDUSTRY)
        ).scalar_one()
    )

    # 2) Industry count. Each sector's latest row up to `:as_of`, not an
    # exact date match: an unchanged hash writes no row that day.
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
            "no sector row in domain_metrics: cannot compute the expected industry count"
        )
    elif report.expected_industries != report.industry_count:
        report.problems.append(
            f"industry count {report.industry_count}, "
            f"against the {report.expected_industries} the API reports"
        )

    # 3) Cell coverage and failures
    if run_id is not None:
        rows = session.execute(
            select(SyncRunItem.status, func.count())
            .where(SyncRunItem.run_id == run_id)
            .group_by(SyncRunItem.status)
        ).all()
        report.cells_by_status = {ItemStatus(status).value: int(count) for status, count in rows}
        report.failed_cells = report.cells_by_status.get(ItemStatus.FAILED.value, 0)
        if report.failed_cells:
            report.problems.append(f"run #{run_id}: {report.failed_cells} failed cells")

    return report

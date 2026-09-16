"""Retention (pruning) operations. Off by default: pruned rows cannot be refetched.

Each scope's latest as-of day is always kept: the gate row would survive
the delete, the next run would see an equal hash and skip, and the day is lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Table, and_, delete, func, select, tuple_
from sqlalchemy.orm import Session

from yfin.models import Base, News, NewsSymbol
from yfin.models.market import CALENDAR_TIME_COLUMNS
from yfin.storage.changes import ChangeCollector
from yfin.storage.db import rowcount
from yfin.storage.purge import delete_rows

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Type-only, so the local imports below keep doing their job: this
    # module must not depend on `datasets` at runtime (circular import).
    from yfin.datasets.registry import Registry

# Defaults for the symbol side; parameterized to also serve the domain side.
# Not imported from the `datasets` package at module level (circular import),
# so the names are duplicated here -- `test_prune_domain.py` keeps the two in
# sync.
_DEFAULT_GATE = "asof_state"
_DEFAULT_SCOPE_COLUMN = "symbol"


class PruneDisabledError(RuntimeError):
    """Pruning is disabled (YF_PRUNE_ENABLED=false) and --force was not given."""


@dataclass
class PruneReport:
    """Row counts deleted (or that would be deleted under --dry-run)."""

    orphan_news: int = 0
    # Reports left dangling as `domain_report_links` gets pruned.
    orphan_reports: int = 0
    calendars: dict[str, int] = field(default_factory=dict)
    history: dict[str, int] = field(default_factory=dict)
    asof: dict[str, int] = field(default_factory=dict)
    domain_asof: dict[str, int] = field(default_factory=dict)
    # Discovery tables scope on `query_term`, not `symbol` -- pruning with
    # the default would produce the wrong protected set.
    discovery_asof: dict[str, int] = field(default_factory=dict)
    screens: dict[str, int] = field(default_factory=dict)
    # The run audit itself; the freshness query reads it, so it must not grow unbounded.
    audit: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False

    @property
    def total(self) -> int:
        return (
            self.orphan_news
            + self.orphan_reports
            + sum(self.calendars.values())
            + sum(self.history.values())
            + sum(self.asof.values())
            + sum(self.domain_asof.values())
            + sum(self.discovery_asof.values())
            + sum(self.screens.values())
            + sum(self.audit.values())
        )


def history_tables() -> list[str]:
    """Snapshot history tables: suffixed `_history` and carrying `fetched_at`.

    Derived from metadata instead of a hand-maintained list, so a new
    snapshot pair is covered by pruning automatically.
    """
    return sorted(
        table.name
        for table in Base.metadata.tables.values()
        if table.name.endswith("_history") and "fetched_at" in table.c
    )


def _symbol_registry() -> Registry[Any]:
    """Local import: this module must not depend on `datasets` at runtime."""
    from yfin.datasets import SYMBOL_DATASETS

    return SYMBOL_DATASETS


def asof_table_datasets(
    registry: Registry[Any] | None = None,
    gate_table: str = _DEFAULT_GATE,
) -> dict[str, list[str]]:
    """As-of table -> names of the datasets that write to it.

    Decided by the `AsOfGate` base class, not an `as_of_date` column. The
    gate table itself is excluded: deleting a gate row loses `first_seen_at`.
    """
    from yfin.datasets.asof_base import AsOfGate

    reg = _symbol_registry() if registry is None else registry
    mapping: dict[str, list[str]] = {}
    for name in reg:
        dataset = reg[name]
        if not isinstance(dataset, AsOfGate):
            continue
        # `search` / `lookup` are also `AsOfGate` but gate on
        # `discovery_asof_state`; pruning them against another gate would
        # leave even their latest day unprotected.
        if dataset.asof_gate_table != gate_table:
            continue
        for table in dataset.produces:
            if table == gate_table:
                continue
            # Targets without `as_of_date` are not subject to as-of pruning;
            # listing them here would make them look pruned.
            if "as_of_date" not in Base.metadata.tables[table].c:
                continue
            mapping.setdefault(table, []).append(name)
    return mapping


def _asof_protected(
    session: Session,
    table: Table,
    before: date,
    datasets: list[str],
    *,
    gate_table: str = _DEFAULT_GATE,
    scope_column: str = _DEFAULT_SCOPE_COLUMN,
) -> list[tuple[str, date]]:
    """(scope, as_of_date) pairs to protect: gate rows plus GROUP BY over the table.

    The gate is authoritative when several datasets write one table; the
    GROUP BY stays in the union for rows that have no gate row.
    """
    gate = Base.metadata.tables[gate_table]
    # `domain_asof_state`'s PK also has `region`; protected pairs collapse to
    # (domain_key, as_of_date) -- more conservative across regions, not less.
    gate_stmt = select(gate.c[scope_column], gate.c["as_of_date"]).where(
        gate.c["dataset"].in_(datasets)
    )
    table_stmt = (
        select(table.c[scope_column], func.max(table.c["as_of_date"]))
        .group_by(table.c[scope_column])
        .having(func.max(table.c["as_of_date"]) < before)
    )
    pairs = {
        (str(scope), as_of)
        for stmt in (gate_stmt, table_stmt)
        for scope, as_of in session.execute(stmt)
        if as_of is not None
    }
    return sorted(pairs)


def prune_asof(
    session: Session,
    before: date,
    *,
    dry_run: bool = False,
    registry: Registry[Any] | None = None,
    gate_table: str = _DEFAULT_GATE,
    scope_column: str = _DEFAULT_SCOPE_COLUMN,
    collector: ChangeCollector | None = None,
) -> dict[str, int]:
    """Delete as-of rows older than `before`; the latest day is always kept.

    Domain side: `(DOMAIN_DATASETS, "domain_asof_state", "domain_key")`;
    `symbol` in domain tables is the company's symbol, not the scope.
    """
    removed: dict[str, int] = {}
    for name, datasets in sorted(asof_table_datasets(registry, gate_table).items()):
        table = Base.metadata.tables[name]
        # Pruning operates on (scope_column, as_of_date); a target missing
        # either column (shared `research_reports`, static `domains`) is excluded.
        if scope_column not in table.c or "as_of_date" not in table.c:
            continue
        where = table.c["as_of_date"] < before
        protected = _asof_protected(
            session,
            table,
            before,
            datasets,
            gate_table=gate_table,
            scope_column=scope_column,
        )
        if protected:
            where = and_(
                where,
                tuple_(table.c[scope_column], table.c["as_of_date"]).not_in(protected),
            )
        if dry_run:
            stmt = select(func.count()).select_from(table).where(where)
            removed[name] = int(session.execute(stmt).scalar_one())
            continue
        removed[name] = delete_rows(session, table, where, collector=collector)
    return removed


def prune_orphan_reports(
    session: Session,
    *,
    dry_run: bool = False,
    collector: ChangeCollector | None = None,
) -> int:
    """Delete reports with no remaining link in `domain_report_links`.

    The FK cascade runs from report to links, not the other way, so
    unlinked reports need their own step.
    """
    from yfin.models import DomainReportLink, ResearchReport, SearchReportHit

    # Both link tables count: a report found only via Search has no domain
    # link and must not be treated as orphaned.
    domain_linked = select(DomainReportLink.report_id)
    search_linked = select(SearchReportHit.report_id)
    unlinked = ResearchReport.report_id.not_in(domain_linked) & ResearchReport.report_id.not_in(
        search_linked
    )
    if dry_run:
        stmt = select(func.count()).select_from(ResearchReport).where(unlinked)
        return int(session.execute(stmt).scalar_one())
    return delete_rows(
        session, Base.metadata.tables["research_reports"], unlinked, collector=collector
    )


def prune_orphan_news(
    session: Session,
    *,
    dry_run: bool = False,
    collector: ChangeCollector | None = None,
) -> int:
    """Delete news rows with no remaining link in news_symbols."""
    linked = select(NewsSymbol.news_id)
    if dry_run:
        stmt = select(func.count()).select_from(News).where(News.news_id.not_in(linked))
        return int(session.execute(stmt).scalar_one())
    return delete_rows(
        session,
        Base.metadata.tables["news"],
        News.news_id.not_in(linked),
        collector=collector,
    )


def _prune_by_time(
    session: Session, tables: dict[str, str], before: datetime, *, dry_run: bool,
    collector: ChangeCollector | None = None,
) -> dict[str, int]:
    removed: dict[str, int] = {}
    for name, column in tables.items():
        table = Base.metadata.tables[name]
        where = table.c[column] < before
        if dry_run:
            stmt = select(func.count()).select_from(table).where(where)
            removed[name] = int(session.execute(stmt).scalar_one())
            continue
        removed[name] = delete_rows(session, table, where, collector=collector)
    return removed


def prune_calendars(
    session: Session,
    before: datetime,
    *,
    dry_run: bool = False,
    collector: ChangeCollector | None = None,
) -> dict[str, int]:
    """Delete calendar rows older than `before`.

    Calendar tables accumulate and are symbol-independent, so they can't be
    cleaned via an FK; this is the only cleanup path.
    """
    return _prune_by_time(
        session, CALENDAR_TIME_COLUMNS, before, dry_run=dry_run, collector=collector
    )


def prune_history(
    session: Session,
    before: datetime,
    *,
    dry_run: bool = False,
    collector: ChangeCollector | None = None,
) -> dict[str, int]:
    """Delete `_history` snapshots older than `before`.

    `market_summary_history` grows fastest: its price changes every run, so
    the content_hash gate never filters it out.
    """
    tables = dict.fromkeys(history_tables(), "fetched_at")
    return _prune_by_time(session, tables, before, dry_run=dry_run, collector=collector)


def prune_screens(session: Session, before: date, *, dry_run: bool = False) -> dict[str, int]:
    """Prune screen tables; the latest day per screen (per symbol for quotes) is kept.

    `prune_asof` does not apply: `screener` is a `HashGate`, `screen_runs`
    has no `dataset` column, and `screen_quotes` has no scope column.
    """
    from yfin.models.discovery import ScreenMember, ScreenRun, screen_quotes

    removed: dict[str, int] = {}

    # 1. Membership: the latest day per screen is kept (same rule as as-of).
    latest = (
        select(ScreenRun.screen_key, func.max(ScreenRun.as_of_date).label("keep"))
        .group_by(ScreenRun.screen_key)
        .subquery()
    )
    member_cond = ScreenMember.as_of_date < before
    protected = select(latest.c.screen_key, latest.c.keep)
    keep_pairs = {(k, d) for k, d in session.execute(protected).all()}

    def _member_rows() -> list[tuple[str, date]]:
        stmt = select(ScreenMember.screen_key, ScreenMember.as_of_date).where(member_cond)
        return [(k, d) for k, d in session.execute(stmt).all() if (k, d) not in keep_pairs]

    victims = _member_rows()
    if victims:
        if dry_run:
            removed["screen_members"] = len(victims)
        else:
            count = 0
            for key, day in {(k, d) for k, d in victims}:
                result = session.execute(
                    delete(ScreenMember).where(
                        ScreenMember.screen_key == key, ScreenMember.as_of_date == day
                    )
                )
                count += rowcount(result)
            removed["screen_members"] = count
            session.execute(
                delete(ScreenRun).where(
                    ScreenRun.as_of_date < before,
                    tuple_(ScreenRun.screen_key, ScreenRun.as_of_date).not_in(keep_pairs),
                )
            )

    # 2. Quotes: gateless, the latest day per symbol is kept.
    keep_quotes = (
        select(screen_quotes.c.symbol, func.max(screen_quotes.c.as_of_date).label("keep"))
        .group_by(screen_quotes.c.symbol)
        .subquery()
    )
    quote_cond = screen_quotes.c.as_of_date < before
    quote_cond &= tuple_(screen_quotes.c.symbol, screen_quotes.c.as_of_date).not_in(
        select(keep_quotes.c.symbol, keep_quotes.c.keep)
    )
    if dry_run:
        n = session.execute(
            select(func.count()).select_from(screen_quotes).where(quote_cond)
        ).scalar_one()
        if n:
            removed["screen_quotes"] = int(n)
    else:
        result = session.execute(delete(screen_quotes).where(quote_cond))
        if result.rowcount:  # type: ignore[attr-defined]
            removed["screen_quotes"] = rowcount(result)
    return removed


def prune_audit(
    session: Session,
    before: datetime,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Deletes finished runs older than `before`, and their scheduler rows.

    Items and run_metrics cascade from `sync_runs`. A RUNNING row is never
    deleted, whatever its age: an operator must still be able to see it.
    """
    removed: dict[str, int] = {}
    runs = Base.metadata.tables["sync_runs"]
    scheduler = Base.metadata.tables["scheduler_runs"]

    run_where = and_(runs.c["started_at"] < before, runs.c["finished_at"].is_not(None))
    scheduler_where = and_(
        scheduler.c["scheduled_at"] < before, scheduler.c["finished_at"].is_not(None)
    )

    if dry_run:
        for name, table, where in (
            ("sync_runs", runs, run_where),
            ("scheduler_runs", scheduler, scheduler_where),
        ):
            removed[name] = int(
                session.execute(
                    select(func.count()).select_from(table).where(where)
                ).scalar_one()
            )
        return removed

    removed["sync_runs"] = rowcount(session.execute(runs.delete().where(run_where)))
    removed["scheduler_runs"] = rowcount(
        session.execute(scheduler.delete().where(scheduler_where))
    )
    return removed


def run_prune(
    session: Session,
    *,
    enabled: bool,
    orphan_news: bool = True,
    orphan_reports: bool = True,
    calendars_before: datetime | None = None,
    history_before: datetime | None = None,
    asof_before: datetime | None = None,
    audit_before: datetime | None = None,
    dry_run: bool = False,
    collector: ChangeCollector | None = None,
) -> PruneReport:
    """Single entry point for the pruning flow.

    Date-limited pruning does nothing while `enabled` is False: this is the
    one place that enforces "the feature exists but defaults to off".
    """
    if (calendars_before or history_before or asof_before or audit_before) and not enabled:
        raise PruneDisabledError(
            "pruning is disabled: set YF_PRUNE_ENABLED=true or pass --force"
        )

    report = PruneReport(dry_run=dry_run)
    if audit_before:
        report.audit = prune_audit(session, audit_before, dry_run=dry_run)
    if orphan_news:
        report.orphan_news = prune_orphan_news(session, dry_run=dry_run, collector=collector)
    if calendars_before:
        report.calendars = prune_calendars(
            session, calendars_before, dry_run=dry_run, collector=collector
        )
    if history_before:
        report.history = prune_history(
            session, history_before, dry_run=dry_run, collector=collector
        )
    if asof_before:
        report.asof = prune_asof(
            session, asof_before.date(), dry_run=dry_run, collector=collector
        )
        # Second pass, with the domain triple. Grouping by `symbol` would
        # produce the wrong protected scope -- `symbol` in domain tables is
        # the company's symbol.
        from yfin.datasets.registry import DOMAIN_DATASETS, SYMBOL_DATASETS

        report.domain_asof = prune_asof(
            session,
            asof_before.date(),
            dry_run=dry_run,
            registry=DOMAIN_DATASETS,
            gate_table="domain_asof_state",
            scope_column="domain_key",
            collector=collector,
        )
        # Third pass, with the discovery triple: discovery tables scope on
        # `query_term`; the default `symbol` scope would skip or misprotect them.
        report.discovery_asof = prune_asof(
            session,
            asof_before.date(),
            dry_run=dry_run,
            registry=SYMBOL_DATASETS,
            gate_table="discovery_asof_state",
            scope_column="query_term",
            collector=collector,
        )
        report.screens = prune_screens(session, asof_before.date(), dry_run=dry_run)
    # Orphan report cleanup must come after as-of pruning, since that's the
    # step that removes the link.
    if orphan_reports:
        report.orphan_reports = prune_orphan_reports(
            session, dry_run=dry_run, collector=collector
        )
    if not dry_run:
        session.commit()
    return report

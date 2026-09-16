"""The exporter's queries against a real database.

`PREPARE` puts every statement through the planner, which is where a renamed
column, a missing enum literal or an unresolvable cast shows up. The freshness
arithmetic cases can only be expressed as rows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.config import Settings
from yfin.models import ItemStatus, RunScope, RunStatus, SyncRun, SyncRunItem
from yfin.scheduler import queries as q
from yfin.scheduler.queries import Context

pytestmark = pytest.mark.repo

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
HOUR = 3600.0

#: `sync` every day, so a symbol cell is stale two days after its last good
#: run at the default factor of 2.
INTERVALS = {"sync": 24 * HOUR, "market": HOUR, "domain": 7 * 24 * HOUR}


@pytest.fixture
def factory(db_session: Session) -> sessionmaker[Session]:
    return sessionmaker(
        bind=db_session.connection(),
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest.fixture
def ctx(factory: sessionmaker[Session]) -> Context:
    return Context(
        session_factory=factory, settings=Settings(), intervals=INTERVALS, now=NOW
    )


class _Runs:
    """Writes runs and items straight to the tables, bypassing `pipeline/audit.py`.

    Going through the writer would make every case depend on the writer
    agreeing to produce it; some of these rows it never would.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def run(
        self,
        *,
        scope: RunScope = RunScope.SYMBOLS,
        ago_hours: float = 0,
        status: RunStatus = RunStatus.OK,
        finished: bool = True,
        job_run_id: int | None = None,
        **rows: int,
    ) -> int:
        started = NOW - timedelta(hours=ago_hours)
        run = SyncRun(
            started_at=started,
            scope=scope,
            status=status,
            finished_at=started + timedelta(minutes=5) if finished else None,
            job_run_id=job_run_id,
            **rows,
        )
        self._session.add(run)
        self._session.flush()
        return int(run.id)

    def item(
        self,
        run_id: int,
        symbol: str,
        dataset: str,
        status: ItemStatus,
        *,
        region: str | None = None,
        table_name: str | None = None,
        error_kind: str | None = None,
    ) -> None:
        self._session.add(
            SyncRunItem(
                run_id=run_id,
                symbol=symbol,
                dataset=dataset,
                status=status,
                region=region,
                table_name=table_name,
                error_kind=error_kind,
            )
        )
        self._session.flush()


@pytest.fixture
def rows(db_session: Session) -> _Runs:
    return _Runs(db_session)


def _cells(ctx: Context) -> dict[tuple[str, str], tuple[float, float]]:
    """`(scope, dataset) -> (total, stale)`."""
    out: dict[tuple[str, str], list[float]] = {}
    for sample in q.freshness(ctx):
        key = (sample.labels["scope"], sample.labels["dataset"])
        slot = out.setdefault(key, [0.0, 0.0])
        slot[0 if sample.name == "yfin_cells_total" else 1] = sample.value
    return {key: (value[0], value[1]) for key, value in out.items()}


def _find(samples: list[q.Sample], name: str, **labels: str) -> float | None:
    """The one sample with this name and exactly these labels, or None.

    Label ORDER is not part of the answer, so an assertion names the labels
    it means instead of depending on how the query happened to emit them.
    """
    for sample in samples:
        if sample.name == name and sample.labels == labels:
            return sample.value
    return None


class TestEveryStatementIsValid:
    """`PREPARE` runs the parser AND the planner over the real schema."""

    def test_they_all_prepare(self, db_session: Session) -> None:
        for index, (_name, sql) in enumerate(q.statements()):
            # `text()` renders `:name` markers; PostgreSQL wants `$n`, and
            # the types have to be inferrable, which every statement here
            # makes explicit with a cast for exactly this reason.
            prepared = sql
            for number, param in enumerate(_params(sql), start=1):
                prepared = prepared.replace(f":{param}", f"${number}")
            db_session.execute(text(f"PREPARE stmt_{index} AS {prepared}"))
            db_session.execute(text(f"DEALLOCATE stmt_{index}"))


def _params(sql: str) -> list[str]:
    """Bind parameter names in first-appearance order."""
    ordered: list[str] = []
    for name in text(sql).compile().params:
        if name not in ordered:
            ordered.append(name)
    # `text()` returns them sorted; PostgreSQL numbers by first use, so
    # re-order against the statement itself.
    return sorted(ordered, key=sql.index)


class TestEveryQueryRunsAgainstAnEmptyDatabase:
    """The first refresh after a fresh install must not be the one that
    discovers a query cannot execute."""

    @pytest.mark.parametrize("query", q.QUERIES, ids=lambda query: query.name)
    def test_it_returns_rows_or_nothing(self, query: q.Query, ctx: Context) -> None:
        assert isinstance(query.run(ctx), list)


class TestFreshness:
    def test_a_cell_written_today_is_not_stale(self, ctx: Context, rows: _Runs) -> None:
        run = rows.run(ago_hours=1)
        rows.item(run, "AAPL", "info", ItemStatus.OK)
        assert _cells(ctx)[("symbols", "info")] == (1.0, 0.0)

    def test_a_cell_past_the_factor_is_stale(self, ctx: Context, rows: _Runs) -> None:
        """Daily job, factor 2: three days is past two."""
        run = rows.run(ago_hours=72)
        rows.item(run, "AAPL", "info", ItemStatus.OK)
        assert _cells(ctx)[("symbols", "info")] == (1.0, 1.0)

    def test_a_skip_counts_as_a_write(self, ctx: Context, rows: _Runs) -> None:
        """A content-hash skip IS a verification. Counted as a gap, every
        dataset that rarely changes would be permanently stale."""
        run = rows.run(ago_hours=1)
        rows.item(run, "AAPL", "financials", ItemStatus.SKIPPED)
        assert _cells(ctx)[("symbols", "financials")] == (1.0, 0.0)

    def test_a_failed_run_does_not_refresh_the_cell(
        self, ctx: Context, rows: _Runs
    ) -> None:
        """Staleness is measured from the last GOOD run, not the last
        attempt -- otherwise a job failing every night would look fresh
        forever, which is the exact failure this gauge exists to catch."""
        old = rows.run(ago_hours=72)
        rows.item(old, "AAPL", "info", ItemStatus.OK)
        recent = rows.run(ago_hours=1)
        rows.item(recent, "AAPL", "info", ItemStatus.FAILED)
        assert _cells(ctx)[("symbols", "info")] == (1.0, 1.0)

    def test_a_cell_that_never_succeeded_is_stale(
        self, ctx: Context, rows: _Runs
    ) -> None:
        run = rows.run(ago_hours=1)
        rows.item(run, "AAPL", "info", ItemStatus.FAILED)
        assert _cells(ctx)[("symbols", "info")] == (1.0, 1.0)

    def test_one_failed_table_makes_the_whole_cell_fail(
        self, ctx: Context, rows: _Runs
    ) -> None:
        """A multi-table dataset writes one item PER TABLE. Reduced to the
        worst status per run, or a dataset that half-wrote would report as
        verified."""
        good = rows.run(ago_hours=72)
        for table in ("income_statement", "balance_sheet"):
            rows.item(good, "AAPL", "financials", ItemStatus.OK, table_name=table)
        mixed = rows.run(ago_hours=1)
        rows.item(mixed, "AAPL", "financials", ItemStatus.OK, table_name="income_statement")
        rows.item(mixed, "AAPL", "financials", ItemStatus.FAILED, table_name="balance_sheet")
        assert _cells(ctx)[("symbols", "financials")] == (1.0, 1.0)

    def test_a_multi_table_cell_is_counted_once(self, ctx: Context, rows: _Runs) -> None:
        """Three tables, one cell. Counting items instead would multiply the
        denominator by however many tables a dataset happens to write."""
        run = rows.run(ago_hours=1)
        for table in ("a", "b", "c"):
            rows.item(run, "AAPL", "financials", ItemStatus.OK, table_name=table)
        assert _cells(ctx)[("symbols", "financials")] == (1.0, 0.0)


class TestTheUniverse:
    def test_out_of_scope_leaves_it(self, ctx: Context, rows: _Runs) -> None:
        """Bar datasets for symbols outside `intraday_scope` are not gaps --
        deciding not to fetch them is the design working."""
        run = rows.run(ago_hours=1)
        rows.item(run, "AAPL", "bars_1m", ItemStatus.OUT_OF_SCOPE)
        assert ("symbols", "bars_1m") not in _cells(ctx)

    def test_an_unknown_symbol_leaves_it(self, ctx: Context, rows: _Runs) -> None:
        run = rows.run(ago_hours=1)
        rows.item(run, "NOPE", "info", ItemStatus.UNKNOWN_SYMBOL)
        assert ("symbols", "info") not in _cells(ctx)

    def test_not_attempted_stays_in_it_and_is_not_good(
        self, ctx: Context, rows: _Runs
    ) -> None:
        """A shard that was pulled must show as a gap. Treated like
        out-of-scope it would VANISH, and the dashboard would improve as
        the pipeline broke."""
        run = rows.run(ago_hours=1)
        rows.item(run, "AAPL", "info", ItemStatus.NOT_ATTEMPTED)
        assert _cells(ctx)[("symbols", "info")] == (1.0, 1.0)

    def test_a_cell_that_left_scope_after_a_write_is_gone(
        self, ctx: Context, rows: _Runs
    ) -> None:
        """The LATEST status decides, not the history: a symbol removed from
        `intraday_scope` must stop counting against freshness."""
        old = rows.run(ago_hours=72)
        rows.item(old, "AAPL", "bars_1m", ItemStatus.OK)
        new = rows.run(ago_hours=1)
        rows.item(new, "AAPL", "bars_1m", ItemStatus.OUT_OF_SCOPE)
        assert ("symbols", "bars_1m") not in _cells(ctx)

    def test_one_written_table_keeps_a_partly_out_of_scope_cell(
        self, ctx: Context, rows: _Runs
    ) -> None:
        run = rows.run(ago_hours=1)
        rows.item(run, "AAPL", "bars_1m", ItemStatus.OK, table_name="price_bars")
        rows.item(run, "AAPL", "bars_1m", ItemStatus.OUT_OF_SCOPE, table_name="bar_gaps")
        assert _cells(ctx)[("symbols", "bars_1m")] == (1.0, 0.0)

    def test_an_opt_in_dataset_nobody_ran_is_absent(
        self, ctx: Context, rows: _Runs
    ) -> None:
        """A dataset with no items has no cells, so it cannot be stale.
        Enumerating the registry instead would report every opt-in dataset
        as a gap on every install."""
        run = rows.run(ago_hours=1)
        rows.item(run, "AAPL", "info", ItemStatus.OK)
        assert set(_cells(ctx)) == {("symbols", "info")}


class TestScopes:
    def test_market_cells_use_the_market_cadence(
        self, ctx: Context, rows: _Runs
    ) -> None:
        """Hourly job, factor 2: three hours is stale, one is not. The same
        row age would be perfectly fresh for the daily symbol job."""
        stale = rows.run(scope=RunScope.MARKET, ago_hours=3)
        rows.item(stale, "market", "market_status", ItemStatus.OK)
        fresh = rows.run(scope=RunScope.MARKET, ago_hours=0.5)
        rows.item(fresh, "market", "market_summary", ItemStatus.OK)
        cells = _cells(ctx)
        assert cells[("market", "market_status")] == (1.0, 1.0)
        assert cells[("market", "market_summary")] == (1.0, 0.0)

    def test_a_domain_cell_exists_once_per_region(
        self, ctx: Context, rows: _Runs
    ) -> None:
        """`region` is part of the cell. Left out, two regions of one
        taxonomy key would collapse into a single cell and one of them could
        go stale unseen."""
        run = rows.run(scope=RunScope.DOMAIN, ago_hours=1)
        rows.item(run, "^YH311", "sector_profile", ItemStatus.OK, region="US")
        rows.item(run, "^YH311", "sector_profile", ItemStatus.OK, region="TR")
        assert _cells(ctx)[("domain", "sector_profile")] == (2.0, 0.0)

    def test_one_region_can_be_stale_alone(self, ctx: Context, rows: _Runs) -> None:
        old = rows.run(scope=RunScope.DOMAIN, ago_hours=24 * 30)
        rows.item(old, "^YH311", "sector_profile", ItemStatus.OK, region="TR")
        new = rows.run(scope=RunScope.DOMAIN, ago_hours=1)
        rows.item(new, "^YH311", "sector_profile", ItemStatus.OK, region="US")
        assert _cells(ctx)[("domain", "sector_profile")] == (2.0, 1.0)

    def test_a_symbol_cell_with_a_null_region_still_joins(
        self, ctx: Context, rows: _Runs
    ) -> None:
        """`region` is NULL for every symbol cell. A plain `=` in the join
        back to the last good run matches none of them, and the whole
        universe would report stale."""
        run = rows.run(ago_hours=1)
        rows.item(run, "AAPL", "info", ItemStatus.OK, region=None)
        assert _cells(ctx)[("symbols", "info")] == (1.0, 0.0)

    def test_an_unscheduled_job_produces_no_staleness(
        self, factory: sessionmaker[Session], rows: _Runs
    ) -> None:
        """0 means "cannot judge". Dividing by the cadence of a job nobody
        runs would report the entire universe stale the moment an operator
        cleared a cron."""
        ctx = Context(
            session_factory=factory, settings=Settings(), intervals={}, now=NOW
        )
        run = rows.run(ago_hours=24 * 365)
        rows.item(run, "AAPL", "info", ItemStatus.OK)
        assert _cells(ctx)[("symbols", "info")] == (1.0, 0.0)

    def test_the_factor_moves_the_threshold(
        self, factory: sessionmaker[Session], rows: _Runs
    ) -> None:
        """Same row, a factor an operator raised: an installation that
        accepts a wider gap must stop reporting it. The default factor of 2
        over these same 72 hours is already
        `TestFreshness::test_a_cell_past_the_factor_is_stale`, so only the
        raised factor is asserted here."""
        run = rows.run(ago_hours=72)
        rows.item(run, "AAPL", "info", ItemStatus.OK)
        lax = Context(
            session_factory=factory,
            settings=Settings(yf_freshness_factor=10),
            intervals=INTERVALS,
            now=NOW,
        )
        assert _cells(lax)[("symbols", "info")] == (1.0, 0.0)


class TestAudit:
    def _by(self, ctx: Context) -> list[q.Sample]:
        return q.audit(ctx)

    def test_a_scheduled_run_and_a_manual_one_are_separate(
        self, ctx: Context, rows: _Runs
    ) -> None:
        """"Did last night's job work" is about the scheduled run; a manual
        backfill must not overwrite that answer."""
        scheduled = rows.run(ago_hours=10, job_run_id=1, rows_written=100)
        rows.item(scheduled, "AAPL", "info", ItemStatus.OK)
        manual = rows.run(ago_hours=1, rows_written=5)
        rows.item(manual, "MSFT", "info", ItemStatus.FAILED, error_kind="rate_limited")
        found = self._by(ctx)
        assert _find(found, "yfin_audit_rows", scope="symbols", kind="scheduled",
            measure="written") == 100.0
        assert _find(found, "yfin_audit_rows", scope="symbols", kind="manual",
            measure="written") == 5.0
        assert _find(found, "yfin_audit_items", scope="symbols", kind="scheduled",
            status="ok") == 1.0
        assert _find(found, "yfin_audit_items", scope="symbols", kind="manual",
            status="failed") == 1.0

    def test_errors_are_grouped_by_the_kind(self, ctx: Context, rows: _Runs) -> None:
        run = rows.run(ago_hours=1, status=RunStatus.PARTIAL)
        rows.item(run, "A", "info", ItemStatus.FAILED, error_kind="rate_limited")
        rows.item(run, "B", "info", ItemStatus.FAILED, error_kind="rate_limited")
        rows.item(run, "C", "info", ItemStatus.FAILED, error_kind="network")
        found = self._by(ctx)
        assert _find(found, "yfin_audit_errors", scope="symbols", kind="manual",
            error_kind="rate_limited") == 2.0
        assert _find(found, "yfin_audit_errors", scope="symbols", kind="manual",
            error_kind="network") == 1.0

    def test_a_failure_with_no_kind_is_reported_as_unknown(
        self, ctx: Context, rows: _Runs
    ) -> None:
        """Dropping it would make the error count disagree with the item
        count, and a failure nobody classified is still a failure."""
        run = rows.run(ago_hours=1)
        rows.item(run, "A", "info", ItemStatus.FAILED)
        assert _find(self._by(ctx), "yfin_audit_errors", scope="symbols", kind="manual",
            error_kind="unknown") == 1.0

    def test_a_finished_run_gets_a_verdict(self, ctx: Context, rows: _Runs) -> None:
        rows.run(ago_hours=1, status=RunStatus.PARTIAL)
        assert _find(self._by(ctx), "yfin_audit_status", scope="symbols", kind="manual") == 1.0

    def test_a_running_run_has_no_verdict(self, ctx: Context, rows: _Runs) -> None:
        """`running` is not a grade. Published as one, every long sync would
        look like a new failure mode."""
        rows.run(ago_hours=1, status=RunStatus.RUNNING, finished=False)
        found = self._by(ctx)
        assert _find(found, "yfin_audit_status", scope="symbols", kind="manual") is None
        assert _find(found, "yfin_audit_running", scope="symbols") == 1.0

    def test_nothing_running_reports_zero_rather_than_nothing(
        self, ctx: Context, rows: _Runs
    ) -> None:
        """A gauge that disappears when the answer is "no" cannot be graphed
        against one that appears when it is "yes"."""
        rows.run(ago_hours=1, status=RunStatus.OK)
        assert _find(self._by(ctx), "yfin_audit_running", scope="symbols") == 0.0

    def test_a_manual_run_in_flight_shows_while_the_scheduled_one_is_done(
        self, ctx: Context, rows: _Runs
    ) -> None:
        rows.run(ago_hours=10, job_run_id=1, status=RunStatus.OK)
        rows.run(ago_hours=1, status=RunStatus.RUNNING, finished=False)
        assert _find(self._by(ctx), "yfin_audit_running", scope="symbols") == 1.0

    def test_only_the_latest_run_is_reported(self, ctx: Context, rows: _Runs) -> None:
        """A gauge is a value now, not a history. Summing every run would
        make the number grow forever and mean nothing."""
        first = rows.run(ago_hours=48, rows_written=1)
        rows.item(first, "AAPL", "info", ItemStatus.OK)
        second = rows.run(ago_hours=1, rows_written=2)
        rows.item(second, "AAPL", "info", ItemStatus.OK)
        assert _find(self._by(ctx), "yfin_audit_rows", scope="symbols", kind="manual",
            measure="written") == 2.0

    def test_every_scope_is_reported_on_its_own(
        self, ctx: Context, rows: _Runs
    ) -> None:
        rows.run(scope=RunScope.MARKET, ago_hours=1, rows_written=7)
        rows.run(scope=RunScope.DOMAIN, ago_hours=1, rows_written=9)
        found = self._by(ctx)
        assert _find(found, "yfin_audit_rows", scope="market", kind="manual",
            measure="written") == 7.0
        assert _find(found, "yfin_audit_rows", scope="domain", kind="manual",
            measure="written") == 9.0


class TestTheRepublishedSyncCounters:
    def test_a_run_s_shards_are_summed(
        self, ctx: Context, rows: _Runs, db_session: Session
    ) -> None:
        """Shards are separate `spawn` processes, so the run's real count
        exists nowhere until they are added up here."""
        from yfin.models.ops import RunMetric

        run = rows.run(ago_hours=1)
        for shard in (0, 1):
            db_session.add(
                RunMetric(
                    run_id=run,
                    shard_index=shard,
                    name="yfin_sync_retries_total",
                    labels='{"kind":"rate_limited"}',
                    value=3,
                )
            )
        db_session.flush()
        samples = {(s.name, s.labels["kind"]): s.value for s in q.sync_counters(ctx)}
        assert samples[("yfin_sync_retries", "rate_limited")] == 6.0

    def test_an_undeclared_row_is_skipped(
        self, ctx: Context, rows: _Runs, db_session: Session
    ) -> None:
        """`run_metrics` keeps history. A counter that was retired still has
        rows, and publishing them would register a series whose label set no
        longer matches its declaration."""
        from yfin.models.ops import RunMetric

        run = rows.run(ago_hours=1)
        db_session.add(
            RunMetric(
                run_id=run,
                shard_index=0,
                name="yfin_sync_retired_total",
                labels="{}",
                value=1,
            )
        )
        db_session.flush()
        assert q.sync_counters(ctx) == []

    def test_a_row_whose_labels_no_longer_match_is_skipped(
        self, ctx: Context, rows: _Runs, db_session: Session
    ) -> None:
        from yfin.models.ops import RunMetric

        run = rows.run(ago_hours=1)
        db_session.add(
            RunMetric(
                run_id=run,
                shard_index=0,
                name="yfin_sync_retries_total",
                labels='{"gone":"x"}',
                value=1,
            )
        )
        db_session.flush()
        assert q.sync_counters(ctx) == []

    def test_only_the_latest_run_per_scope(
        self, ctx: Context, rows: _Runs, db_session: Session
    ) -> None:
        from yfin.models.ops import RunMetric

        old = rows.run(ago_hours=48)
        new = rows.run(ago_hours=1)
        for run_id, value in ((old, 100), (new, 1)):
            db_session.add(
                RunMetric(
                    run_id=run_id,
                    shard_index=0,
                    name="yfin_sync_retries_total",
                    labels='{"kind":"network"}',
                    value=value,
                )
            )
        db_session.flush()
        assert [s.value for s in q.sync_counters(ctx)] == [1.0]


class TestIntradayScope:
    """Not the same question as freshness: a cell fresh by the schedule is still
    unrecoverable once the newest bar is older than Yahoo's retention depth.
    """

    def _scope(self, session: Session, symbol: str, interval: str) -> None:
        from yfin.models import IntradayScope, Symbol

        session.add(Symbol(symbol=symbol, is_active=True))
        # Flushed on its own: the FK is checked at the INSERT, and
        # SQLAlchemy has no dependency between two `add`s in one flush.
        session.flush()
        session.add(
            IntradayScope(
                symbol=symbol, bar_interval=interval, enabled=True, added_at=NOW
            )
        )
        session.flush()

    def _bar(self, session: Session, symbol: str, interval: str, days_ago: float) -> None:
        from decimal import Decimal

        from yfin.models import PriceBar

        when = NOW - timedelta(days=days_ago)
        session.add(
            PriceBar(
                symbol=symbol,
                bar_interval=interval,
                ts_utc=when,
                local_date=when.date(),
                close=Decimal("1.0"),
            )
        )
        session.flush()

    def _stale(self, ctx: Context) -> dict[str, float]:
        return {
            sample.labels["interval"]: sample.value
            for sample in q.intraday_scope_stale(ctx)
        }

    def test_a_recent_bar_is_not_a_warning(
        self, ctx: Context, db_session: Session
    ) -> None:
        self._scope(db_session, "AAPL", "1m")
        self._bar(db_session, "AAPL", "1m", days_ago=1)
        assert self._stale(ctx)["1m"] == 0.0

    def test_a_bar_near_the_retention_edge_is(
        self, ctx: Context, db_session: Session
    ) -> None:
        """1m reaches back 29 days; the default 3-day margin makes 26 the
        line, and 27 days of silence is past it."""
        self._scope(db_session, "AAPL", "1m")
        self._bar(db_session, "AAPL", "1m", days_ago=27)
        assert self._stale(ctx)["1m"] == 1.0

    def test_a_symbol_with_no_bars_at_all_counts(
        self, ctx: Context, db_session: Session
    ) -> None:
        """It is in scope and has nothing. That is the worst case, not an
        absent one."""
        self._scope(db_session, "AAPL", "1m")
        assert self._stale(ctx)["1m"] == 1.0

    def test_each_interval_has_its_own_edge(
        self, ctx: Context, db_session: Session
    ) -> None:
        """60m reaches back 729 days, so 40 days of silence is nothing there
        and long past the line for 1m."""
        self._scope(db_session, "AAPL", "1m")
        self._scope(db_session, "MSFT", "60m")
        self._bar(db_session, "AAPL", "1m", days_ago=40)
        self._bar(db_session, "MSFT", "60m", days_ago=40)
        found = self._stale(ctx)
        assert found["1m"] == 1.0
        assert found["60m"] == 0.0

    def test_a_disabled_scope_row_is_not_counted(
        self, ctx: Context, db_session: Session
    ) -> None:
        from yfin.models import IntradayScope, Symbol

        db_session.add(Symbol(symbol="AAPL", is_active=True))
        db_session.flush()
        db_session.add(
            IntradayScope(
                symbol="AAPL", bar_interval="1m", enabled=False, added_at=NOW
            )
        )
        db_session.flush()
        assert self._stale(ctx) == {}


class TestBarGaps:
    def _gap(
        self,
        session: Session,
        *,
        interval: str = "1m",
        days_ago: float = 1,
        reason: str = "fetch_failed",
        resolved_by: str | None = None,
    ) -> None:
        from yfin.models import BarGap, Symbol

        if session.get(Symbol, "AAPL") is None:
            session.add(Symbol(symbol="AAPL", is_active=True))
            session.flush()
        start = NOW - timedelta(days=days_ago)
        session.add(
            BarGap(
                symbol="AAPL",
                bar_interval=interval,
                gap_start_utc=start,
                gap_end_utc=start + timedelta(minutes=1),
                detected_at=start,
                reason=reason,
                resolved_at=NOW if resolved_by else None,
                resolved_by=resolved_by,
            )
        )
        session.flush()

    def _by(self, ctx: Context) -> dict[tuple[str, ...], float]:
        return {
            (sample.name, *(v for _, v in sorted(sample.labels.items()))): sample.value
            for sample in q.bars(ctx)
        }

    def test_an_open_gap_is_counted_by_reason(
        self, ctx: Context, db_session: Session
    ) -> None:
        self._gap(db_session, reason="fetch_failed")
        assert self._by(ctx)[("yfin_bar_gaps_open", "1m", "fetch_failed")] == 1.0

    def test_a_closed_gap_is_not_open(self, ctx: Context, db_session: Session) -> None:
        self._gap(db_session, resolved_by="reconcile")
        found = self._by(ctx)
        assert ("yfin_bar_gaps_open", "1m", "fetch_failed") not in found
        assert found[("yfin_bar_gaps_resolved", "1m", "reconcile")] == 1.0

    def test_the_oldest_open_gap_sets_the_age(
        self, ctx: Context, db_session: Session
    ) -> None:
        self._gap(db_session, days_ago=1)
        self._gap(db_session, days_ago=5)
        age = self._by(ctx)[("yfin_bar_gaps_oldest_age_seconds", "1m")]
        assert age == pytest.approx(5 * 24 * 3600, rel=1e-6)

    def test_a_gap_near_the_edge_is_expiring(
        self, ctx: Context, db_session: Session
    ) -> None:
        """After Yahoo's edge only `stream reconcile` can still close a 1m
        gap, which is why the warning has to arrive before it."""
        self._gap(db_session, days_ago=27)
        assert self._by(ctx)[("yfin_bar_gaps_expiring", "1m")] == 1.0

    def test_a_fresh_gap_is_not_expiring(
        self, ctx: Context, db_session: Session
    ) -> None:
        self._gap(db_session, days_ago=1)
        assert ("yfin_bar_gaps_expiring", "1m") not in self._by(ctx)

    def test_pending_rescales_are_always_reported(
        self, ctx: Context, db_session: Session
    ) -> None:
        """Unlabelled, so the panel exists at zero rather than appearing
        only once something is wrong."""
        assert self._by(ctx)[("yfin_bar_rescales_pending",)] == 0.0

    def test_a_split_after_the_archive_starts_is_pending(
        self, ctx: Context, db_session: Session
    ) -> None:
        from datetime import date
        from decimal import Decimal

        from yfin.models import PriceBar, Split, Symbol

        db_session.add(Symbol(symbol="AAPL", is_active=True))
        db_session.flush()
        db_session.add(
            PriceBar(
                symbol="AAPL",
                bar_interval="1m",
                ts_utc=NOW - timedelta(days=10),
                local_date=date(2026, 8, 28),
                close=Decimal("1.0"),
            )
        )
        db_session.add(
            Split(symbol="AAPL", split_date=date(2026, 9, 1), ratio=Decimal("2"))
        )
        db_session.flush()
        assert self._by(ctx)[("yfin_bar_rescales_pending",)] == 1.0

    def test_a_split_predating_the_archive_is_not(
        self, ctx: Context, db_session: Session
    ) -> None:
        """Those bars already arrived at the post-split scale. Reporting
        them would make a fresh install -- where `rescale --seed` was
        skipped -- look like it had years of unapplied work."""
        from datetime import date
        from decimal import Decimal

        from yfin.models import PriceBar, Split, Symbol

        db_session.add(Symbol(symbol="AAPL", is_active=True))
        db_session.flush()
        db_session.add(
            PriceBar(
                symbol="AAPL",
                bar_interval="1m",
                ts_utc=NOW,
                local_date=date(2026, 9, 7),
                close=Decimal("1.0"),
            )
        )
        db_session.add(
            Split(symbol="AAPL", split_date=date(2020, 8, 31), ratio=Decimal("4"))
        )
        db_session.flush()
        assert self._by(ctx)[("yfin_bar_rescales_pending",)] == 0.0


class TestStream:
    def _session_row(self, session: Session, *, finished: bool) -> int:
        from yfin.models import StreamSession, StreamStatus

        row = StreamSession(
            started_at=NOW - timedelta(hours=1),
            finished_at=NOW if finished else None,
            status=StreamStatus.OK if finished else StreamStatus.RUNNING,
            messages_received=1_000,
            rows_rejected=7,
        )
        session.add(row)
        session.flush()
        return int(row.id)

    def _health(
        self,
        session: Session,
        session_id: int,
        key: str,
        *,
        state: str = "open",
        heartbeat_minutes: float = 1,
        canary_minutes: float = 2,
        subscribed: int = 100,
        reconnects: int = 3,
    ) -> None:
        from yfin.models import StreamConnectionHealth

        session.add(
            StreamConnectionHealth(
                connection_key=key,
                session_id=session_id,
                state=state,
                subscribed_count=subscribed,
                heartbeat_at=NOW - timedelta(minutes=heartbeat_minutes),
                last_canary_at=NOW - timedelta(minutes=canary_minutes),
                reconnect_count=reconnects,
            )
        )
        session.flush()

    def _by(self, ctx: Context) -> dict[tuple[str, ...], float]:
        return {
            (sample.name, *(v for _, v in sorted(sample.labels.items()))): sample.value
            for sample in q.stream(ctx)
        }

    def test_an_empty_table_is_zeroes_not_nulls(
        self, ctx: Context
    ) -> None:
        """A NULL would reach `float()` and blow up the whole query, which
        is exactly the pass a fresh install makes first."""
        found = self._by(ctx)
        assert found[("yfin_stream_heartbeat_age_seconds",)] == 0.0
        assert found[("yfin_stream_subscribed_symbols",)] == 0.0

    def test_the_oldest_heartbeat_wins(self, ctx: Context, db_session: Session) -> None:
        """One silent connection IS the failure; averaging would hide it
        behind the ones still working."""
        sid = self._session_row(db_session, finished=False)
        self._health(db_session, sid, "NMS#0", heartbeat_minutes=1)
        self._health(db_session, sid, "NMS#1", heartbeat_minutes=30)
        found = self._by(ctx)
        assert found[("yfin_stream_heartbeat_age_seconds",)] == pytest.approx(1800, rel=1e-6)

    def test_subscriptions_and_reconnects_are_summed(
        self, ctx: Context, db_session: Session
    ) -> None:
        sid = self._session_row(db_session, finished=False)
        self._health(db_session, sid, "NMS#0", subscribed=100, reconnects=2)
        self._health(db_session, sid, "NMS#1", subscribed=80, reconnects=1)
        found = self._by(ctx)
        assert found[("yfin_stream_subscribed_symbols",)] == 180.0
        assert found[("yfin_stream_reconnects",)] == 3.0

    def test_connections_are_counted_by_state(
        self, ctx: Context, db_session: Session
    ) -> None:
        sid = self._session_row(db_session, finished=False)
        self._health(db_session, sid, "NMS#0", state="open")
        self._health(db_session, sid, "NYQ#0", state="closed")
        found = self._by(ctx)
        assert found[("yfin_stream_connections", "open")] == 1.0
        assert found[("yfin_stream_connections", "closed")] == 1.0

    def test_the_open_session_supplies_the_totals(
        self, ctx: Context, db_session: Session
    ) -> None:
        self._session_row(db_session, finished=False)
        found = self._by(ctx)
        assert found[("yfin_stream_messages",)] == 1000.0
        assert found[("yfin_stream_rejects",)] == 7.0

    def test_a_finished_session_supplies_none(
        self, ctx: Context, db_session: Session
    ) -> None:
        """Its totals are history. On a gauge they would freeze at the last
        value and read as a stream that is still running."""
        self._session_row(db_session, finished=True)
        assert ("yfin_stream_messages",) not in self._by(ctx)


class TestOutboxes:
    def test_both_relays_are_reported(self, ctx: Context) -> None:
        """The changes design generalised `relay_lag(spec)`, so the exporter
        covers `pipeline_outbox` as well as `stream_outbox`."""
        found = {
            (sample.name, sample.labels["outbox"]) for sample in q.outboxes(ctx)
        }
        assert ("yfin_outbox_unpublished_rows", "stream_outbox") in found
        assert ("yfin_outbox_unpublished_rows", "pipeline_outbox") in found
        assert ("yfin_outbox_oldest_age_seconds", "pipeline_outbox") in found


class TestApiUsage:
    def _rows(self, session: Session) -> None:
        from datetime import date

        from yfin.api.models.clients import ApiClient
        from yfin.api.models.plans import ApiPlan, ApiUsageDaily

        session.add(
            ApiPlan(
                plan="free",
                requests_per_second=2,
                burst=10,
                monthly_quota=50_000,
                max_page_size=100,
                max_concurrency=2,
            )
        )
        session.flush()
        session.add(
            ApiClient(
                client_id="c1", name="test", owner_email="test-owner", plan="free"
            )
        )
        session.flush()
        session.add(
            ApiUsageDaily(
                client_id="c1", day=date(2026, 9, 5), endpoint_family="reference", request_count=10
            )
        )
        session.add(
            ApiUsageDaily(
                client_id="c1",
                day=date(2026, 9, 6),
                endpoint_family="reference",
                request_count=25,
                estimated=True,
            )
        )
        session.flush()

    def test_only_the_most_recent_flushed_day(
        self, ctx: Context, db_session: Session
    ) -> None:
        """Today's counts are still in Redis. A gauge that was half a
        flushed day and half a live one would mean nothing at either end."""
        self._rows(db_session)
        found = {
            (s.name, s.labels.get("family")): s.value for s in q.api_usage(ctx)
        }
        assert found[("yfin_api_usage_requests", "reference")] == 25.0

    def test_the_day_it_is_about_is_published(
        self, ctx: Context, db_session: Session
    ) -> None:
        """Without it the number is a count with no period attached."""
        from datetime import date

        self._rows(db_session)
        found = {s.name: s.value for s in q.api_usage(ctx)}
        assert found["yfin_api_usage_day_timestamp"] > 0
        assert date.fromtimestamp(found["yfin_api_usage_day_timestamp"]).year == 2026

    def test_reconstructed_days_are_counted(
        self, ctx: Context, db_session: Session
    ) -> None:
        """Billing has to be able to tell a measured row from one the
        counters could not measure."""
        self._rows(db_session)
        found = {s.name: s.value for s in q.api_usage(ctx)}
        assert found["yfin_api_usage_estimated_days"] == 1.0


class TestOnlyTheOpenSessionIsHealth:
    """`stream_connection_health` is current state only, but rows of finished
    sessions are never deleted, so an unfiltered read would report a stale
    canary age from a dead session and keep `StreamStale` firing.
    """

    def _health(
        self,
        session: Session,
        session_id: int,
        key: str,
        *,
        minutes_old: float,
        subscribed: int = 96,
    ) -> None:
        from yfin.models import StreamConnectionHealth

        session.add(
            StreamConnectionHealth(
                connection_key=key,
                session_id=session_id,
                state="open",
                subscribed_count=subscribed,
                heartbeat_at=NOW - timedelta(minutes=minutes_old),
                last_canary_at=NOW - timedelta(minutes=minutes_old),
                reconnect_count=0,
            )
        )
        session.flush()

    def _session(self, session: Session, *, finished: bool) -> int:
        from yfin.models import StreamSession, StreamStatus

        row = StreamSession(
            started_at=NOW - timedelta(hours=14),
            finished_at=NOW - timedelta(hours=13) if finished else None,
            status=StreamStatus.OK if finished else StreamStatus.RUNNING,
            messages_received=0,
            rows_rejected=0,
        )
        session.add(row)
        session.flush()
        return int(row.id)

    @pytest.fixture
    def mixed(self, db_session: Session) -> None:
        """One row from a dead session, two from the live one."""
        dead = self._session(db_session, finished=True)
        self._health(db_session, dead, "OLD#0", minutes_old=810, subscribed=2)
        live = self._session(db_session, finished=False)
        self._health(db_session, live, "NMS#0", minutes_old=0.1)
        self._health(db_session, live, "NMS#1", minutes_old=0.1)

    def test_a_dead_session_does_not_age_the_canary(
        self, ctx: Context, mixed: None
    ) -> None:
        age = _find(q.stream(ctx), "yfin_stream_canary_age_seconds")
        assert age is not None
        assert age < 60, "a finished session's row is still being counted"

    def test_a_dead_session_does_not_age_the_heartbeat(
        self, ctx: Context, mixed: None
    ) -> None:
        age = _find(q.stream(ctx), "yfin_stream_heartbeat_age_seconds")
        assert age is not None
        assert age < 60

    def test_it_counts_the_live_connections_only(
        self, ctx: Context, mixed: None
    ) -> None:
        """103 where 102 were running is a number an operator checks
        against Yahoo's per-connection quota."""
        states = {
            s.labels["state"]: s.value
            for s in q.stream(ctx)
            if s.name == "yfin_stream_connections"
        }
        assert states == {"open": 2.0}

    def test_it_sums_the_live_subscriptions_only(
        self, ctx: Context, mixed: None
    ) -> None:
        assert _find(q.stream(ctx), "yfin_stream_subscribed_symbols") == 192.0


class TestPendingRescalesIsOneGroupedPass:
    """Pending rescales must be one grouped pass, not a correlated `MIN(local_date)`
    per split; the exporter runs the `bars` query every five minutes.
    """

    def _fixture(self, session: Session, split_day: str, bar_day: str) -> None:
        from datetime import date
        from decimal import Decimal

        from yfin.models import PriceBar, Split, Symbol

        session.add(Symbol(symbol="AAPL", is_active=True))
        session.flush()
        session.add(
            PriceBar(
                symbol="AAPL",
                bar_interval="1m",
                ts_utc=NOW,
                local_date=date.fromisoformat(bar_day),
                close=Decimal("1.0"),
            )
        )
        session.add(
            Split(
                symbol="AAPL",
                split_date=date.fromisoformat(split_day),
                ratio=Decimal("2"),
            )
        )
        session.flush()

    def _pending(self, ctx: Context) -> float | None:
        return _find(q.bars(ctx), "yfin_bar_rescales_pending")

    def test_a_split_after_the_archive_starts_counts(
        self, ctx: Context, db_session: Session
    ) -> None:
        self._fixture(db_session, split_day="2026-09-01", bar_day="2026-08-01")
        assert self._pending(ctx) == 1.0

    def test_a_split_predating_the_archive_does_not(
        self, ctx: Context, db_session: Session
    ) -> None:
        """Those bars already arrived at the post-split scale."""
        self._fixture(db_session, split_day="2020-01-01", bar_day="2026-08-01")
        assert self._pending(ctx) == 0.0

    def test_a_symbol_with_no_bars_is_not_pending(
        self, ctx: Context, db_session: Session
    ) -> None:
        """The JOIN replaced a correlated subquery whose NULL result
        excluded the row; an inner join has to drop it for the same
        reason -- there is no archive to rescale."""
        from datetime import date
        from decimal import Decimal

        from yfin.models import Split, Symbol

        db_session.add(Symbol(symbol="AAPL", is_active=True))
        db_session.flush()
        db_session.add(
            Split(symbol="AAPL", split_date=date(2026, 9, 1), ratio=Decimal("2"))
        )
        db_session.flush()
        assert self._pending(ctx) == 0.0

    def test_an_applied_split_is_not_pending(
        self, ctx: Context, db_session: Session
    ) -> None:
        from datetime import date
        from decimal import Decimal

        from yfin.models import BarRescale

        self._fixture(db_session, split_day="2026-09-01", bar_day="2026-08-01")
        db_session.add(
            BarRescale(
                symbol="AAPL",
                split_date=date(2026, 9, 1),
                ratio=Decimal("2"),
                applied_at=NOW,
                rows_affected=10,
            )
        )
        db_session.flush()
        assert self._pending(ctx) == 0.0

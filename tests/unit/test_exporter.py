"""The exporter loop, and the arithmetic its queries do without a database: a failing query
blinds one gauge family only, the success timestamp is what goes stale, a label combination
disappears only when its owning query succeeded, and the SQL parses. The numbers themselves
are checked against real rows in `tests/repo/test_exporter_queries_repo.py`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from yfin.core.config import Settings
from yfin.scheduler import queries as q
from yfin.scheduler.exporter import Exporter
from yfin.scheduler.queries import Context, Query, Sample


@dataclass
class _Recorder:
    """What `set_gauge` and `clear_gauge` were asked to do."""

    sets: list[tuple[str, float, tuple[tuple[str, str], ...]]]
    cleared: list[str]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """The Prometheus side, replaced by a list. Patched in `exporter`, where the names are
    bound; the real registry would make each test depend on what the previous one left."""
    rec = _Recorder(sets=[], cleared=[])

    def set_gauge(name: str, value: float, **labels: str) -> None:
        rec.sets.append((name, value, tuple(sorted(labels.items()))))

    monkeypatch.setattr("yfin.scheduler.exporter.set_gauge", set_gauge)
    monkeypatch.setattr("yfin.scheduler.exporter.clear_gauge", rec.cleared.append)
    return rec


def _exporter(*queries: Query, job_samples: object = None) -> Exporter:
    """An exporter with no engine work to do.

    `sessionmaker(bind=None)` is never used: every query here is a stub, and
    the point is the loop around them.
    """
    return Exporter(
        engine=None,  # type: ignore[arg-type]
        settings=Settings(),
        intervals=lambda: {"sync": 3600.0},
        job_samples=job_samples,  # type: ignore[arg-type]
        queries=queries,
    )


def _names(rec: _Recorder) -> list[str]:
    return [name for name, _, _ in rec.sets]


class TestOnePass:
    def test_a_query_publishes_its_samples(self, recorder: _Recorder) -> None:
        query = Query("probe", ("yfin_proxies",), lambda _ctx: [
            Sample("yfin_proxies", 3, {"state": "healthy"})
        ])
        assert _exporter(query).refresh() == 0
        assert ("yfin_proxies", 3, (("state", "healthy"),)) in recorder.sets

    def test_it_clears_before_it_republishes(self, recorder: _Recorder) -> None:
        """A proxy that was deleted must stop being reported, or the last
        value it had would sit on the dashboard forever."""
        query = Query("probe", ("yfin_proxies",), lambda _ctx: [])
        _exporter(query).refresh()
        assert recorder.cleared == ["yfin_proxies"]

    def test_every_pass_times_itself(self, recorder: _Recorder) -> None:
        query = Query("probe", (), lambda _ctx: [])
        _exporter(query).refresh()
        assert "yfin_exporter_query_seconds" in _names(recorder)

    def test_a_clean_pass_moves_the_success_timestamp(self, recorder: _Recorder) -> None:
        _exporter(Query("probe", (), lambda _ctx: [])).refresh()
        assert "yfin_exporter_last_success_timestamp" in _names(recorder)


class TestAFailingQuery:
    @staticmethod
    def _boom(_ctx: Context) -> list[Sample]:
        raise RuntimeError("no such table")

    def test_it_does_not_clear_what_it_could_not_refresh(
        self, recorder: _Recorder
    ) -> None:
        """The previous refresh's numbers stand. A gauge dropped to zero
        would fire every alert that reads it and say nothing true."""
        _exporter(Query("probe", ("yfin_proxies",), self._boom)).refresh()
        assert recorder.cleared == []

    def test_it_does_not_stop_the_others(self, recorder: _Recorder) -> None:
        """The queries read different tables: a stream outage says nothing
        about whether the freshness numbers can be produced."""
        good = Query("good", (), lambda _ctx: [Sample("yfin_proxies", 1, {"state": "dead"})])
        assert _exporter(Query("bad", (), self._boom), good).refresh() == 1
        assert ("yfin_proxies", 1, (("state", "dead"),)) in recorder.sets

    def test_the_success_timestamp_stays_behind(self, recorder: _Recorder) -> None:
        """Which is what `ExporterStale` alerts on -- the one signal that
        says some part of this is blind."""
        _exporter(Query("bad", (), self._boom)).refresh()
        assert "yfin_exporter_last_success_timestamp" not in _names(recorder)

    def test_a_query_that_fails_is_not_timed(self, recorder: _Recorder) -> None:
        """A duration for a query that produced nothing would make the
        exporter look healthy in the one panel meant to show it is not."""
        _exporter(Query("bad", (), self._boom)).refresh()
        assert "yfin_exporter_query_seconds" not in _names(recorder)


class TestTheJobGauges:
    """The scheduler's own numbers, which are in its memory and in no table."""

    def test_they_are_published_alongside_the_queries(self, recorder: _Recorder) -> None:
        exporter = _exporter(
            job_samples=lambda: [Sample("yfin_job_running", 1, {"job_name": "sync"})]
        )
        assert exporter.refresh() == 0
        assert ("yfin_job_running", 1, (("job_name", "sync"),)) in recorder.sets

    def test_a_failure_there_is_a_failure_too(self, recorder: _Recorder) -> None:
        def boom() -> list[Sample]:
            raise RuntimeError("scheduler is shutting down")

        assert _exporter(job_samples=boom).refresh() == 1
        assert "yfin_exporter_last_success_timestamp" not in _names(recorder)


class TestTheContext:
    def test_the_cadence_is_read_fresh_every_pass(self) -> None:
        """The schedule reloads every sixty seconds. A cadence captured at
        start-up would keep judging freshness against a cron nobody runs."""
        seen = [{"sync": 60.0}, {"sync": 7200.0}]
        exporter = Exporter(
            engine=None,  # type: ignore[arg-type]
            settings=Settings(),
            intervals=lambda: seen.pop(0),
            queries=(),
        )
        assert exporter.context().intervals == {"sync": 60.0}
        assert exporter.context().intervals == {"sync": 7200.0}

    def test_staleness_is_the_factor_times_the_job_interval(self) -> None:
        ctx = Context(
            session_factory=None,  # type: ignore[arg-type]
            settings=Settings(yf_freshness_factor=3),
            intervals={"sync": 3600.0, "market": 900.0},
        )
        assert ctx.stale_after("symbols") == 10_800.0
        assert ctx.stale_after("market") == 2_700.0

    def test_an_unscheduled_job_gives_no_threshold(self) -> None:
        """0 is read as "cannot judge" by the queries. Dividing by the
        cadence of a job nobody runs would report the whole universe stale
        the moment someone cleared a cron."""
        ctx = Context(
            session_factory=None,  # type: ignore[arg-type]
            settings=Settings(),
            intervals={},
        )
        assert ctx.stale_after("domain") == 0.0

    def test_an_unknown_scope_gives_no_threshold(self) -> None:
        ctx = Context(
            session_factory=None,  # type: ignore[arg-type]
            settings=Settings(),
            intervals={"sync": 3600.0},
        )
        assert ctx.stale_after("something_new") == 0.0


class TestTheStatusRanking:
    """The universe rule: reducing a multi-table dataset's items to one status per run with
    a `MAX` is the one place ordering matters, and each case here must survive it."""

    def test_failed_beats_everything(self) -> None:
        assert q.STATUS_RANK["failed"] == max(q.STATUS_RANK.values())

    def test_a_pulled_shard_is_worse_than_any_good_status(self) -> None:
        """`not_attempted` must show as a gap. Ranked below `ok` it would
        vanish behind a sibling table that happened to be written."""
        assert q.STATUS_RANK["not_attempted"] > max(q.GOOD_RANKS)

    def test_a_pulled_shard_stays_in_the_universe(self) -> None:
        assert q.STATUS_RANK["not_attempted"] not in q.OUT_OF_UNIVERSE_RANKS

    def test_only_two_statuses_leave_the_universe(self) -> None:
        assert set(q.OUT_OF_UNIVERSE_RANKS) == {
            q.STATUS_RANK["out_of_scope"],
            q.STATUS_RANK["unknown_symbol"],
        }

    def test_out_of_scope_ranks_below_a_write(self) -> None:
        """`MAX` over the per-table ranks has to pick the write, not the skip, or a
        multi-table dataset would drop out of the universe as soon as one table went out of
        scope. Real rows: `tests/repo/test_exporter_queries_repo.py::TestTheUniverse`."""
        assert q.STATUS_RANK["out_of_scope"] < q.STATUS_RANK["ok"]

    def test_a_skip_counts_as_a_write(self) -> None:
        """A content-hash skip IS a verification: the row was fetched and
        compared. Counting it as a gap would make an unchanging dataset
        look permanently stale."""
        assert q.STATUS_RANK["skipped"] in q.GOOD_RANKS

    def test_every_item_status_is_ranked(self) -> None:
        """A status with no rank makes `MAX(CASE ...)` NULL for the whole
        cell, which silently drops it out of the universe."""
        from yfin.models.sync import ItemStatus

        assert {s.value for s in ItemStatus} == set(q.STATUS_RANK)


class TestTheStatements:
    def test_no_statement_has_an_unsubstituted_placeholder(self) -> None:
        """A leftover `{limits}` would reach PostgreSQL as a syntax error at
        the first refresh, five minutes after a deploy. Whether the SQL is
        VALID is a question only a server can answer -- that is
        `tests/repo/test_exporter_queries_repo.py`, which prepares every one
        of these against the real schema."""
        for name, sql in q.statements():
            assert "{" not in sql, f"{name}: unsubstituted placeholder"
            assert sql.strip(), name

    def test_every_statement_binds_the_parameters_it_uses(self) -> None:
        """`text()` finds the `:name` markers; a `::text` cast mistaken for
        one would show up here as a bind parameter nobody passes."""
        from sqlalchemy import text

        allowed = {"now", "warn", "ids", "stale_symbols", "stale_market", "stale_domain"}
        for name, sql in q.statements():
            found = set(text(sql).compile().params)
            assert found <= allowed, f"{name}: {sorted(found - allowed)}"

    def test_the_freshness_statement_carries_its_constants(self) -> None:
        sql = q.freshness_sql()
        assert "WHEN 'not_attempted' THEN 5" in sql
        assert "IN (2, 3, 4)" in sql
        assert "NOT IN (0, 1)" in sql

    def test_freshness_reduces_a_cell_in_one_pass(self) -> None:
        """One aggregation pass, not two `DISTINCT ON` CTEs joined back
        together: that join can only merge on `symbol` and discards most
        rows in a filter."""
        sql = q.freshness_sql()
        assert "DISTINCT ON" not in sql
        assert sql.count("GROUP BY") == 3

    def test_the_interval_limits_come_from_bar_limits(self) -> None:
        """1wk and 1mo have no retention edge, so nothing about them can
        expire and they must not appear with a sentinel depth."""
        from yfin.datasets.bars import BAR_LIMITS

        assert set(q.BOUNDED_INTERVALS) == {"1m", "5m", "15m", "60m"}
        assert q.BOUNDED_INTERVALS["1m"] == BAR_LIMITS["1m"][1]


class TestTheRegistry:
    def test_every_query_owns_declared_gauges(self) -> None:
        from yfin.core.metrics import METRICS

        for query in q.QUERIES:
            for name in query.gauges:
                assert METRICS[name].kind == "gauge", name

    def test_no_two_queries_own_the_same_gauge(self) -> None:
        """Owning one twice would let a query clear numbers another just
        published, in the same pass."""
        owned = [name for query in q.QUERIES for name in query.gauges]
        assert len(owned) == len(set(owned))

    def test_query_names_are_unique(self) -> None:
        """They are the `query` label on the self-health gauge."""
        names = [query.name for query in q.QUERIES]
        assert len(names) == len(set(names))

    def test_the_republished_counters_are_derived(self) -> None:
        """A shard counter added to METRICS must be exported with no edit in
        `queries.py`, or the new counter is the one nobody sees."""
        from yfin.core.metrics import METRICS, exported_name

        owned = {name for query in q.QUERIES for name in query.gauges}
        for spec in METRICS.values():
            if spec.kind == "counter" and spec.name.startswith("yfin_sync_"):
                assert exported_name(spec.name) in owned


def test_publish_sets_one_gauge_per_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    from yfin.scheduler import exporter as mod

    seen: list[str] = []
    monkeypatch.setattr(mod, "set_gauge", lambda name, value, **labels: seen.append(name))
    assert mod.publish([Sample("yfin_proxies", 1), Sample("yfin_proxies", 2)]) == 2
    assert seen == ["yfin_proxies", "yfin_proxies"]


def test_the_context_stamps_one_clock_for_the_whole_pass() -> None:
    """Every age in a refresh is measured from the same instant, so the
    gauges of one pass cannot disagree about what "now" was."""
    exporter = Exporter(
        engine=None,  # type: ignore[arg-type]
        settings=Settings(),
        queries=(),
    )
    before = datetime.now(UTC)
    ctx = exporter.context()
    assert before <= ctx.now <= datetime.now(UTC)

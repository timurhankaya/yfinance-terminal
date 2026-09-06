"""Proxy pool and sharded writes: real PostgreSQL, no network."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from yfin.models import ItemStatus, Proxy, ProxyHealth, ProxyScheme, SyncRunItem
from yfin.proxy import (
    HealthEvent,
    ProxyPolicy,
    persist_event,
    select_eligible,
)
from yfin.runner import (
    EXIT_OK,
    EXIT_PARTIAL,
    ItemRecord,
    finalize_run,
    open_run,
    record_not_attempted,
    write_items,
)

pytestmark = pytest.mark.repo


def _load_seed_proxies() -> Any:
    """`scripts/` is not a package; load it from the file directly."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "seed_proxies.py"
    spec = importlib.util.spec_from_file_location("_seed_proxies_repo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

POLICY = ProxyPolicy(failure_threshold=3, cooldown_seconds=900, dead_rounds=3)
NOW = datetime.now(UTC)


# Hosts are generated deterministically. The previous version used
# abs(hash(label)); Python's string hash is seeded differently per process,
# so two labels could land on the same host and violate the UNIQUE
# (scheme, host, port, username) constraint -- a real source of flakiness.
_HOSTS = itertools.count(1)


def _proxy(label: str, **kwargs: object) -> Proxy:
    defaults: dict[str, object] = {
        "label": label,
        "scheme": ProxyScheme.HTTP,
        "host": f"10.0.{next(_HOSTS) // 250}.{next(_HOSTS) % 250 + 1}",
        "port": 3128,
        "username": "",
    }
    defaults.update(kwargs)
    return Proxy(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def clean_proxies(test_engine: Engine) -> Iterator[None]:
    with test_engine.connect() as conn:
        conn.execute(text("DELETE FROM proxies"))
        conn.commit()
    yield
    with test_engine.connect() as conn:
        conn.execute(text("DELETE FROM proxies"))
        conn.commit()


@pytest.mark.usefixtures("clean_proxies")
class TestEligibility:
    """Eligibility truth table."""

    def test_truth_table(self, committed_session: Session) -> None:
        rows = [
            _proxy("ok-unknown"),
            _proxy("ok-healthy", health=ProxyHealth.HEALTHY),
            _proxy("no-disabled", is_enabled=False),
            _proxy("no-dead", health=ProxyHealth.DEAD),
            _proxy(
                "no-cooling",
                health=ProxyHealth.COOLDOWN,
                cooldown_until=NOW + timedelta(hours=1),
            ),
            _proxy(
                "ok-expired",
                health=ProxyHealth.COOLDOWN,
                cooldown_until=NOW - timedelta(hours=1),
            ),
        ]
        committed_session.add_all(rows)
        committed_session.commit()

        labels = {p.label for p in select_eligible(committed_session, limit=10)}
        assert labels == {"ok-unknown", "ok-healthy", "ok-expired"}

    def test_unknown_ranks_before_previously_measured(self, committed_session: Session) -> None:
        """Otherwise a newly added proxy would never be used, while one that
        has repeatedly gone through cooldown would be preferred."""
        committed_session.add_all(
            [
                _proxy(
                    "seasoned",
                    health=ProxyHealth.COOLDOWN,
                    cooldown_until=NOW - timedelta(hours=1),
                    last_latency_ms=50,
                ),
                _proxy("fresh"),
            ]
        )
        committed_session.commit()
        order = [p.label for p in select_eligible(committed_session, limit=10)]
        assert order.index("fresh") < order.index("seasoned")

    def test_healthy_ranks_first_and_fastest_wins(self, committed_session: Session) -> None:
        committed_session.add_all(
            [
                _proxy("slow", health=ProxyHealth.HEALTHY, last_latency_ms=900),
                _proxy("fast", health=ProxyHealth.HEALTHY, last_latency_ms=20),
                _proxy("fresh"),
            ]
        )
        committed_session.commit()
        assert [p.label for p in select_eligible(committed_session, limit=10)] == [
            "fast",
            "slow",
            "fresh",
        ]

    def test_limit_is_applied(self, committed_session: Session) -> None:
        committed_session.add_all([_proxy(f"p{i}") for i in range(5)])
        committed_session.commit()
        assert len(select_eligible(committed_session, limit=2)) == 2


@pytest.mark.usefixtures("clean_proxies")
class TestUniqueness:
    def test_same_endpoint_cannot_be_added_twice(self, committed_session: Session) -> None:
        """username is NOT NULL DEFAULT '', so the constraint actually applies;
        if nullable, uniqueness would not be enforced."""
        committed_session.add(_proxy("a", host="10.0.0.9", port=1080))
        committed_session.commit()
        committed_session.add(_proxy("b", host="10.0.0.9", port=1080))
        # PG raises "duplicate key value violates unique constraint" + the
        # constraint name. Matching on the constraint name is
        # language-independent and proves which uniqueness was violated.
        with pytest.raises(Exception, match="uq_proxies_endpoint"):
            committed_session.commit()
        committed_session.rollback()

    def test_host_case_does_not_create_a_second_proxy(
        self, committed_session: Session
    ) -> None:
        """Hostnames are case-insensitive (RFC 4343).

        MySQL enforced this in the schema via `ascii_general_ci`; the column
        is now COLLATE "C", so case-insensitivity moved to the write path
        (scripts/seed_proxies.py). This test proves that move works against
        the real schema: two normalized forms land on the same row.
        """
        from yfin.models import ProxyScheme as _Scheme

        seed = _load_seed_proxies()
        left = seed.parse_line("HOST.Example.COM:1080", _Scheme.HTTP)
        right = seed.parse_line("host.example.com:1080", _Scheme.HTTP)
        assert left is not None and right is not None
        assert left.host == right.host == "host.example.com"

        committed_session.add(_proxy("case-a", host=left.host, port=left.port))
        committed_session.commit()
        committed_session.add(_proxy("case-b", host=right.host, port=right.port))
        with pytest.raises(Exception, match="uq_proxies_endpoint"):
            committed_session.commit()
        committed_session.rollback()


@pytest.mark.usefixtures("clean_proxies")
class TestPersistEvent:
    def test_counters_are_incremental(self, committed_session: Session) -> None:
        """Writing back a read value would produce a lost update: a child's
        flush, the parent's SHARD_CRASH, and `proxy check` all hit the same row."""
        committed_session.add(_proxy("p1", success_count=5, failure_count=2))
        committed_session.commit()
        row = committed_session.execute(select(Proxy)).scalar_one()

        persist_event(committed_session, row.id, HealthEvent.SUCCESS, policy=POLICY)
        committed_session.commit()
        committed_session.expire_all()

        after = committed_session.execute(select(Proxy)).scalar_one()
        assert after.success_count == 6
        assert after.failure_count == 2
        assert after.health is ProxyHealth.HEALTHY

    def test_shard_crash_cools_down_immediately(self, committed_session: Session) -> None:
        committed_session.add(_proxy("p1"))
        committed_session.commit()
        row = committed_session.execute(select(Proxy)).scalar_one()

        persist_event(committed_session, row.id, HealthEvent.SHARD_CRASH, policy=POLICY)
        committed_session.commit()
        committed_session.expire_all()

        after = committed_session.execute(select(Proxy)).scalar_one()
        assert after.health is ProxyHealth.COOLDOWN
        assert after.cooldown_rounds == 1
        assert after.cooldown_until is not None

    def test_error_text_is_redacted(self, committed_session: Session) -> None:
        committed_session.add(_proxy("p1"))
        committed_session.commit()
        row = committed_session.execute(select(Proxy)).scalar_one()

        persist_event(
            committed_session,
            row.id,
            HealthEvent.NETWORK,
            policy=POLICY,
            error="http://acct:s3cret@10.0.0.1:3128 refused",
        )
        committed_session.commit()
        committed_session.expire_all()

        after = committed_session.execute(select(Proxy)).scalar_one()
        assert after.last_error is not None
        assert "s3cret" not in after.last_error
        assert "acct:***@" in after.last_error


class TestRunAggregation:
    """Totals and the exit code are computed from the DB."""

    def test_not_attempted_prevents_exit_zero(self, test_engine: Engine) -> None:
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
        run_id = open_run(factory, symbol_count=3, dataset_count=1, shard_count=2)
        write_items(
            factory,
            run_id,
            [
                ItemRecord(symbol="AAA", dataset="history", status=ItemStatus.OK, rows_written=5),
            ],
            shard_index=0,
            proxy_id=None,
            proxy_label="eu-1",
        )
        record_not_attempted(factory, run_id, ["BBB", "CCC"])

        tally = finalize_run(factory, run_id, symbol_count=3, dataset_count=1)
        assert tally.counts[ItemStatus.NOT_ATTEMPTED.value] == 2
        assert tally.resolved_symbols == 3
        assert tally.exit_code() == EXIT_PARTIAL

        with Session(test_engine) as session:
            labels = set(
                session.execute(
                    select(SyncRunItem.proxy_label).where(SyncRunItem.run_id == run_id)
                ).scalars()
            )
        assert labels == {"eu-1", None}

    def test_symbols_missing_from_audit_cannot_exit_zero(self, test_engine: Engine) -> None:
        """A symbol held by a crashed/timed-out shard.

        `shard.py` only drains what remains in the queue; a symbol the child
        picked up but never finished produces no `sync_run_items` row.
        Without reconciliation, aggregation would treat the missing data as
        complete and return EXIT_OK -- breaking the guarantee that the exit
        code is never 0 while a symbol was left unprocessed.
        """
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
        run_id = open_run(factory, symbol_count=3, dataset_count=1, shard_count=2)
        # Only two of three show up in the audit; the third vanishes without a trace
        write_items(
            factory,
            run_id,
            [
                ItemRecord(symbol="AAA", dataset="history", status=ItemStatus.OK, rows_written=5),
                ItemRecord(symbol="BBB", dataset="history", status=ItemStatus.OK, rows_written=5),
            ],
        )

        tally = finalize_run(factory, run_id, symbol_count=3, dataset_count=1)

        assert tally.counts[ItemStatus.NOT_ATTEMPTED.value] == 1
        assert tally.exit_code() != EXIT_OK
        assert tally.exit_code() == EXIT_PARTIAL

    def test_clean_run_exits_zero(self, test_engine: Engine) -> None:
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
        run_id = open_run(factory, symbol_count=1, dataset_count=1)
        write_items(
            factory,
            run_id,
            [ItemRecord(symbol="AAA", dataset="history", status=ItemStatus.OK, rows_written=2)],
        )
        tally = finalize_run(factory, run_id, symbol_count=1, dataset_count=1)
        assert tally.exit_code() == EXIT_OK
        assert tally.totals["rows_written"] == 2

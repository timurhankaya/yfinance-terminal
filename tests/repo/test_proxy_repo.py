"""Proxy havuzu ve shard'li yazim: gercek MySQL, ag yok (P10.2)."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

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

POLICY = ProxyPolicy(failure_threshold=3, cooldown_seconds=900, dead_rounds=3)
NOW = datetime.now(UTC)


# Host DETERMINISTIK uretilir. Onceki surum abs(hash(label)) kullaniyordu;
# Python'un string hash'i her process'te farkli tohumlandigi icin iki label
# ayni host'a dusup UNIQUE (scheme, host, port, username) kisitini ihlal
# edebiliyordu - testi kararsiz yapan gercek bir kusurdu.
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
    """Uygunluk dogruluk tablosu (P3.2)."""

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
        """Aksi halde yeni eklenen proxy HIC kullanilmaz, defalarca
        cooldown'a girmis olan tercih edilirdi (P5.1)."""
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
        """username NOT NULL DEFAULT '' oldugu icin kisit GERCEKTEN
        uygulanir; nullable olsaydi MySQL tekilligi zorlamazdi (P3.3)."""
        committed_session.add(_proxy("a", host="10.0.0.9", port=1080))
        committed_session.commit()
        committed_session.add(_proxy("b", host="10.0.0.9", port=1080))
        # PG: "duplicate key value violates unique constraint" + kisit adi.
        # Kisit ADI arandi: mesajin dilinden bagimsiz ve hangi
        # tekilligin ihlal edildigini de kanitlar.
        with pytest.raises(Exception, match="uq_proxies_endpoint"):
            committed_session.commit()
        committed_session.rollback()


@pytest.mark.usefixtures("clean_proxies")
class TestPersistEvent:
    def test_counters_are_incremental(self, committed_session: Session) -> None:
        """Okunan degeri geri yazmak lost update uretirdi: ayni satira
        child'in flush'i, parent'in SHARD_CRASH'i ve `proxy check` biner."""
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
    """Toplamlar ve cikis kodu DB'den hesaplanir (P4.5)."""

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
        """B2 regresyonu: coken/timeout olan shard'in ELINDEKI sembol.

        `shard.py` yalnizca kuyrukta KALANI drain eder; child'in alip
        bitiremedigi sembol icin hicbir `sync_run_items` satiri olusmaz.
        Mutabakat olmadan agregasyon eksik veriyi tam sanip EXIT_OK
        dondururdu -- P8.1'in "islenmemis sembol varken cikis kodu asla 0
        olamaz" garantisi tam da burada kirilirdi.
        """
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
        run_id = open_run(factory, symbol_count=3, dataset_count=1, shard_count=2)
        # Ucunden YALNIZ ikisi denetime yansidi; ucuncusu iz birakmadan yok
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

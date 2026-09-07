"""The turn's error and transaction policy, now that it has one home.

The market and domain runners each carried a copy of this. The bodies
differed only in log keys and `normalize`'s arity; the policy below was
identical in both, which meant a fix to one silently missed the other.
"""

from __future__ import annotations

from typing import Any

from yfin.core.errors import ErrorKind
from yfin.datasets.base import NormalizedResult
from yfin.datasets.registry import Registry
from yfin.models import ItemStatus
from yfin.pipeline.turn import Turn, run_turn
from yfin.storage.contracts import RowWriter, TableWrite, WriteStats


class _Dataset:
    name = "probe"
    produces = ("dividends", "splits")


class _Tracker:
    def __init__(self) -> None:
        self.withdrawn = False
        self.successes = 0
        self.errors: list[ErrorKind] = []

    def record_success(self) -> None:
        self.successes += 1

    def record_error(self, kind: ErrorKind, message: str | None = None) -> None:
        self.errors.append(kind)

    def flush(self, session: Any) -> None:  # pragma: no cover - unused here
        pass


class _Session:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def _factory(session: _Session) -> Any:
    return lambda: session


def _registry() -> Registry[Any]:
    """`failed_records` resolves `produces` through the registry, so a
    dataset that is not registered gets a single table_name=NULL row."""
    registry: Registry[Any] = Registry()
    registry.register(_Dataset())
    return registry


def _turn(**kw: Any) -> Turn:
    base: dict[str, Any] = {
        "dataset": _Dataset(),
        "fetch": lambda: "raw",
        "normalize": lambda raw: NormalizedResult(
            writes=[
                TableWrite(
                    table="dividends",
                    rows=[{"symbol": "A", "ex_date": "2026-01-01"}],
                    key_columns=("symbol", "ex_date"),
                    update_columns=(),
                )
            ]
        ),
        "upsert": lambda writer, result: WriteStats(
            attempted={"dividends": 1}, verified={"dividends": 1}
        ),
        "audit_key": "US",
        "registry": _registry(),
        "kind": "market",
        "log_context": {"scope": "US"},
    }
    return Turn(**(base | kw))


def test_a_clean_turn_commits_and_records_success() -> None:
    session, tracker = _Session(), _Tracker()
    records = run_turn(_factory(session), _turn(), tracker)
    assert session.committed and not session.rolled_back
    assert tracker.successes == 1 and not tracker.errors
    assert [r.status for r in records] == [ItemStatus.OK]


def test_a_fetch_failure_never_opens_a_transaction() -> None:
    """The write session must not be opened at all: a fetch that failed
    has nothing to write, and opening one would burn a connection per
    failing cell across a whole run."""
    session, tracker = _Session(), _Tracker()

    def boom() -> Any:
        raise ValueError("upstream is down")

    records = run_turn(_factory(session), _turn(fetch=boom), tracker)
    assert not session.committed and not session.rolled_back
    assert tracker.errors and tracker.successes == 0
    assert all(r.status is ItemStatus.FAILED for r in records)


def test_a_failed_turn_still_leaves_one_record_per_table() -> None:
    """Absence is not a status. Without a row per `produces` table, "when
    was this dataset last attempted" silently answers with the last
    SUCCESSFUL run."""

    def boom() -> Any:
        raise RuntimeError("no")

    records = run_turn(_factory(_Session()), _turn(fetch=boom), _Tracker())
    assert {r.table_name for r in records} == {"dividends", "splits"}


def test_a_write_failure_rolls_back_and_is_still_audited() -> None:
    """The rollback must not take the audit with it: the caller writes
    these records on a different session, which is the whole reason the
    turn returns them instead of writing them itself."""
    session, tracker = _Session(), _Tracker()

    def boom(writer: RowWriter, result: NormalizedResult) -> WriteStats:
        raise RuntimeError("deadlock")

    records = run_turn(_factory(session), _turn(upsert=boom), tracker)
    assert session.rolled_back and not session.committed
    # The fetch succeeded, so proxy health must NOT be blamed for a
    # database failure.
    assert tracker.successes == 1 and not tracker.errors
    assert all(r.status is ItemStatus.FAILED for r in records)


def test_the_region_is_carried_into_every_record() -> None:
    """The domain runner's cells are (dataset x key x region); losing the
    region would make two regions' rows indistinguishable."""
    records = run_turn(_factory(_Session()), _turn(region="EUROPE"), _Tracker())
    assert {r.region for r in records} == {"EUROPE"}

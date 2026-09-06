"""Sharded runs: exit code, shard formula, monotonic column."""

from __future__ import annotations

from typing import Any

import pytest

from yfin.core.config import Settings
from yfin.datasets import SYMBOL_DATASETS
from yfin.models import ItemStatus
from yfin.pipeline.runner import (
    EXIT_ALL_FAILED,
    EXIT_NO_SYMBOL_RESOLVED,
    EXIT_OK,
    EXIT_PARTIAL,
    RunTally,
    list_source,
)
from yfin.pipeline.shard import NoEligibleProxy, ShardSpec, _effective_shards


def _tally(counts: dict[ItemStatus, int], *, symbols: int = 1, resolved: int = 1) -> RunTally:
    return RunTally(
        run_id=1,
        symbol_count=symbols,
        dataset_count=1,
        resolved_symbols=resolved,
        counts={k.value: v for k, v in counts.items()},
    )


class TestExitCode:
    def test_ok_when_no_failures(self) -> None:
        assert _tally({ItemStatus.OK: 3}).exit_code() == EXIT_OK

    def test_empty_and_skipped_are_not_failures(self) -> None:
        """No source data != an error."""
        tally = _tally({ItemStatus.EMPTY: 2, ItemStatus.SKIPPED: 1})
        assert tally.exit_code() == EXIT_OK

    def test_partial_when_some_failed(self) -> None:
        assert _tally({ItemStatus.OK: 1, ItemStatus.FAILED: 1}).exit_code() == EXIT_PARTIAL

    def test_all_failed(self) -> None:
        assert _tally({ItemStatus.FAILED: 3}).exit_code() == EXIT_ALL_FAILED

    def test_no_symbol_resolved(self) -> None:
        tally = _tally({ItemStatus.UNKNOWN_SYMBOL: 2}, symbols=2, resolved=0)
        assert tally.exit_code() == EXIT_NO_SYMBOL_RESOLVED

    def test_not_attempted_forbids_exit_zero(self) -> None:
        """Otherwise, a run where half the universe was never fetched would
        return 'ok' and exit 0 -- silent data loss."""
        tally = _tally({ItemStatus.OK: 5, ItemStatus.NOT_ATTEMPTED: 3}, symbols=8, resolved=5)
        assert tally.exit_code() == EXIT_PARTIAL

    def test_not_attempted_is_excluded_from_all_failed(self) -> None:
        """An unfetched symbol is not a "failed cell"; ALL_FAILED only
        counts cells that were actually attempted."""
        tally = _tally({ItemStatus.FAILED: 2, ItemStatus.NOT_ATTEMPTED: 4}, symbols=6, resolved=2)
        assert tally.cells == 2
        assert tally.exit_code() == EXIT_ALL_FAILED


class TestDelistProtection:
    """A dead proxy must not deactivate the whole universe."""

    def test_transport_faults_are_recognised(self) -> None:
        from yfin.core.errors import PROXY_FAULT_KINDS, ErrorKind

        assert {
            ErrorKind.RATE_LIMITED,
            ErrorKind.BLOCKED,
            ErrorKind.NETWORK,
        } == PROXY_FAULT_KINDS
        # A symbol genuinely failing to resolve is not a transport fault
        assert ErrorKind.UNKNOWN_SYMBOL not in PROXY_FAULT_KINDS
        assert ErrorKind.DATA not in PROXY_FAULT_KINDS


class TestListSource:
    def test_drains_once_then_returns_none(self) -> None:
        source = list_source(["A", "B"])
        assert [source(), source(), source(), source()] == ["A", "B", None, None]

    def test_is_shared_safely_between_callers(self) -> None:
        """The source is shared across threads; each symbol is handed out
        exactly once."""
        source = list_source([f"S{i}" for i in range(50)])
        seen = []
        while (symbol := source()) is not None:
            seen.append(symbol)
        assert len(seen) == len(set(seen)) == 50


class TestShardSelection:
    def test_no_proxy_forces_single_direct_shard(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert (
            _effective_shards(
                None,  # type: ignore[arg-type]  # no session is used on the no_proxy path
                settings=settings,
                max_shards=8,
                no_proxy=True,
                require_proxy=False,
            )
            == []
        )

    def test_require_proxy_raises_when_pool_unusable(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        class _EmptyPool:
            def execute(self, *_: Any, **__: Any) -> Any:
                class _R:
                    @staticmethod
                    def scalars() -> list[Any]:
                        return []

                return _R()

        with pytest.raises(NoEligibleProxy):
            _effective_shards(
                _EmptyPool(),  # type: ignore[arg-type]
                settings=settings,
                max_shards=None,
                no_proxy=False,
                require_proxy=True,
            )


class TestShardSpec:
    def test_cache_key_follows_proxy_not_shard_index(self) -> None:
        """shard-0 may get a different proxy on the next run; if the index
        were keyed by shard_index, a cookie minted for A's IP would be used
        with B's egress IP."""
        base = {
            "run_id": 1,
            "dataset_names": ("history",),
            "full_refresh": False,
            "database": None,
        }
        first = ShardSpec(shard_index=0, proxy_id=7, **base)  # type: ignore[arg-type]
        second = ShardSpec(shard_index=3, proxy_id=7, **base)  # type: ignore[arg-type]
        assert first.proxy_key == second.proxy_key == "proxy-7"

    def test_direct_key_when_no_proxy(self) -> None:
        spec = ShardSpec(
            run_id=1, shard_index=0, dataset_names=(), full_refresh=False, database=None
        )
        assert spec.proxy_key == "direct"

    def test_spec_is_picklable(self) -> None:
        """Crosses a process boundary via spawn; must be picklable."""
        import pickle

        spec = ShardSpec(
            run_id=9,
            shard_index=1,
            dataset_names=("history", "news"),
            full_refresh=True,
            database="yfinance_test",
            proxy_id=3,
            proxy_label="eu-1",
            proxy_dsn="http://10.0.0.1:3128",
        )
        assert pickle.loads(pickle.dumps(spec)) == spec


class TestRepairColumn:
    def test_is_repaired_is_monotonic(self) -> None:
        """Repair heuristics depend on window length; the same row can come
        back as 1 once and 0 the next time. A plain upsert would write that
        back and reset the column's audit value."""
        from yfin.datasets.history import MONOTONIC_COLUMNS, UPDATE_COLUMNS

        assert "is_repaired" in UPDATE_COLUMNS
        assert MONOTONIC_COLUMNS == ("is_repaired",)

    def test_is_repaired_not_in_column_map(self) -> None:
        """If it were in _COLUMN_MAP, the generic loop would run it through
        to_decimal and write a Decimal into a BOOLEAN column."""
        from yfin.datasets.history import _COLUMN_MAP

        assert "Repaired?" not in _COLUMN_MAP

    def test_history_write_declares_monotonic_column(self) -> None:
        import sys

        sys.path.insert(0, "tests")
        from helpers import as_frame, load_fixture

        frame = as_frame(load_fixture("AAPL", "history"))
        result = SYMBOL_DATASETS["history"].normalize(frame, "AAPL")
        write = next(w for w in result.writes if w.table == "price_history")
        assert write.monotonic_columns == ("is_repaired",)
        # The column is present in every row; otherwise the `present`
        # intersection would drop it from the ON DUPLICATE KEY UPDATE scope.
        assert all("is_repaired" in row for row in write.rows)


class TestRepairExtra:
    """Without the yfinance[repair] extra, repair blows up on every symbol
    and price_history is never written (verified in a live run)."""

    def test_repair_extra_is_installed(self) -> None:
        """The dependency is declared as `yfinance[repair]`; this test
        catches the extra silently disappearing."""
        import importlib.util

        assert importlib.util.find_spec("scipy.ndimage") is not None
        assert importlib.util.find_spec("sklearn.cluster") is not None

    def test_disabled_by_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import yfin.core.config as config_mod
        import yfin.datasets.history as history_mod

        monkeypatch.setattr(
            config_mod, "_settings", Settings(yf_history_repair=False, _env_file=None)
        )
        monkeypatch.setattr(history_mod, "_REPAIR_AVAILABLE", None)
        assert history_mod.repair_enabled() is False

    def test_missing_extra_degrades_instead_of_failing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A working run without repair, instead of a total data outage."""
        import builtins

        import yfin.core.config as config_mod
        import yfin.datasets.history as history_mod

        monkeypatch.setattr(
            config_mod, "_settings", Settings(yf_history_repair=True, _env_file=None)
        )
        monkeypatch.setattr(history_mod, "_REPAIR_AVAILABLE", None)
        real_import = builtins.__import__

        def _no_scipy(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith(("scipy", "sklearn")):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_scipy)
        assert history_mod.repair_enabled() is False


class TestFrameConsumers:
    def test_all_action_tables_are_consumers(self) -> None:
        """The minimum watermark among tables consuming the shared frame's
        `start`; if one were missing, a narrow window would miss data."""
        from yfin.datasets.history import FRAME_CONSUMERS

        assert set(FRAME_CONSUMERS) == {"history", "dividends", "splits", "capital_gains"}

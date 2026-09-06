"""Shard'li calistirma: cikis kodu, shard formulu, monotonik kolon (P10.1)."""

from __future__ import annotations

from typing import Any

import pytest

from yfin.config import Settings
from yfin.datasets import SYMBOL_DATASETS
from yfin.models import ItemStatus
from yfin.runner import (
    EXIT_ALL_FAILED,
    EXIT_NO_SYMBOL_RESOLVED,
    EXIT_OK,
    EXIT_PARTIAL,
    RunTally,
    list_source,
)
from yfin.shard import NoEligibleProxy, ShardSpec, _effective_shards


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
        """S8.2: kaynak veri yok != hata."""
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
        """P4.7/P8.1: aksi halde evrenin yarisi hic cekilmemisken run 'ok'
        ve exit 0 donerdi - sessiz veri kaybi."""
        tally = _tally({ItemStatus.OK: 5, ItemStatus.NOT_ATTEMPTED: 3}, symbols=8, resolved=5)
        assert tally.exit_code() == EXIT_PARTIAL

    def test_not_attempted_is_excluded_from_all_failed(self) -> None:
        """Cekilmemis sembol 'basarisiz hucre' degildir; ALL_FAILED
        yalnizca gercekten denenmis hucreleri sayar."""
        tally = _tally({ItemStatus.FAILED: 2, ItemStatus.NOT_ATTEMPTED: 4}, symbols=6, resolved=2)
        assert tally.cells == 2
        assert tally.exit_code() == EXIT_ALL_FAILED


class TestDelistProtection:
    """Olu bir proxy tum evreni pasiflestirmemeli (P5.2 simetrigi)."""

    def test_transport_faults_are_recognised(self) -> None:
        from yfin.errors import PROXY_FAULT_KINDS, ErrorKind

        assert {
            ErrorKind.RATE_LIMITED,
            ErrorKind.BLOCKED,
            ErrorKind.NETWORK,
        } == PROXY_FAULT_KINDS
        # Sembolun gercekten cozulememesi tasima hatasi DEGILDIR
        assert ErrorKind.UNKNOWN_SYMBOL not in PROXY_FAULT_KINDS
        assert ErrorKind.DATA not in PROXY_FAULT_KINDS


class TestListSource:
    def test_drains_once_then_returns_none(self) -> None:
        source = list_source(["A", "B"])
        assert [source(), source(), source(), source()] == ["A", "B", None, None]

    def test_is_shared_safely_between_callers(self) -> None:
        """Kaynak thread'ler arasinda paylasilir; her sembol TAM OLARAK
        bir kez dagitilir."""
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
                None,  # type: ignore[arg-type]  # no_proxy yolunda session kullanilmaz
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
        """P4.11: shard-0 bir sonraki run'da baska bir proxy olabilir;
        dizin shard_index ile anahtarlansaydi A'nin IP'siyle mintlenmis
        cookie B'nin cikis IP'siyle kullanilirdi."""
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
        """spawn ile process sinirindan gecer; pickle'lanabilir OLMALIDIR."""
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
        """P6.3: onarim heuristikleri pencere uzunluguna baglidir; ayni
        satir bir kez 1, ertesi kez 0 gelebilir. Duz upsert bunu geri
        yazar ve kolonun denetim degeri sifirlanirdi."""
        from yfin.datasets.history import MONOTONIC_COLUMNS, UPDATE_COLUMNS

        assert "is_repaired" in UPDATE_COLUMNS
        assert MONOTONIC_COLUMNS == ("is_repaired",)

    def test_is_repaired_not_in_column_map(self) -> None:
        """_COLUMN_MAP'e konsaydi jenerik dongu onu to_decimal ile isler
        ve BOOLEAN kolona Decimal yazardi."""
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
        # Kolon HER SATIRDA bulunur; aksi halde `present` kesisimi onu
        # ON DUPLICATE KEY UPDATE kapsamindan dusururdu.
        assert all("is_repaired" in row for row in write.rows)


class TestRepairExtra:
    """yfinance[repair] ekstrasi olmadan onarim HER sembolde patlar ve
    price_history hic yazilmaz (canli kosuda dogrulandi)."""

    def test_repair_extra_is_installed(self) -> None:
        """Bagimlilik `yfinance[repair]` olarak beyan edilir; bu test
        ekstranin sessizce dusmesini yakalar."""
        import importlib.util

        assert importlib.util.find_spec("scipy.ndimage") is not None
        assert importlib.util.find_spec("sklearn.cluster") is not None

    def test_disabled_by_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import yfin.config as config_mod
        import yfin.datasets.history as history_mod

        monkeypatch.setattr(
            config_mod, "_settings", Settings(yf_history_repair=False, _env_file=None)
        )
        monkeypatch.setattr(history_mod, "_REPAIR_AVAILABLE", None)
        assert history_mod.repair_enabled() is False

    def test_missing_extra_degrades_instead_of_failing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Toplam veri kesintisi yerine onarimsiz ama CALISAN kosu."""
        import builtins

        import yfin.config as config_mod
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
        """Paylasilan cercevenin `start`'i TUKETEN tablolarin watermark
        minimumudur; biri eksik kalirsa dar pencere veri kacirirdi."""
        from yfin.datasets.history import FRAME_CONSUMERS

        assert set(FRAME_CONSUMERS) == {"history", "dividends", "splits", "capital_gains"}

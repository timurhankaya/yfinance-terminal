"""Case-insensitivity lives in the write and query paths; PostgreSQL columns are
COLLATE "C". Does not touch a database."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load_seed_proxies() -> Any:
    """`scripts/` is not a package; loaded from the file directly."""
    path = _ROOT / "scripts" / "seed_proxies.py"
    spec = importlib.util.spec_from_file_location("_seed_proxies", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_seed_proxies"] = module
    spec.loader.exec_module(module)
    return module


class TestSymbolFieldsAreUppercased:
    """`--exchange nms` and `--exchange NMS` must give the same result."""

    def test_normalize_upper_cases_exchange_and_quote_type(self) -> None:
        from yfin.datasets.symbols import SymbolsDataset, SymbolsPayload

        payload = SymbolsPayload(
            fast_info={"quoteType": "equity", "exchange": "nms", "currency": "usd"},
            metadata={},
            fetched_at=None,
        )
        result = SymbolsDataset().normalize(payload, "AAPL")
        row = result.writes[0].rows[0]
        assert row["quote_type"] == "EQUITY"
        assert row["exchange"] == "NMS"

    def test_none_stays_none(self) -> None:
        """For a symbol added via `yfin symbols add`, these fields are NULL
        until the first sync; .upper() must not blow up on None."""
        from yfin.datasets.symbols import SymbolsDataset, SymbolsPayload

        payload = SymbolsPayload(fast_info={}, metadata={}, fetched_at=None)
        row = SymbolsDataset().normalize(payload, "AAPL").writes[0].rows[0]
        assert row["quote_type"] is None
        assert row["exchange"] is None


class TestProxyHostIsLowercased:
    """Hostnames are case-insensitive (RFC 4343); `uq_proxies_endpoint` relies on the write
    path normalizing them, or the same proxy would be inserted twice."""

    def test_seed_line_lowercases_host(self) -> None:
        from yfin.models import ProxyScheme

        seed = _load_seed_proxies()
        left = seed.parse_line("HOST.Example.COM:8080", ProxyScheme.HTTP)
        right = seed.parse_line("host.example.com:8080", ProxyScheme.HTTP)
        assert left is not None and right is not None
        assert left.host == "host.example.com"
        assert left.host == right.host

    def test_dsn_path_already_lowercases(self) -> None:
        """`urlsplit(...).hostname` already lowercases the hostname; there
        is nothing to do here, but it's pinned as a regression guard -- if
        this ever moves to manual parsing, this test will fail."""
        from yfin.proxy.dsn import parse_dsn

        dsn = "http://u:pw" + "@" + "HOST.Example.COM:8080"
        assert parse_dsn(dsn).host == "host.example.com"


@pytest.mark.parametrize("given", ["nms", "NMS", "Nms"])
def test_cli_filter_input_is_normalized(given: str) -> None:
    """Filter input is also normalized via `.upper()`; since the write path
    uppercases, both sides meet in the same form and the
    `ix_symbols_exchange` index stays usable (wrapping the column in
    func.upper would make it unusable)."""
    from yfin.cli.common import normalize_filter_values

    assert normalize_filter_values([given]) == ["NMS"]

"""`GET /ui/api/sparklines`: the limits, checked before any query runs. The route reads up
to 200 symbols x 90 sessions in one statement, so its own bounds, not `RequestBrake`, keep
one call cheap."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yfin.api.core.config import ApiSettings
from yfin.ui.data import (
    SPARKLINE_DEFAULT_POINTS,
    SPARKLINE_MAX_POINTS,
    SPARKLINE_MAX_SYMBOLS,
    SPARKLINE_MIN_POINTS,
    parse_sparkline_symbols,
)

KEY = "k" * 32


def settings() -> ApiSettings:
    return ApiSettings(
        _env_file=None,
        jwt_signing_key=KEY,
        jwt_kid="k1",
        jwt_issuer="yfin-api",
        ui_enabled=True,
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from yfin.api.app import create_app
    from yfin.ui import pages

    monkeypatch.setattr(pages, "default_dist_dir", lambda: tmp_path / "absent")
    return TestClient(create_app(settings()))


def test_the_symbol_ceiling_is_the_sockets(client: TestClient) -> None:
    """200, the same number one live connection may subscribe to -- a page
    that can watch a list can draw a sparkline beside every row of it."""
    symbols = ",".join(f"SYM{i}" for i in range(SPARKLINE_MAX_SYMBOLS + 1))
    response = client.get("/ui/api/sparklines", params={"symbols": symbols})
    assert response.status_code == 422
    assert response.json()["type"] == "invalid_parameter"


def test_no_symbols_at_all_is_422(client: TestClient) -> None:
    """An empty list is a caller bug, and an empty 200 would hide it."""
    response = client.get("/ui/api/sparklines", params={"symbols": " , "})
    assert response.status_code == 422
    assert response.json()["type"] == "invalid_parameter"


def test_symbols_is_required(client: TestClient) -> None:
    assert client.get("/ui/api/sparklines").status_code == 422


@pytest.mark.parametrize("points", [SPARKLINE_MIN_POINTS - 1, SPARKLINE_MAX_POINTS + 1, 0, -1])
def test_points_outside_the_window_is_422(client: TestClient, points: int) -> None:
    response = client.get("/ui/api/sparklines", params={"symbols": "AAPL", "points": points})
    assert response.status_code == 422
    assert response.json()["type"] == "invalid_parameter"


def test_an_interval_is_refused_rather_than_ignored(client: TestClient) -> None:
    """A sparkline is daily closes. Silently ignoring `interval=5m` would
    return a month of sessions under a label saying five minutes; intraday
    is `PX`'s question and has its own route."""
    response = client.get(
        "/ui/api/sparklines", params={"symbols": "AAPL", "interval": "5m"}
    )
    assert response.status_code == 422
    assert response.json()["type"] == "invalid_parameter"


def test_the_route_is_not_in_the_schema(client: TestClient) -> None:
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    assert "/ui/api/sparklines" not in paths


def test_the_default_window_is_a_trading_month() -> None:
    assert SPARKLINE_MIN_POINTS < SPARKLINE_DEFAULT_POINTS < SPARKLINE_MAX_POINTS
    assert SPARKLINE_DEFAULT_POINTS == 30


class TestParseSymbols:
    def test_uppercases_and_trims(self) -> None:
        assert parse_sparkline_symbols(" aapl , msft ") == ["AAPL", "MSFT"]

    def test_keeps_the_order_asked_for(self) -> None:
        """The panel maps rows to series by symbol, but a deterministic
        order is what makes a response diffable."""
        assert parse_sparkline_symbols("MSFT,AAPL") == ["MSFT", "AAPL"]

    def test_drops_a_repeat_rather_than_querying_it_twice(self) -> None:
        assert parse_sparkline_symbols("AAPL,aapl,MSFT") == ["AAPL", "MSFT"]

    def test_drops_empty_tokens(self) -> None:
        assert parse_sparkline_symbols("AAPL,,MSFT,") == ["AAPL", "MSFT"]

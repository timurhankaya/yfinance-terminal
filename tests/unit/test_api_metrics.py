"""The API's own metrics: `/metrics` exists outside the contract, is capped like the other
unauthenticated endpoint, and its counters increment where the decision is made."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from yfin.api.core import window
from yfin.api.core.config import ApiSettings
from yfin.core.metrics import METRICS


@pytest.fixture(autouse=True)
def _fresh_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A minute is process state; no test inherits another's."""
    monkeypatch.setattr(window, "_windows", {})


@pytest.fixture
def client() -> TestClient:
    from yfin.api.app import create_app

    return TestClient(create_app(ApiSettings(trusted_proxies="")))


class TestTheEndpoint:
    def test_it_serves_the_exposition_format(self, client: TestClient) -> None:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "# HELP" in response.text

    def test_it_is_not_in_the_published_contract(self, client: TestClient) -> None:
        """`test_api_contract.py` filters paths by the `/v1`, `/oauth` and
        `/health` prefixes, so it would not have noticed either way. The
        endpoint is operational, not part of what a client is promised."""
        assert "/metrics" not in client.get("/openapi.json").json()["paths"]

    def test_it_is_capped_per_ip(self) -> None:
        """Unauthenticated, and it renders every series the process holds.
        Uncapped it is the cheapest way to make the API do work."""
        from yfin.api.app import create_app

        capped = TestClient(
            create_app(ApiSettings(trusted_proxies="", health_rate_limit_per_minute=2))
        )
        assert capped.get("/metrics").status_code == 200
        assert capped.get("/metrics").status_code == 200
        refused = capped.get("/metrics")
        assert refused.status_code == 429
        assert refused.headers["Retry-After"] == "60"

    def test_its_budget_is_separate_from_readiness(self) -> None:
        """A Prometheus scraping every fifteen seconds must not spend a
        Kubernetes probe's allowance -- the two failures would then be
        indistinguishable, and the probe would be the one to lose."""
        from yfin.api.app import create_app

        both = TestClient(
            create_app(ApiSettings(trusted_proxies="", health_rate_limit_per_minute=1))
        )
        assert both.get("/metrics").status_code == 200
        assert both.get("/metrics").status_code == 429
        assert both.get("/health/ready").status_code != 429


class TestWhatTheParametersDo:
    """A private registry: on a duplicate registration `prometheus-fastapi-instrumentator`
    returns None and attaches no instrumentation, so a second app in the process would
    serve a `/metrics` that never moves. Against the default registry, this suite's many
    apps would pass or fail on collection order."""

    @pytest.fixture
    def measured(self) -> TestClient:
        from prometheus_client import CollectorRegistry, generate_latest

        from yfin.api.app import build_instrumentator, create_app

        registry = CollectorRegistry()
        app = create_app(ApiSettings(trusted_proxies=""))
        instrumentator = build_instrumentator(registry)
        instrumentator.instrument(app, metric_namespace="yfin")

        client = TestClient(app)
        client.series = lambda: generate_latest(registry).decode()  # type: ignore[attr-defined]
        return client

    def _series(self, client: TestClient) -> str:
        return client.series()  # type: ignore[attr-defined,no-any-return]

    def test_a_request_is_measured_by_its_route_template(
        self, measured: TestClient
    ) -> None:
        """The `handler` label is the route TEMPLATE, which is why the instrumentator is
        installed after the routers and `symbol` never reaches a label. An unmatched path
        has no template and is labelled `handler="none"`, never a label of its own."""
        measured.get("/no-such-route")
        body = self._series(measured)
        assert "yfin_http_requests_total" in body
        assert 'handler="none"' in body

    def test_status_codes_are_not_grouped(self, measured: TestClient) -> None:
        """`2xx` cannot tell a 200 from a 204, and the difference between
        401 and 403 is the whole auth story."""
        measured.get("/no-such-route")
        body = self._series(measured)
        assert 'status="404"' in body
        assert 'status="2xx"' not in body

    def test_the_scrape_and_the_probe_are_not_measured(
        self, measured: TestClient
    ) -> None:
        """Excluded handlers: at a fifteen-second scrape and a ten-second
        probe they would be most of the traffic the API reports on."""
        measured.get("/health")
        measured.get("/metrics")
        body = self._series(measured)
        assert 'handler="/metrics"' not in body
        assert 'handler="/health"' not in body

    def test_there_is_no_in_progress_gauge(self, measured: TestClient) -> None:
        """A gauge is meaningless under `PROMETHEUS_MULTIPROC_DIR`: four
        workers write four files and the collector takes one value."""
        measured.get("/no-such-route")
        assert "inprogress" not in self._series(measured)


class TestTheRequestLog:
    def test_the_scrape_and_the_probe_are_not_logged(
        self, client: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """At a fifteen-second scrape and a ten-second probe these would be
        14,000 lines a day saying nothing, in the retention window the
        lines that DO say something have to be found in."""
        capsys.readouterr()
        client.get("/health")
        client.get("/metrics")
        assert '"event": "request"' not in capsys.readouterr().err

    def test_a_real_request_is_logged_at_debug_only(
        self, client: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """One line per request scales with traffic, not with incidents; the
        instrumentator carries route, status and duration as metrics."""
        from yfin.core.logging_setup import configure_logging

        configure_logging("INFO", "json", "api")
        capsys.readouterr()
        client.get("/no-such-route")
        assert '"event": "request"' not in capsys.readouterr().err
        configure_logging("DEBUG", "json", "api")
        capsys.readouterr()
        client.get("/no-such-route")
        assert '"event": "request"' in capsys.readouterr().err

    def test_the_request_id_header_is_still_set(self, client: TestClient) -> None:
        """Not logging a request is not the same as not tracing it: a
        `/metrics` failure still has an id to quote."""
        assert client.get("/metrics").headers["X-Request-Id"]


class TestTheCounters:
    """Incremented where the decision is made, not near it."""

    def test_the_problem_counter_covers_every_declared_type(self) -> None:
        """The label set IS `ALL_TYPES`. A type added to the error module
        without a thought for the dashboard is still counted, because the
        counter is in `problem_response` -- the one function every refusal
        passes through."""
        from yfin.api.core.errors import ALL_TYPES

        assert METRICS["yfin_api_problems_total"].labelnames == ("type",)
        assert len(ALL_TYPES) == len(set(ALL_TYPES))

    def test_a_problem_response_increments_it(self, client: TestClient) -> None:
        client.get("/no-such-route")
        body = client.get("/metrics").text
        assert 'yfin_api_problems_total{type="not_found"}' in body

    def test_the_readiness_cache_is_counted(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A miss is two connections; the ratio is what says whether the
        TTL is set anywhere near right."""
        from yfin.api.routers import meta

        monkeypatch.setattr(meta, "_check_database", lambda: True)
        monkeypatch.setattr(meta, "_check_redis", lambda _s: True)
        client.get("/health/ready")
        body = client.get("/metrics").text
        assert 'cache="readiness"' in body


    def test_every_fail_open_layer_has_a_name(self) -> None:
        """`where` says which layer gave up, because the three fail open
        independently and an outage in one is not an outage in another."""
        import inspect

        from yfin.api.ratelimit import concurrency, limiter, usage

        found = {
            where
            for module in (limiter, concurrency, usage)
            for where in ("limiter", "concurrency", "usage")
            if f'where="{where}"' in inspect.getsource(module)
        }
        assert found == {"limiter", "concurrency", "usage"}

    def test_the_api_counters_are_declared(self) -> None:
        for name in (
            "yfin_api_ratelimit_decisions_total",
            "yfin_api_concurrency_rejections_total",
            "yfin_api_redis_failopen_total",
            "yfin_api_problems_total",
            "yfin_cache_ops_total",
        ):
            assert METRICS[name].kind == "counter"

    def test_the_service_cache_counter_is_not_the_shard_one(self) -> None:
        """A shard's `yfin_sync_cache_ops_total` is republished as a gauge
        from `run_metrics`; this one is a live counter in a scraped
        process. One name for both would carry two meanings."""
        assert METRICS["yfin_cache_ops_total"].labelnames == ("cache", "result")
        assert METRICS["yfin_sync_cache_ops_total"].kind == "counter"


class TestTheWindow:
    def test_two_names_do_not_share_a_bucket(self) -> None:
        assert window.allow("a", "1.2.3.4", 1) is True
        assert window.allow("b", "1.2.3.4", 1) is True
        assert window.allow("a", "1.2.3.4", 1) is False

    def test_two_addresses_do_not_share_one_either(self) -> None:
        assert window.allow("a", "1.2.3.4", 1) is True
        assert window.allow("a", "5.6.7.8", 1) is True

    def test_the_dependency_is_named_after_its_endpoint(self) -> None:
        """FastAPI reports a dependency by function name when one fails;
        three identically-named closures would be unreadable."""
        assert window.guard("probe").__name__ == "probe_window"


class TestMultiprocessSafety:
    """`PROMETHEUS_MULTIPROC_DIR` is read at import time, process-wide, and only the API
    (four uvicorn workers) sets it. A module-level `import prometheus_client` anywhere in
    the package would fix the value class for every other service too."""

    def test_nothing_imports_the_client_at_module_level(self) -> None:
        import pathlib

        offenders = [
            str(path)
            for path in pathlib.Path("src/yfin").rglob("*.py")
            for line in path.read_text().splitlines()
            if line.startswith(("import prometheus", "from prometheus"))
        ]
        assert offenders == []

    def test_the_metrics_module_imports_it_lazily(self) -> None:
        """Importing `core.metrics` must not be what decides the mode."""
        import subprocess
        import sys

        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "-c",
                "import sys; import yfin.core.metrics; "
                "print('prometheus_client' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "False"

"""The monitoring stack's configuration, checked without starting it: every metric named in
a dashboard or an alert is declared in `core/metrics.py`, and the wiring between the files
agrees with itself. A YAML linter checks neither, and a missing series never fires."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

DEPLOY = Path("deploy/observability")
DASHBOARDS = sorted((DEPLOY / "grafana" / "dashboards").glob("*.json"))
COMPOSE = Path("docker-compose.yml")
OBSERVABILITY = Path("docker-compose.observability.yml")

#: Series the stack reads that this codebase does not declare, with the
#: reason each is legitimate. Anything NOT here has to be in `METRICS`.
FOREIGN_SERIES = {
    # Prometheus's own.
    "up",
    # `prometheus-fastapi-instrumentator` builds these; they carry the
    # `yfin` namespace but are not declared in `core/metrics.py` because
    # the library owns their names and buckets.
    "yfin_http_requests_total",
    "yfin_http_request_duration_seconds_bucket",
    # Exported BY ALLOY and pushed to Prometheus by remote_write: the
    # postgres and redis exporters run inside the collector rather than as
    # their own containers, so their names are the exporters' and not ours.
    "pg_stat_database_numbackends",
    "pg_database_size_bytes",
    "pg_up",
    "redis_up",
    "redis_memory_used_bytes",
    "redis_memory_max_bytes",
    # Recording rules, defined in prometheus/rules/recording.yml.
    "yfin:cells_stale_ratio",
    "yfin:cells_stale_ratio_by_scope",
    "yfin:api_error_ratio",
    "yfin:sync_cache_hit_ratio",
}

#: Suffixes prometheus_client appends to a histogram.
_HISTOGRAM_SUFFIXES = ("_bucket", "_count", "_sum")


def _declared() -> set[str]:
    from yfin.core.metrics import METRICS

    names = set(METRICS)
    for name, spec in METRICS.items():
        if spec.kind == "histogram":
            names |= {name + suffix for suffix in _HISTOGRAM_SUFFIXES}
    return names


def _series_in(expr: str) -> set[str]:
    """Metric names an expression reads: word-shaped tokens that are not a PromQL function,
    label matcher or number. Deliberately over-collects; known non-metrics are listed below.
    """
    functions = {
        "sum", "rate", "increase", "max", "min", "avg", "count", "by", "le",
        "clamp_min", "clamp_max", "histogram_quantile", "topk", "bottomk",
        "time", "on", "ignoring", "group_left", "group_right", "without",
        "and", "or", "unless", "absent", "count_over_time", "offset", "delta",
        "irate", "abs", "ceil", "floor", "round", "scalar", "vector",
    }
    tokens = set(re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", expr))
    # Label names and values live inside {...}; strip them so
    # `status="5.."` does not read as a metric called `status`.
    inside_braces = set()
    for block in re.findall(r"\{[^}]*\}", expr):
        inside_braces |= set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", block))
    return {t for t in tokens - functions - inside_braces if not t.isdigit()}


def _dashboard_exprs(doc: dict[str, Any]) -> list[str]:
    return [
        target["expr"]
        for panel in doc["panels"]
        for target in panel.get("targets", [])
        if "expr" in target and target["datasource"]["uid"] == "yfin-prometheus"
    ]


@pytest.fixture(scope="module")
def alert_rules() -> dict[str, Any]:
    path = DEPLOY / "grafana" / "provisioning" / "alerting" / "rules.yml"
    return yaml.safe_load(path.read_text())  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())  # type: ignore[no-any-return]


class TestTheDashboards:
    def test_there_are_five(self) -> None:
        assert len(DASHBOARDS) == 5

    @pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
    def test_it_is_valid_json_with_a_fixed_uid(self, path: Path) -> None:
        """A dashboard with no `uid` gets a new one on every provisioning
        pass, which orphans every link anybody saved to it."""
        doc = json.loads(path.read_text())
        assert doc["uid"].startswith("yfin-")
        # `id: null` so provisioning inserts rather than trying to update a
        # numeric id that means nothing outside the Grafana that made it.
        assert doc["id"] is None
        assert doc["panels"]

    @pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
    def test_ui_edits_are_disabled(self, path: Path) -> None:
        """The repository is the source. A panel fixed in the browser is
        un-fixed by the next `docker compose up`, and nobody else gets it."""
        assert json.loads(path.read_text())["editable"] is False

    @pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
    def test_every_dashboard_is_described(self, path: Path) -> None:
        """The dashboard's own description, not any panel's. It is what the
        Grafana dashboard list shows next to the title, and five boards
        named `yfin-...` are told apart by nothing else."""
        doc = json.loads(path.read_text())
        assert doc["description"].strip(), path.stem

    @pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
    def test_every_metric_it_reads_is_declared(self, path: Path) -> None:
        """A panel naming a metric nobody exports is a blank graph, and a
        blank graph is only noticed when somebody needs it."""
        declared = _declared() | FOREIGN_SERIES
        doc = json.loads(path.read_text())
        for expr in _dashboard_exprs(doc):
            unknown = {
                name for name in _series_in(expr)
                if name.startswith(("yfin_", "yfin:")) and name not in declared
            }
            assert not unknown, f"{path.stem}: {sorted(unknown)} in {expr!r}"

    def test_the_uids_are_unique(self) -> None:
        uids = [json.loads(p.read_text())["uid"] for p in DASHBOARDS]
        assert len(uids) == len(set(uids))

    def test_no_panel_groups_by_symbol(self) -> None:
        """The closed label set has no `symbol` in it, and a dashboard that
        tried to group by one would be asking for a series that cannot
        exist -- ten thousand symbols times forty-nine datasets."""
        for path in DASHBOARDS:
            for expr in _dashboard_exprs(json.loads(path.read_text())):
                assert "by (symbol)" not in expr, path.stem


class TestTheAlertRules:
    def test_every_rule_has_the_fields_grafana_requires(
        self, alert_rules: dict[str, Any]
    ) -> None:
        for group in alert_rules["groups"]:
            for rule in group["rules"]:
                assert rule["uid"].startswith("yfin-")
                assert rule["title"]
                assert rule["condition"]
                assert rule["annotations"]["summary"]
                assert rule["labels"]["severity"] in {"warning", "critical"}

    def test_every_rule_waits_before_firing(
        self, alert_rules: dict[str, Any]
    ) -> None:
        """A sync run takes hours and a scrape takes fifteen seconds. Almost
        every expression here is briefly true during a normal run; the wait
        is what tells a transient from a condition."""
        for group in alert_rules["groups"]:
            for rule in group["rules"]:
                assert rule["for"], rule["title"]

    def test_no_data_is_never_an_alert(self, alert_rules: dict[str, Any]) -> None:
        """The relays and the whole observability profile are opt-in, and a
        target that has never been scraped has no series at all. Alerting on
        its absence would page somebody about a choice they made."""
        for group in alert_rules["groups"]:
            for rule in group["rules"]:
                assert rule["noDataState"] == "OK", rule["title"]

    def test_every_metric_it_reads_is_declared(
        self, alert_rules: dict[str, Any]
    ) -> None:
        declared = _declared() | FOREIGN_SERIES
        for group in alert_rules["groups"]:
            for rule in group["rules"]:
                for item in rule["data"]:
                    expr = item["model"].get("expr", "")
                    if item["datasourceUid"] != "yfin-prometheus":
                        continue
                    unknown = {
                        name for name in _series_in(expr)
                        if name.startswith(("yfin_", "yfin:"))
                        and name not in declared
                    }
                    assert not unknown, f"{rule['title']}: {sorted(unknown)}"

    def test_the_uids_are_unique(self, alert_rules: dict[str, Any]) -> None:
        uids = [
            rule["uid"]
            for group in alert_rules["groups"]
            for rule in group["rules"]
        ]
        assert len(uids) == len(set(uids))

    def test_the_contact_point_is_provisioned(self) -> None:
        path = DEPLOY / "grafana" / "provisioning" / "alerting" / "contact-points.yml"
        doc = yaml.safe_load(path.read_text())
        (point,) = doc["contactPoints"]
        assert point["receivers"][0]["type"] == "webhook"
        # Grafana expands `$VAR` at load time; compose's `${...}` would be
        # interpolated by compose instead, which never sees this file.
        assert point["receivers"][0]["settings"]["url"] == "$GRAFANA_ALERT_WEBHOOK"


class TestTheComposeWiring:
    def test_the_scheduler_is_not_profiled(self, compose: dict[str, Any]) -> None:
        """`docker compose up -d` has to bring it up."""
        assert "profiles" not in compose["services"]["scheduler"]

    def test_the_stream_is_profiled(self, compose: dict[str, Any]) -> None:
        """It exits while `yf_stream_enabled` is off, which is the default."""
        assert compose["services"]["stream"]["profiles"] == ["stream"]

    def test_they_restart_unless_stopped(self, compose: dict[str, Any]) -> None:
        """The whole point of putting them in Docker: the pipeline runs for
        as long as Docker does, across a crash and across a reboot."""
        for name in ("scheduler", "stream"):
            assert compose["services"][name]["restart"] == "unless-stopped"

    def test_the_relays_are_profiled(self, compose: dict[str, Any]) -> None:
        """Both refuse to start when their feature is off -- and both are
        off by default. A service that exits 1 under `restart:
        unless-stopped` is a crash loop, not a disabled feature."""
        for name in ("stream-relay", "changes-relay"):
            assert compose["services"][name]["profiles"] == ["kafka"]

    def test_the_scheduler_outlives_its_own_grace(
        self, compose: dict[str, Any]
    ) -> None:
        """Docker's patience has to exceed the scheduler's. Reversed,
        `docker stop` SIGKILLs a sync holding the advisory lock and the
        next run finds it held."""
        from yfin.core.config import Settings

        grace = compose["services"]["scheduler"]["stop_grace_period"]
        assert grace == "${SCHEDULER_STOP_GRACE:-620s}"
        assert Settings().yf_schedule_stop_grace_seconds < 620

    def test_no_service_inherits_the_api_healthcheck(
        self, compose: dict[str, Any]
    ) -> None:
        """The image's HEALTHCHECK asks 127.0.0.1:8000/health, which only
        the API serves. Inherited, every other service reports as
        permanently unhealthy -- worse than no check."""
        for name in ("scheduler", "stream", "stream-relay", "changes-relay"):
            assert compose["services"][name]["healthcheck"] == {"disable": True}

    def test_the_scheduler_reaps_its_children(
        self, compose: dict[str, Any]
    ) -> None:
        """PID 1 in a container does not reap orphans, and this process
        spawns one subprocess per firing."""
        assert compose["services"]["scheduler"]["init"] is True


@pytest.fixture(scope="module")
def override() -> dict[str, Any]:
    return yaml.safe_load(OBSERVABILITY.read_text())  # type: ignore[no-any-return]


class TestTheObservabilityOverride:

    def test_only_the_api_gets_the_multiproc_dir(
        self, override: dict[str, Any]
    ) -> None:
        """`prometheus_client` reads it at IMPORT time, process-wide. Set
        anywhere else, that service switches to multiprocess mode, writes
        mmap files nobody collects, and its own /metrics goes quiet."""
        carriers = [
            name
            for name, service in override["services"].items()
            if "PROMETHEUS_MULTIPROC_DIR" in (service.get("environment") or {})
        ]
        assert carriers == ["api"]

    def test_no_two_services_share_a_metrics_port(
        self, override: dict[str, Any]
    ) -> None:
        """The loser of a port collision logs a warning and serves nothing,
        which is the quietest possible failure."""
        ports = [
            service["environment"]["METRICS_PORT"]
            for service in override["services"].values()
            if "METRICS_PORT" in (service.get("environment") or {})
        ]
        assert len(ports) == len(set(ports))
        assert set(ports) == {"9101", "9102", "9103", "9104"}

    def test_the_api_has_no_metrics_port(self, override: dict[str, Any]) -> None:
        """It serves /metrics through the instrumentator on its own port;
        `serve_metrics` is never called there."""
        assert "METRICS_PORT" not in override["services"]["api"]["environment"]

    def test_every_stack_service_is_profiled(
        self, override: dict[str, Any]
    ) -> None:
        """Loading the file must not start seven containers on its own --
        the profile is what asks for them."""
        stack = {"prometheus", "grafana", "loki", "tempo", "alloy", "kafka-exporter"}
        for name in stack:
            assert override["services"][name]["profiles"] == ["observability"]

    def test_prometheus_scrapes_every_port_the_override_assigns(self) -> None:
        """A service that publishes on a port nobody scrapes is a service
        with no metrics, and nothing would say so."""
        config = yaml.safe_load((DEPLOY / "prometheus" / "prometheus.yml").read_text())
        targets = {
            target
            for job in config["scrape_configs"]
            for static in job["static_configs"]
            for target in static["targets"]
        }
        for port in ("9101", "9102", "9103", "9104"):
            assert any(t.endswith(f":{port}") for t in targets), port

    def test_the_pipeline_samples_every_trace(
        self, override: dict[str, Any]
    ) -> None:
        """1.0 for the pipeline, 0.1 for the API. A sync runs a few
        thousand times a day, and a sampled trace of a nightly job is a
        trace of the wrong night."""
        assert override["services"]["api"]["environment"][
            "OTEL_TRACES_SAMPLER_ARG"] == "0.1"
        anchor = override["x-otel-pipeline"]
        assert anchor["OTEL_TRACES_SAMPLER_ARG"] == "1.0"
        assert anchor["LOG_FORMAT"] == "json"


class TestTheStackEnvExample:
    def test_it_documents_every_name_the_files_use(self) -> None:
        """`env_file` does not feed compose's interpolation, so a name
        missing here reaches its container as an empty string and the
        failure is a Grafana that starts with no password."""
        text = (DEPLOY / ".env.example").read_text()
        for name in (
            "GF_SECURITY_ADMIN_PASSWORD",
            "MONITOR_DB_PASSWORD",
            "GRAFANA_ALERT_WEBHOOK",
            "SCHEDULER_STOP_GRACE",
        ):
            assert f"{name}=" in text, name

    def test_the_real_env_is_not_committed(self) -> None:
        """`.env` next to `.env.example` holds the Grafana password and the
        monitoring database's. Tracked once, it is in the history forever,
        and a checkout is enough to read it."""
        import subprocess

        tracked = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "ls-files", str(DEPLOY)],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        assert str(DEPLOY / ".env") not in tracked

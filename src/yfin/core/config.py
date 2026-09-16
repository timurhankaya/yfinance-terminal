"""Application configuration.

Two layers: `.env` + model default (pydantic-settings), and the `settings` table as
a DB override for the fields declared with `_cfg`. Init kwargs beat env values in
pydantic, so `CLI flag > table > .env > default` falls out without extra logic."""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

from yfin.core.logging_setup import configure_logging, get_logger

log = get_logger(__name__)

# Not a `Settings` field, and must not be: the switch that turns off the DB
# layer can't itself be read from the DB layer (chicken-and-egg). Read via
# `os.getenv`.
SETTINGS_SOURCE_VAR = "YF_SETTINGS_SOURCE"

# The 14 groups the admin panel organizes by. Adding a new group name is a
# deliberate decision; `tests/unit/test_settings_split.py` rejects unknown
# groups.
SETTING_GROUPS: tuple[str, ...] = (
    "client",
    "runner",
    "shard",
    "proxy",
    "symbols",
    "datasets",
    "market",
    "domain",
    "discovery",
    "bars",
    "maintenance",
    "stream",
    "scheduler",
    "monitoring",
)

#: Every setting whose value is a cron expression. Listed once, so the
#: validator and any future reader cannot disagree about which they are.
_SCHEDULE_FIELDS = (
    "yf_schedule_sync",
    "yf_schedule_market",
    "yf_schedule_domain",
    "yf_schedule_stream_reconcile",
    "yf_schedule_bars_maintain",
    "yf_schedule_prune",
    "yf_schedule_usage_flush",
)


def _cfg(group: str, description: str, **kwargs: Any) -> Any:
    """`Field` plus panel metadata.

    `group` goes into `json_schema_extra` because `FieldInfo` rejects arbitrary
    keys; keeping it on the field line keeps it from drifting."""
    return Field(description=description, json_schema_extra={"group": group}, **kwargs)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "yfin"
    db_password: str = ""
    db_name: str = "yfinance"
    # A SEPARATE DATABASE, not just a schema: keeps live data physically apart
    # from test data. Per-process test SCHEMAS open inside this database.
    db_test_name: str = "yfinance_test"

    yf_rate_limit_per_sec: float = _cfg(
        "client", "Request rate cap per second (token bucket).", default=2.0, gt=0
    )
    yf_max_workers: int = _cfg(
        "runner", "Number of concurrent symbol worker threads.", default=4, ge=1
    )
    yf_queue_maxsize: int = _cfg(
        "runner", "Symbol queue cap (backpressure).", default=8, ge=1
    )
    yf_retry_attempts: int = _cfg(
        "client", "Total retry attempts on a transient error.", default=5, ge=1
    )
    yf_retry_initial_sec: float = _cfg(
        "client", "Initial wait for exponential backoff (seconds).", default=1.0, ge=0
    )
    yf_retry_max_sec: float = _cfg(
        "client", "Backoff wait cap (seconds).", default=16.0, ge=0
    )
    # Each expiry after the first is one more request per symbol, so "all"
    # would multiply a run by the expiry count.
    yf_option_expiries: int = _cfg(
        "datasets", "Expiries fetched per symbol; each one is a request.", default=4, ge=1, le=24
    )

    yf_news_count: int = _cfg(
        "datasets", "Number of news items to fetch per symbol.", default=50, ge=1
    )
    yf_news_tab: str = _cfg(
        "datasets", "Yahoo news tab (all / news / press releases).", default="all"
    )
    yf_incremental_overlap_days: int = _cfg(
        "symbols", "Lookback overlap window for incremental fetches (days).", default=7, ge=0
    )
    yf_delist_threshold: int = _cfg(
        "symbols",
        "Consecutive unknown_symbol count that sets is_active=0 (unknown_streak threshold).",
        default=5,
        ge=1,
    )

    # --- sharded runs (proxy pool) ------------------------------------
    yf_max_shards: int = _cfg(
        "shard", "Max concurrent shards (one process per proxy).", default=4, ge=1
    )
    yf_shard_timeout_seconds: int = _cfg(
        "shard",
        "Cap on a single shard process's completion time (seconds).",
        default=3600,
        ge=60,
    )
    yf_txn_retry_attempts: int = _cfg(
        "runner", "Transaction retry attempts on deadlock/lock timeout.", default=3, ge=1
    )

    # --- proxy policy ---------------------------------------------------
    yf_proxy_cooldown_seconds: int = _cfg(
        "proxy", "Rest period for a proxy that exceeds the failure threshold (seconds).",
        default=900, ge=1
    )
    yf_proxy_failure_threshold: int = _cfg(
        "proxy", "Consecutive failures that trigger cooldown.", default=3, ge=1
    )
    yf_proxy_dead_rounds: int = _cfg(
        "proxy", "Consecutive cooldown rounds after which a proxy is marked dead.",
        default=3, ge=1
    )
    yf_proxy_check_timeout: float = _cfg(
        "proxy", "Proxy health-check request timeout (seconds).", default=10.0, gt=0
    )
    # Fernet key; empty means no password-protected proxy can be added (the
    # password never enters code or DB as plaintext).
    yf_proxy_secret_key: str = ""

    # --- yfinance advanced ----------------------------------------------
    # yfinance's tz/cookie/ISIN cache is SQLite; each shard gets its own
    # directory, otherwise a second writer hits SQLITE_BUSY.
    yf_tz_cache_dir: str = _cfg(
        "datasets", "Root directory for yfinance's tz/cookie/ISIN cache.",
        default=".cache/yfinance"
    )
    yf_history_repair: bool = _cfg(
        "datasets", "history(repair=True): fix split/currency errors.", default=True
    )

    yf_earnings_dates_max_pages: int = _cfg(
        "datasets", "Page cap for earnings_dates.", default=3, ge=1
    )
    yf_market_regions: str = _cfg(
        "market",
        "Market summary regions (comma-separated).",
        default="US,EUROPE,ASIA,GB,CURRENCIES,CRYPTOCURRENCIES,COMMODITIES,RATES",
    )
    yf_calendar_lookback_days: int = _cfg(
        "market", "Calendar window lookback (days).", default=7, ge=0
    )
    yf_calendar_lookahead_days: int = _cfg(
        "market", "Calendar window lookahead (days).", default=30, ge=0
    )
    yf_calendar_page_limit: int = _cfg(
        "market", "Calendar page size; Yahoo caps at 100.", default=100, ge=1, le=100
    )
    yf_calendar_max_pages: int = _cfg(
        "market", "Page cap for calendar.", default=5, ge=1
    )

    # --- sector / industry ------------------------------------------------
    # ISO 3166-1 alpha-2, comma-separated. THE FIRST IS PRIMARY: region-less
    # datasets use its response, and the validation probe uses it as baseline.
    yf_domain_regions: str = _cfg(
        "domain", "Sector/industry regions (comma-separated, FIRST IS PRIMARY).", default="US"
    )
    # Reference sector for the region validation probe.
    yf_domain_reference_sector: str = _cfg(
        "domain", "Reference sector for the region validation probe.", default="technology"
    )

    # --- discovery: Search / Lookup / Screener ----------------------------
    yf_search_max_results: int = _cfg(
        "discovery", "Search: number of quotes returned.", default=10, ge=1
    )
    yf_search_news_count: int = _cfg(
        "discovery", "Search: number of news items returned.", default=5, ge=0
    )
    yf_search_lists_count: int = _cfg(
        "discovery", "Search: number of lists returned.", default=10, ge=0
    )
    # Yahoo caps `all` documents at roughly this; asking for fewer truncates
    # even narrow terms.
    yf_lookup_count: int = _cfg(
        "discovery", "Lookup: documents requested per call.", default=1000, ge=1
    )
    # Above this `lookupTotals.all`, the `all` call is truncated and the code
    # falls back to the per-type branch; kept below Yahoo's `all` cap.
    yf_lookup_all_threshold: int = _cfg(
        "discovery", "Above this, fall back to the per-type branch instead of `all`.",
        default=500, ge=1
    )
    # Yahoo's cap is 250; exceeding it makes `yf.screen` raise ValueError.
    # le=250 turns that into a CONFIGURATION error caught when `Settings`
    # loads, not mid-run.
    yf_screen_size: int = _cfg(
        "discovery", "Screener page size; Yahoo caps at 250.", default=250, ge=1, le=250
    )
    # A screen hitting the cap shows as a gap between `screen_runs.total` and
    # `fetched_rows`.
    yf_screen_max_pages: int = _cfg(
        "discovery", "Page cap per screen.", default=4, ge=1
    )
    # Empty = all screens in `screens.py` (except those disabled in the DB).
    yf_screen_keys: str = _cfg(
        "discovery", "Screen keys to run (comma-separated; empty = all).", default=""
    )

    # --- price_bars ---------------------------------------------------------
    # Extended bars are not limited to US stocks, and turning this off means
    # PERMANENTLY losing them.
    yf_bar_prepost: bool = _cfg(
        "bars",
        "Also write extended-hours bars (turning this off loses them permanently).",
        default=True,
    )
    # The overlap is idempotent; the cost is redundant upserts, the benefit is
    # not missing a bar at the session boundary.
    yf_bar_overlap_days: int = _cfg(
        "bars", "Lookback overlap for the incremental bar window (days).", default=2, ge=0
    )

    # Pruning defaults to OFF: calendar and _history rows can't be re-fetched,
    # so deletion never runs unless explicitly enabled.
    yf_prune_enabled: bool = _cfg(
        "maintenance", "Master switch for pruning; defaults to OFF.", default=False
    )

    # --- live WebSocket stream --------------------------------------------
    #
    # None of these is a secret, so they all live in the settings table
    # alongside yf_max_shards rather than in a separate BaseSettings the
    # way the API's signing key does.

    yf_stream_enabled: bool = _cfg(
        "stream", "Master switch for the live tick stream; defaults to OFF.", default=False
    )
    # Yahoo silently discards symbols past 100 per connection; 95 leaves room
    # for the canary and a scope edit landing mid-rebalance.
    yf_stream_max_symbols_per_connection: int = _cfg(
        "stream",
        "Symbols per upstream connection. Yahoo's hard limit is 100 including the canary.",
        default=95,
        ge=1,
        le=99,
    )
    # Not a safety valve but a capacity: 10,000 symbols need ~106
    # connections. Exceeding it raises rather than dropping symbols.
    yf_stream_max_connections: int = _cfg(
        "stream", "Ceiling on upstream connections; exceeding it is an error.",
        default=256, ge=1,
    )
    yf_stream_canary_symbols: str = _cfg(
        "stream",
        "Comma-separated 24/7 symbols appended to every subscription to detect silence.",
        default="BTC-USD",
    )
    # ~40 seconds of the projected load. Deeper buffers do not help: a
    # writer that is minutes behind has a problem no queue depth fixes.
    yf_stream_queue_maxsize: int = _cfg(
        "stream", "Bounded tick queue; overflow drops ticks and counts them.",
        default=10_000, ge=100,
    )
    # A smaller batch keeps latency down and narrows what a crash can lose.
    yf_stream_batch_size: int = _cfg(
        "stream", "Rows per write batch.", default=500, ge=1
    )
    yf_stream_batch_interval_ms: int = _cfg(
        "stream", "Flush a partial batch after this long.", default=250, ge=10
    )
    # live_quotes is the most expensive part of the batch and a derived view;
    # a few hundred ms of staleness costs nothing.
    yf_stream_quotes_every_n_batches: int = _cfg(
        "stream", "Write live_quotes every Nth batch.", default=4, ge=1
    )
    yf_stream_idle_timeout_seconds: int = _cfg(
        "stream", "Reconnect a connection that has gone silent this long.",
        default=300, ge=10,
    )
    yf_stream_reconnect_max_seconds: float = _cfg(
        "stream", "Backoff ceiling for reconnects.", default=60.0, gt=0
    )
    yf_stream_archive_default: bool = _cfg(
        "stream", "Whether new scope rows archive ticks by default.", default=True
    )
    yf_stream_reject_sample_per_hour: int = _cfg(
        "stream", "Reject rows kept per (symbol, reason) per hour; counts are never sampled.",
        default=100, ge=0,
    )

    # --- browser publish path (Redis pub/sub) ------------------------------
    # At-most-once fan-out to open browser tabs, unlike the durable Kafka path:
    # losing a message costs one repainted price; the archive is already committed.
    yf_stream_publish_enabled: bool = _cfg(
        "stream", "Publish committed ticks to Redis for the web terminal.", default=False
    )
    # Env-only, unlike every other stream setting: a Redis URL carries its
    # password in the string, and the settings table stores values in clear
    # text (only proxy secrets get Fernet). Empty means publishing is off
    # whatever the switch above says -- there is nowhere to publish to.
    yf_stream_publish_redis_url: str = ""

    # --- optional Kafka publish path ---------------------------------------
    #
    # Off by default, and off means nothing is written: with this false the
    # outbox stays empty, so the second write path costs exactly zero.
    yf_kafka_enabled: bool = _cfg(
        "stream", "Publish ticks to Kafka through the transactional outbox.", default=False
    )
    yf_kafka_bootstrap_servers: str = _cfg(
        "stream", "Kafka bootstrap servers, comma separated.", default=""
    )
    # Topic per exchange, partition key per symbol: that is what gives
    # per-symbol ordering. A topic per symbol would be thousands of topics
    # and would take the broker's metadata down with it.
    yf_kafka_topic_pattern: str = _cfg(
        "stream", "Topic name pattern; {exchange} is substituted.",
        default="yfin.ticks.{exchange}",
    )
    yf_kafka_relay_batch: int = _cfg(
        "stream", "Outbox rows read per relay pass.", default=1000, ge=1
    )

    # Publishing PIPELINE writes, as opposed to ticks. Off means no collector
    # is created: no predicate, no `RETURNING *`, no outbox row.
    yf_changes_enabled: bool = _cfg(
        "stream",
        "Publish pipeline row changes to Kafka through the pipeline outbox.",
        default=False,
    )
    # Seven topics, one per DataFamily, lining up with the seven
    # `<family>:read` scopes so a consumer's ACL is one line per family.
    yf_changes_topic_pattern: str = _cfg(
        "stream",
        "Change topic name pattern; {family} is substituted.",
        default="yfin.changes.{family}",
    )
    # Above this many inserted rows, a write to a bars table publishes one span
    # instead of one event per bar; a first sync crosses it, daily writes do not.
    yf_changes_range_threshold: int = _cfg(
        "stream",
        "Bar inserts above this count publish as one range event.",
        default=1000,
        ge=1,
    )

    # --- scheduler ---------------------------------------------------------
    # Job definitions are SETTINGS: an operator retimes a run with `yfin config
    # set` and the scheduler reloads it. The set of jobs is fixed in code, so
    # what is configurable is when, not what. An empty expression unregisters the job.
    yf_schedule_sync: str = _cfg(
        "scheduler", "Cron for `yfin sync`. Empty disables the job.", default="0 2 * * *"
    )
    yf_schedule_market: str = _cfg(
        "scheduler", "Cron for `yfin market sync`.", default="30 1 * * *"
    )
    yf_schedule_domain: str = _cfg(
        "scheduler", "Cron for `yfin domain sync`.", default="0 3 * * 0"
    )
    yf_schedule_stream_reconcile: str = _cfg(
        "scheduler", "Cron for `yfin stream reconcile`.", default="15 * * * *"
    )
    yf_schedule_bars_maintain: str = _cfg(
        "scheduler", "Cron for `yfin bars maintain`.", default="0 4 1 * *"
    )
    yf_schedule_prune: str = _cfg(
        "scheduler",
        "Cron for `yfin prune`. Empty by default: pruning is irreversible.",
        default="",
    )
    yf_schedule_usage_flush: str = _cfg(
        "scheduler", "Cron for `yfin api usage flush`.", default="5 0 * * *"
    )
    yf_schedule_timezone: str = _cfg(
        "scheduler", "Timezone every cron expression is read in.", default="UTC"
    )
    # The EFFECTIVE grace per job is min(cadence / 2, this), so an hourly
    # job does not accept a firing fifty minutes late.
    yf_schedule_misfire_grace_seconds: int = _cfg(
        "scheduler",
        "How late a firing may still run before it counts as missed.",
        default=3600,
        ge=0,
    )
    # Must stay BELOW compose's `stop_grace_period`, or Docker's own timeout
    # kills the scheduler before it can wait for a shard that holds the sync
    # advisory lock.
    yf_schedule_stop_grace_seconds: int = _cfg(
        "scheduler",
        "How long SIGTERM waits for a running job before SIGKILL.",
        default=600,
        ge=0,
    )

    # --- monitoring --------------------------------------------------------
    yf_exporter_interval_seconds: int = _cfg(
        "monitoring",
        "How often the exporter refreshes its gauges from the database.",
        default=300,
        ge=10,
    )
    # A cell is stale when its last good write is older than this many times
    # the interval of the job that writes it. One factor, not a cadence per
    # family: the schedule already says how often each job runs.
    yf_freshness_factor: int = _cfg(
        "monitoring",
        "A cell is stale past this multiple of its job's interval.",
        default=2,
        ge=1,
    )
    yf_intraday_retention_warn_days: int = _cfg(
        "monitoring",
        "Warn when an intraday gap is within this many days of Yahoo's limit.",
        default=3,
        ge=0,
    )

    yf_stream_rescan_seconds: int = _cfg(
        "stream", "How often scope and settings are re-read while running.",
        default=60, ge=5,
    )

    log_level: str = "INFO"
    # `console` on a TTY, `json` otherwise. Env-only for the same reason as
    # log_level: the format is decided before the first line, which is
    # before any database exists.
    log_format: str = ""
    # 0 = off, which is the default for every process. The observability
    # compose override gives each service its own port; one value in the
    # settings table would bind five services to the same one.
    metrics_port: int = 0

    @field_validator(*_SCHEDULE_FIELDS)
    @classmethod
    def _validate_cron(cls, value: str) -> str:
        """Rejects a bad cron expression where the operator can see it.

        A validator rather than a scheduler check so `yfin config set` fails at
        the command, not at the next reload. APScheduler is imported lazily: a
        module-level import would make the whole CLI depend on `[scheduler]`."""
        expression = value.strip()
        if not expression:
            # Empty is how a job is switched off, and `prune` ships that way.
            return expression
        try:
            from apscheduler.triggers.cron import CronTrigger
        except ImportError:
            if len(expression.split()) != 5:
                raise ValueError(
                    "a cron expression has five fields "
                    "(minute hour day month day-of-week); "
                    f"got {len(expression.split())} in {expression!r}"
                ) from None
            return expression
        try:
            CronTrigger.from_crontab(expression)
        except Exception as exc:
            raise ValueError(f"invalid cron expression {expression!r}: {exc}") from None
        return expression


    def db_url(self, database: str | None = None) -> URL:
        """SQLAlchemy URL object; credentials are never embedded in a string."""
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=database if database is not None else self.db_name,
        )

    def bootstrap_url(self) -> URL:
        """Maintenance-database (`postgres`) connection, for `CREATE DATABASE` only.

        `information_schema` and `pg_namespace` are database-scoped, so schema
        creation and cleanup must use `db_url(db_test_name)` instead."""
        return self.db_url(database="postgres")


# --- field split ------------------------------------------------------------

ENV_ONLY_FIELDS = frozenset(
    {
        # Bootstrap paradox: the value that opens the connection can't live
        # behind that connection.
        "db_host",
        "db_port",
        "db_user",
        "db_password",
        "db_name",
        "db_test_name",
        # Putting the Fernet key next to the proxy passwords it encrypts
        # would defeat the point of proxy/crypto.py.
        "yf_proxy_secret_key",
        # A Redis URL carries its password inside the string. The settings
        # table stores values in clear text, so this one stays in env with
        # db_password -- the switch that turns publishing on is DB-managed,
        # the credential is not.
        "yf_stream_publish_redis_url",
        # configure_logging() is called before create_db_engine() (shard.py).
        # The log ordering also relies on this field being in env: the
        # loader's warnings -- including the security boundary one -- must
        # land in the configured logger.
        "log_level",
        # Same ordering argument as log_level: the format has to be decided
        # before the first log line, which is before any database exists.
        "log_format",
        # Cannot be a database setting: one value would bind five services
        # to one port. Each service is given its own in the compose
        # override, and 0 means off, which is the default everywhere else.
        "metrics_port",
    }
)

# Derived as the COMPLEMENT: any new `Settings` field automatically becomes
# DB-managed. Writing the list by hand would let a new field silently join
# neither set. The fail-open risk of deriving it is guarded by four checks in
# `tests/unit/test_settings_split.py` (consumability, metadata, secret-name
# pattern, scalar-ness).
DB_MANAGED_FIELDS = frozenset(Settings.model_fields) - ENV_ONLY_FIELDS


_settings: Settings | None = None
# Raw overrides the loader APPLIED. The shard parent carries these to the
# child via `ShardSpec`; the child never re-reads them.
_overrides: dict[str, str] = {}
# Two worker threads must not produce two separate `Settings` objects: test
# patches rely on object identity.
_lock = threading.Lock()


def source_is_env() -> bool:
    """`YF_SETTINGS_SOURCE=env` means the DB layer is never read.

    Any other non-empty value logs a WARNING: a recovery switch silently doing
    nothing because of a typo would leave the operator believing it was off."""
    raw = os.getenv(SETTINGS_SOURCE_VAR)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value == "env":
        return True
    if value:
        log.warning(
            "unrecognised YF_SETTINGS_SOURCE value; the DB layer stays ON",
            value=raw,
            expected="env",
        )
    return False


def bootstrap_settings() -> Settings:
    """A `Settings` instance that never touches the DB layer.

    For the loader itself, commands that run before the database exists, and
    `seed --adopt-env`, which must not read back the rows it just wrote."""
    return Settings()


def _load() -> Settings:
    global _overrides
    _overrides = {}
    env_only = bootstrap_settings()

    # FIRST: `log_level` and `log_format` are ENV_ONLY, so both are ready
    # here -- which is the reason they are. Anything logged before this
    # goes out through whatever handler stdlib logging fell back to, and
    # the redact_credentials processor is not in that path.
    configure_logging(env_only.log_level, env_only.log_format)

    if source_is_env():
        return env_only

    # LOCAL import: settings_store -> db/models -> config would otherwise
    # cycle. The codebase's pattern for this case is a local import (see
    # prune.py).
    from yfin.storage.settings_store import load_overrides

    overrides = load_overrides(env_only)
    if not overrides:
        return env_only
    _overrides = overrides
    # Init kwargs override env in pydantic, so precedence (DB > env >
    # default) falls out for free. An invalid value raises ValidationError
    # here and the run never starts -- a silent fallback would ignore the
    # operator's intent.
    return settings_from_overrides(overrides)


@dataclass(frozen=True)
class FieldSchema:
    """Everything the admin panel needs to draw the form.

    `min` / `max` are derived from `Field` constraints, never written by hand,
    so the panel cannot validate against a stale range."""

    key: str
    group: str
    type: str
    default: Any
    min: float | None
    max: float | None
    description: str


def _bounds(metadata: list[Any]) -> tuple[float | None, float | None]:
    """(min, max) from `Field` constraints.

    `gt` / `lt` count as min/max too: same informational value for the panel
    (`gt=0` -> "greater than 0"), actual validation still stays in pydantic.
    """
    low: float | None = None
    high: float | None = None
    for item in metadata:
        for attr in ("ge", "gt"):
            value = getattr(item, attr, None)
            if value is not None:
                low = float(value)
        for attr in ("le", "lt"):
            value = getattr(item, attr, None)
            if value is not None:
                high = float(value)
    return low, high


def settings_schema() -> list[FieldSchema]:
    """Machine-readable schema of DB-managed fields. Pure: never touches the DB.

    Schema and state are separate: the schema is constant for the process
    lifetime, `value` can change on every read."""
    out: list[FieldSchema] = []
    for key in sorted(DB_MANAGED_FIELDS):
        info = Settings.model_fields[key]
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        low, high = _bounds(list(info.metadata))
        annotation = info.annotation
        out.append(
            FieldSchema(
                key=key,
                group=str(extra.get("group", "")),
                type=getattr(annotation, "__name__", str(annotation)),
                default=info.default,
                min=low,
                max=high,
                description=info.description or "",
            )
        )
    return out


def settings_from_overrides(overrides: Mapping[str, str]) -> Settings:
    """Build `Settings` from raw TEXT overrides.

    Both `.env` and the `settings` table hand back text and pydantic converts,
    so the `type: ignore` is funnelled through this one spot."""
    return Settings(**overrides)  # type: ignore[arg-type]


def get_settings() -> Settings:
    global _settings
    if _settings is None:  # fast path, no lock
        with _lock:
            if _settings is None:  # double-checked
                _settings = _load()
    return _settings


def applied_overrides() -> dict[str, str]:
    """Raw overrides the loader APPLIED (a copy).

    The `settings` table is never RE-READ: a running sync using one
    consistent snapshot makes cross-shard configuration skew impossible.
    """
    return dict(_overrides)


def install_settings(settings: Settings, overrides: Mapping[str, str] | None = None) -> None:
    """Install a resolved `Settings` into the process; the loader never runs again.

    Shard children get the parent's resolved value via `ShardSpec` and never
    read the DB: an intervening `yfin config set` would otherwise give shards
    different configuration. `overrides` is installed alongside for the same reason."""
    global _settings, _overrides
    with _lock:
        _settings = settings
        _overrides = dict(overrides or {})


def reset_settings() -> None:
    """Clear the singleton (for tests and the `yfin config` write path).

    Without a reset, repo tests would run against the previous test's singleton
    and `load_overrides` would never be called."""
    global _settings, _overrides
    with _lock:
        _settings = None
        _overrides = {}

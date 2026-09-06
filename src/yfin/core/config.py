"""Application configuration.

Two layers:

  1. `.env` + model default -- pydantic-settings' own resolution.
  2. `settings` TABLE -- a DB override for 38 fields.

Precedence `CLI flag > settings table > .env > model default` falls out
for free: passing init kwargs to `Settings(**overrides)` overrides pydantic's
env values (verified live). No separate precedence logic is written -- one
would risk silently diverging from pydantic's own.

Metadata (type / default / range / description / group) lives HERE, not in
the `settings` table: duplicating it would let `Field(ge=1)` drift to `ge=2`
while the table's copy went stale. The admin panel form is drawn from here
via `settings_schema()`.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

from yfin.core.logging_setup import configure_logging, get_logger

log = get_logger(__name__)

# Not a `Settings` field, and must not be: the switch that turns off the DB
# layer can't itself be read from the DB layer (chicken-and-egg). Read via
# `os.getenv`.
SETTINGS_SOURCE_VAR = "YF_SETTINGS_SOURCE"

# The 11 groups the admin panel organizes by. Adding a new group name is a
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
)


def _cfg(group: str, description: str, **kwargs: Any) -> Any:
    """`Field` plus panel metadata.

    `group` goes into `json_schema_extra` because pydantic's `FieldInfo`
    rejects arbitrary keys. A separate module-level dict would let the field
    and its group live in two places and drift apart; here it sits on the
    same line as the field definition.
    """
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
    # There is no YF_PROBE_SUSTAINABILITY key, deliberately removed. The
    # original spec left it DB-managed, but the same spec later moved the
    # dataset to `register(..., opt_in=True)`, after which nothing read the
    # flag. A setting with no effect, in a layer that exists purely to feed
    # an admin panel, is the worst kind of noise: a control that looks live
    # but does nothing. Same call as for `yf_discovery_enabled` and
    # YF_BAR_INTERVALS (YAGNI).
    #
    # The need it served is already met: the dataset is invisible in the
    # `all` expansion and runs when requested by name via
    # `--datasets sustainability`.
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
    #
    # TWO SETTINGS, NOT THREE. An early draft had a
    # `yf_domain_include_reports` flag; removed because nothing read it, and
    # reading it would have made the profile dataset's `produces` CONDITIONAL,
    # tying the audit's cell count to configuration. A user who doesn't want
    # reports can just ask for `--datasets sector_rankings,industry_rankings`.
    yf_domain_regions: str = _cfg(
        "domain", "Sector/industry regions (comma-separated, FIRST IS PRIMARY).", default="US"
    )
    # Reference sector for the region validation probe.
    yf_domain_reference_sector: str = _cfg(
        "domain", "Reference sector for the region validation probe.", default="technology"
    )

    # --- discovery: Search / Lookup / Screener ----------------------------
    # There is no YF_DISCOVERY_ENABLED key, deliberately removed. The
    # original design gated `search`/`lookup` registration behind a flag
    # (the `sustainability` pattern). In practice this had two flaws:
    #   1. With the flag off, `--datasets search` also didn't work -- the
    #      dataset wasn't in the registry at all.
    #   2. The moment the flag was turned on, a BARE `yfin sync` started
    #      pulling them too: +9,000 requests/day. So the flag didn't solve
    #      the problem, it only postponed it until the user flipped it.
    # Fixed by moving to the registry: `register(..., opt_in=True)` -- runs
    # when requested by name, never appears in the `all` expansion.
    yf_search_max_results: int = _cfg(
        "discovery", "Search: number of quotes returned.", default=10, ge=1
    )
    yf_search_news_count: int = _cfg(
        "discovery", "Search: number of news items returned.", default=5, ge=0
    )
    yf_search_lists_count: int = _cfg(
        "discovery", "Search: number of lists returned.", default=10, ge=0
    )
    # Measured document cap for `all` is ~1,000; requesting 1000 gets
    # everything up to that cap. Requesting 250 would truncate even narrow
    # terms (BTC: count=250 -> 248 docs, though total is 503).
    yf_lookup_count: int = _cfg(
        "discovery", "Lookup: documents requested per call.", default=1000, ge=1
    )
    # If `lookupTotals.all` exceeds this value, the `all` call was truncated
    # and the code falls back to the per-type branch. Measured: BTC 503 ->
    # `all` is the full set; GOLD 7,273 -> `all` returns only 995 docs, the
    # typed union returns 3,313. The threshold is kept BELOW the observed
    # `all` cap (~1,000) so the fallback triggers before truncation starts.
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
    # Page cap per screen. 19 predefined screens: unlimited is 49 requests,
    # cap=4 is 32. The most expensive screen is `most_shorted_stocks` (total
    # 4,022, 17 pages). A screen hitting the cap is visible from the gap
    # between `screen_runs.total` and `fetched_rows`.
    yf_screen_max_pages: int = _cfg(
        "discovery", "Page cap per screen.", default=4, ge=1
    )
    # Empty = all screens in `screens.py` (except those disabled in the DB).
    yf_screen_keys: str = _cfg(
        "discovery", "Screen keys to run (comma-separated; empty = all).", default=""
    )

    # --- price_bars ---------------------------------------------------------
    # There is no YF_BAR_INTERVALS key, deliberately not added. It was in the
    # spec; the audit found nothing reading it, so it was dropped (YAGNI).
    # Two reasons:
    #   1. It would create a second source of truth: "which intervals run"
    #      would be answered by both the registry alias and .env, and the
    #      two could silently diverge.
    #   2. The need it served is already met: a user can write
    #      `--datasets bars_5m,bars_15m` or use the `intraday` alias.
    # Whether to write extended-hours bars. Extended bars aren't limited to
    # US stocks -- SHEL.L and VWCE.DE report hasPrePostMarketData=False and
    # still return 5 and 8 extended bars (measured). Turning this off means
    # PERMANENTLY losing those bars.
    yf_bar_prepost: bool = _cfg(
        "bars",
        "Also write extended-hours bars (turning this off loses them permanently).",
        default=True,
    )
    # Lookback overlap in the incremental window. The overlap is idempotent
    # (measured: no value differences across 78 overlapping bars); the cost is
    # a few hundred redundant upserts, the benefit is not missing a bar at the
    # session boundary.
    yf_bar_overlap_days: int = _cfg(
        "bars", "Lookback overlap for the incremental bar window (days).", default=2, ge=0
    )

    # Pruning defaults to OFF: calendar and _history rows can't be re-fetched,
    # so deletion never runs unless explicitly enabled.
    yf_prune_enabled: bool = _cfg(
        "maintenance", "Master switch for pruning; defaults to OFF.", default=False
    )

    log_level: str = "INFO"

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
        """Maintenance-database connection, for `CREATE DATABASE`.

        PostgreSQL requires connecting to SOME database, so the maintenance
        connection uses `postgres`.

        This URL is ONLY for `CREATE DATABASE`. `information_schema` and
        `pg_namespace` are DATABASE-SCOPED: a connection opened here cannot
        see schemas inside `yfinance_test`. Schema creation, deletion, and
        stale-schema cleanup must use `db_url(db_test_name)` -- otherwise
        cleanup silently does nothing and schemas accumulate forever.
        """
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
        # configure_logging() is called before create_db_engine() (shard.py).
        # The log ordering also relies on this field being in env: the
        # loader's warnings -- including the security boundary one -- must
        # land in the configured logger.
        "log_level",
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
# Being unlocked today would be harmless (~1 ms). Combined with a DB read,
# two worker threads could produce TWO SEPARATE `Settings` objects, silently
# breaking test patches that rely on object identity (test_client.py).
_lock = threading.Lock()


def source_is_env() -> bool:
    """`YF_SETTINGS_SOURCE=env` means the DB layer is never read.

    Compared via `strip().lower()`; any non-empty value other than `env`
    logs a WARNING. A recovery switch silently doing nothing because of a
    typo is not acceptable -- the operator would think the DB layer was off
    while it stayed on.
    """
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

    Needed in three places: (a) the loader itself, (b) commands that run
    before the database EXISTS YET, like `yfin db create` / `yfin db
    revision`, (c) `seed --adopt-env` -- so reading the effective value
    doesn't feed back the rows it just wrote.
    """
    return Settings()


def _load() -> Settings:
    global _overrides
    _overrides = {}
    env_only = bootstrap_settings()

    # FIRST: `log_level` is ENV_ONLY so it's ready here. structlog runs with
    # cache_logger_on_first_use=True; any line logged before this falls into
    # the unconfigured PrintLogger and the redact_credentials processor
    # doesn't run.
    configure_logging(env_only.log_level)

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

    `min` / `max` are NOT written by hand: they're derived from `Field`
    constraints (`Ge`, `Le`, `Gt`, `Lt`). Duplicating them would let `ge=1`
    drift to `ge=2` while the panel kept validating against a stale range.
    """

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

    Schema and state are deliberately separate: the schema is constant for
    the process lifetime, `value` can change on every read. Combining them
    would make the pure schema untestable without a DB and uncacheable.
    """
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

    `type: ignore` is required and not temporary: fields are TYPED as `int` /
    `float` / `bool`, but both `.env` and the `settings` table hand back
    every value as text, and pydantic does the conversion. Funneling this
    through one spot keeps the suppression from spreading across the
    codebase.
    """
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

    `overrides` is installed alongside it. Without that, `applied_overrides()`
    would come back EMPTY in the child -- nothing calls it today, but a
    singleton installed with empty overrides would silently produce a wrong
    answer; passing both values through the same door avoids that trap
    entirely.

    For shard children: the parent's resolved value is carried via
    `ShardSpec` and the child never looks at the DB. If the child called its
    own `get_settings()`, an intervening `yfin config set` could make
    shard-0 and shard-3 run with DIFFERENT configuration; worse, the child
    connects using `settings.db_name` but does its actual work in
    `spec.database` -- i.e. it would read settings from a schema it isn't
    even routed to.
    """
    global _settings, _overrides
    with _lock:
        _settings = settings
        _overrides = dict(overrides or {})


def reset_settings() -> None:
    """Clear the singleton (for tests and the `yfin config` write path).

    Without a reset, repo tests would run against a singleton left over from
    the previous test, `load_overrides` would never be called, and tests
    would stay green FOR THE WRONG REASON.
    """
    global _settings, _overrides
    with _lock:
        _settings = None
        _overrides = {}

# yfin

A production-grade ingestion pipeline that pulls the full Yahoo Finance
surface into PostgreSQL 18 + TimescaleDB, and keeps it correct.

Open source under **AGPL-3.0**. A hosted version with the same schema is
available if you would rather not run it yourself — see
[Hosted service](#hosted-service).

---

## What it actually does

`yfinance` gives you DataFrames. This gives you a **queryable, auditable,
incrementally-maintained archive**:

- **61 datasets** across 74 tables — prices, fundamentals, analyst
  estimates, ownership, funds, news, filings, sector/industry taxonomy,
  market calendars, screeners.
- **Intraday bar archive.** Yahoo drops 1-minute data after 29 days. The
  pipeline stores it before that happens, and records the gaps it could
  not fill so "no data" and "we missed it" stay distinguishable.
- **Every write is verified.** Row counts come from reading the keys back,
  not from the driver's affected-row count.
- **Every run is audited.** `sync_runs` / `sync_run_items` record the
  outcome of every (symbol × dataset) cell: `ok`, `empty`, `skipped`,
  `out_of_scope` or `failed` with the error.
- **Incremental by default.** Watermarks per symbol and interval;
  content-hash gates so unchanged data is not rewritten.
- **Proxy pool** with health tracking, cooldown and encrypted
  credentials, plus sharded parallel runs.

Design decisions are backed by measurements, not guesses — see
[`docs/measurements/`](docs/measurements/).

---

## Quick start

```bash
git clone <repo> && cd yfin
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

cp .env.example .env        # set DB_PASSWORD at minimum
docker compose up -d        # PostgreSQL 18.6 + TimescaleDB 2.29.2

.venv/bin/yfin db create
.venv/bin/yfin db upgrade head

.venv/bin/yfin symbols add AAPL MSFT
.venv/bin/yfin sync --symbols AAPL
```

A single-symbol full sync writes ~37,000 rows across ~40 tables.

### Common commands

```bash
yfin sync                         # every active symbol, every dataset
yfin sync --datasets history,info # a subset
yfin sync --exchange NMS          # filter the universe
yfin market sync                  # market-wide (calendars, status, summary)
yfin domain sync                  # sector / industry taxonomy
yfin bars scope add AAPL 1m       # opt a symbol into the 1m archive
yfin bars gaps                    # unfilled intraday windows
yfin rescale --seed               # baseline the split-adjustment ledger
yfin config list                  # effective settings and their source
```

### Web terminal

```bash
cd web && npm ci && npm run build   # writes src/yfin/ui/static/dist
YFAPI_UI_ENABLED=true uvicorn yfin.api.app:app --port 8000
open http://localhost:8000/ui
```

The terminal is public by default: no login, and the page reads the
archive through its own mirror of the `/v1` routers at `/ui/api/v1`
(no Bearer token, no plan metering, a per-address brake of
`YFAPI_UI_REQUESTS_PER_MINUTE` requests instead). `/v1` itself and its
`openapi.json` do not change. Behind a reverse proxy set
`YFAPI_TRUSTED_PROXIES`, or every browser in the world shares one
brake bucket. The footer credits Yahoo Finance, where the data comes
from, and the `yfinance` package that fetches it.

To put the terminal behind a password instead, set
`YFAPI_UI_PUBLIC=false` and `YFAPI_UI_PASSWORD=<choose one>`. Then set
`YFAPI_PUBLIC_BASE_URL` to the `https://` origin so the session cookie
is marked `Secure`; with it empty the cookie travels over plain HTTP,
which is acceptable on localhost and nowhere else. Changing
`YFAPI_UI_PASSWORD` signs every browser session out; changing
`YFAPI_JWT_SIGNING_KEY` does that AND revokes every API token.

Use `npm ci` in `web/`; plain `npm install` crashes on the npm that
ships with Node 22 (an npm 10.9 resolver bug) -- the committed
lockfile is the source of truth.

The command box reads `[SYMBOL] [FUNCTION] [ARGS]`; a bare symbol keeps
the current function, a bare function keeps the current symbol:

```bash
AAPL                  # description (DES) of a symbol
FA balance quarterly  # statements: income|balance|cash, annual|quarterly|ttm
ANR                   # analyst ratings on the current symbol
N                     # news; j/k to move, Enter to open
CF 10-K               # SEC filings of one type; Enter expands exhibits
HELP                  # every function and shortcut; Esc goes back
```

---

## Integration status

### Data sources

| Integration | Status | Notes |
|---|---|---|
| Yahoo Finance — prices & history | **Stable** | `history`, `dividends`, `splits`, `capital_gains`, `shares_full`, `history_metadata` |
| Yahoo Finance — intraday bars | **Stable** | `1m`/`5m`/`15m`/`60m` hypertable; `1wk`/`1mo` in a separate table |
| Yahoo Finance — fundamentals | **Stable** | income / balance / cashflow, annual + quarterly + TTM, valuation measures |
| Yahoo Finance — analyst data | **Stable** | recommendations, upgrades/downgrades, price targets, EPS trend & revisions, growth & earnings estimates |
| Yahoo Finance — ownership | **Stable** | institutional, mutual fund, insider transactions/roster/activity, major holders |
| Yahoo Finance — funds | **Stable** | profile, metrics, weightings, top holdings |
| Yahoo Finance — news & filings | **Stable** | `news`, `news_symbols`, `sec_filings`, `sec_filing_exhibits` |
| Yahoo Finance — sector/industry | **Stable** | taxonomy, metrics, rankings, research reports |
| Yahoo Finance — market calendars | **Stable** | earnings, economic, IPO, splits; market status & summary |
| Yahoo Finance — search & lookup | **Opt-in** | `--datasets search,lookup`; excluded from `all` because they add ~2 requests/symbol |
| Yahoo Finance — screener | **Opt-in** | `--datasets screener` |
| Yahoo Finance — ESG / sustainability | **Opt-in, off by default** | Returned empty for 19 of 19 symbols tested |

### Infrastructure

| Integration | Status | Notes |
|---|---|---|
| PostgreSQL 18 | **Stable** | `postgresql+psycopg` (psycopg 3) |
| TimescaleDB 2.29 | **Stable** | Hypertables for `price_bars` and `price_history` |
| Alembic migrations | **Stable** | Single squashed baseline; `revision --autogenerate` must produce an empty diff |
| Docker Compose | **Stable** | Pinned image, healthcheck, tuned `max_connections` |
| Proxy pool | **Stable** | HTTP/HTTPS/SOCKS5, Fernet-encrypted credentials, health & cooldown |
| Sharded parallel sync | **Stable** | Process-per-shard, advisory-lock guarded |
| Settings in database | **Stable** | 39 settings overridable at runtime; `yfin config` |
| Compression / retention policies | **Not enabled** | Deliberate: the rescale path rewrites historical rows. Needs measurement first. |
| Continuous aggregates | **Not enabled** | Out of scope so far |

### Planned

| Integration | Status | Notes |
|---|---|---|
| **Kafka producer** | **TODO** | Publish each verified write as an event so downstream consumers do not poll the database. Open questions: topic per table vs per dataset, and whether the outbox lives in `sync_run_items` or a dedicated table. |
| **WebSocket streaming** | **TODO** | Yahoo's live quote socket for intraday updates between scheduled runs, plus an outbound socket so clients can subscribe to symbols instead of polling. Needs a decision on how live ticks reconcile with the bar archive. |
| **Web terminal** | **In progress** | Keyboard-first browser UI under `/ui`, served by the API process. Public by default. Every dataset in the archive is readable: `DS` browses the whole catalogue, `DES`/`FA`/`ANR`/`N`/`CF`/`CA`/`PX` and the tabbed `HDS`/`ERN`/`FUND`/`CAL`/`MKT`/`SCR`/`SRCH`/`DOM`/`REF` panels cover it by family; live ticks and charts follow (`docs/superpowers/specs/2026-09-07-web-terminal-design.md`). |

---

## Architecture

```
CLI (typer)
  └── runner / market_runner / domain_runner
        ├── Registry          dataset resolution, dependency order, opt-in
        ├── Dataset           fetch → normalize → upsert   (no SQLAlchemy)
        │     └── RowWriter   protocol
        └── PostgresRowWriter ON CONFLICT, chunking, dedupe, verification
```

The dataset layer depends on the `RowWriter` **protocol**, never on
SQLAlchemy. That boundary is why the MySQL → PostgreSQL migration touched
none of the 40+ dataset modules.

Datasets declare what they produce; the registry resolves names, expands
aliases and topologically orders dependencies. Adding a dataset means
adding a module and registering it — no other file changes.

### Layout

| Path | Contents |
|---|---|
| `src/yfin/datasets/` | One module per dataset; engine-agnostic |
| `src/yfin/models/` | SQLAlchemy models, type factories, views |
| `src/yfin/persistence.py` | Write mechanics: upsert, chunking, verification |
| `src/yfin/runner.py` | Orchestration, retries, audit records |
| `src/yfin/proxy/` | Pool, health, encrypted credentials |
| `src/yfin/ui/` | Web terminal: session cookie, `/ui/api` routes, SPA pages |
| `web/` | The SPA source (React + Vite); builds into `src/yfin/ui/static/dist` |
| `migrations/` | Alembic |
| `docs/measurements/` | Evidence behind the design decisions |

---

## Development

```bash
.venv/bin/ruff check src tests scripts migrations
.venv/bin/mypy --strict
.venv/bin/pytest                 # unit tests, no database, no network
.venv/bin/pytest -m repo         # against a real PostgreSQL + TimescaleDB
.venv/bin/pytest -m live         # against the real Yahoo API
```

Unit tests run without a database or network. `-m repo` tests create a
schema per process, so parallel runs cannot collide.

**Conventions**

- Every claim in a comment is measured. If you cannot measure it, do not
  claim it.
- New tables must satisfy the schema invariants in
  `tests/unit/test_schema_invariants.py` — FK policy, timestamp
  precision, collation, as-of key ordering.
- `alembic revision --autogenerate` must produce an empty diff before a
  change is considered done.

---

## Hosted service

The pipeline is free to self-host and always will be. If you want the
data without operating it, a hosted service offers the same schema with
managed ingestion, backfilled history and API access on paid plans.

Self-hosting is not a degraded tier: it is the same code, the same
schema, and the same migrations. The hosted product sells operation, not
capability — running this well means a database you maintain, a proxy
pool you keep healthy, and a scheduler that does not miss the 29-day
window on 1-minute bars.

---

## Licence

**GNU Affero General Public License v3.0** — see [`LICENSE`](LICENSE).

AGPL was chosen deliberately for a project that also funds a hosted
service. In practice:

- **Self-hosting, internally**: use it however you like. Running it for
  your own analysis, inside your company, triggers nothing.
- **Modifying it**: your changes are AGPL too, and you must offer the
  source to anyone you distribute the software to.
- **Offering it as a network service**: section 13 applies. If you run a
  modified version and let others interact with it over a network, you
  must offer those users its source. This is the clause that keeps a
  competing hosted service from building on this work while keeping its
  improvements private.

Contributions are accepted under the same licence. If AGPL does not work
for your use case, ask about a commercial licence.

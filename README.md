# yfin

A production-grade ingestion pipeline that pulls the full Yahoo Finance
surface into PostgreSQL 18 + TimescaleDB, and keeps it correct.

Open source under [the licence below](#licence). A hosted version with
the same schema is available if you would rather not run it yourself —
see [Hosted service](#hosted-service).

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
managed ingestion, backfilled history and API/streaming access on paid
plans.

Self-hosting is not a degraded tier: it is the same code, the same
schema, and the same migrations. The hosted product sells operation, not
capability.

---

## Licence

See `LICENSE`.

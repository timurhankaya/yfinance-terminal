# Publishing pipeline writes as change events

Status: approved, not yet implemented
Date: 2026-09-07
Extends the tick relay shipped under `src/yfin/stream/` (`ea9bd8f`,
`0228a66`, `9fceb75`). Revises nothing; the tick outbox keeps its table,
its topics and its CLI.

## Why

`yfin stream relay` publishes live ticks to Kafka through a transactional
outbox. Nothing publishes what the *pipeline* writes: a consumer who wants
to know that AAPL's income statement changed, that a new SEC filing
landed, or that an analyst target moved has to poll the API and diff.
README lists "Publishing pipeline writes -- as opposed to ticks -- is not
started."

This design makes every pipeline write observable as a row-level event on
Kafka, with the same at-least-once guarantee the tick relay gives, and
without a second delivery mechanism: the read API stays the way a
consumer fetches current state, Kafka is how it learns that state moved.

### What the code base already has

Verified against the working tree, 2026-09-07.

| Fact | Where |
| --- | --- |
| Outbox row written inside the tick transaction, COPY, JSON payload, `id` order | `stream/writer.py:308-318,429-475` |
| Relay walks `id > last_published_id`, advances the offset only after every ack | `stream/relay.py:120-150` |
| Topic per exchange, key per symbol, `acks=all`, idempotent producer, no headers | `stream/kafka.py:49-62,72-102,147-166` |
| Published chunks dropped with `drop_chunks`, never `DELETE` | `stream/relay.py:205-243` |
| One symbol = one transaction; audit in a second transaction | `pipeline/persist.py:26-41,107-138`, `pipeline/audit.py:236-247` |
| Market and domain turns persist through the same writer, one transaction per turn | `pipeline/turn.py`, `market_runner.py:76`, `domain_runner.py:171` |
| The writer returns counts, not identities: `attempted`, `verified`, `skipped` | `storage/contracts.py:52-63`, `storage/persistence.py:135-164` |
| Verification is an independent key-existence read; `RETURNING` is unused | `storage/persistence.py:245-281` |
| `replace_scope` deletes the scope, then re-inserts everything | `storage/persistence.py:219-243`; nine dataset modules use it |
| Content-hash gates skip whole datasets, not rows | `datasets/snapshot_base.py`, `datasets/hash_gated.py`, `datasets/asof_base.py` |
| Family is declared on `ApiExposure`, not on `Dataset`; bars have no exposure | `datasets/exposure.py:27-40`, `datasets/base.py:194-220` |

The gap is precise: the pipeline knows *which datasets* changed (through
the gates) but not *which rows*, and it has no place to put a row-level
fact that survives the transaction.

## Decisions

1. **Row-level events, one per inserted, updated or deleted row.** A
   dataset-level notification would force the consumer back to the API
   for every change, and a dataset-level snapshot would ship hundreds of
   rows to say that one moved. The row is the unit the consumer can
   apply directly.

2. **Every dataset, all three registries.** The 49 per-symbol datasets,
   the 7 market-wide ones and the 5 sector/industry ones. Bars are in:
   a rescale or a backfill rewrites history, and "no event" must never
   mean "nothing changed".

3. **No new API surface.** The existing `/v1` endpoints serve current
   state; a consumer without Kafka reads those. Consequently the outbox
   needs no retention beyond delivery and is dropped chunk by chunk, as
   the tick outbox is.

4. **Topic per data family, key per entity.** `yfin.changes.{family}`,
   seven topics that line up with the seven `<family>:read` scopes, so a
   Kafka ACL is drawn on the same line the API draws. The key is the
   symbol for symbol datasets, `domain_key` for domain datasets and the
   table name for market datasets. Ordering is per key, which is what a
   consumer mirroring one symbol needs.

5. **Deletes are published.** `replace_scope` tables (holders lists,
   officers, fund holdings, rankings) would otherwise republish every
   row as an insert on every sync, and `symbols purge` / `yfin prune`
   would leave the consumer's mirror stale.

6. **Application-level transactional outbox, not CDC.** Logical
   replication (Debezium) would add a JVM Connect service, a replication
   slot to babysit, `_hyper_*` chunk names to route, and no
   dataset/run context. Triggers on 68 tables would put a per-row cost
   on bar backfills and need `SET LOCAL` to carry context. The writer
   already sits on the one code path every pipeline row passes through.

7. **Off by default.** `yf_changes_enabled = False`. When off, the
   writer emits statements byte-for-byte identical to today's; a test
   pins that.

## Architecture

### The outbox table

`pipeline_outbox`, the same shape as `stream_outbox`:

| column | type | notes |
| --- | --- | --- |
| `id` | `BigInteger`, `Identity`, primary key | ordering, not identity: the relay walks `id > last_published_id` |
| `created_at` | `TsType()`, primary key | hypertable time column, 1-hour chunks, no default indexes |
| `family` | `String(16, collation="C")`, not null | picks the topic |
| `partition_key` | `String(64, collation="C")`, not null | Kafka message key |
| `payload` | `RawJsonType()`, not null | the envelope below |

One index, `ix_pipeline_outbox_id`. No foreign keys, for the reason the
tick outbox has none: the row is transient and the FK would cost a lookup
on every write. `created_at` is the writer's clock at flush time, so the
relay's chunk cleanup and its `id` walk agree.

`pipeline_relay_offset`: `id SmallInteger PK CHECK (id = 1)`,
`last_published_id BigInteger NOT NULL DEFAULT 0`, `updated_at`. Its own
table rather than a second row in `stream_relay_offset`, so the two
relays never share a row lock. Advisory lock name `yfin_pipeline_relay`.

One Alembic migration adds both tables and the hypertable DDL; the
`revision --autogenerate` empty-diff rule applies.

### The envelope

JSON text, version 1:

```json
{
  "v": 1,
  "op": "insert",
  "family": "fundamentals",
  "dataset": "income_statement",
  "table": "financial_facts",
  "key": {"symbol": "AAPL", "statement": "income", "freq": "annual",
          "period_end": "2025-09-27", "item": "TotalRevenue"},
  "row": {"...": "the whole aligned row"},
  "run_id": 4711,
  "occurred_at": "2026-09-07T10:15:32.118Z"
}
```

- `op` is `insert`, `update`, `delete` or `rescale`.
- `row` is the full row for `insert` and `update` -- not the changed
  columns, so a consumer never has to hold the previous row to apply the
  next one. `null` for `delete`. For `rescale` it is
  `{"symbol", "factor", "applied_before"}`.
- `key` is the table's `key_columns` in order.
- Decimal and datetime are rendered as text, sharing `_jsonable` from the
  tick writer: a float would reintroduce the f32 artefact the pipeline
  removes.
- `run_id` is the `sync_runs.id` of the run that wrote the row; `null`
  for `prune` and `purge`, which run outside a sync.
- Consumer dedupe key: `(table, key, occurred_at)`. Delivery is
  at-least-once; a redelivered event has the same triple.

There is no explicit size limit. A row-level event is bounded by the
widest row, the `info` snapshot, at a few kilobytes.

### Capturing changes: `ChangeCollector`

New module `storage/changes.py`. `PostgresRowWriter` takes an optional
collector; `None` keeps every statement exactly as it is today.

```python
class ChangeCollector:
    def __init__(self, *, run_id: int | None) -> None: ...
    def enter_dataset(self, dataset: DatasetMeta) -> None
        # family + partition-key column from the registry
    def record(self, table, op, key, row) -> None
    def record_deletes(self, table, keys) -> None
    def flush(self, session) -> int       # COPY into pipeline_outbox
    def reset(self) -> None               # on rollback
```

`enter_dataset` is called by `persist_symbol` and `run_turn` before
`dataset.upsert(writer, result)`. Tables listed in
`INFRASTRUCTURE_TABLES` (the audit pair, `proxies`, `settings`,
`asof_state`, `domain_asof_state`, `discovery_asof_state`, `bar_gaps`,
`bar_rescales`, `intraday_scope`, the five API tables, the eight stream
tables and the two new outbox tables) are ignored by `record`; a unit test asserts that
`produces ∪ INFRASTRUCTURE_TABLES == Base.metadata.tables`, so a new
table has to be put on one side or the other.

### The upsert path

With a collector present, `_insert_stmt` gains two clauses.

**`RETURNING <key_columns>, (xmax = 0) AS inserted`.** A tuple written
by the INSERT branch has `xmax = 0`; one rewritten by `ON CONFLICT DO
UPDATE` carries the updating transaction's id. This is the insert/update
distinction, at no extra read. It is measured, not assumed: the
observation goes into `docs/measurements/database.md`, including on a
hypertable chunk.

**`DO UPDATE ... WHERE (t.c1, t.c2, ...) IS DISTINCT FROM
(excluded.c1, excluded.c2, ...)`** over the *comparable* update columns.
A row whose comparable columns are unchanged is not updated and does not
appear in `RETURNING`. The comparable set is `update_columns` minus:

- `volatile_columns`, a new `TableWrite` field defaulting to
  `("fetched_at", "first_seen_at", "as_of_date")`. `asof_base.VOLATILE_COLUMNS`
  is redefined in terms of the same constant so the two cannot drift.
- `monotonic_columns`, which use `GREATEST` and are excluded from the
  comparison; a GREATEST that leaves the stored value alone is not a
  change.

`guard_column` keeps its existing `excluded.guard > t.guard` predicate,
`AND`ed with the distinctness test.

The rows returned are matched back to the aligned, deduplicated `rows`
list by key; the whole row is the event's `row`. No second read.

**`fetched_at` still means "last verified at".** `HashGate` relies on
that. With the distinctness predicate, an unchanged row would stop
receiving `fetched_at`. So, with a collector present and only for tables
that have `fetched_at`, the writer issues one extra
`UPDATE <table> SET fetched_at = :now WHERE <key> IN (...)` for the keys
that the RETURNING did *not* report. Without a collector the single
original statement already updates every row, so nothing changes.

`_verify` is untouched. Verification stays an independent key-existence
read after the write; the events are a by-product, not the proof.

### The `replace_scope` path

Today: `DELETE` the scope, `INSERT` everything. With a collector:

1. `SELECT <key_columns> FROM t WHERE <scope>` -- the keys currently in
   scope.
2. `DELETE ... WHERE <key> IN (<present − incoming>) RETURNING <key>`
   -- one `delete` event per removed key.
3. The incoming rows go through the normal upsert path, so unchanged
   rows are silent and changed ones are `update`.

Without a collector the single `DELETE` stays. The nine dataset modules
that use `replace_scope` do not change.

### Deletes outside a sync

`pipeline/prune.py` (seven delete sites) and `symbols purge`
(`cli/app.py:416-436`) already use ORM `delete()`. Each gains
`RETURNING <key>` and hands the keys to `collector.record_deletes`. The
collector is created with `run_id=None`; `--dry-run` never creates one.

### Rescale

`storage/rescale.apply_pending` rewrites `price_bars` with an ORM
`UPDATE`, not through the writer. Emitting one event per rescaled bar
would write millions of outbox rows to say "multiply by the factor". A
single `op=rescale` event per applied split, keyed by symbol, family
`bars`, carries `{symbol, factor, applied_before}`; the consumer re-reads
the affected range from the API.

### Flushing

`persist_symbol` and `run_turn` call `collector.flush(session)` after the
last `upsert` and before `session.commit()`. `flush` renders the events
with the shared `copy_body` and `COPY pipeline_outbox (created_at,
family, partition_key, payload) FROM STDIN`, one statement per
transaction. `persist_with_retry` calls `collector.reset()` in its
rollback branch, so a replayed transaction produces one set of events,
not two.

### Family on the dataset

`Dataset` gains a required class attribute `family: DataFamily`.
`Registry.register` refuses a dataset without one and checks that every
`ApiExposure.family` on it equals it. The table→family map the collector
needs is then `{table: ds.family for ds in registries for table in
ds.produces}`; no separate map file. Bars → `bars`; market calendars →
`fundamentals` and market status → `reference`, as their exposures
already say; domain datasets → `domains`.

The partition-key column comes from the registry too: `symbol` for
`SYMBOL_DATASETS`, `domain_key` for `DOMAIN_DATASETS`, the table name for
`MARKET_DATASETS`.

### The relay, generalised

`stream/relay.py` moves to `outbox/relay.py` and takes an `OutboxSpec`:

```python
@dataclass(frozen=True)
class OutboxSpec:
    table: str                 # "stream_outbox" | "pipeline_outbox"
    offset_table: str
    lock_name: str
    route_column: str          # "exchange" | "family"
    key_column: str            # "symbol" | "partition_key"
    topic_pattern: str
```

`OutboxRelay`, `publish`, `DeliveryTracker`, `build_producer`,
`drop_published_chunks`, `relay_lag` and `verify_topics` keep one
implementation each. The tick relay is `OutboxSpec("stream_outbox",
"stream_relay_offset", RELAY_LOCK_NAME, "exchange", "symbol",
settings.yf_kafka_topic_pattern)`; its tests change only in import
path. `topic_for` already normalises the route value, so a family name
goes through the same sanitiser an exchange does.

Topics are verified, never created: partition count is the operator's
decision, and raising it later reorders keys. Seven topics,
`yfin.changes.{reference,bars,fundamentals,holders,news,discovery,domains}`.

### Settings

Group `stream`, alongside the Kafka settings:

| setting | default |
| --- | --- |
| `yf_changes_enabled` | `False` |
| `yf_changes_topic_pattern` | `"yfin.changes.{family}"` |

`yf_kafka_bootstrap_servers` and `yf_kafka_relay_batch` are shared with
the tick relay. When `yf_changes_enabled` is off no collector is
constructed and no outbox row is written.

### CLI

New group `yfin changes`:

- `yfin changes relay [--once]` -- drains `pipeline_outbox`. Exit 1 when
  `yf_changes_enabled` is off or the last pass recorded an error.
- `yfin changes status` -- `N row(s) unpublished; oldest Ns behind`.

`yfin status` prints the same line when the feature is on. `yfin stream
relay` is unchanged: two relays, two processes, two locks, two offsets.

## Error handling and guarantees

- **Atomic with the data.** A failed commit loses both the rows and their
  events; a successful one guarantees eventual publication. A broker
  outage never blocks a sync; the outbox grows and `changes status`
  shows by how much.
- **Retry replay** produces one set of events: the collector resets on
  rollback and the distinctness predicate cannot see rolled-back writes.
- **Hash gates** skip child writes entirely, so they also skip events.
  Gate state tables are infrastructure and never published.
- **Full refresh / first sync** publishes every row as `insert`. That is
  the intended way for a consumer to build a mirror from nothing.
- **Relay failure** leaves the offset where it was and retries the
  batch; `RelayStats.last_error` turns into exit 1. A missing topic is a
  warning at start-up and a stuck batch at publish time -- visible,
  rather than a silently mis-partitioned auto-created topic.
- **Schema evolution.** Adding a column is compatible; consumers ignore
  unknown fields. Removing or renaming one bumps `v`. Because the event
  row *is* the table row, `docs/changes/schema.json` -- every published
  table's column list -- is committed and diffed in CI, the way
  `openapi.json` is.

## Tests

Unit, no database:

- `ChangeCollector`: op recording, dataset context, volatile exclusion,
  infrastructure tables ignored, JSON rendering of Decimal/datetime,
  `reset`.
- `_insert_stmt` with a collector compiles to a statement containing
  `RETURNING` and `IS DISTINCT FROM`; without one it compiles to exactly
  today's SQL (regression lock, compared as text).
- Registry: every dataset declares `family`; exposure families agree;
  `produces ∪ INFRASTRUCTURE_TABLES == metadata.tables`.
- Relay with a fake producer: the tick spec and the changes spec drive
  the same class; existing relay tests pass unchanged apart from imports.
- `docs/changes/schema.json` equals the generated document.
- `tests/unit/test_dataset_layer_boundary.py` still passes: no dataset
  module imports the collector or the ORM.

Repo (`-m repo`):

- Writing the same rows twice yields events once; a change to
  `fetched_at` alone yields none but the column is updated.
- `xmax = 0` distinguishes insert from update on a plain table and on a
  `price_bars` chunk.
- `replace_scope`: removed key → `delete`, unchanged → nothing, new →
  `insert`, changed → `update`.
- `guard_column` rejects the older row and emits nothing.
- `persist_with_retry` on a forced `40001` emits one set.
- `prune` and `purge` land in the outbox with `run_id = null`.
- `drop_published_chunks` and `relay_lag` work against `pipeline_outbox`.

## Measurements to record

Into `docs/measurements/database.md`:

- Write-time cost of `RETURNING` + `IS DISTINCT FROM`: a full AAPL sync
  (37,295 rows) with the collector on and off. Acceptance: within 10 %.
- Event volume: a daily sync of an unchanged symbol (expected near zero)
  and a first sync (expected equal to rows written).
- The `xmax = 0` observation, with the command and its raw output.

## Files

New:

- `src/yfin/storage/changes.py` -- `ChangeCollector`, `INFRASTRUCTURE_TABLES`, envelope rendering
- `src/yfin/outbox/__init__.py`, `src/yfin/outbox/relay.py`, `src/yfin/outbox/kafka.py` -- moved from `stream/`
- `src/yfin/models/changes.py` -- `pipeline_outbox`, `pipeline_relay_offset`
- `src/yfin/cli/changes.py` -- `yfin changes relay|status`
- `migrations/versions/<ts>_pipeline_outbox.py`
- `docs/changes/schema.json`, `scripts/dump_change_schema.py`
- tests listed above

Changed:

- `storage/contracts.py` -- `TableWrite.volatile_columns`
- `storage/persistence.py` -- collector hook, `RETURNING`, distinctness, `replace_scope` diff, `fetched_at` touch
- `storage/rescale.py` -- `op=rescale` event
- `datasets/base.py`, `datasets/registry.py`, every dataset module -- `family`
- `datasets/asof_base.py` -- `VOLATILE_COLUMNS` from the shared constant
- `pipeline/persist.py`, `pipeline/turn.py`, `pipeline/prune.py`, `cli/app.py` -- collector lifecycle, delete events
- `stream/relay.py`, `stream/kafka.py`, `cli/stream.py` -- re-exports / imports after the move
- `core/config.py` -- two settings; `.env.example`
- `README.md` -- Planned table, command list; `docker-compose.yml` comment on the broker

## Implementation order

1. `family` on `Dataset` + registry validation + infrastructure-table
   test. Pure refactor, no behaviour change.
2. `outbox/` extraction with `OutboxSpec`; tick relay green on the new
   module.
3. Models + migration; `ChangeCollector` with unit tests.
4. Writer: `RETURNING`, distinctness, `fetched_at` touch; repo tests;
   the `xmax` measurement.
5. `replace_scope` diff; `prune`/`purge`/rescale events.
6. Flush in `persist_symbol` / `run_turn`; retry reset.
7. Settings, CLI group, `yfin status` line, schema dump + CI diff, docs.
8. Cost measurement on a full AAPL sync; record it.

## Out of scope

- A change-feed endpoint on the API (`/v1/changes?cursor=`), or `since`
  filters on dataset endpoints.
- Kafka ACLs, topic creation, schema registry, Avro/Protobuf.
- A consumer SDK.
- Per-bar events for a rescale (see Rescale).

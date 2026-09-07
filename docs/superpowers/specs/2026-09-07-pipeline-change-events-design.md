# Publishing pipeline writes as change events

Status: approved, not yet implemented
Date: 2026-09-07
Revised 2026-09-07 after independent review (see "Revisions" at the end).
Extends the tick relay shipped under `src/yfin/stream/` (`ea9bd8f`,
`0228a66`, `9fceb75`). Revises nothing; the tick outbox keeps its table,
its topics and its CLI. Sibling of
`2026-09-07-observability-design.md`, which instruments the relay this
design generalises; the ordering between the two is given under
"Implementation order".

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
| Outbox row written inside the tick transaction, COPY, JSON payload, `id` order | `stream/writer.py:318-330,438-484` |
| The relay's `id` walk and chunk cleanup are justified by a **single writer thread** | `stream/relay.py:15-18` |
| Relay walks `id > last_published_id`, advances the offset only after every ack | `stream/relay.py:120-150` |
| Topic per exchange, key per symbol, `acks=all`, idempotent producer, no headers, `client.id = yfin-stream-relay` | `stream/kafka.py:49-62,72-102,147-166` |
| `topic_for` substitutes the literal `{exchange}` and upper-cases the value | `stream/kafka.py:57-58` |
| Published chunks dropped with `drop_chunks`, never `DELETE` | `stream/relay.py:205-243` |
| One symbol = one transaction; audit in a second transaction | `pipeline/persist.py:26-41,107-138`, `pipeline/audit.py:236-247` |
| Symbol transactions commit **concurrently**: N shard processes × worker threads | `pipeline/runner.py:276-330`, `pipeline/shard.py:236-281` |
| Market and domain turns persist through the same writer, one transaction per turn | `pipeline/turn.py`, `market_runner.py:76`, `domain_runner.py:171` |
| `run_id` is known in the runners, not in `persist_symbol` / `run_turn` | `runner.py:196,229`, `market_runner.py:135`, `domain_runner.py:237` |
| The writer returns counts, not identities: `attempted`, `verified`, `skipped` | `storage/contracts.py:52-63`, `storage/persistence.py:135-164` |
| Verification is an independent key-existence read; `RETURNING` is unused | `storage/persistence.py:245-281` |
| The upsert has three shapes: `DO UPDATE`, `DO UPDATE ... WHERE guard`, `DO NOTHING` (empty update map) | `storage/persistence.py:166-217` |
| `replace_scope` deletes the scope, then re-inserts everything | `storage/persistence.py:219-243`; nine dataset modules use it |
| Content-hash gates skip whole datasets, not rows; `UNCHANGED_UPDATE_COLUMNS = ("fetched_at",)` | `datasets/snapshot_base.py`, `datasets/hash_gated.py:25,77-81`, `datasets/asof_base.py:164` |
| Several tables are written by datasets of different API families: `symbols` (6 datasets), `news` (`news`, `search`), `research_reports` (`search`, `sector_profile`) | registry enumeration |
| A mandatory `family` on `Dataset` was considered and rejected | `datasets/exposure.py:8-12`, `core/families.py:5-9` |
| `symbols purge` deletes across `symbol_scoped_tables()`, hypertables included | `cli/app.py:417-437`, `models/__init__.py:142-165` |
| Timescale DDL is collected per model module and applied by tests through `all_timescale_ddl()` | `models/stream.py:435-464`, `models/__init__.py:298-307`, `tests/conftest.py:126` |
| `canonical_json` already handles NaN, numpy scalars, dates | `core/normalize.py:294-300` |

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

4. **Routing is a property of the table, not of the dataset.** A table
   has one family and one partition column, declared once in a routing
   map. Datasets of different families write the same table (`symbols`,
   `news`, `research_reports`), so a per-dataset family would send one
   table's rows to two topics and break the "one ACL line per family"
   promise. This keeps the earlier decision not to put a mandatory
   `family` on `Dataset`.

5. **Topic per data family, key per row's partition column.**
   `yfin.changes.{family}`, seven topics that line up with the seven
   `<family>:read` scopes. The key is the table's partition column:
   `symbol` wherever the table has one, otherwise `domain_key`, `region`
   or the table's own identifier (`news_id`, `report_id`, `query_term`).
   Ordering is per key.

6. **Deletes are published.** `replace_scope` tables would otherwise
   republish every row as an insert on every sync, and `symbols purge`
   / `yfin prune` would leave the consumer's mirror stale.

7. **Bulk writes to bar tables coalesce into range events.** A first sync
   or `--full-refresh` writes ~20k bars per symbol; across a 4,500-symbol
   universe that is on the order of 10⁸ row events to say "the history
   is here". A write to a `bars`-family table that produces more than
   `yf_changes_range_threshold` rows (default 1000) emits one
   `op=range` event carrying the interval and the time span instead.
   Steady-state daily writes (≈390 one-minute bars) stay row-level;
   repairs, which are small, stay row-level.

8. **The pipeline relay walks transaction ids, not row ids.** Symbol
   transactions commit concurrently, so `id` order is not commit order;
   an `id`-ordered walk would skip rows of a transaction that took its
   ids early and committed late. The tick relay keeps its `id` walk: it
   has a single writer thread, which is the premise its code states.

9. **Application-level transactional outbox, not CDC.** Logical
   replication (Debezium) would add a JVM Connect service, a replication
   slot to babysit, `_hyper_*` chunk names to route, and no
   dataset/run context. Triggers on 68 tables would put a per-row cost
   on bar backfills and need `SET LOCAL` to carry context. The writer
   already sits on the one code path every pipeline row passes through.

10. **Off by default.** `yf_changes_enabled = False`. When off, the
    writer emits statements byte-for-byte identical to today's; a test
    pins that.

## Architecture

### The outbox table

`pipeline_outbox`:

| column | type | notes |
| --- | --- | --- |
| `id` | `BigInteger`, `Identity`, primary key | row identity; carried to the consumer as the dedupe key |
| `created_at` | `TsType()`, primary key | hypertable time column, 1-hour chunks, no default indexes |
| `xid` | `xid8`, not null, `DEFAULT pg_current_xact_id()` | the writing transaction; the relay's cursor |
| `family` | `String(16, collation="C")`, not null | picks the topic |
| `partition_key` | `Text(collation="C")`, not null | Kafka message key |
| `payload` | `RawJsonType()`, not null | the envelope below |

Indexes: `ix_pipeline_outbox_xid (xid, id)`. No foreign keys, for the
reason the tick outbox has none: the row is transient and the FK would
cost a lookup on every write. `xid8` needs a small `UserDefinedType` in
`models/types.py`; SQLAlchemy has no built-in for it. PostgreSQL 13+
provides the type and `pg_current_xact_id()`; the project pins 18.

`pipeline_relay_offset`: `id SmallInteger PK CHECK (id = 1)`,
`last_published_xid xid8 NOT NULL DEFAULT '0'`, `updated_at TsType NOT
NULL`. Its own table rather than a second row in `stream_relay_offset`,
so the two relays never share a row lock. Advisory lock name
`yfin_pipeline_relay`.

`models/changes.py` also exposes `changes_timescale_ddl()`, wired into
`all_timescale_ddl()` so the repo fixtures create the hypertable the
same way the migration does. One Alembic migration adds both tables and
the hypertable DDL; the `revision --autogenerate` empty-diff rule
applies.

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
          "period_end": "2025-09-27", "item_key": "TotalRevenue"},
  "row": {"...": "the row as PostgreSQL returned it"},
  "run_id": 4711,
  "occurred_at": "2026-09-07T10:15:32.118000+00:00"
}
```

- `op` is `insert`, `update`, `delete`, `range` or `rescale`.
- `row` is the full row **as returned by the database** for `insert` and
  `update` -- `RETURNING *` -- so columns outside the update map,
  `GREATEST`-merged columns and server defaults are what the consumer
  sees, not what the pipeline proposed. `null` for `delete`. For
  `range`: `{"bar_interval", "ts_from", "ts_to", "rows"}`. For
  `rescale`: `{"factor", "applied_before", "split_date"}`.
- `key` is the table's `key_columns` in order; for `range` and `rescale`
  it is `{"symbol"}` plus, for `rescale`, `split_date`.
- `dataset` is `null` for `prune`, `purge` and `rescale`, which run
  outside a dataset.
- Rendering shares `canonical_json`'s `default` handling from
  `core/normalize.py`: `Decimal` as text (a float would reintroduce the
  f32 artefact), `datetime`/`date` as ISO-8601, NaN as `null`, numpy
  scalars unwrapped. `_jsonable` in `stream/writer.py` is replaced by the
  same helper, moved to `storage/copy.py` together with `copy_body`, so
  `storage/` does not import from `stream/`.
- `run_id` is the `sync_runs.id` of the run that wrote the row; `null`
  outside a sync. A scheduler run (observability design) is reachable
  through `sync_runs.job_run_id`; it is not repeated in the envelope.
- `occurred_at` equals the outbox row's `created_at`; both come from one
  `SELECT clock_timestamp()` at flush time, so the database clock orders
  events across shard processes and hosts.
- **Dedupe.** The relay sets a Kafka header `yfin-outbox-id` with the
  outbox `id`. The consumer's dedupe key is `(topic, yfin-outbox-id)`.
  `(table, key, occurred_at)` is not enough: one transaction can write
  the same row twice (`symbols` from `symbols`, `isin` and `lookup`;
  `news` from `news` and `search`) with one flush timestamp. Tick topics
  keep carrying no headers.
- **Same-row order across transactions.** Two transactions updating the
  same row serialise on the row lock, and each takes `occurred_at` after
  all its writes, so the later `occurred_at` is the later write. The
  relay's transaction-id order (below) is start order, not commit
  order; a consumer that mirrors state applies the event with the
  greater `occurred_at` and ignores the other. Within one transaction
  events are in write order.

Message size is bounded by the widest row: `ticker_info` (191 columns
plus `raw_json`) and `screen_quotes` reach tens of kilobytes, under the
broker's 1 MB default.

### Routing: `storage/routing.py`

```python
@dataclass(frozen=True)
class Route:
    family: DataFamily
    partition_column: str

ROUTES: Mapping[str, Route]          # table name -> route
INFRASTRUCTURE_TABLES: frozenset[str]
GATE_TABLES: frozenset[str]          # asof_state, domain_asof_state, discovery_asof_state
```

The partition column is chosen per table by the rule: `symbol` if the
table has it; else `domain_key`; else `region`; else the table's own
identifier (`news_id`, `report_id`, `query_term`, `screen_key`). The map
is written out explicitly so the rule is checkable, and tests assert:

- every table in any registry's `produces` has a route, except the
  gate tables and `bar_gaps`, which are produced and infrastructure at
  once and are listed as such;
- every `ApiExposure(table=t).family == ROUTES[t].family`;
- every `partition_column` exists on the table;
- `metadata.tables − produces ⊆ INFRASTRUCTURE_TABLES` and
  `INFRASTRUCTURE_TABLES ⊆ metadata.tables`, with `yfin.api.models`
  imported explicitly so the API tables are in the universe.

`INFRASTRUCTURE_TABLES` holds: `sync_runs`, `sync_run_items`, `proxies`,
`settings`, `asof_state`, `domain_asof_state`, `discovery_asof_state`,
`bar_gaps`, `bar_rescales`, `intraday_scope`, the five API tables, the
eight stream tables, `pipeline_outbox`, `pipeline_relay_offset`, and --
once the observability design lands -- `scheduler_runs` and
`run_metrics`. Rows written to these tables are never published.

`docs/changes/schema.json` -- one entry per routed table with its
family, partition column and column list rendered with the wire-type
map the API catalogue already uses (`api/storage/catalog.py`, moved to
`core/` so both consumers share it) -- is generated by
`scripts/dump_change_schema.py --check` and diffed in CI like
`openapi.json`. The document changes whenever a table does; that churn
is accepted here because the event row *is* the table row. Columns the
API hides (`ApiExposure.hidden`) are still in the event: the event is
the table, not the resource.

### Capturing changes: `ChangeCollector`

New module `storage/changes.py`. `PostgresRowWriter` takes an optional
collector; `None` keeps every statement exactly as it is today.

```python
class ChangeCollector:
    def __init__(self, *, run_id: int | None, range_threshold: int) -> None: ...
    def enter_dataset(self, name: str | None) -> None   # the envelope's dataset field
    def record(self, table, op, key, row) -> None       # looks up ROUTES[table]
    def record_range(self, table, symbol, interval, ts_from, ts_to, rows) -> None
    def record_rescale(self, symbol, split_date, factor, applied_before) -> None
    def flush(self, session) -> int                     # COPY into pipeline_outbox
```

The collector is constructed by whoever knows the run: `runner.py` /
`shard.py` for symbol runs, `market_runner.py` and `domain_runner.py`
for turns, the CLI for `prune`, `purge` and `bars rescale`. It reaches
the writer through a new optional field on `SymbolPayload` and on
`Turn`; `persist_symbol` and `run_turn` call `enter_dataset(name)`
before each `dataset.upsert(writer, result)` and `flush(session)` at
the end, before the caller commits. `persist_with_retry` constructs a
fresh collector per attempt, so a replayed transaction produces one set
of events. `record` ignores tables in `INFRASTRUCTURE_TABLES`.

### The upsert path

With a collector present, `_insert_stmt` changes as follows.

**`RETURNING *, (xmax = 0) AS inserted`.** A tuple written by the INSERT
branch has `xmax = 0`; one rewritten by `ON CONFLICT DO UPDATE` carries
the updating transaction's id. This is an implementation detail
PostgreSQL does not document; the repo test is the guard, and the
observation goes into `docs/measurements/database.md`, including on a
hypertable chunk. `RETURNING` yields nothing for the `DO NOTHING` branch
and nothing for a `DO UPDATE ... WHERE` that evaluates false, which is
exactly what makes the next clause work. The returned row is the
event's `row`; no matching back to the proposed rows, no second read.

**The distinctness predicate.** `DO UPDATE ... WHERE <changed>`, where
`<changed>` is:

```
(t.c1, t.c2, ...) IS DISTINCT FROM (excluded.c1, excluded.c2, ...)   -- comparable columns
OR GREATEST(t.m, excluded.m) IS DISTINCT FROM t.m                    -- each monotonic column
```

Comparable columns are `update_columns ∩ present` minus
`volatile_columns` minus `monotonic_columns`. A monotonic column that
`GREATEST` would raise is a change and must both update and emit; one it
would leave alone is neither. When the comparable set and the monotonic
set are both empty -- `UNCHANGED_UPDATE_COLUMNS = ("fetched_at",)`
writes from the hash gates are exactly this -- **no predicate is added
and no event is emitted**; the statement is today's. `guard_column`
keeps its `excluded.guard > t.guard` predicate, `AND`ed with
`<changed>`. The `DO NOTHING` shape is unchanged: inserts are returned,
existing rows are left alone, as today.

`volatile_columns` is a new `TableWrite` field, default
`("fetched_at", "first_seen_at", "as_of_date", "raw_json")`.
`asof_base.VOLATILE_COLUMNS` is redefined from the same constant so the
two cannot drift. `raw_json` is there because Yahoo re-serialises it
with different ordering and timestamps on every fetch; without the
exclusion every `ticker_info` row would emit an update per run. Whether
that holds is measured (see Measurements) before the default is final.

**Volatile columns keep today's behaviour.** With the predicate, a row
whose comparable columns are unchanged is not updated at all -- so
`fetched_at` (which `HashGate` reads as "last verified at") and
`as_of_date` (which `prune_asof` reads) would freeze. To prevent that,
after the INSERT the writer runs one `UPDATE t SET v1 = :v1, v2 = :v2
WHERE <key> IN (<keys not returned>)` over `volatile_columns ∩
update_map`, with the values from the proposed rows (the same
`SyncContext.fetched_at` every table of the symbol gets), so every
volatile column ends up exactly where today's single statement would
have put it. The touch runs only for the `DO UPDATE` shapes -- never for
`DO NOTHING`, never for rows the guard rejected -- and never on tables
where a volatile column is part of the key (`*_history` snapshots). The
touch emits no event.

`_verify` is untouched. Verification stays an independent key-existence
read after the write; the events are a by-product, not the proof.

### Range coalescing for bars

When a `TableWrite` targets a table whose route family is `bars` and the
`RETURNING` rows with `inserted = true` exceed `range_threshold`, the
collector replaces those inserts with one `op=range` event per
`(symbol, bar_interval)` present in the write, spanning the min and max
timestamp of the inserted rows. Updated rows (repairs) stay row-level
regardless of count. The consumer re-reads the span from
`/v1/symbols/{symbol}/bars`.

### The `replace_scope` path

Today: `DELETE` the scope, `INSERT` everything. With a collector:

1. `SELECT <key_columns> FROM t WHERE <scope>` -- the keys currently in
   scope. `scope_columns` is a primary-key prefix on every
   `replace_scope` table (`company_officers`, `financial_facts`,
   `domain_top_*`), so this is an index scan.
2. `DELETE ... WHERE <key> IN (<present − incoming>) RETURNING <key>`
   -- one `delete` event per removed key. When the write has no rows,
   `incoming` is empty and the whole scope is deleted, as today.
3. The incoming rows go through the upsert path with the update map
   widened to **every non-key column of the table** (`excluded.col`
   carries the column default for columns the row omits). That is what
   makes upsert equal to delete-plus-insert: the current
   `update_columns` on these tables are partial (`financial_facts`:
   `("value",)`, `sec_filings`: `("url",)`) and would otherwise leave
   stale values behind. Delete events precede the upserts in the
   outbox, so a key that is removed and re-added in one write cannot
   happen -- it is in `incoming`, hence not deleted.

Without a collector the single `DELETE` and the original update map
stay. The nine dataset modules that use `replace_scope` do not change.

### Deletes outside a sync

`pipeline/prune.py` (seven delete sites) and `symbols purge`
(`cli/app.py:417-437`) already use ORM `delete()`. For tables outside the
`bars` family each gains `RETURNING <key>` and hands the keys to
`collector.record(table, "delete", key, None)`. For `bars`-family
tables (`price_bars`, `periodic_bars`, `price_history`, `dividends`,
`splits`, `capital_gains`, `shares_full`) a purge or a time-bounded prune
emits one `op=range` event per `(symbol, table)` with `rows` = the
deleted count and `op` qualified as `range` with `"kind": "delete"`;
returning millions of bar keys from a `DELETE` is the cost the range
event exists to avoid. The collector is created with `run_id=None`;
`--dry-run` never creates one.

### Rescale

`storage/rescale.apply_pending(session, symbol)` gains a keyword
`collector: ChangeCollector | None`. It rewrites `price_bars` with an
ORM `UPDATE` and records one `op=rescale` event per applied split via
`record_rescale`. Both call sites pass the collector: `persist_symbol`
(it runs `rescale_before_bars` before the dataset loop, so the
collector exists before `enter_dataset`) and `yfin bars rescale`
(`cli/bars.py`, `run_id=None`).

### Flushing

`collector.flush(session)` runs `SELECT clock_timestamp()`, renders the
events with `copy_body` and executes one `COPY pipeline_outbox
(created_at, family, partition_key, payload) FROM STDIN` per
transaction. `xid` takes its default. The COPY is the last statement of
the transaction, so the writing transaction's `xid` is already assigned
by the data writes that precede it.

### The relay, generalised

`stream/relay.py` and `stream/kafka.py` move to `outbox/relay.py` and
`outbox/kafka.py`; `stream/` re-exports nothing and its module docstring
is updated. `OutboxRelay` takes an `OutboxSpec`:

```python
@dataclass(frozen=True)
class OutboxSpec:
    table: str                       # "stream_outbox" | "pipeline_outbox"
    offset_table: str
    cursor: Literal["id", "xid"]     # how the relay walks and what the offset stores
    lock_name: str                   # literal; outbox/ does not import stream/
    route_column: str                # "exchange" | "family"
    key_column: str                  # "symbol" | "partition_key"
    topic_pattern: str
    placeholder: str                 # "{exchange}" | "{family}"
    upper_case_route: bool           # True for exchanges, False for families
    client_id: str                   # "yfin-stream-relay" | "yfin-changes-relay"
    id_header: bool                  # False for ticks, True for changes
```

`OutboxMessage` becomes `(id, cursor_value, key, route, payload)`;
`publish` produces with `key`, the rendered topic, and -- when
`id_header` -- the `yfin-outbox-id` header. `topic_for` takes the
placeholder and the case rule from the spec. The tick spec reproduces
today's behaviour exactly and the Kafka unit tests change only where
`OutboxMessage` fields were renamed; `OutboxRelay(factory, config)`
gains a `spec` argument and `tests/repo/test_stream_relay_repo.py`
passes the tick spec.

**Cursor `xid`.** One pass reads

```sql
SELECT id, xid, partition_key, family, payload
  FROM pipeline_outbox
 WHERE xid > :last_xid
   AND xid < pg_snapshot_xmin(pg_current_snapshot())
 ORDER BY xid, id
 LIMIT :batch
```

and, after every acknowledgement, stores the batch's last `xid`. A row
becomes eligible only once every transaction with a smaller `xid` has
ended, and new rows only ever carry larger `xid`s, so a transaction that
took its outbox ids early and commits late is waited for, never skipped.
The batch boundary is moved to the end of the last complete `xid` in it,
so one transaction's events are never split across an offset advance.

**Chunk cleanup with concurrent writers.** `drop_published_chunks` for
the `xid` cursor uses `cutoff = LEAST(min(created_at) WHERE xid >
:last_xid, now() - 2 × chunk interval)`. A transaction that has run
`COPY` and not yet committed holds a lock on its chunk, and `drop_chunks`
would wait for it and then drop its unpublished rows; the second term
keeps the two newest chunks alive, which bounds the exposure to a
transaction that stays open more than two hours after its flush. The
flush is the last statement before commit, so that does not happen
outside a pathological lock wait, and that case is logged by
`persist_with_retry` anyway. The tick relay keeps its current cutoff.

`relay_lag(spec)` becomes cursor-aware and is the one function
`yfin changes status`, `yfin status` and the observability exporter
call; for `xid` it counts visible rows with `xid > :last_xid`.

**Topics** are verified, never created: partition count is the
operator's decision, and raising it later reorders keys. Seven topics,
`yfin.changes.{reference,bars,fundamentals,holders,news,discovery,domains}`,
lower case, exactly the `DataFamily` values.

### Settings

Group `stream`, alongside the Kafka settings:

| setting | default |
| --- | --- |
| `yf_changes_enabled` | `False` |
| `yf_changes_topic_pattern` | `"yfin.changes.{family}"` |
| `yf_changes_range_threshold` | `1000` |

`yf_kafka_bootstrap_servers` and `yf_kafka_relay_batch` are shared with
the tick relay. Each setting also appears in `.env.example`, which
`tests/unit/test_env_example.py` requires. When `yf_changes_enabled` is
off no collector is constructed and no outbox row is written.

### CLI

New group `yfin changes`:

- `yfin changes relay [--once]` -- drains `pipeline_outbox`. Exit 1 when
  `yf_changes_enabled` is off or the last pass recorded an error, as
  `yfin stream relay` does.
- `yfin changes status` -- `N row(s) unpublished; oldest Ns behind`.

`yfin status` prints the same line only when `yf_changes_enabled` is on.
`yfin stream relay` is unchanged in behaviour: two relays, two
processes, two locks, two offsets, two `client.id`s.

## Error handling and guarantees

- **Atomic with the data.** A failed commit loses both the rows and their
  events; a successful one guarantees eventual publication. A broker
  outage never blocks a sync; the outbox grows and `changes status`
  shows by how much.
- **Retry replay** produces one set of events: the collector is built
  per attempt and the distinctness predicate cannot see rolled-back
  writes.
- **Hash gates** skip child writes entirely, so they also skip events;
  their header writes carry only `fetched_at` and, by the empty-set
  rule, emit nothing. Gate state tables are infrastructure and never
  published.
- **Full refresh / first sync** publishes every non-bar row as `insert`
  and every bar table as `range` events. That is the intended way for a
  consumer to build a mirror from nothing.
- **Relay failure** leaves the offset where it was and retries the
  batch; `RelayStats.last_error` turns into exit 1. A missing topic is a
  warning at start-up and a stuck batch at publish time -- visible,
  rather than a silently mis-partitioned auto-created topic.
- **Schema evolution.** Adding a column is compatible; consumers ignore
  unknown fields. Removing or renaming one bumps `v`. The committed
  `docs/changes/schema.json` makes every such change a reviewable diff.

## Tests

Unit, no database:

- `ChangeCollector`: op recording, dataset context, infrastructure
  tables ignored, range coalescing at the threshold, JSON rendering of
  `Decimal`, `datetime`, `date`, NaN, numpy scalars, one collector per
  attempt.
- `_insert_stmt` with a collector compiles to a statement containing
  `RETURNING *` and the predicate, including the `GREATEST` term for
  monotonic columns; with `update_columns=("fetched_at",)` it compiles
  to exactly today's SQL; without a collector every shape compiles to
  exactly today's SQL (regression lock, compared as text).
- Routing: the four assertions listed under Routing.
- `outbox/`: the tick spec and the changes spec drive the same relay
  against a fake producer; the header appears only for the changes
  spec; `topic_for` upper-cases exchanges and not families.
- `docs/changes/schema.json` equals the generated document.
- `tests/unit/test_dataset_layer_boundary.py` is widened to reject
  `yfin.storage` and `yfin.models` imports from dataset modules, not
  only `sqlalchemy`.

Repo (`-m repo`):

- Writing the same rows twice yields events once; a change to a
  volatile column alone yields none but the column is updated to the
  proposed value; `DO NOTHING` and guard-rejected rows are not touched.
- `xmax = 0` distinguishes insert from update on a plain table and on a
  `price_bars` chunk.
- A monotonic column raised by `GREATEST` emits `update`; one left alone
  emits nothing.
- `replace_scope`: removed key → `delete`, unchanged → nothing, new →
  `insert`, changed → `update`, and a column omitted by the new rows is
  reset as delete-plus-insert would have reset it.
- Bars above the threshold → one `range` per `(symbol, interval)`;
  below → rows.
- Two concurrent transactions where the earlier `xid` commits later:
  the relay publishes both, in `xid` order, and never advances past
  the open one.
- `persist_with_retry` on a forced `40001` emits one set.
- `prune` and `purge` land in the outbox with `run_id = null`; bars
  tables as `range`.
- `drop_published_chunks` keeps the two newest chunks; `relay_lag`
  counts visible unpublished rows for both cursors.

## Measurements to record

Into `docs/measurements/database.md`:

- Write-time cost of `RETURNING *` + the predicate + the volatile touch:
  a full AAPL sync (37,295 rows) with the collector on and off.
  Acceptance: within 10 %.
- Event volume: a daily sync of an unchanged symbol with and without
  `raw_json` in `volatile_columns` (this decides the default), and a
  first sync (expected: rows written, bars as ranges).
- Relay throughput (messages/s) against the local broker and the outbox
  growth rate during a first sync of 100 symbols.
- The `xmax = 0` observation, with the command and its raw output.

## Files

New:

- `src/yfin/storage/changes.py` -- `ChangeCollector`, envelope rendering
- `src/yfin/storage/routing.py` -- `Route`, `ROUTES`, `INFRASTRUCTURE_TABLES`, `GATE_TABLES`
- `src/yfin/storage/copy.py` -- `copy_body`, `jsonable` (moved from `stream/writer.py`)
- `src/yfin/outbox/__init__.py`, `outbox/relay.py`, `outbox/kafka.py` -- moved from `stream/`, generalised
- `src/yfin/models/changes.py` -- `pipeline_outbox`, `pipeline_relay_offset`, `changes_timescale_ddl`
- `src/yfin/models/types.py` -- `Xid8Type`
- `src/yfin/cli/changes.py` -- `yfin changes relay|status`
- `migrations/versions/<ts>_pipeline_outbox.py`
- `docs/changes/schema.json`, `scripts/dump_change_schema.py`
- tests listed above

Changed:

- `storage/contracts.py` -- `TableWrite.volatile_columns`
- `storage/persistence.py` -- collector hook, `RETURNING *`, the predicate, the volatile touch, `replace_scope` diff and widened update map
- `storage/rescale.py` -- `collector` keyword, `op=rescale`
- `datasets/asof_base.py` -- `VOLATILE_COLUMNS` from the shared constant
- `pipeline/payload.py`, `pipeline/turn.py` -- `collector` field
- `pipeline/persist.py`, `pipeline/runner.py`, `pipeline/shard.py`, `pipeline/market_runner.py`, `pipeline/domain_runner.py` -- collector lifecycle
- `pipeline/prune.py`, `cli/app.py` (`purge`), `cli/bars.py` (`rescale`) -- delete / range / rescale events
- `models/__init__.py` -- `all_timescale_ddl`
- `stream/writer.py`, `stream/__init__.py`, `cli/stream.py` -- imports after the move
- `api/storage/catalog.py` -- wire-type map moved to `core/`
- `core/config.py`, `.env.example` -- three settings
- `tests/unit/test_dataset_layer_boundary.py` -- widened
- `README.md` -- Architecture diagram (`outbox/`), Layout table (`cli/`, `outbox/`), Integration status "Kafka producer" row, command list, "87 tables; 19 infrastructure" → 89 / 21, settings count
- `docker-compose.yml` -- the "nothing produces to this broker yet" comment

## Implementation order

1. `storage/routing.py` with the four assertions; `storage/copy.py`;
   the widened boundary test. Pure refactor, no behaviour change.
2. `outbox/` extraction with `OutboxSpec`; tick relay green on the new
   module with the tick spec.
3. `Xid8Type`, models, migration, `changes_timescale_ddl`;
   `ChangeCollector` with unit tests.
4. Writer: `RETURNING *`, the predicate, the volatile touch; repo
   tests; the `xmax` measurement.
5. `replace_scope` diff; range coalescing; `prune` / `purge` / rescale
   events.
6. Collector lifecycle in the runners, `persist_symbol`, `run_turn`.
7. `xid` cursor in the relay, cleanup cutoff, `relay_lag(spec)`; the
   concurrency repo test.
8. Settings, CLI group, `yfin status` line, schema dump + CI diff,
   docs.
9. Cost, volume and throughput measurements; the `raw_json` decision.

The observability design's relay metrics, `{outbox}` label and
`relay_lag` gauges depend on step 2 and step 7; its `INFRASTRUCTURE_TABLES`
entries depend on step 1. Its own steps 1–5 (metrics module,
`scheduler_runs`, `run_metrics`, scheduler, exporter) are independent of
this design and can proceed in parallel with steps 3–6 here.

## Out of scope

- A change-feed endpoint on the API (`/v1/changes?cursor=`), or `since`
  filters on dataset endpoints.
- Kafka ACLs, topic creation, schema registry, Avro/Protobuf.
- A consumer SDK.
- Per-bar events for a rescale or a bulk backfill (see Range
  coalescing).

## Revisions

Changes made after the independent review of the first draft:

- **Relay cursor.** The first draft walked `id` like the tick relay;
  concurrent symbol transactions make that lose rows. Now `xid8` with
  a snapshot-xmin bound, and a cleanup cutoff that keeps the two newest
  chunks.
- **Routing.** `Dataset.family` is dropped; family and partition column
  are per table in `storage/routing.py`, because several tables are
  written by datasets of different families and some symbol-registry
  tables have no `symbol` column.
- **Predicate edge cases.** Empty comparable set adds no predicate;
  monotonic columns get a `GREATEST` term; the volatile touch is
  limited to the `DO UPDATE` shapes and covers every volatile column in
  the update map, with the proposed values, so `fetched_at` and
  `as_of_date` behave exactly as today.
- **`RETURNING *`** instead of matching returned keys back to proposed
  rows, so the event carries the stored row.
- **`replace_scope`** widens the update map to every non-key column so
  upsert equals delete-plus-insert.
- **Range events** for bulk bar writes, purges and prunes of bar
  tables.
- **Dedupe** on the outbox `id` carried as a Kafka header.
- **Rendering** shares `canonical_json`'s defaults; `date`, NaN and
  numpy handled; `copy_body` moved under `storage/`.
- **Collector lifecycle** owned by the runners; one collector per
  attempt; `apply_pending` takes the collector.
- Line references, `item_key`, message-size and README claims
  corrected.

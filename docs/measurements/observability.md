# Observability

What the monitoring stack costs the thing it monitors. An exporter that
makes the pipeline slower has spent the budget it was supposed to protect,
so every number here is about the price of looking.

Environment: PostgreSQL 18.6 + TimescaleDB 2.29.2 (the pinned image in
`docker-compose.yml`), default `work_mem`, on the development machine.
Durations are wall clock from the Python side, so they include the round
trip and result materialisation — which is what the exporter thread
actually waits for.

## The freshness query

The exporter runs it every `yf_exporter_interval_seconds` (300). The
acceptance criterion in the design is **5 seconds**: above that the next
design is a materialised watermark table, not a bigger machine.

    uv run python scripts/measure_freshness_query.py
    uv run python scripts/measure_freshness_query.py --runs 7 --explain

The script builds a synthetic `sync_run_items` in a throwaway schema —
10,000 symbols × 49 symbol datasets = **490,000 cells**, one run per night,
with a status mix of 80 % `ok` / 15 % `skipped` / 3 % `empty` / 2 %
`failed`.

| History kept | `sync_run_items` rows | median | max | verdict |
|---|---|---|---|---|
| 3 nights | 1,470,000 | 2.35 s | 2.61 s | PASS |
| 7 nights | 3,430,000 | 4.89 s | 5.12 s | **FAIL** |

**The cost scales with rows, not with cells.** Both runs report the same
49 series and the same 490,000 cells; the only thing that changed is how
many times each cell appears. The query has to reduce every item of every
run before it can pick the latest one, and there is no way to look at a
cell's newest row without having grouped its older ones.

So the acceptance criterion is met **only with a bounded audit retention**.
`yfin prune --audit-days N` is what bounds it, and this measurement is the
reason it is not optional in a deployment that runs the exporter: at a
nightly `sync`, seven days of audit history is already past the limit.
Three days is comfortable, and the plan degrades linearly, so the rule of
thumb is roughly **0.7 s per million items**.

### The shape of the statement, measured twice

The first version followed the design literally: two `DISTINCT ON` CTEs —
the latest run per cell, and the latest *good* run per cell — joined back
together on `(symbol, region, dataset)`.

| Statement | median (3 nights) |
|---|---|
| Two `DISTINCT ON` CTEs + `LEFT JOIN` | 4.24 s |
| One `GROUP BY` with `array_agg` and a `FILTER`ed `MAX` | **2.35 s** |

`EXPLAIN (ANALYZE, BUFFERS)` says why: the join could only merge on
`symbol` — `region IS NOT DISTINCT FROM` is not a mergeable condition — so
it produced 23.5 million rows and threw 23.0 million of them away in a join
filter, on top of sorting 1.47 million rows **twice**. The single-pass
version sorts once and never materialises the intermediate cross product:

```
GroupAggregate                                    (actual time=2244..2402 rows=49)
  -> Sort  Sort Key: scope, dataset               (external merge  Disk: 20MB)
     -> GroupAggregate  Group Key: symbol, region, dataset   (rows=490,000)
        -> Sort  Sort Key: symbol, region, dataset, run_id DESC
                                                  (external merge  Disk: 77MB)
           -> Hash Join  (rows=1,470,000)
              -> HashAggregate  Group Key: run_id, symbol, region, dataset
                 -> Seq Scan on sync_run_items    (rows=1,470,000)
```

Both statements return identical numbers — the 65 cases in
`tests/repo/test_exporter_queries_repo.py` passed unchanged across the
rewrite, which is what made the swap safe to make on a measurement alone.

### `ix_sync_run_items_cell_run` is not used by this query

The design added `(symbol, region, dataset, run_id DESC)` on the premise
that the freshness query walks a cell backwards to its latest run. It does
— but it does that for **every** cell, so the planner reads the table
sequentially and never touches the index. The plan above is the whole
evidence: `Seq Scan on sync_run_items`, no index node anywhere.

The index is not wrong, it is simply not what makes this query fast. It
still serves a single-cell lookup, which is what a "why is this symbol
stale" investigation is. Whether to keep paying for it on a table that
grows by symbols × datasets every night is a retention decision, and it is
recorded here rather than acted on: dropping an index is a migration, and
this measurement is not an argument for one until somebody has asked the
single-cell question in anger.

### `work_mem` is not the bottleneck

Both sorts spill to disk at the default `work_mem` (77 MB and 20 MB of
external merge). Raising it to 256 MB takes the median from 2.35 s to
**2.09 s** — 11 %. Worth setting on a machine that has the memory, not
worth a deployment note, and nowhere near enough to change the retention
conclusion above.

## `asof_state` answers a cheaper question, not the same one

The design left this open: whether `asof_state.fetched_at` could replace
the freshness query for the as-of datasets. It is already a materialised
watermark — one row per `(symbol, dataset)`, `fetched_at` updated on every
verification even when the hash is unchanged — so the same 490,000 cells
cost a single grouped scan:

| Source | rows scanned | median |
|---|---|---|
| `sync_run_items`, 3 nights | 1,470,000 | 2.35 s |
| `asof_state` | 490,000 | **0.048 s** |

Forty-nine times faster, and it is still the wrong table for this gauge.
Three things it cannot express:

* **No status.** `fetched_at` says a fetch happened, not how it ended. The
  universe rule is defined on status — `out_of_scope` and `unknown_symbol`
  leave, `not_attempted` stays and counts as a gap — and none of those
  three exist here. A symbol that dropped out of `intraday_scope` would
  keep counting against freshness forever.
* **No region.** Domain cells exist once per region; `asof_state` is keyed
  by `(symbol, dataset)` alone, so two regions of one taxonomy key collapse
  into one row and one of them can go stale unseen.
* **Not every dataset.** It covers the hash-gated ones. The datasets most
  worth watching for staleness are the ones that write on every run.

The conclusion is the one the numbers point at without being the one they
seemed to promise: `asof_state` is the right shape for a watermark table
and the wrong contents. If the freshness query outgrows its budget, the
next design is a watermark table **carrying the status and the region**,
and this measurement is the evidence that such a table would run in
milliseconds.

## What the instrumentation costs

    uv run python scripts/measure_observability_cost.py --lines-per-sync 101000

All three numbers answer the same question — does the instrumentation
change the thing it measures — and all three answer *no*, one of them for
a reason nobody predicted.

### Log rendering, per line

| format | per line |
|---|---|
| `console` | 18.9 µs |
| `json` | **15.3 µs** |
| a line below the level | 1.58 µs |

**JSON is the cheaper format, by about 20 %.** That is the opposite of the
assumption the design was written under. `ConsoleRenderer` pads keys,
aligns columns and decides colours; `JSONRenderer` is one `json.dumps`.
The console number here is even the *favourable* one: it was measured with
`colors=False`, because the benchmark's stderr is not a terminal, and
colouring only adds to it.

So the format is chosen for who reads it — a human at a terminal, or
Loki — and not for what it costs. The `LOG_FORMAT` default (`console` on a
TTY, `json` otherwise) already picks on exactly that basis.

The third row is the one that matters most and is the easiest to overlook.
A call below the threshold costs 1.58 µs because `structlog.stdlib.
filter_by_level` is FIRST in the chain: the line is dropped before
redaction, timestamping or trace lookup runs. A chain that filtered last
would pay the full 15 µs for every DEBUG call in a process running at INFO.

### Over one full sync

Measured on the backfill of the full 5,888-symbol universe: **17.2 log
lines per symbol**, so a full pass writes about **101,000 lines**.

| format | total rendering time |
|---|---|
| `console` | 1.9 s |
| `json` | 1.5 s |

Against a pass that takes 33 hours, the whole difference is 0.4 seconds --
0.0003 %. The log format is not a performance decision at this scale, and
nothing in the pipeline should be shaped around it.

### Spans

| state | per span |
|---|---|
| off (`_enabled` false) | 0.54 µs |
| on, sampling 1.0 | 16.7 µs |

A symbol draws 47 spans: one `sync.symbol` around its persist, and one
`sync.dataset.fetch` per dataset. At sampling 1.0 that is **0.78 ms per
symbol**.

The measured throughput of the same run is 176 symbols/hour, i.e. 20
seconds per symbol, so tracing costs **0.004 %** of a symbol's wall clock.
The design's worry — that a span per dataset is too many — is not borne
out; the fetch it wraps is three orders of magnitude more expensive than
the span.

The off case is the one worth keeping an eye on. 0.54 µs is the cost of a
`with` on a generator that yields `None`, and it is paid 47 times per
symbol in every process that is not being traced — 25 µs per symbol,
which is still nothing. It stays that cheap only because `span()` checks a
module-level flag before it imports or looks up anything.

### One scrape

| | |
|---|---|
| exposition lines | 323 |
| `generate_latest()` | 0.93 ms |

This is the question behind "the metrics endpoint's effect on `stream
run`'s write ceiling": rendering the registry holds the GIL, and the
writer thread wants it. At a 15-second scrape interval the endpoint holds
the GIL for **0.0062 %** of wall clock.

Against `websocket.md`'s measured write ceiling of 22,291 ticks/s on the
COPY path, that is about 1.4 ticks' worth of delay per scrape — inside a
batch that already carries hundreds. The stream's own
`yfin_stream_batch_seconds` on the running stack reads **14 ms at p50**
against a 250 ms batch interval, with the endpoint scraped throughout.

A caveat this measurement cannot remove: it was taken on a Sunday evening
with the equity markets closed, so the message rate was crypto and
after-hours only. The comparison against a full regular session is still
open, and is the same pending measurement `websocket.md` already names.

## Still to record

* The first real values behind every alert threshold and the freshness
  factor. Every number in
  `deploy/observability/grafana/provisioning/alerting/rules.yml` is a
  starting value, and the file says so in its own header: a month of real
  data is what replaces them, and nothing shorter will do.
* The stream's write ceiling during a full regular session, against
  `websocket.md`. The scrape measurement above was taken with the equity
  markets closed.

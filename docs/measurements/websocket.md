# Live WebSocket streaming — measured behaviour

Measurement date: 2026-09-06 (**Sunday** — US and European markets closed).
Environment: PostgreSQL 18.6 + TimescaleDB 2.29.2 (`timescale/timescaledb:2.29.2-pg18`),
yfinance 1.7.0, macOS, local Docker.

Each entry is the basis for a number or a claim in the WebSocket
streaming design. Cited by name rather than by path: measurements outlive
the design documents that consume them, which is why they live here.

> **Scope warning.** The **write side** is complete and independent of
> the day of the week. The **stream side** was captured on a Sunday: only
> 24/7 crypto symbols produced messages, so message rate and field
> fill-rates for US equities count as **not measured**. These must be
> repeated during open market hours before any capacity claim about
> equities is made; the write-path ceiling below is what the current
> `yf_stream_*` defaults rest on, and it does not depend on them.
>
> The subscription limit and connection behaviour, on the other hand, are
> independent of the day of the week and have been measured — the first
> round's flawed result and its correction are recorded explicitly below.

## Subscription limit: 100 symbols / connection

### First, a corrected error

The first measurement round produced the result "Yahoo has no symbol
limit, 50,000 symbols accepted". **This result was wrong and came from a
methodology error.**

The test put 24/7 crypto symbols at the *front* of the list and checked
whether "messages are coming in". Because the server truncates the list
to the first 100 entries, those canaries always stayed inside the
truncation point; the stream appeared to keep going, but nothing past
symbol 100 had ever actually been subscribed.

The symptom was in the logs but read wrong: at the 10,000 and 50,000
levels, the "distinct symbol" count stayed at 8–12. This was attributed
to Sunday; the real cause was silent truncation.

### Canary method and the real limit

The correct method is to put the canary at the **end** of the list: if
truncation happens, the canary falls outside the truncation point and
goes silent.

| List (canary **at the end**) | Total symbols | Canaries receiving data |
|---|---:|---:|
| 50 filler + 5 canary | 55 | 5/5 |
| 90 filler + 5 canary | 95 | 5/5 |
| 94 filler + 5 canary | 99 | 5/5 |
| **95 filler + 5 canary** | **100** | **5/5** |
| **96 filler + 5 canary** | **101** | **4/5** |
| 120 filler + 5 canary | 125 | 0/5 |
| 200 filler + 5 canary | 205 | 0/5 |
| *Control:* 5 canary **at the front** + 200 filler | 205 | 5/5 |

**The limit is exactly 100 symbols.** At symbol 101, exactly one canary
drops — the server truncates the list to the first 100 entries,
preserving order. The control row confirms "first come, first served"
semantics and also explains the first round's error.

Truncation **produces no feedback at all**: no error frame, the
connection doesn't close, no acknowledgment. A client can believe it
subscribed to 10,000 symbols, receive 100, and never notice.

### Other properties of the limit

The following were measured in the first round by an independent
research agent; the truncation point above was verified identically in
this session.

| Property | Observation |
|---|---|
| Scope | Not per message, **cumulative per connection**. 100 symbols + 5 canaries in a separate frame → canaries go silent. |
| Priority | **First come, first served.** Later subscriptions do not evict existing ones. |
| `unsubscribe` | Frees slots **back up**; rotation is possible. |
| Invalid symbols | **Consumes a slot.** 95 fake + 5 canary → 5/5; 200 fake + 5 canary → 0/5. A dirty symbol list burns capacity directly. |
| Frame size | Not a limiting factor: a 103 KB frame with 13,193 symbols was accepted (and the first 100 processed). |

### A malformed message closes the connection silently

| Sent | Result |
|---|---|
| `{"subscribe": "BTC-USD"}` (not a list) | **connection closed** |
| `hello world` (not JSON) | **connection closed** |
| `{"bogus_action": ["X"]}` | **connection closed** |
| `{"subscribe": [null]}` | **connection closed** |
| `{"subscribe": []}` | alive |
| `{}` | alive |
| `{"unsubscribe": ["never-subscribed"]}` | alive |

The closure doesn't even carry a status code (1005). A single `None`
symbol leak drops the entire connection.

## Concurrent connections and client model

10,000 symbols ÷ 100 = **at least 100 concurrent connections**. Whether
this number is reachable was measured directly.

### Yahoo side: 110 connections without issue

With a client that only opens the socket (no read loop), from a single
IP:

| Open pattern | Established |
|---|---|
| 110 simultaneous, no delay | **110/110** |
| 110 staggered (100 ms apart) | **110/110** |

On Yahoo's side, neither connection refusal, rate limiting, nor a need
for staggered opening was observed.

### Client side: thread-per-connection collapses at 110

The same task — establish 110 connections, subscribe, read messages for
20 seconds — under two models:

| Model | Established | Received data | Messages |
|---|---:|---:|---:|
| A. Thread per connection (`websockets.sync`) | **19**/110 | 19 | 171 |
| B. Single asyncio event loop | **110**/110 | 110 | 1,210 |

`ulimit -n` is 1,048,576; it is not the file-descriptor limit. The sync
model opens one read thread (plus a timer) per connection and reaches
~330 threads at 110 connections; at this scale most connections never
get established.

**This is the measured justification for the design's K1 decision:**
the 10,000-symbol target is reachable with a single asyncio event loop,
not with thread-per-connection.

## Nature of the stream: not ticks, one snapshot per second

Intervals between consecutive messages for BTC-USD: `5.0, 4.0, 4.9, 5.0,
5.0, 5.0, 6.0, 3.0, 6.0, 5.0, 4.1, 5.9, 5.1, 5.0, 6.1` seconds — **all
whole-second multiples**, median 6.0 sec. The `time` field is in
milliseconds but always ends in `...000`.

BTC trades hundreds of times per second; this regularity can only be
explained by server-side sampling. An independent record points the same
way: for an NSE stock, `day_volume` increases by 1,000–1,200 units
between consecutive messages, meaning multiple trades per message
([yfinance#319](https://github.com/ranaroussi/yfinance/pull/319#issuecomment-842056388)).

**Conclusion: the incoming data is not tick-by-tick, it is a
consolidated snapshot fitted to a ~1-second grid.** This directly
confirms the design's decision not to derive a `volume` field:
`day_volume` is cumulative and jumps by thousands between messages, so
it cannot be reliably converted into per-minute volume.

The rate for a single symbol does not drop under load: on a 2-symbol
connection BTC received 23 messages, on a 99-symbol connection also 23.
The bottleneck is not bandwidth, it's the 100 quota.

**A 15-second resubscribe heartbeat is not necessary.** The connection
stayed open and the stream continued for 240 seconds with no message
sent. yfinance resends the entire set every 15 seconds; this is both
unnecessary and it makes the 100 truncation permanent. (Idle longer than
4 minutes was not tested.)

## Open-market round — pre-market, 2026-09-07 08:05 UTC

Second measurement round: Monday 04:05 EDT, the first minutes of
pre-market. 13 symbols (10 liquid US equities + 3 crypto), 5 minutes,
decoded in memory without writing to the database.

### Equities do not stream in pre-market

| Symbol group | Messages in 5 minutes |
|---|---:|
| AAPL, NVDA, TSLA, MSFT, META, SPY, QQQ, AMD | **0** |
| AMZN, GOOGL | 1 each |
| BTC-USD / ETH-USD / SOL-USD | 60 / 54 / 45 |

Pre-market is technically open at 04:00 ET, but nothing flows. **A
meaningful equity rate can only be measured during the regular session
(13:30 UTC)**, so that part of the design's stage 0 is still open.

Crypto confirms the first round: median inter-message gap is exactly
**5.0 seconds**, i.e. a server-side sampled snapshot rather than a tick
feed.

### The float32 artefact is real and near-universal

**148 of 161 prices (92%)** arrive from protobuf with a widened `repr` --
`232.35000610351562` where the wire said `232.35`. Without `f32_decimal`
nine tenths of the archive would carry that artefact. The smallest-looking
decision in the design turns out to have the broadest reach.

### The snapshot sent on subscribe can be days old

Lag distribution: **p50 2.61s, p95 6.46s**, but **p99 216,347s (~2.5
days)**.

The outliers are not a fault. Right after subscribing, Yahoo sends the
last known price, and in a closed market that price is Friday's close. So
`received_at - ts_utc` always looks enormous for the first seconds of a
connection.

Two consequences, both already handled:

- `yfin stream status` reports **p50 and p95**, not p99. A handful of
  stale snapshots would make p99 meaningless on its own.
- The `live_quotes` guard (`excluded.ts_utc > live_quotes.ts_utc`) stops
  those snapshots from rolling a current price backwards. The assumption
  that out-of-order delivery is ordinary on reconnect held up.

### Field coverage: bid/ask never arrive

| Field | Equity | Crypto |
|---|---:|---:|
| `price`, `change`, `change_percent`, `price_hint`, `exchange`, `quote_type`, `market_hours`, `id`, `time` | 100% | 100% |
| `day_high`, `day_low`, `open_price`, `previous_close`, `day_volume`, `currency` | **0%** | 100% |
| **`bid`, `ask`, `bid_size`, `ask_size`** | **0%** | **0%** |
| `circulating_supply`, `market_cap`, `vol_24hr`, `from_currency` | 0% | 100% |

**`bid`/`ask` arrived in neither group.** That was the open question: they
exist in the proto and not in the stream. So §7.2's "NULL means absent or
zero" caveat is, for these two fields, permanent.

The equity sample is only 2 messages, so this table is **not conclusive**
and must be repeated during the regular session.

### market_hours: PRE_MARKET (0) was never observed

All 161 messages carried **`market_hours = 1` (REGULAR)** -- including the
two equity messages that arrived during pre-market hours.

This neither confirms nor refutes the presence exception (§7.2). Treating
`0` as PRE_MARKET rather than "absent" remains an assumption until a
message actually carries it. The reasoning for keeping it stands -- if
wrong, the cost is writing a redundant `0`; if right, the gain is that
`is_extended` can be derived at all -- but the line stays unmeasured.

`quote_type` is confirmed: 8 (EQUITY) and 41 (CRYPTOCURRENCY).

## `_verify()` on a many-chunk hypertable

The first round measured this at 2% -- but on a single day's data, i.e.
one chunk, and the design left it as an open question. Measured against
200 days (200 chunks, 100,000 rows):

| Query | Chunks touched | Median |
|---|---:|---:|
| No range clause (original) | **200 / 200** | 21.5 ms |
| With `ts_utc BETWEEN` | **2** | **2.2 ms** |

TimescaleDB cannot infer a time bound from a row-constructor `IN`, so
without the clause **every batch touches every chunk**, and the cost grows
linearly with the age of the archive. On one day of data the two are
indistinguishable -- which is exactly why it was missed the first time.

`writer._verify` now adds the batch's own `ts_utc` range to the
predicate. The verification guarantee is unchanged; only the number of
chunks the planner visits.

## Enum codes

`pricing.proto` defines these fields as bare `int32`; their meaning must
be mapped by the client. Values are from
[yliveticker/yaticker.proto](https://github.com/yahoofinancelive/yliveticker/blob/main/yliveticker/yaticker.proto),
partly confirmed by measurement:

```
MarketHoursType: PRE_MARKET=0  REGULAR_MARKET=1  POST_MARKET=2
                 EXTENDED_HOURS_MARKET=3
QuoteType:       NONE=0 ALTSYMBOL=5 HEARTBEAT=7 EQUITY=8 INDEX=9
                 MUTUALFUND=11 MONEYMARKET=12 OPTION=13 CURRENCY=14
                 WARRANT=15 BOND=17 FUTURE=18 ETF=20 COMMODITY=23
                 ECNQUOTE=28 CRYPTOCURRENCY=41 INDICATOR=42 INDUSTRY=1000
OptionType:      CALL=0  PUT=1
```

Confirmed by measurement: BTC-USD/ETH-USD → `quote_type=41`
(CRYPTOCURRENCY), `market_hours=1` (REGULAR_MARKET; crypto is 24/7).

**`PRE_MARKET = 0` is decisive for the design:** in proto3, 0 is
indistinguishable from "not sent", so the "a default value means the
field was absent, store NULL" rule that `protocol.py` applies elsewhere
cannot be applied to `market_hours` — doing so would silently erase every
pre-market quote.

## Envelope format

The `version=2` outer envelope is JSON:

```json
{"type":"pricing","message":"CgdCVEMtVVNEFX03nEcYwLDW6o5oIgNVU0Qq..."}
```

The only observed `type` value is `"pricing"`. yfinance only reads the
`message` field and never checks `type`; if another `type` arrives, an
empty string is passed to the protobuf and it silently produces an empty
record.

## Write path capacity

Method: an exact copy of the `live_ticks` schema (36 columns, FK to
`symbols`, `by_range(ts_utc, INTERVAL '1 day')` hypertable), in a
temporary schema, with a single writer connection, rotating through 500
symbols. Each stage was measured on top of the previous stage.

### Step-by-step cost (batch = 500 rows)

| Stage | rows/sec | extra cost |
|---|---:|---|
| A. `INSERT ... ON CONFLICT DO NOTHING` only | 10,683 | — |
| B. + key-existence `verify` query | 10,513 | **2%** |
| C. + `live_quotes` guarded upsert | 6,985 | 34% |
| D. + `stream_outbox` write (Kafka on) | 6,219 | 11% |

Two results directly affect the design:

- **`_verify()` is practically free (2%).** The cost of preserving the
  "every write is verified by reading it back" guarantee in the live
  stream too is not measurably significant. It would be wrong to treat
  this as a trade-off.
- **The `live_quotes` upsert alone eats 34%.** This is the single most
  expensive piece; it means one conflict resolution per distinct symbol
  in the batch.

### Effect of batch size

| Batch | rows/sec |
|---:|---:|
| 500 | 12,071 |
| 2,000 | 12,686 |
| 5,000 | 12,864 |

Growing the batch tenfold gains only **6%**. A small batch (500) can be
preferable: it keeps latency low and narrows the loss window.

### `COPY` path

| Method (batch = 2,000) | rows/sec |
|---|---:|
| `INSERT ... ON CONFLICT` | 12,686 |
| **`COPY` → temp table → `INSERT ... SELECT ... ON CONFLICT`** | **39,301** |

**3.1x.** Because `COPY` doesn't support `ON CONFLICT`, two steps are
needed: the body is loaded via `COPY` into an `ON COMMIT DROP` temp
table, then moved to the target with a single `INSERT ... SELECT`. Dedup
and FK checking are preserved in the second step.

### `live_quotes` cadence

| Method | rows/sec |
|---|---:|
| Upsert every batch (of 500) | 8,976 |
| Upsert every 4th batch (accumulated latest values) | 11,024 |

**23% gain.** `live_quotes` is a derived view; its source is
`live_ticks`. Updating it on a fixed cadence instead of every batch
delays the latest state by a few hundred milliseconds, it does not lose
data.

### Result: ceiling

The full path measured with a single writer thread (tick + verify +
outbox + quotes):

| Configuration | ticks/sec |
|---|---:|
| A. `INSERT` + `live_quotes` every batch + `INSERT` outbox | **6,219** |
| B. `COPY` ticks + `INSERT` outbox + sparse quotes | 13,634 |
| C. **`COPY` ticks + `COPY` outbox + sparse quotes** | **22,291** |

The difference between B and C is instructive on its own: moving
`live_ticks` to `COPY` is not enough. As soon as that's done, **the
bottleneck shifts to `stream_outbox` itself** — once the outbox is also
written with `COPY`, throughput jumps another notch. With Kafka on, the
outbox deserves the same write technique as the tick table.

A scenario where 10,000 symbols each produce an average of 1
message/second means 10,000 ticks/sec:

- A (the initial design) does **not** meet this.
- C meets this with a 2.2x margin.

This makes `COPY` not an optimization but a precondition for the
10,000-symbol target. The other two preconditions of the same target
were measured above: **≥100 connections** (100 symbol/connection quota)
and **a single asyncio event loop** (thread-per-connection collapses at
110).

## Library behaviour (yfinance 1.7.0)

| Claim | Observation |
|---|---|
| `AsyncWebSocket`'s reconnect does not work | The `except` branch of `listen()` calls `_connect()`; `_connect()` only connects when `self._ws is None`, and on the error path `_ws` is never reset to `None`. An infinite error loop that spins every 3 sec on a closed socket. (`live.py`) |
| Synchronous `WebSocket.listen()` stops silently on error | The generic `except` branch does `break`. (`live.py`) |
| Using `websockets.sync.client.connect` outside a context manager is **deprecated** | `DeprecationWarning: connect() must be used as a context manager` — yfinance calls it exactly this way (`sync_connect(self.url)`). |
| Invalid symbol is silently ignored | No error message came back for 45,000 filler symbols. |
| Message envelope | `{"message": "<base64 protobuf>"}`; body is `PricingData`, 33 fields. |
| First-message latency | 1.1–3.8 sec after subscribing (crypto, Sunday). |
| **`_subscriptions` is a `set`** | Sending is done with `list(self._subscriptions)`. Once past 100 symbols, **which 100 symbols survive is decided by Python's set ordering** — it varies across processes with `PYTHONHASHSEED`. No warning is given to the user. |
| `subscribe()` resends the entire accumulated set | Not just the new ones; `_periodic_subscribe` repeats this every 15 sec and makes the truncation permanent. |

## Symbol universe (in this setup)

From the `symbols` table, 2026-09-06:

| Metric | Value |
|---|---|
| Total symbols | 5,735 |
| `exchange IS NULL` | 0 |
| Distinct `exchange` | 9 |

Distribution (at first measurement, when there were 2,717 symbols): NAS
698, PCX 617, NYQ 474, NMS 335, NGM 236, BTS 178, YHD 156, NCM 18, ASE 5.

**The distribution is extremely unbalanced** — the largest group is 140x
the smallest. A design that opens one connection per exchange would run
a 5-symbol connection side by side with a 698-symbol connection. The
exchange count (9) being close to the connection ceiling is also a
coincidence, and it changes as the universe grows.

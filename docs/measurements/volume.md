# Volume projections

Measured bar density: `AAPL` 1m without pre/post market is 390 bars per
session, with pre/post ~960 (04:00–19:55). 252 sessions per year.

## Bars, first year

| interval | symbols | rows/year | ~size/year |
|---|---|---|---|
| `1m` | 500 (`intraday_scope`) | ~121 M | ~12 GB |
| `5m` | 5,000 | ~242 M | ~25 GB |
| `15m` | 5,000 | ~81 M | ~8 GB |
| `60m` | 5,000 | ~20 M | ~2 GB |
| `1wk` + `1mo` | 5,000 | ~0.32 M | ~30 MB |
| **total** | | **~464 M** | **~50 GB** |
| + `ix_price_bars_local_date` | | | ~14 GB |
| **end of first year** | | **~464 M** | **~64 GB** |

**This is a conservative upper bound, not an average.** The US pre/post
density (960 bars/session) and 252 sessions/year were applied to the
whole universe. Exchanges without pre/post trading come in well below it;
24/7 instruments exceed it.

`1m` is restricted to a 500-symbol subset for this reason: across the
full universe it would be ~1.21 billion rows/year (~129 GB). That list is
data, not configuration — it lives in the `intraday_scope` table.

## Why `1wk`/`1mo` live in their own table

This table is the whole argument:

| interval | share of rows | time span covered |
|---|---:|---|
| `5m` | 52% | 59 days |
| `1m` | 26% | 29 days |
| `15m` | 17% | 59 days |
| `60m` | 4% | 729 days |
| **`1wk` + `1mo`** | **0.07%** | **16,700 days (46 years)** |

Weekly and monthly bars are **0.07% of the rows but 100% of the time
span**. In a shared hypertable with a 7-day chunk interval they alone
produce 16,700 / 7 ≈ 2,386 chunks.

Measured on a single symbol before the split: **21,934 rows across 2,388
chunks**, about 9 rows per chunk. After moving `1wk`/`1mo` to a plain
table: **105 chunks** for `price_bars`, which is the 729-day span of
`60m` divided by 7.

A single chunk interval cannot serve both regimes — intraday wants
roughly one day, a 46-year sparse series wants roughly one year. That is
a 365× ratio, so the tables are separate.

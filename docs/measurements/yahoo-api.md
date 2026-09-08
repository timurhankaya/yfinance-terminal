# Yahoo Finance API — observed behaviour

All figures observed against the live API with a cold cache. Yahoo
publishes no limits; these are what the API actually accepted or refused.

## Interval window and depth limits

Measured by walking the request window up until the API refused.

| interval | max per request | max lookback depth | requests for first backfill |
|---|---|---|---|
| `1m` | **8 days** (9 → *"Only 8 days worth of 1m granularity data are allowed to be fetched per request"*) | **29 days** (30 → refused; 29 → 1950 bars) | 4 |
| `5m` | 59 days (60 full days refused) | 59 days | 1 |
| `15m` | 59 days | 59 days | 1 |
| `60m` | **729 days** (730 refused) | 729 days | 1 |
| `1wk` | unlimited | ~99 years | 1 (`period="max"`) |
| `1mo` | unlimited | ~99 years | 1 (`period="max"`) |

These are the **accepted maxima, not the advertised ones**. Yahoo's error
messages say "60 days" and "730 days", but 60 and 730 were refused while
59 and 729 were accepted. The limits are enforced in seconds and are
relative to "now", so sitting exactly on the boundary is unsafe.

**Consequences that shaped the schema:**

- `period="max"` is not "max" for intraday intervals. For `1m` it returns
  8 days.
- `1m` data is **permanently lost after 29 days**. Any pipeline that
  wants a 1m archive must run at least every 29 days; this is why the
  archive exists at all.
- Coarser intervals cannot be derived from `1m`: a 46-year weekly series
  has no 1m source to derive from.

## Request cost per symbol

Counted by wrapping `YfData.get_raw_json` on a single `Ticker`.

| Yahoo module | datasets fed | requests |
|---|---|---|
| `recommendationTrend` | `recommendations` | 1 |
| `upgradeDowngradeHistory` | `upgrades_downgrades` | 1 |
| `financialData` | `analyst_price_targets` | 1 |
| `earningsTrend` | `earnings_estimate`, `revenue_estimate`, `eps_trend`, `eps_revisions` | 1 |
| `earningsHistory` | `earnings_history` | 1 |
| `industryTrend,sectorTrend,indexTrend` | `growth_estimates` | 1 |
| holders bundle (7 modules) | 6 ownership datasets | 1 |
| `esgScores` | `sustainability` | 1 |
| `quoteType,summaryProfile,topHoldings,fundProfile` | `funds_data` | 1 (funds only) |

**Net overhead: 7 requests per symbol** — `sustainability` is off by
default, `funds_data` only fires for funds (8 for a fund). For
comparison: `history` costs 1, `info` costs 3.

This sharing holds **only** while a symbol uses a single `Ticker`
instance. Giving any dataset its own fresh `Ticker` multiplies the cost.

## Cost of `repair=True`

Counted by hooking `YfData.get` and `YfData.cache_get` with an empty cache.

| interval | `repair=False` | `repair=True` | sub-intervals fetched |
|---|---|---|---|
| `5m` | 1 | **6** | `2m`, `1m` |
| `15m` | 1 | **8** | `5m`, `2m` |
| `60m` | 1 | 2 | — |
| `1d` | 1 | 2 | — |
| `1wk` | 1–3 | 1 | `1d` (resample) |
| `1mo` | 1–2 | 2 | `1d` (resample) |

**`1wk`/`1mo` request counts are unstable.** Repeated across symbols:
`KO 1wk` → 1 request, `PEP 1wk` → 3 (one of them `1d`). The difference
comes from whether the symbol's timezone/metadata cache is warm and from
the `_get_history_cache` call that `actions=True` triggers. Budget
against an upper bound, never a fixed number.

**These are observed worst cases, not constants.** A separate request
opens per contiguous damaged block; blocks older than the sub-interval's
own window are skipped without a request. On a clean frame the cost is 1.

## Split rescaling in historical data

NVDA, 2024-06-10, 10:1 split, fetched with `auto_adjust=False,
repair=False`:

```
2024-06-05  Close=122.44  Volume=528,402,000  Stock Splits=0
2024-06-07  Close=120.89  Volume=412,386,000  Stock Splits=0
2024-06-10  Close=121.79  Volume=313,434,100  Stock Splits=10.0
```

Pre-split days show a price near 120 (actually ~1,200) and volume near
5×10⁸ (actually ~5×10⁷). **Yahoo rescales history to the current split
basis: prices divided, volume multiplied.**

Daily history does not care — it is rewritten in full on every run. The
intraday archive does: a 1m bar older than 29 days can never be refetched,
so preserving its scale is the pipeline's responsibility.

## Multi-day bar timestamps

| interval | stamp | example |
|---|---|---|
| `1wk` | Monday of that week, local 00:00 | `2025-09-01 00:00-04:00` |
| `1mo` | first day of the month, local 00:00 | `2025-09-01 00:00-04:00` |

The stamp is the period's **start**, in the exchange's local timezone.

## Empty is not failure

These datasets legitimately return nothing for these symbols. No test
asserts they must be populated.

| Dataset | Returns empty for |
|---|---|
| all 17 analyst/ownership datasets | `SPY`, `VFIAX`, `BTC-USD`, `^GSPC`, `EURUSD=X`, `GC=F`, unknown symbols (`funds_data` excepted: populated for SPY and VFIAX) |
| `sustainability` | **19 of 19** symbols tested |
| `upgrades_downgrades`, `earnings_history`, `institutional_holders`, `mutualfund_holders`, `insider_transactions`, `insider_roster_holders` | `THYAO.IS`, `NESN.SW` |
| `fund_top_holdings`, `fund_weightings(category='sector')` | `BND`, `TLT` (bond funds) |
| all of `funds_data` | every non-fund symbol |

`sustainability` is disabled by default because of the 19/19 result:
enabling it spends one request per symbol to record an empty cell.

## Extended-hours bars

`hasPrePostMarketData` is **not** a reliable signal. `SHEL.L` and
`VWCE.DE` both report `False` and still return 5 and 8 extended bars
respectively. The only dependable source is the `tradingPeriods`
start/end range.

## Surfaces that no longer exist upstream — 2026-09-08

Two `Ticker` properties look like uncollected coverage and are not.
Observed against the pinned yfinance 1.7.0, by reading
`yfinance/scrapers/fundamentals.py` rather than by calling them:

| Surface | What it does now | Where the data is instead |
|---|---|---|
| `Ticker.earnings`, `Ticker.quarterly_earnings` | returns `None` and warns: "deprecated as not available via API. Look for \"Net Income\" in Ticker.income_stmt" | `financial_facts`, item `NetIncome` (the `income_stmt` datasets) |
| `Ticker.get_shares()` | reads `Fundamentals.shares`, which is never populated: the property raises `YFNotImplementedError('shares')` | `shares_full` (`get_shares_full()`), which is the live path |

**Why this is written down.** Both are absences a reader can only
distinguish from an oversight by checking upstream, and this project
justifies every exclusion in writing. The justification is now beside
each dataset as well (`datasets/shares_full.py`,
`datasets/financials/statements.py`).

**What IS an oversight, and is still open:** `Ticker.options` and
`Ticker.option_chain()` are alive upstream and collected nowhere. That is
a new table family and needs its own design
(`docs/superpowers/specs/2026-09-07-kalan-isler.md`, madde 3).

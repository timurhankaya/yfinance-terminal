# Naming, landscape and launch

Status: research, decision pending
Date: 2026-09-07
All availability checks and star counts below were read on 2026-09-07 and
are dated for that reason: PyPI names and domains are claimed by other
people while you think about them.

## Summary

1. **`yfin` cannot be the name.** The PyPI name is taken, and taken by a
   *competitor*: `yfin 0.2.0`, "Lightweight Python SDK for yfin: hosted
   Yahoo Finance quotes, history, options, fundamentals, screeners, and
   search", at `yfin.dev`, published by `bluefin-ai`. That is the same
   pitch as this project's own "Hosted service" section. README's promise
   that "yfin is the project, the distribution (`pip install yfin`), the
   package and the CLI -- one name in all four places" is already false in
   one of the four, and the collision is with the one party a user is most
   likely to confuse it with.
2. **The niche is genuinely empty.** Nothing maintained occupies "the full
   Yahoo Finance surface, normalised into a relational schema, verified and
   audited". The neighbours are either much bigger and differently shaped
   (OpenBB, qlib, Nautilus) or hobby scripts with zero stars.
3. **The launch has to be sequenced around a legal fact**, not just around
   audience size: Yahoo's ToS prohibit automated access and redistribution.
   That does not stop the project, but it decides what the README says,
   what the hosted service can be, and which channels are safe to be loud
   in.

---

## 1. What is actually being named

Naming is downstream of positioning, so state the positioning first. In one
sentence, and this is the sentence the whole launch reuses:

> Keeps a verified, auditable, incrementally-maintained copy of the Yahoo
> Finance surface in your own PostgreSQL.

The three words that carry the differentiation, in order:

- **verified** -- row counts come from reading the keys back, not from the
  driver's affected-row count.
- **audited** -- `sync_runs` / `sync_run_items` record the outcome of every
  (symbol × dataset) cell, so `empty`, `skipped` and `failed` stay
  distinguishable.
- **incremental** -- watermarks and content-hash gates, so a daily run
  costs a day.

Everything else the project has -- 61 datasets, 87 tables, TimescaleDB
hypertables, the proxy pool, the read API, the tick stream, the Kafka
outbox -- is evidence for those three, not a separate claim. A launch that
leads with "61 datasets" competes on a number anyone can inflate. A launch
that leads with "no data source lies to you silently" competes on something
none of the neighbours even attempt.

The second, quieter claim, and the one that will actually land with the
audience that matters:

> It is a database, not a DataFrame. You can query it, join it, and hand it
> to something that is not Python.

That is the whole distance between this project and `yfinance`, and between
this project and every "financial data pipeline" repo on GitHub.

---

## 2. Landscape

### 2.1 The direct niche is empty

GitHub repository search, 2026-09-07:

| Query | Total repos | Best result |
| --- | --- | --- |
| `yfinance timescaledb` | **4** | all four at **0 stars**, all portfolio/hobby projects |
| `yfinance postgres pipeline` | **2** | both at **0 stars** |

The four that exist (`QuantTradingOS/data-ingestion-service`,
`chitown2016/tradingAssistant`, `burger56487/quant-platform`,
`aneessaheba/stock-data-etl-warehouse-pipelinee`) are single-developer
learning projects: a handful of tables, no migrations, no verification, no
audit trail, no incremental strategy. `mangobased/yf-pg-ch-pet-project` is
named "pet project" by its own author.

This is the most important finding in the document, and it cuts both ways.
There is no incumbent to displace -- and there is also no proof that anyone
wants this, because nobody has succeeded at it publicly. The closest
attempt, **`camisatx/pySecMaster`** ("An automated system to store and
maintain financial data", PostgreSQL, 24 tables), reached **71 stars** and
was last pushed **2019-04-16**. It is the shape of this project, seven
years dead. Worth reading its issues before launch: whatever killed it is
the risk here too, and the honest guess is that maintaining a scraper
against a hostile upstream alone is unrewarding.

### 2.2 The neighbours, and why none of them is a competitor

| Project | Stars | Last push | What it is | Why it is not this |
| --- | --- | --- | --- | --- |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | 72,737 | 2026-07-30 | Open Data Platform: normalises 30+ providers behind one Python SDK, plus a Workspace product | **Fetch-time normalisation, not storage.** OpenBB hands you a standardised object per call; it does not own a schema, does not persist, does not verify, does not audit. Also AGPL-3.0, same licence -- so it is a *complement*: an OpenBB provider extension backed by this project's Postgres is a real integration story, not a fight. |
| [qlib](https://github.com/microsoft/qlib) | 48,381 | 2026-09-02 | AI-oriented quant investment platform | Consumes data, does not acquire it. Its own storage is a bar-oriented binary format for model training. Different layer. |
| [Nautilus Trader](https://github.com/nautechsystems/nautilus_trader) | 28,593 | 2026-09-07 | Rust-native event-driven trading engine | Execution and backtesting. Its data catalog is Parquet for replay, not a queryable archive. |
| [yfinance](https://github.com/ranaroussi/yfinance) | 25,186 | 2026-08-27 | The upstream client | **This project depends on it and must never look like a fork.** See §5. |
| [ArcticDB](https://github.com/man-group/ArcticDB) | 2,509 | 2026-09-07 | High-performance DataFrame database for time series | Storage, but for versioned DataFrames on object storage. No ingestion, no schema, no audit, no relational query. A serious answer to "where do the bars go", not to "how do they get there correctly". |
| `alpacahq/marketstore` | — | — | Was a Go time-series server for market data | **The repository 404s as of 2026-09-07.** Frequently still recommended; do not cite it as a live alternative. |

### 2.3 Where that leaves the pitch

Draw the layers and the project sits in the one nobody occupies:

```
acquire        yfinance, provider APIs          <- upstream, crowded
normalise      OpenBB                            <- at fetch time, in memory
>>> PERSIST + VERIFY + AUDIT + MAINTAIN <<<      <- EMPTY
store          ArcticDB, Parquet, DuckDB         <- bytes, no semantics
consume        qlib, Nautilus, notebooks         <- crowded
```

The launch sentence writes itself from that diagram: *"Everyone shows you
how to fetch market data. Nobody shows you how to keep it."*

### 2.4 The uncomfortable question to answer before launching

Why has nobody done this? Three candidate answers, and the pitch must
survive all three:

1. **Because Yahoo is a hostile upstream and it is unrewarding work.**
   True, and the honest counter is the audit trail: this project's whole
   point is that it tells you when the upstream broke instead of silently
   writing nothing. Lead with that.
2. **Because serious users pay for a vendor feed** (Polygon, Databento,
   EODHD, Norgate). Partly true, and the counter is that the schema and the
   pipeline are the expensive part; the source is swappable, and the
   `datasets/` layer already imports no SQLAlchemy precisely so a second
   provider is a module, not a rewrite. **Say this out loud at launch** --
   it converts "a Yahoo scraper" into "an ingestion framework whose first
   provider is Yahoo", which is a much bigger and much more defensible
   thing.
3. **Because nobody wants it.** Possible. The cheap test is §6's Tier 0.

---

## 3. Name candidates

### 3.1 Constraints

- PyPI name free, GitHub org free, one `.dev` or `.io` domain free.
- Not confusable with `yfinance` -- and `yfin` fails this on its face,
  regardless of the PyPI collision.
- Not tied to Yahoo, because the source is meant to become swappable
  (§2.4). **Any name containing `yf`, `yahoo` or `yf-` caps the project at
  "the Yahoo thing" forever.** This alone disqualifies `yfin`,
  `yfarchive` and friends.
- Not tied to `tick` either, if the pipeline (61 datasets, fundamentals,
  filings, ownership) is the main product and the tick stream is one
  subsystem. Naming the whole after `tick` undersells it by an order of
  magnitude.

That second and third constraint together kill most of the obvious
candidates, which is useful: it narrows the field to names about *keeping*
things.

### 3.2 Availability, checked 2026-09-07

| Name | PyPI | GitHub org | `.dev` | `.io` | Repos with the name | Note |
| --- | --- | --- | --- | --- | --- | --- |
| **tapehouse** | free | free | free | free | 6 | "the tape" is the price feed; a house that keeps it |
| **marketkeep** | free | free | free | free | 2 | plainest reading of the pitch |
| **quotebase** | free | free | free | taken | 16 | `-base` reads as database; "quote" undersells |
| **tickvault** | free | free | free | taken | 11 | `tick` undersells (§3.1) |
| **tickhouse** | free | free | free | free | 1 | fewest collisions, but reads as a ClickHouse fork |
| `yfin` | **TAKEN** | — | — | — | — | **and by a competitor** |
| single words | | | | | | `assay`, `touchstone`, `hallmark`, `crucible`, `bellwether`, `lodestone`, `tally`, `beacon`, `anvil`, `foundry`, `almanac`, `granary`, `stockpile`, `quarry`, `bourse` are **all taken on PyPI**. Do not spend time here. |

### 3.3 Recommendation

**`tapehouse`.**

- Clean on all four surfaces -- PyPI, GitHub org, `tapehouse.dev`,
  `tapehouse.io` -- which no other candidate manages.
- "The tape" is the oldest name in the business for the price feed, and a
  *house* is where something is kept and looked after. The name says
  "archive of the market record" to the audience that matters and stays
  meaningful to everyone else.
- Source-agnostic and subsystem-agnostic: it does not say Yahoo, it does
  not say tick, it does not say Postgres. It survives the second provider
  and the second storage backend.
- Reads as one word, types as one word, and `pip install tapehouse` /
  `tapehouse sync` / `import tapehouse` all scan.

**Second choice: `marketkeep`**, if the priority is that a stranger
understands the name without knowing the idiom. It is more literal, less
distinctive, and equally clean; "keep" carries both "storage" and
"custody", which is the right connotation for a project whose selling point
is trustworthiness. Note `marketkeep.com` is parked by a squatter, so the
`.dev` is the only real option.

**Do not** pick `tickvault`, `tickhouse` or `quotebase`: the first two name
the smallest subsystem, and the third collides with 16 repos.

### 3.4 What renaming costs

Do it before the GitHub repo is public, and it costs a day:

- `pyproject.toml` name, `src/yfin/` → `src/tapehouse/`, every
  `from yfin...` import, the `yfin` console script, `YF_*` environment
  prefix (**decide deliberately**: `YF_` reads as Yahoo Finance and has the
  same source-lock problem as the package name -- `TH_` costs one
  find-replace now and is impossible later, once anyone has a `.env`).
- `README.md`'s "Name" section, which currently argues at length for a name
  that cannot be used. Rewrite it to argue the *positioning* instead: what
  this is versus `yfinance`, which is the genuinely useful part of that
  section and does not depend on the name.
- Database identifiers (`yfin_sync`, `yfin_stream`, `yfin_stream_relay`
  advisory lock names, the `yfin_` metric namespace in the observability
  design) -- all pre-release, all free to change now.
- Alembic revision names, the `docker-compose.yml` service names, the
  `.env.example` prefix.

Every one of those is cheap today and expensive after the first user.

---

## 4. Before announcing anything

A launch converts attention into stars once. Spending it on a repo that
does not survive first contact wastes the only free shot.

| Gate | Why it gates the launch |
| --- | --- |
| The rename is done | Announcing twice under two names splits every inbound link |
| `docker compose up` → `sync AAPL` works from a cold clone, on a machine that is not yours | The single highest-leverage thing. Most people evaluate a data project by whether the quick start works in five minutes, and stop otherwise |
| A screenshot or a 30-second asciinema of a real sync | Text launches underperform badly; a terminal recording of 37,295 verified rows landing is the whole pitch in one image |
| One page of real SQL against the schema | "It is a database, not a DataFrame" has to be *shown*. Three queries: a fundamentals join, a gap report, an audit query |
| The ToS section is written (§5) | The first HN comment will be about legality. Answering it in the README beats answering it in the thread |
| `LICENSE`, `CONTRIBUTING`, issue templates, a `v0.1.0` tag | Signals maintenance intent; the pySecMaster failure mode is visible abandonment |
| Observability/change-events work is either merged or on a public roadmap | Half-finished branches in `main` read as abandonment |

Explicitly **not** gates: 100% coverage, published PyPI package, a website,
a logo. Ship the repo.

---

## 5. The legal fact, and how it shapes everything

Yahoo's Terms of Service prohibit automated access without written
permission and prohibit redistribution of the data. `yfinance` itself is
explicit that it is not affiliated with or endorsed by Yahoo and is
intended for research and educational use. Enforcement in practice is
graduated: personal and internal research use carries low risk; a
commercial product that redistributes the data or serves it to customers
carries meaningfully more.

Three consequences, and they are not optional:

1. **The README needs its own disclaimer section**, in the same spirit as
   `yfinance`'s. Not affiliated with or endorsed by Yahoo. The user is
   responsible for their own compliance. This project moves data into the
   user's own database and redistributes nothing.
2. **The framing is "your own copy", never "our data".** This is not
   marketing spin, it is the accurate description: the software runs on the
   user's machine against the user's own access, exactly the way `yfinance`
   does. Every sentence in the launch posts should be about the *pipeline*,
   not about the *data*.
3. **The "Hosted service" line in the README is the one real exposure.** A
   hosted service serving Yahoo-derived data to paying customers is the
   exact activity the ToS names. Either remove it from the launch README,
   or make it explicitly "managed hosting of *your* instance, against
   *your* access" -- which is a different and defensible product. Decide
   this before launch, because it is the difference between "an open-source
   tool" and "a business built on someone else's data", and the internet
   will make that distinction for you if you do not.

The path that removes this risk entirely is §2.4's second provider: a
licensed source (Databento, Polygon, EODHD) behind the same `datasets/`
protocol turns the hosted product legitimate. Worth naming as a roadmap
item at launch -- it changes how the project is read.

---

## 6. Where to announce

Tiered by what each channel costs and what it returns. **The order
matters**: the small channels are a rehearsal, and they surface the
objections you would rather not meet on the front page of Hacker News.

### Tier 0 — validate before building an audience (week -2, cost: an hour)

The point is to find out whether §2.4's third answer is the true one,
cheaply, before spending the one-shot channels.

- **r/algotrading** and **r/datasets**: not a launch, a *question*. "Where
  do you keep your historical market data, and how do you know it is
  complete?" The replies are your positioning research and your first
  users. If nobody has the problem, you have learned that for free.
- **A GitHub issue on `yfinance`** is *not* a channel. Do not do this: it
  reads as hijacking, and upstream goodwill is worth more than the traffic.

### Tier 1 — the audience that actually has this problem (week 0)

These are small, technical, and they convert. Do these **first**, and fix
what they tell you before Tier 2.

| Channel | Why it fits | How to post |
| --- | --- | --- |
| **r/algotrading** (~2M) | Exactly the people running a nightly `yfinance` script into CSVs and losing data | Lead with the *problem*: "Yahoo drops 1m bars after 29 days; here is what I built to not lose them." Never lead with the feature list. Weekday morning US time |
| **r/quant**, **r/quantfinance** | Smaller, sharper; will interrogate the verification claim | Post the audit-trail design, not the repo |
| **r/selfhosted** (~600k) | Docker Compose + Postgres + "own your data" is precisely their thesis | Frame as "self-hosted market data archive". This subreddit will care about the compose file more than the datasets |
| **r/PostgreSQL**, **r/Python**, **r/dataengineering** | The schema, `mypy --strict`, `alembic check`, the dataset/storage boundary test | Different post each: for r/dataengineering the interesting artefact is the *transactional outbox* and the run audit, not the finance |
| **[awesome-quant](https://github.com/wilsonfreitas/awesome-quant)** (29,477 stars, pushed 2026-09-07 — actively maintained) | Permanent, compounding discovery; a PR, not a post | Submit under data acquisition/storage. Also `ernie55ernie/awesome-quant`, `awesome-quant-ai` |
| **Lobsters** | Small, hostile to marketing, excellent signal | Only if you have an invite. Tag `databases`, `python` |

### Tier 2 — the one-shot channels (week 1–2, after Tier 1 feedback)

| Channel | Notes |
| --- | --- |
| **Show HN** | The single biggest lever and it fires once. Title: `Show HN: Tapehouse – Keep a verified copy of the market record in Postgres`. Post Tue–Thu ~08:00 ET. **Be present in the thread for six hours.** The first three comments will be: (1) ToS/legality, (2) "why not just use OpenBB/DuckDB/Parquet", (3) "Yahoo data quality is bad anyway". Have all three answered in the README first; §5 and §2.2 are those answers. HN rewards the measurements — the 37,295-row AAPL sync, the 22,291 vs 6,219 ticks/s COPY measurement, `docs/measurements/` — far more than any feature list |
| **PyCoder's Weekly** and **Python Weekly** | Both take submissions and both have run for over a decade. Submit the week *after* HN, so the link carries social proof |
| **Console.dev** | Curates developer tools specifically; submissions open |
| **Postgres Weekly** | The audience that will appreciate `alembic check` producing an empty diff and the 39-constraint fix. Submit the *schema design*, not the finance |
| **Hacker Newsletter** | Automatic if HN goes well; no action needed |

### Tier 3 — compounding, slow (ongoing)

- **A technical blog post per subsystem**, each of which is its own
  submission to Tier 1 and 2. You already have four written as design
  documents in `docs/superpowers/specs/` and `docs/measurements/`:
  the transactional outbox, the `xid8` relay cursor, the 29-day intraday
  race, the dataset/storage boundary test. These are the posts that get
  syndicated; the launch post is not.
- **Timescale's community and blog** actively publish user case studies.
  A hypertable-based market archive with real measurements is exactly
  their content, and it reaches an audience already paying for this
  problem.
- **OpenBB integration** (§2.2): a provider extension backed by this
  project is a genuine technical contribution *and* a distribution channel
  into 72,737 stars' worth of attention. The highest-leverage single item
  on this list, and it is engineering, not marketing.
- **GitHub topics**: `market-data`, `stock-market-data`,
  `yahoo-finance-api`, `timescaledb`, `financial-data`, `etl`. Free, and
  the topic pages are browsed.
- **Answer the recurring questions** on Stack Overflow and in
  `yfinance`'s issue tracker where someone is genuinely asking "how do I
  store this". Helpfully, not as a link drop.

### What to skip

- **Product Hunt**: wrong audience for an AGPL developer tool with no UI.
- **Twitter/X and LinkedIn without a following**: zero reach, real time
  cost. Post there *after* HN, as an archive of the launch, not as a
  channel.
- **Dev.to and Medium reposts**: low signal, and they compete with your own
  repo for the search result.
- **Paid anything.** There is no ad channel for this audience.

### Sequencing

```
week -2   Tier 0 validation question; rename; quick-start test on a clean machine
week -1   README rewrite (positioning + ToS + SQL examples), asciinema, v0.1.0 tag
week  0   Tier 1: r/algotrading, r/selfhosted, r/dataengineering; awesome-quant PR
week  1   fix what Tier 1 surfaced; Show HN Tue-Thu 08:00 ET; be in the thread
week  2   PyCoder's / Python Weekly / Console.dev / Postgres Weekly submissions
ongoing   one subsystem post a month; OpenBB provider extension; Timescale case study
```

### What success looks like

Do not measure stars. Measure:

- **issues opened by strangers** -- the only proof someone ran it;
- **`docker compose up` completing for someone who is not you** -- ask for
  this explicitly in the launch post;
- **one external contributor** within three months.

pySecMaster had 71 stars and died. Stars were not the missing thing.

---

## Sources

- [PyPI `yfin`](https://pypi.org/project/yfin/) — the name collision
- [ranaroussi/yfinance](https://github.com/ranaroussi/yfinance)
- [OpenBB](https://github.com/OpenBB-finance/OpenBB) · [Open Data Platform](https://openbb.co/products/odp/)
- [man-group/ArcticDB](https://github.com/man-group/ArcticDB)
- [microsoft/qlib](https://github.com/microsoft/qlib)
- [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader)
- [camisatx/pySecMaster](https://github.com/camisatx/pySecMaster) — the seven-year-dead precedent
- [wilsonfreitas/awesome-quant](https://github.com/wilsonfreitas/awesome-quant)
- [timescale/timescaledb](https://github.com/timescale/timescaledb)
- [PyCoder's Weekly](https://pycoders.com/) · [Python Weekly](https://www.pythonweekly.com/)
- [Yahoo Finance legal guidelines](https://legal.yahoo.com/us/en/yahoo/finance-guidelines/index.html)
- [Why enterprises move from Yahoo Finance scraping to managed feeds (2026)](https://www.promptcloud.com/blog/scrape-yahoo-finance/) — ToS enforcement gradient

# Measurement evidence

Every non-obvious decision in this codebase is backed by a measurement,
not by a guess. This directory is where those measurements live.

The rule the project follows: **if a comment claims a number, that number
was observed.** When a measurement contradicted an assumption, the
assumption lost and the finding is recorded here rather than quietly
dropped.

## Why a separate directory

Measurements have a different lifetime from the code. A field width or an
API limit stays true long after the function that consumed it is
rewritten, and a future contributor asking "why 729 and not 730?" needs
the observation, not the call site. Keeping them inline would bury them;
keeping them in design documents would tie them to a design that changes.

## Contents

| File | Covers |
|---|---|
| [`yahoo-api.md`](yahoo-api.md) | Upstream limits, request cost per symbol, which datasets legitimately return empty |
| [`volume.md`](volume.md) | Row-count and storage projections per interval |
| [`database.md`](database.md) | PostgreSQL and TimescaleDB behaviours the schema depends on |
| [`websocket.md`](websocket.md) | Yahoo's live quote socket: subscription and connection limits, message cadence, field coverage, the float32 artefact, snapshot lag, envelope format, write-path ceiling |
| [`observability.md`](observability.md) | What watching the pipeline costs it: the freshness query against audit retention, log/span/scrape overhead, and why `asof_state` cannot answer the freshness question |
| [`web-viz.md`](web-viz.md) | The terminal's SVG primitives: treemap layout and render by cell count, the 400-cell cap, a watchlist of sparklines |

## Reproducing a measurement

Each entry names the command or the hook used to obtain it. API
measurements were taken against the live upstream with a cold cache;
database measurements against the pinned image in `docker-compose.yml`
(PostgreSQL 18.6 + TimescaleDB 2.29.2).

Numbers taken from the upstream API are **observations, not contracts**.
Yahoo publishes no limits and changes them without notice. Where a value
sits at a boundary the code leaves headroom and says so.

## Adding a measurement

1. Record the command and its raw output.
2. State what the number *means* for a decision — a number with no
   decision attached is trivia.
3. If it refutes something already written here, replace it and note that
   it was refuted. Superseded measurements are more useful than missing
   ones.

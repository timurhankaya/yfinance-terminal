"""The prose half of the published contract.

OpenAPI describes shapes well and intent badly. Fifty-five resources sit
behind one path -- `/v1/datasets/{name}` -- so a reader opening ReDoc
sees a single generic endpoint and no way to learn what is actually
available, what scope each thing needs, or what may be filtered.

The fix is not a path per resource. That would make the frozen
openapi.json churn on every dataset added, and a contract lock that
changes constantly is one nobody reads. Instead the catalogue is rendered
into the document's description, generated from the same `CATALOG` the
API serves from -- so the table cannot describe a resource that does not
exist, or miss one that does.

The introduction covers the four things every caller gets wrong once:
how to get a token, how paging works, that decimals are strings, and that
the two date columns are not the same date.
"""

from __future__ import annotations

from yfin.api.storage.catalog import CATALOG, CatalogEntry
from yfin.core.families import DataFamily

INTRO = """
Read-only access to the yfin market data warehouse: symbols, price bars,
corporate actions, financial statements, analyst coverage, holdings,
news, discovery results and the sector/industry taxonomy.

Every example in this document uses `ACME`, a fictional symbol. The
response bodies were captured against it, so the parameters you see and
the payloads you read describe one company throughout, and no number here
is a real company's reported figure.

**Live ticks are not served here.** The pipeline collects them and
publishes them to Kafka for consumers that need them; this API serves the
stored history and reference data. Everything readable over HTTP is in
the resource table below.

## Getting a token

Authentication is the OAuth2 **client credentials** grant. Send your
client id and secret as HTTP Basic to `/oauth/token`; you get a bearer
token valid for 15 minutes.

```
curl -u "$CLIENT_ID:$CLIENT_SECRET" \\
     -d grant_type=client_credentials \\
     https://<host>/oauth/token
```

Ask for fewer scopes than you hold with `-d scope="bars:read"`. The token
carries what was granted, and the response says so.

Revocation is immediate: rotating a secret or narrowing a scope stops the
tokens already issued, without waiting for them to expire.

## Paging

Collections return `{"data": [...], "next_cursor": ...}`. Pass
`next_cursor` back as `?cursor=` to get the next page, and stop when it
comes back `null`. There is no `offset`: on tables with hundreds of
millions of rows an offset gets slower the deeper you go and can skip
rows while data is being written.

A cursor belongs to the query that produced it. Change the interval, the
range or a filter and it is refused with `422 invalid_cursor` rather than
quietly returning the wrong page.

## Reading values

**Numbers arrive as strings.** Prices and share counts are stored as
exact decimals; sending them as JSON numbers would round them on the way
out. Parse them with a decimal type, not a float.

**Two different dates.** `ts_utc` is always UTC. `session_date` is the
exchange's trading session; `local_date` is the bar's local calendar day.
They are not interchangeable -- a bar late in the local evening can
belong to the next session.

**Ranges are half-open.** `from` is included, `to` is not, so consecutive
pages never overlap.

**Bars default to regular hours.** Pass `session=all` for intraday
extended-hours bars. The default is deliberate: mixing them into a
regular series silently distorts anything computed from it.

## Limits

Scoped responses carry `RateLimit-*` for the per-second budget and
`X-Quota-*` for the monthly one, on a `200` and on a `304` alike. Both
resets are in **seconds**, not timestamps. A `429` says which limit was
hit in its `type` and how long to wait in `Retry-After`.

## Errors

Errors are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem
documents on `application/problem+json`, except on `/oauth/token`, which
uses the OAuth2 error shape its own standard requires -- every OAuth2
client library parses that and nothing else.

Branch on `type`. It is an **opaque token, not a URL**: do not try to
fetch it. `request_id` names this request in our logs and is the value to
quote in a support request; it is also the response's `X-Request-Id`.

| `type` | status | what happened | what to do |
| --- | --- | --- | --- |
| `unauthenticated` | 401 | No credentials were sent | Send `Authorization: Bearer` |
| `invalid_token` | 401 | Expired, malformed or revoked | Get a new token |
| `client_disabled` | 401 | The client was turned off | Talk to us; retrying will not help |
| `insufficient_scope` | 403 | The token lacks a scope | Get one with it; the header names it |
| `not_found` | 404 | No such symbol or dataset | Check the name |
| `invalid_parameter` | 422 | A parameter is unusable | Read `detail`; it says which |
| `invalid_cursor` | 422 | The cursor is from another query | Restart the walk |
| `range_too_large` | 422 | The range exceeds this interval's cap | Ask for a narrower window |
| `rate_limit_exceeded` | 429 | Over the per-second budget | Wait `Retry-After` seconds |
| `quota_exceeded` | 429 | The monthly quota is used up | Wait for the reset |
| `concurrency_limit` | 429 | Too many requests in flight | Reduce parallelism |
| `query_timeout` | 504 | The query was cancelled | Narrow the range or the page size |
| `internal_error` | 500 | Our fault | Retry; quote the `request_id` |

A `5xx` refunds the quota unit and is not counted. A `429` is not counted
either -- it was refused before any work. Everything else is, a `304`
included: a conditional request still costs a request. A `504` is billed
like a success, because the work was really done.

A method this document does not list answers `405` with
`type: invalid_parameter`.

## Caching

Read responses carry a weak `ETag` derived from the body, so it changes
when the data does. Send it back as `If-None-Match` and an unchanged
resource answers `304` with no body. The dataset routes carry no
validator, so there is nothing to revalidate there.

`Cache-Control` is always `private`. Responses vary by scope and by your
plan's page size, so a shared cache holding one and serving it to another
client would be a data leak, not just a stale answer.

`X-Data-As-Of` is when the data was last verified against the source --
a fetch time, not a data time. It is absent wherever the schema records
none; the price tables deliberately keep no per-row fetch timestamp.

`Accept` is not negotiated: successful bodies are `application/json`,
errors `application/problem+json`. There is no `Link` header --
`next_cursor` is the only paging affordance. `HEAD` and `OPTIONS` are not
part of the contract, and a client-supplied `X-Request-Id` is ignored:
the header is ours to set.

## Versioning

`/v1` is the surface's version; `info.version` is this document's. They
move independently.

Additive changes -- a new field, a new resource, a new optional parameter
-- happen without a `/v1` bump, so **tolerate fields you do not know**. A
removal or an incompatible change would arrive as a new prefix, with the
old operations marked `deprecated` and carrying `Deprecation` and
`Sunset` headers first. Nothing is deprecated today, so neither header is
implemented yet.

Changes are recorded in the API changelog, `docs/api/CHANGELOG.md`.
"""

TAGS = [
    {
        "name": "oauth",
        "description": "Token issuance. Start here.",
    },
    {
        "name": "market",
        "description": (
            "The resources people ask for by name: symbols, price bars, "
            "corporate actions and financial statements. These are shaped by "
            "hand, so they carry the things a generic route cannot -- interval "
            "routing, the session filter and per-interval range caps."
        ),
    },
    {
        "name": "datasets",
        "description": (
            "Everything else, through one pattern. The catalogue lists what "
            "this token can read; the data route serves any of it. See the "
            "resource table in the introduction."
        ),
    },
    {
        "name": "meta",
        "description": "Liveness and readiness. No token required.",
    },
]


def _row(entry: CatalogEntry) -> str:
    filters = ", ".join(f"`{name}`" for name in entry.exposure.filters) or "—"
    symbol = "required" if entry.symbol_required else ("optional" if entry.has_symbol else "—")
    # Two things are deliberately not columns. The scope is the same for
    # every row in a section and the heading already says it, and the sort
    # order is almost always "newest first" -- a column that repeats one
    # value costs width the resource names need.
    #
    # The column COUNT rather than the column names: fifty-five rows each
    # listing every field would be unreadable, and `GET /v1/datasets`
    # already serves the names with their types.
    fields = len(entry.table.columns)
    return (
        f"| `{entry.name}` | {entry.exposure.description} | {symbol} | "
        f"{filters} | {fields} |"
    )


def catalogue_section() -> str:
    """The resource table, generated from what the API actually serves."""
    if not CATALOG:
        return ""

    lines = [
        "",
        # Headings stay plain text: ReDoc renders markdown in the body but
        # shows the raw form in its navigation, so backticks in a heading
        # arrive as literal <code> tags in the sidebar.
        "## Resources",
        "",
        "Everything below is served by `GET /v1/datasets/{name}`, where the",
        "name is the first column. `symbol` says whether the resource is",
        "per-symbol: *required* means you must pass `?symbol=`, *optional*",
        "means you may browse without one. Time-ordered resources come",
        "newest first. `fields` is how many columns a row carries; their",
        "names and types come from `GET /v1/datasets`.",
        "",
    ]
    for family in DataFamily:
        entries = sorted(
            (e for e in CATALOG.values() if e.family is family), key=lambda e: e.name
        )
        if not entries:
            continue
        lines += [
            # h2, not h3: ReDoc puts only h1 and h2 in its navigation,
            # so this is what makes a family one click away. Just the
            # family name -- the scope goes underneath, where it does not
            # have to fit in a sidebar.
            f"## {family.value}",
            "",
            f"Requires the `{entries[0].scope}` scope.",
            "",
            "| name | what it is | symbol | filters |",
            "| --- | --- | --- | --- |",
            *(_row(entry) for entry in entries),
            "",
        ]
    return "\n".join(lines)


def description() -> str:
    return INTRO.strip() + "\n" + catalogue_section()

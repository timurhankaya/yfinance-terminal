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

Every response carries `RateLimit-*` for the per-second budget and
`X-Quota-*` for the monthly one. A `429` says which limit was hit in its
`type` field and how long to wait in `Retry-After`. Requests that fail
with a `5xx` are not counted against your quota.

Errors are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem
documents, except on `/oauth/token`, which uses the OAuth2 error shape
its own standard requires.
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
    return f"| `{entry.name}` | {entry.exposure.description} | {symbol} | {filters} |"


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
        "newest first.",
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

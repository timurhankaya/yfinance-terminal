# Finalising the published OpenAPI contract

Status: approved, not yet implemented
Date: 2026-09-07
Supersedes nothing. Extends `2026-09-06-read-api-oauth2-design.md`; the
places where it revises that document are listed under "Revisions to the
prior design".

## Why

`openapi.json` is committed, diffed in CI and served at `/openapi.json`,
`/docs` and `/redoc`. The machinery around it is sound: `docs.py` renders
the catalogue into the description so the document cannot list a resource
the API does not serve, `dump_openapi.py` makes every contract change a
reviewable diff, and `test_api_contract.py` fuzzes every operation.

What is published inside that machinery is not finished. The document
describes the happy path of ten operations and nothing else: no error
responses, no problem schema, no examples, no response headers, no
`servers`. In two places it is actively wrong -- it publishes
`HTTPValidationError`, a body the API never sends, and it names the
column-level shape of fifty-five resources `dict[str, Any]`.

A caller integrating against this document learns the shape of a
successful bar page and has to discover everything else by provoking it.

### The gaps, as measured

Verified against the committed document and the source, 2026-09-07.

| # | Gap | Evidence |
| --- | --- | --- |
| 1 | No error responses on `/v1` operations | every `/v1` operation publishes exactly `200` and `422` |
| 2 | RFC 9457 problem schema absent; the **thirteen** `type` values in `errors.py` are unpublished | `errors.py:31-43`; not in `components.schemas` |
| 3 | No `servers`, so generated clients have no base URL to resolve paths against | `servers` key missing |
| 4 | No examples anywhere, request or response | zero `example`/`examples` keys |
| 5 | Response headers undocumented | thirteen distinct headers set in code, none in the contract |
| 6 | Generated `operationId`s (`list_bars_v1_symbols__symbol__bars_get`) become client method names | all **ten** operations |
| 7 | The document lies: `HTTPValidationError`/`ValidationError` are published, but `_validation_error` answers every 422 with problem+json | `errors.py:114-132,176` |
| 8 | Auto-named request schema `Body_issue_token_oauth_token_post`; `TokenResponse` fields carry no description | `oauth.py:53-57` |
| 9 | Dataset rows are `dict[str, Any]` -- the columns of fifty-five resources are undescribed | `Collection_dict_str__Any__`; `len(CATALOG) == 55` over 53 tables |
| 10 | `ETag` is emitted but `If-None-Match`/`304` is neither implemented nor published | `market.py:55-79`; `app.py:71` already lets the header through CORS |
| 11 | `info` has only `{title, version, description}`; no versioning or deprecation policy is published | |
| 12 | Most query parameters have no description; `interval` is an unconstrained `str` | only `q` and `all` are described; `market.py:190` |

Three claims in an earlier draft of this table were wrong and are
corrected above: there are thirteen error types, not fourteen; ten
operations, not nine; and gap 3 is about `servers`, not about the
relative `tokenUrl` (see decision 5).

## Decisions

**1. Dataset shape (gap 9): catalogue column metadata, one path.** The
columns are published as metadata on `/v1/datasets` and counted in the
resource table; the data route keeps its single generic path and
`dict[str, Any]` response. A schema per resource -- whether as `oneOf` or
as a path each -- was rejected for the reason `docs.py` already gives: it
would make the frozen document churn on every dataset added, and a
contract lock that changes constantly is one nobody reads.

**2. Cross-cutting facts are injected once; per-operation truth stays at
the source.** `core/openapi.py` owns what is true of many operations:
error responses, response headers, `servers`, `info`, the `Problem`
schema. What is true of one operation stays where that operation is
declared: parameter descriptions, the `interval` enum, `TokenRequest`,
`OAuthError`, and the `operationId`, which is set on the route object so
the running app and the document cannot disagree.

This is enforcement, not automation. `OPERATION_IDS`, the example set and
the header list all have to be remembered; what the design guarantees is
that forgetting fails a test rather than shipping.

**3. Scope: documentation plus the honesty fixes it requires.** Where the
contract cannot be written truthfully without a behaviour change, the
behaviour changes. That is three things, listed under "Runtime changes".
Nothing else in the runtime moves.

**4. Examples are captured from real responses, then committed.**
Hand-written examples drift from what the API sends. Captured ones cannot
-- but capturing needs a database, and `dump_openapi.py` deliberately does
not have one. The "Examples" section resolves that with a two-stage
pipeline whose strictness lives in tests, not in `build`.

**5. `tokenUrl` stays relative.** The prior design chose
`tokenUrl: /oauth/token` so that the Authorize button in `/docs` targets
the host the documentation was loaded from. In OpenAPI 3.1 a relative
`tokenUrl` resolves against the document's own retrieval URL, so a
generator that fetches `/openapi.json` from the server resolves it
correctly; `servers` does not affect it either way. An absolute
`tokenUrl` would make a developer's local `/docs` authenticate against
production. Gap 3 is therefore about `servers` alone, and the prior
decision stands. `info.description` says explicitly that `tokenUrl` is
relative to the document's URL, because a generator fed a saved copy of
the file from disk has no base to resolve against.

**6. `304` is metered exactly like a `200`.** The prior design §5.6 made
this call and it holds: a conditional request still costs a request. The
saving is bandwidth and a database query, not a counter. `ratelimit/`
is therefore untouched, and the `304` carries the same `RateLimit-*` and
`X-Quota-*` headers as the `200` it replaces.

**7. `type` stays an opaque token, not a URI.** RFC 9457 §3.1.1 defines
`type` as a URI reference defaulting to `about:blank`; bare tokens like
`quota_exceeded` are valid relative references but are not the stable,
globally unique identifiers the RFC intends. `errors.py` chose bare
tokens deliberately -- "clients branch on them, so they are stable strings
rather than generated URLs" -- and changing them is a breaking change for
a benefit no client here asks for. The decision stands, and
`info.description` states plainly that `type` is an opaque token and must
not be dereferenced. `instance` is deliberately not published:
`request_id` already identifies the occurrence, and it is the value the
server logs under.

**8. The work ships as three plans.** They have different risk profiles
and different CI jobs; see "Implementation order".

## Architecture

One new module, `src/yfin/api/core/openapi.py`, exposing
`finalise_document(app, base) -> dict[str, Any]` -- named so it does not
collide with the existing `scripts/dump_openapi.build_document`.
`create_app` installs it as the application's `openapi` callable,
memoised into `app.openapi_schema` the way FastAPI's own implementation
is, so the document is built once per process. It is installed
unconditionally, including when `docs_enabled` is false: the document is
then not served, but it must not differ, or the contract lock would
describe an application nobody runs.

The division of labour: `core/docs.py` keeps the prose a human reads --
the introduction, the tag descriptions, the catalogue table.
`core/openapi.py` owns the schema a generator consumes. Neither imports
the other's concern; the `servers` constants live in `openapi.py`, not in
`docs.py`.

`finalise_document` applies these steps to FastAPI's generated document,
in order. Each is a small function over the document, tested on its own.

1. **`servers`** -- see below.
2. **`info`** -- `summary`, `contact`, `license`.
3. **`components.schemas.Problem`** and the per-status narrowed variants.
4. **Removal of the lie** -- `HTTPValidationError` and `ValidationError`
   are deleted and every `$ref` to them is replaced by the narrowed
   variant for the status it sits on.
5. **Error responses** -- injected from an explicit per-operation table.
6. **Response headers.**
7. **Media types** -- every problem body is keyed on
   `application/problem+json`; `/oauth/token` errors on
   `application/json`.
8. **Examples** -- loaded from `docs/api/examples/` and attached.
9. **`tags`** -- emitted in `docs.TAGS` order, which is authoritative.

### `servers`

```json
[{"url": "https://yfinance.monafy.com", "description": "Production"},
 {"url": "/", "description": "This deployment"}]
```

The first entry is a constant in `openapi.py` and is what the committed
`openapi.json` carries. `ApiSettings.public_base_url`, when set, replaces
the first entry at runtime. `dump_openapi.py` passes the setting
explicitly -- empty -- alongside the other settings it already pins, so
the committed document stays identical on every machine, which is the
property that script's docstring exists to guarantee.

The relative `"/"` entry is second and deliberate: it is what makes
`/docs` work against a staging host or a laptop without the production
entry being selected by default in every generator.

### `info`

```json
{"title": "yfin Data API",
 "version": "1.0.0",
 "summary": "Read-only access to the yfin market data warehouse.",
 "contact": {"name": "Timurhan Kaya",
             "url": "https://yfinance.monafy.com",
             "email": "<maintainer address>"},
 "license": {"name": "AGPL-3.0-or-later",
             "identifier": "AGPL-3.0-or-later"}}
```

`license.identifier` is OpenAPI 3.1 only and is mutually exclusive with
`license.url`; a test asserts the document's `openapi` field starts with
`3.1`, so a dependency downgrade cannot silently emit an illegal 3.0
document. The SPDX expression matches `pyproject.toml:5`.

`info.version` is the document's version and moves with the contract;
`/v1` is the surface's version and moves only when the surface breaks.
The versioning section of the introduction says so.

> The maintainer's e-mail address is supplied by the maintainer when this
> is implemented. It is not written here because this environment's tooling
> refuses to write e-mail addresses into files.

### The `Problem` schema

`components.schemas.Problem` is the RFC 9457 body as `errors.py` builds
it: `type`, `title`, `status`, `request_id`, optional `detail`. Its
`type` is an enum over all thirteen values, generated from an explicit
`ALL_TYPES` tuple added to `errors.py` -- not by reflection over module
globals, which would silently absorb any future name beginning `TYPE_`. A
test asserts `ALL_TYPES` covers every `TYPE_*` constant in the module, so
the tuple cannot fall behind.

A single thirteen-value enum on every response would weaken the contract:
a `404` would document that it might answer `quota_exceeded`. So each
status also gets a narrowed schema, `allOf: [{$ref: Problem}, {properties:
{type: {enum: [...]}}}]`, and responses reference the narrowed one:

| Status | Schema | Allowed `type` values |
| --- | --- | --- |
| 401 | `Unauthenticated` | `unauthenticated`, `invalid_token`, `client_disabled` |
| 403 | `Forbidden` | `insufficient_scope` |
| 404 | `NotFound` | `not_found` |
| 422 | `InvalidRequest` | `invalid_parameter`, `invalid_cursor` |
| 422 (`listBars` only) | `InvalidBarsRequest` | the above plus `range_too_large` |
| 429 | `RateLimited` | `rate_limit_exceeded`, `quota_exceeded`, `concurrency_limit` |
| 500 | `InternalError` | `internal_error` |
| 504 | `QueryTimeout` | `query_timeout` |

Every one of the thirteen types has a home in this table, which is also
what makes `range_too_large` reachable in the document -- an earlier draft
published it as a type no response admitted.

Where a status admits several types, each gets a named entry in that
response's `examples` map, so a client can see all three `429` bodies
without provoking them.

### Error responses per operation

Explicit, not derived from a property the injector cannot compute. The
table is the source of truth and a test asserts every route appears in
it.

| operationId | Statuses |
| --- | --- |
| `issueToken` | 200, 400, 401, 422, 429, 503 -- all errors in the RFC 6749 shape |
| `getHealth` | 200, 500 |
| `getReadiness` | 200, 429, 500 |
| `listSymbols` | 200, 304, 401, 403, 422, 429, 500, 504 |
| `getSymbol` | 200, 304, 401, 403, 404, 422, 429, 500, 504 |
| `listBars` | 200, 304, 401, 403, 404, 422, 429, 500, 504 |
| `listActions` | 200, 304, 401, 403, 404, 422, 429, 500, 504 |
| `listFinancials` | 200, 304, 401, 403, 404, 422, 429, 500, 504 |
| `listDatasets` | 200, 401, 422, 429, 500 |
| `readDataset` | 200, 401, 403, 404, 422, 429, 500, 504 |

Four of these rows contradict the rules an earlier draft stated, and each
was checked against the code:

- **`listDatasets` publishes no `403` and no `504`.** It depends on
  `Authenticated` (`auth/dependencies.py:183`), which requires no scope,
  and it has no `SessionDep` -- it answers from the in-memory `CATALOG`
  (`datasets.py:86-96`).
- **`403` is not a dataset-route speciality.** `auth/dependencies.py:112`
  raises it for every scoped route.
- **The `304`s are exactly the five handlers that call `_finish`**
  (`market.py:139,170,276,362,445`). Neither dataset route emits an
  `ETag`, so the fifty-five-resource surface gets no conditional
  requests. That is a real asymmetry, and it is out of scope here: those
  rows carry no `as_of` and are already served with a 60-second
  `Cache-Control`.
- **`405`** is produced app-wide by `_http_exception` (`errors.py:107`)
  with `type: invalid_parameter`. It is described once in the
  introduction rather than injected onto ten operations, because a caller
  using a method the document does not list has already left the
  contract.

The `304` rule needs a marker the document builder can see; `_finish` is
a call the builder cannot detect. Each of those five routes carries
`openapi_extra={"x-conditional": True}`, and the builder keys on it.
An `x-` extension is stripped from the published document after use.

`/oauth/token` never carries a `Problem`. Today that promise is broken at
runtime -- a missing `grant_type` reaches `_validation_error` and comes
back as problem+json -- which is fixed under "Runtime changes". Its four
error statuses gain an `OAuthError` schema (`error`, `error_description`)
where today they carry a description string and no schema.

### Response headers

Declared as Header Objects -- a Parameter Object without `name`/`in` --
with real schemas, because a client that has to guess whether
`RateLimit-Reset` is seconds or a timestamp has not been told anything.

| Header | Schema | Where |
| --- | --- | --- |
| `X-Request-Id` | `string` | every response |
| `Strict-Transport-Security`, `X-Content-Type-Options`, `Referrer-Policy` | `string` | every response (`middleware.py:33-40`) |
| `RateLimit-Limit`, `RateLimit-Remaining` | `integer, minimum 0` | every scoped response, `200` and `304` alike |
| `RateLimit-Reset`, `X-Quota-Reset` | `integer, minimum 0` -- seconds, not a timestamp | the same |
| `X-Quota-Limit`, `X-Quota-Remaining` | `integer, minimum 0` | the same |
| `Retry-After` | `integer, minimum 0` -- delta-seconds; the API never sends an HTTP-date | `429` everywhere, and `/oauth/token`'s `503` (`oauth.py:155`) |
| `Cache-Control`, `Vary` | `string` | every `2xx` on `/v1`, and every `304` |
| `ETag` | `string`, weak (`W/"…"`) | the five conditional operations, on `200` and `304` |
| `X-Data-As-Of` | `string, date-time`, not required | the same, omitted where `as_of` is null |
| `WWW-Authenticate` | `string`, with a per-status example | `401` and `403` |
| `Cache-Control: no-store`, `Pragma: no-cache` | `string` | every `/oauth/token` response (`oauth.py:68`) |

`Content-Type` and `Content-Length` are never declared; OpenAPI ignores
them.

RFC 9110 §15.4.5 requires a `304` to carry the headers whose values would
differ from the `200`'s -- `ETag`, `Cache-Control`, `Vary`, `Date` -- and
to have no content. The `304` rows above satisfy that, and the `304`
response object declares `description` only, with no `content`.

`WWW-Authenticate` on a `403` is correct per RFC 6750 §3, which defines
`error="insufficient_scope"` on that status. Its value differs by
operation -- `Bearer …` on `/v1`, `Basic realm="yfin-api"` on
`/oauth/token`'s `401` -- so each gets its own example rather than one
misleading description.

The header names in this table were taken from the code that sets them
(`ratelimit/limiter.py:197-204`, `ratelimit/dependencies.py:78-104`,
`core/middleware.py:33-40,124`, `market.py:71-79`, `meta.py:133`,
`oauth.py:68,155`), and a test asserts the published set equals the set
the code can emit.

### Operation ids

`OPERATION_IDS` maps `(method, path)` to a camelCase name and is applied
to `route.operation_id` at application construction, so the running app
and the document agree and the example filenames -- which are keyed by
`operationId` -- can be derived from either.

`issueToken`, `getHealth`, `getReadiness`, `listSymbols`, `getSymbol`,
`listBars`, `listActions`, `listFinancials`, `listDatasets`,
`readDataset`.

A test asserts every route in the application appears in the map, every
id is unique, and every id matches `^[a-z][A-Za-z0-9]*$`. `HEAD` routes,
which Starlette derives from `GET`, and CORS `OPTIONS` are excluded from
the map and from the document; the introduction says they are not part of
the contract.

This changes generated client method names. The API is at version 1.0.0
with no published consumers, so the change is taken now rather than
becoming permanent.

## Content

### Introduction

`docs.py`'s existing "Limits" section already documents `RateLimit-*`,
`X-Quota-*`, `Retry-After` and the `5xx` quota exemption. It is not
duplicated: it is absorbed into the new "Errors" section, and the
paragraph that currently says "Every response carries `RateLimit-*`" is
narrowed to the scoped operations, which is what the code does.

Three sections replace it.

**Errors.** A table of the thirteen `type` values: what causes each, what
the caller should do about it, and which status carries it. This is the
half of the error contract a schema cannot express. It states that `type`
is an opaque token and not a URI to dereference; that `request_id`
identifies the occurrence and is the value to quote in a support request;
that a `5xx` refunds the quota unit while a `429` and a `304` do not; and
that a method the document does not list answers `405` with
`type: invalid_parameter`.

**Caching and conditional requests.** That `ETag` is returned on the five
read operations, that `If-None-Match` gets a `304`, that a `304` costs a
request like any other, that `Cache-Control` is always `private` and why
(responses vary by scope and by the plan's page size, so a shared cache
holding one and serving it to another client would be a data leak), and
that `X-Data-As-Of` is the `max(as_of)` of the as-of state tables --
when the data was last verified against the source -- and is absent where
the schema records no such time. Also: `Accept` is not negotiated,
successful bodies are `application/json` and errors
`application/problem+json`, and there is no `Link` header -- `next_cursor`
is the only paging affordance.

**Versioning.** That `/v1` is the surface version and `info.version` the
document version; that additive changes (a new field, a new resource, a
new optional parameter) happen without a `/v1` bump and clients must
tolerate them; that a removal would be announced as `deprecated: true` on
the operation plus `Deprecation` and `Sunset` headers, and that neither
header is implemented yet because nothing is deprecated; and that changes
are recorded in `docs/api/CHANGELOG.md`, which `info.description` links.

### The resource table

Fifty-five rows each listing every column would make the table
unreadable, so the table gains a column count per resource and points at
`GET /v1/datasets` for the full list.

### The catalogue endpoint

`CatalogEntryOut` gains `columns: list[ColumnOut]`, where `ColumnOut` is
`{name: str, type: str, nullable: bool}`. Every column of the exposed
table is listed, because `storage/catalog.py:156` issues
`select(entry.table)` and returns all of them; a plan task confirms that
no exposed column is internal bookkeeping before the list is published.

`type` is the wire type, not the SQL type, through an explicit mapping.
The eleven SQLAlchemy types actually present across the fifty-three
exposed tables are `Numeric` (322 columns), `String` (165), `VARCHAR`
(116), `TIMESTAMP` (107), `Integer` (48), `Date` (44), `Text` (41),
`Boolean` (31), `BigInteger` (22), `SmallInteger` (9) and `Enum` (7):

| SQL type | Wire type |
| --- | --- |
| `Numeric` | `string (decimal)` -- `paging.serialise_row` formats it |
| `TIMESTAMP` | `string (date-time)` |
| `Date` | `string (date)` |
| `String`, `VARCHAR`, `Text`, `Enum` | `string` |
| `Integer`, `BigInteger`, `SmallInteger` | `integer` |
| `Boolean` | `boolean` |

`Enum` earns its row: seven catalogue columns use it, and a mapping that
had not been taught it would have failed on day one. There is no `JSONB`,
`JSON`, `ARRAY`, `Interval` or `bytea` anywhere in the exposed tables, so
no unserialisable type exists today.

An unknown SQL type raises at **import**, where `catalog.py` already
raises for a dataset naming a column its table does not have. A `500` on
`/v1/datasets` because someone added a column type is the failure mode
that module was written to prevent.

### Parameters and fields

Descriptions are added to every query parameter that lacks one:
`interval`, `from`, `to` (half-open, so consecutive pages never overlap),
`session`, `active`, `limit` (capped by the plan; exceeding the cap is a
`422`, not a silent clip), `cursor` (it belongs to the query that
produced it), `symbol`, `q`, `all`. `TokenResponse`'s four fields gain
descriptions.

Every operation gets a one-line `summary` and a `description`; a test
asserts both are non-empty, so a docstring edit cannot empty the
document. The security scheme gains a description, and a test asserts its
scope list equals the scopes the code enforces -- the prior design's
§7.2 invariant, which nothing currently checks.

`interval` changes from `str` to a `Literal` over `READABLE_INTERVALS`.
The runtime already refuses an unknown interval; today the document does
not say which ones exist, so a caller learns the list by guessing.

## Examples

Two stages, so that examples are real responses and document generation
stays free of the database.

**Stage one -- capture.** `tests/repo/test_api_examples.py` calls every
operation in every documented status, normalises the response and
compares it to a committed file at
`docs/api/examples/<operationId>.<status>.<name>.json`. The `<name>`
segment is the key in the response's `examples` map, so the three `429`
bodies and the two `422` bodies do not collide on one filename -- which
they would have under an earlier `<operationId>.<status>.json` scheme.
Run with `--snapshot-update` -- a flag added to `tests/conftest.py`, not a
new dependency -- it writes instead of comparing.

The fixtures need work before this is possible, and it is part of the
plan, not an assumption:

- `seeded` and `client` are module-local to
  `tests/repo/test_api_read_endpoints.py:69,159`. They move to
  `tests/repo/conftest.py`.
- `seeded` inserts only symbols, price history, bars, dividends and
  splits. It gains `financial_facts` rows and at least one row per
  catalogue family, or `listFinancials` and `readDataset` have no `200`
  to capture.
- `client` monkeypatches the plan to 1000 rps and a million-request
  quota, so no `429` is reachable through it. A second fixture with a
  one-request plan captures the three `429` bodies.

Normalisation is what makes the files stable. Exactly these are replaced
by fixed placeholders: the JWT in a token response, `request_id` in every
problem body, the `ETag` digest, `X-Data-As-Of`, `next_cursor` (its
payload embeds timestamps), and any `ts_utc`/`as_of` field in a captured
row. Nothing else is touched; a value that drifts and is not on this list
is a finding, not a nuisance.

**Stage two -- injection.** `finalise_document` reads those committed
files and attaches them under the media type's `examples` map -- the
plural key. OpenAPI 3.1 deprecates the singular `example` on a Media Type
Object and forbids using both, and a single `example` could not carry the
several bodies one status admits.

A missing file is **omitted at runtime and fails a test**. Making it a
hard failure inside `finalise_document` would take `/openapi.json` and
`/docs` down in production over a missing documentation file, and would
deadlock CI: the file can only be produced by the database job, while the
no-database job would fail without it. So `finalise_document` skips what
it cannot find, and `tests/unit/test_api_contract.py` -- which needs no
database -- asserts that a file exists for every operation and status in
the injection table.

The failure examples matter as much as the successes: `401`, `403`,
`404`, `422` (both `invalid_parameter` and `invalid_cursor`), `429` (all
three types) and `504` come from the same harness, so the published
problem bodies are bodies the API has actually sent.

## Tests

In `tests/unit/test_api_contract.py`, which needs no database and runs in
the same job as the contract lock:

- the document's `openapi` field starts with `3.1`
- every route appears in `OPERATION_IDS`; every id is unique and matches
  `^[a-z][A-Za-z0-9]*$`; `route.operation_id` equals the published one
- every route appears in the error-response table, and each operation's
  published statuses equal that table's row
- no operation references `HTTPValidationError`, and both schemas are
  gone
- every `4xx`/`5xx` outside `/oauth/token` references its status's
  narrowed problem schema, keyed on `application/problem+json`
- `/oauth/token`'s error responses reference `OAuthError`, keyed on
  `application/json`, and the existing "no problem schema here" assertion
  still passes
- `ALL_TYPES` covers every `TYPE_*` constant in `errors.py`, and the
  union of the narrowed enums equals `ALL_TYPES` -- so a new type must be
  given a status, and a status cannot admit a type that does not exist
- every published header name is one the code can emit, and every header
  the code emits on a documented status is published
- every `304` declares `ETag`, `Cache-Control` and `Vary`, and no
  `content`
- `servers` is present, the first entry is the production constant, and
  the document built with `docs_enabled=false` is byte-identical
- every operation has a non-empty `summary` and `description`
- the security scheme's scopes equal the scopes the code enforces
- an example file exists for every operation and status in the injection
  table
- every embedded example validates against the schema of the response it
  is attached to, and its `status` field equals the response's status
- every catalogue entry publishes a non-empty column list, and every
  column's SQL type is in the wire-type mapping

In `tests/repo/`:

- `test_api_examples.py`, the capture and lock described above
- a conditional-request test: a second request carrying the `ETag` as
  `If-None-Match` answers `304` with no body, carrying `ETag`,
  `Cache-Control`, `Vary` and the rate-limit headers, and counting
  against the quota exactly as the `200` did
- a test that `/oauth/token` answers a missing `grant_type` with the RFC
  6749 body, not problem+json

A `openapi-spec-validator` (or Spectral) lint of `openapi.json` runs in
CI. It catches the classes of error this design had to be reviewed for by
hand -- illegal `license` combinations, `example` alongside `examples`,
malformed header objects.

## Runtime changes

Three, each required for the document to be true.

**1. `If-None-Match` -> `304`.** `_finish` already computes a weak ETag
from the request identity and the freshness stamp. The handler compares
the incoming `If-None-Match` against it and returns `304` when they
match. For four of the five handlers -- `listSymbols`, `getSymbol`,
`listActions`, `listBars` -- the ETag is fully computable before any page
query, so the query is genuinely avoided; `listFinancials` needs
`reads.financials_as_of()` first, which is a cheap query independent of
the page query, so the page query is still avoided. The `304` is metered
like a `200` (decision 6) and carries the rate-limit headers already
written onto the response object.

This also fixes a bug it would otherwise expose: `get_symbol`'s
`payload_key` uses the raw path parameter (`market.py:171`), so
`/v1/symbols/aapl` and `/v1/symbols/AAPL` produce different ETags for
identical bodies. It uses `_normalise_symbol` instead.

**2. `interval` as an enum.** A `Literal` over `READABLE_INTERVALS`. The
refusal already exists; this publishes it.

**3. `/oauth/token` really is excluded from the problem format.** Today
it is not: `_validation_error` is registered app-wide, so a missing
`grant_type` returns problem+json, and the endpoint opens a database
session under the app-wide `OperationalError` and `Exception` handlers,
so a `500` or `504` from it is a problem document too. Every OAuth2
client library parses the RFC 6749 shape and finds no `error` field in
any of these. One check in `errors.py` -- is this the token endpoint --
routes all of them to `_oauth_error`, so the endpoint's stated contract
holds for every status, not just the ones its handler writes by hand.

## CI

The contract lock stays in the fast `check` job, and the example
*presence* check joins it there, so the no-database job still fails when
an operation is added without an example. The example *content* lock runs
in the existing `repo` job (`ci.yml:56`, `-m repo`, with the TimescaleDB
service), because capturing a response needs a database.

That split is deliberate and worth stating, because `ci.yml`'s own
opening comment argues for keeping contract checks together: the property
that matters is that no contract check can be skipped on its own, and
both jobs are required. What cannot be in the fast job is the database.

## Files

New:

- `src/yfin/api/core/openapi.py`
- `docs/api/examples/*.json`
- `docs/api/CHANGELOG.md`
- `tests/repo/test_api_examples.py`

Changed:

- `src/yfin/api/core/docs.py` -- the three new introduction sections
  absorbing the existing "Limits" text, the resource table's column count
- `src/yfin/api/core/errors.py` -- `ALL_TYPES`; the token-endpoint check
- `src/yfin/api/core/config.py` -- `public_base_url`
- `src/yfin/api/app.py` -- install the document hook and the operation ids
- `src/yfin/api/routers/v1/market.py` -- conditional requests, the
  `interval` enum, the ETag normalisation fix, parameter descriptions,
  the `x-conditional` marker
- `src/yfin/api/routers/v1/datasets.py` -- `columns` on the catalogue
- `src/yfin/api/routers/oauth.py` -- `OAuthError`, `TokenRequest`, field
  descriptions
- `src/yfin/api/storage/catalog.py` -- the wire-type mapping, validated
  at import
- `src/yfin/api/schemas/common.py`, `schemas/market.py` -- field
  descriptions where missing
- `scripts/dump_openapi.py` -- pin `public_base_url` explicitly
- `tests/repo/conftest.py` -- `seeded` and `client` move here and are
  extended; a constrained-plan fixture is added
- `tests/repo/test_api_read_endpoints.py` -- imports moved fixtures
- `tests/unit/test_api_contract.py`
- `tests/conftest.py` -- the `--snapshot-update` flag
- `.github/workflows/ci.yml` -- the example content lock and the schema
  lint
- `pyproject.toml` -- the spec validator in the dev extra
- `openapi.json` -- regenerated

## Implementation order

Three plans. Each leaves the repository green.

**Plan 1 -- schema and honesty. No database.** `core/openapi.py`,
`servers`, `info`, `ALL_TYPES`, the `Problem` schema and its narrowed
variants, removal of `HTTPValidationError`, the error-response table,
response headers, media types, `OAuthError`, `TokenRequest`,
`OPERATION_IDS`, operation summaries, the security-scheme description,
the schema lint, all of the unit contract tests, and the regenerated
`openapi.json`. Self-contained, entirely unit-tested, and on its own it
removes both places where the document lies.

**Plan 2 -- runtime.** `If-None-Match` -> `304` and the ETag
normalisation fix, `interval` as a `Literal`, and the token endpoint's
error shape. The only plan that changes what the API does; it needs the
`repo` job and the conditional-request test.

**Plan 3 -- content and examples.** The three introduction sections, the
resource-table column count, `ColumnOut` and the wire-type mapping,
parameter and field descriptions, `docs/api/CHANGELOG.md`, and the whole
example pipeline: the fixture move and extension, `test_api_examples.py`,
`docs/api/examples/`, and the CI wiring. Last, because the document is
correct and publishable before examples become a build input.

## Revisions to the prior design

`2026-09-06-read-api-oauth2-design.md` is amended in one place and
extended in three.

- **§5.6, `304` accounting: upheld, not reversed.** An earlier draft of
  this document exempted a `304` from the quota. It does not.
- **§7.2, relative `tokenUrl`: upheld,** and the reasoning is now
  published in `info.description` rather than living only in a design
  document.
- **§7.3, `Deprecation`/`Sunset` and `deprecated: true`: deferred.** The
  policy is published; the mechanism waits for something to deprecate.
  This is a scope reduction of an approved decision, taken knowingly.
- **§5.7, `503 service_unavailable`: not implemented on `/v1`.** The
  prior design lists it as a first-class problem status. No `/v1` code
  path emits it today -- only `/oauth/token`, in the RFC 6749 shape -- so
  publishing it would be a promise nothing keeps. There is deliberately
  no `TYPE_SERVICE_UNAVAILABLE` constant. Closing this gap is a separate
  piece of work: fail-closed behaviour when Redis or PostgreSQL is
  unreachable, which is a resilience change, not a documentation one.

## Out of scope

- A schema per dataset, in any form. Settled in decision 1.
- Conditional requests on the dataset routes. They emit no `ETag` and
  carry no `as_of`; adding one is a separate change.
- `x-logo`, `termsOfService`, `x-tagGroups`, `externalDocs`: nothing real
  to publish. `tags` order carries the grouping.
- Webhooks, callbacks, `HEAD`, `OPTIONS`, content negotiation: the API
  has none, and the introduction says so.
- `Deprecation`/`Sunset` headers at runtime, and `503` on `/v1`. See
  "Revisions" above.
- Honouring a client-supplied `X-Request-Id`. The introduction states
  that the header is response-only and a supplied one is ignored, so no
  integrator assumes otherwise.
- `type` as a dereferenceable URI, and an RFC 9457 `instance` member.
  Decision 7.
- Any change to authentication, rate limiting, quota accounting or the
  data itself.

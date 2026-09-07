# Finalising the published OpenAPI contract

Status: approved, not yet implemented
Date: 2026-09-07

## Why

`openapi.json` is committed, diffed in CI and served at `/openapi.json`,
`/docs` and `/redoc`. The machinery around it is sound: `docs.py` renders
the catalogue into the description so the document cannot list a resource
the API does not serve, `dump_openapi.py` makes every contract change a
reviewable diff, and `test_api_contract.py` fuzzes every operation.

What is published inside that machinery is not finished. The document
describes the happy path of nine operations and nothing else: no error
responses, no problem schema, no examples, no response headers, no
`servers`. In two places it is actively wrong -- it publishes
`HTTPValidationError`, a body the API never sends, and it names the
column-level shape of fifty-five resources `dict[str, Any]`.

A caller integrating against this document learns the shape of a
successful bar page and has to discover everything else by provoking it.

### The gaps, as measured

| # | Gap | Evidence |
| --- | --- | --- |
| 1 | No error responses on `/v1` operations | every `/v1` operation publishes only `200` and `422` |
| 2 | RFC 9457 problem schema absent; the fourteen `type` values in `errors.py` are unpublished | not in `components.schemas` |
| 3 | No `servers`, so `tokenUrl: /oauth/token` is relative and generators have no base URL | `servers` missing |
| 4 | No examples anywhere, request or response | zero `example`/`examples` keys |
| 5 | Response headers undocumented: `RateLimit-*`, `X-Quota-*`, `Retry-After`, `ETag`, `X-Data-As-Of`, `X-Request-Id`, `Cache-Control` | emitted in code, absent from the contract |
| 6 | Generated `operationId`s (`list_bars_v1_symbols__symbol__bars_get`) become client method names | all nine operations |
| 7 | The document lies: `HTTPValidationError`/`ValidationError` are published, but `_validation_error` answers 422 with problem+json | `core/errors.py` |
| 8 | Auto-named request schema `Body_issue_token_oauth_token_post`; `TokenResponse` fields carry no description | |
| 9 | Dataset rows are `dict[str, Any]` -- the columns of fifty-five resources are undescribed | `Collection_dict_str__Any__` |
| 10 | `ETag` is emitted but `If-None-Match`/`304` is neither implemented nor published | `market.py:_finish` |
| 11 | `info` has no `summary`, `contact` or `license`; no versioning or deprecation policy is published | |
| 12 | Most query parameters have no description; `interval` is an unconstrained `str` | `market.py` |

## Decisions taken

Four questions were settled before this design was written.

**Dataset shape (gap 9): catalogue metadata, one path.** The columns are
published as metadata on `/v1/datasets` and summarised in the resource
table; the data route keeps its single generic path and `dict[str, Any]`
response. A schema per resource -- whether as `oneOf` or as a path each --
was rejected for the reason `docs.py` already gives: it would make the
frozen document churn on every dataset added, and a contract lock that
changes constantly is one nobody reads.

**Error injection: a custom `openapi()` hook.** Common errors, headers,
`servers`, `info` and operation ids are applied to the generated document
in one place rather than repeated in every route decorator. A new
endpoint is then correct by default instead of correct if remembered.

**Scope: documentation plus the small honesty fixes.** Where the contract
cannot be written truthfully without a behaviour change, the behaviour
changes. That is two things: `If-None-Match` -> `304`, and `interval` as
an enum. Nothing else in the runtime moves.

**Examples: captured from real responses.** Hand-written examples drift
from what the API sends. Captured ones cannot -- but capturing needs a
database, and `dump_openapi.py` deliberately does not have one. The
"Examples" section resolves that with a two-stage pipeline.

## Architecture

One new module, `src/yfin/api/core/openapi.py`, exposing
`build_document(app) -> dict[str, Any]`. `create_app` installs it as the
application's `openapi` callable, memoised into `app.openapi_schema` the
way FastAPI's own implementation is, so the document is built once per
process.

The division of labour is deliberate. `core/docs.py` keeps the prose: the
introduction, the tag descriptions and the catalogue table -- everything a
human reads. `core/openapi.py` owns the schema: everything a generator
consumes. Neither imports the other's concern.

`build_document` takes FastAPI's generated document and applies these
steps, in order:

1. **`servers`** -- from `docs.py` constants, overridden by
   `ApiSettings.public_base_url` when it is set.
2. **`info`** -- `summary`, `contact` (the repository URL),
   `license: {name: "AGPL-3.0-or-later", identifier: "AGPL-3.0-or-later"}`.
   `description` continues to come from `docs.description()`.
3. **`components.schemas.Problem`** -- the RFC 9457 body as `errors.py`
   actually builds it: `type`, `title`, `status`, `request_id`, optional
   `detail`. `type` is an `enum` generated from the module's `TYPE_*`
   constants, so a new error type cannot be introduced unpublished.
4. **Removal of the lie** -- `HTTPValidationError` and `ValidationError`
   are deleted from `components.schemas` and every `$ref` to them is
   replaced by `Problem`.
5. **Error responses** -- injected per operation (table below).
6. **Response headers** -- declared on the responses that carry them.
7. **`operationId`** -- rewritten from an explicit map.
8. **Request schema rename** -- `Body_issue_token_oauth_token_post`
   becomes `TokenRequest`.
9. **Examples** -- loaded from `docs/api/examples/` and attached.

Steps 5-9 are each a small function over the document, tested
independently.

### Error responses per operation

Applied by rule, not by hand:

| Response | Where | Body |
| --- | --- | --- |
| `401 unauthenticated` / `invalid_token` / `client_disabled` | every operation with `security` | `Problem` |
| `403 insufficient_scope` | every operation with `security` | `Problem` |
| `422 invalid_parameter` / `invalid_cursor` | every `/v1` operation | `Problem` |
| `429 rate_limit_exceeded` / `quota_exceeded` / `concurrency_limit` | every operation with `security`, plus `/health/ready` | `Problem` |
| `500 internal_error` | every operation | `Problem` |
| `404 not_found` | operations with a path parameter, and `/v1/datasets/{name}` | `Problem` |
| `504 query_timeout` | operations that reach PostgreSQL | `Problem` |
| `304` | operations whose handler calls `_finish` | no body |
| `400` / `401` / `429` / `503` | `/oauth/token` only | RFC 6749 error shape |

`/oauth/token` is excluded from the `Problem` rules entirely. RFC 6749
§5.2 defines its own body, every OAuth2 client library parses that shape,
and `test_api_contract.py` already asserts no problem schema appears
there. Its four responses gain a schema (`OAuthError`: `error`,
`error_description`) where today they carry only a description string.

### Response headers

| Header | Where |
| --- | --- |
| `X-Request-Id` | every response |
| `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` | every metered response |
| `X-Quota-Limit`, `X-Quota-Remaining`, `X-Quota-Reset` | every metered response |
| `Retry-After` | `429`, including `/health/ready`'s |
| `Cache-Control`, `Vary` | every `2xx` on `/v1` |
| `ETag` | responses from handlers calling `_finish` |
| `X-Data-As-Of` | the same, where the resource records a fetch time |
| `WWW-Authenticate` | `401` and `403` |

The header names are taken from the code that sets them
(`ratelimit/dependencies.py`, `core/middleware.py`, `market.py:_finish`),
and a test asserts the two lists agree.

### Operation ids

An explicit map, `OPERATION_IDS`, from `(method, path)` to a camelCase
name: `issueToken`, `getHealth`, `getReadiness`, `listSymbols`,
`getSymbol`, `listBars`, `listActions`, `listFinancials`, `listDatasets`,
`readDataset`. A test asserts every route in the application appears in
the map and every id is unique, so adding a route without naming it fails
rather than silently reintroducing a generated id.

This changes generated client method names. The API is at version 1.0.0
with no published consumers, so the change is taken now rather than
becoming permanent.

## Content

### Introduction

Three sections are added to `INTRO` in `docs.py`.

**Errors.** A table of the fourteen `type` values: what causes each, what
the caller should do about it, and which status carries it. This is the
half of the error contract a schema cannot express -- `Problem` publishes
the shape, the table publishes the meaning.

**Caching.** That `ETag` is returned on read responses, that
`If-None-Match` gets a `304`, that `Cache-Control` is always `private`
and why (responses vary by scope and by the plan's page size, so a shared
cache holding one and serving it to another client would be a data leak),
and that `X-Data-As-Of` is a fetch time, not a data time.

**Versioning.** That `/v1` is the stable surface, that additive changes
(a new field, a new resource, a new optional parameter) happen without a
version bump and clients must tolerate them, and how a removal would be
announced. No `Deprecation`/`Sunset` header implementation is part of
this work -- the policy is published, the mechanism comes when there is
something to deprecate.

### The resource table

Fifty-five rows each listing every column would make the table
unreadable, so the table gains a column count per resource and points at
the catalogue endpoint for the full list. The count's source is
`entry.table.c`: the generic route issues `select(entry.table)`, so every
column of the table is returned, and `paging.serialise_row` maps
`Decimal` to string and leaves the rest to the JSON encoder.

### The catalogue endpoint

`CatalogEntryOut` gains `columns: list[ColumnOut]`, where `ColumnOut` is
`{name: str, type: str, nullable: bool}`. `type` is the wire type, not
the SQL type: `Numeric` maps to `"string (decimal)"`, `DateTime` to
`"string (date-time)"`, and so on, through an explicit mapping that fails
loudly on a SQL type it has not been taught -- silently emitting
`"unknown"` would publish a column shape nobody had checked. Deriving it
in `CatalogEntryOut.of` keeps the mapping in the HTTP layer, where the
existing comment says the wire shape belongs.

### Parameters

Descriptions are added to every query parameter that lacks one:
`interval`, `from`, `to` (half-open, so consecutive pages never overlap),
`session`, `active`, `limit` (capped by the plan, and exceeding the cap
is a 422 rather than a silent clip), `cursor` (it belongs to the query
that produced it), `symbol`, `q`, `all`.

`interval` changes from `str` to a `Literal` over `READABLE_INTERVALS`.
The runtime already refuses an unknown interval; today the document does
not say which ones exist, so a caller learns the list by guessing.

## Examples

Two stages, so that examples are real responses and document generation
stays free of the database.

**Stage one -- capture.** `tests/repo/test_api_examples.py` reuses the
`seeded` and `client` fixtures already established in
`tests/repo/test_api_read_endpoints.py`. It calls every operation, in
both its success and its documented failure modes, normalises the
response and compares it to a committed file at
`docs/api/examples/<operationId>.<status>.json`. Run with
`--snapshot-update`, it writes instead of comparing.

Normalisation is what makes the files stable: the JWT in a token
response, `request_id` in every problem body, and the `ETag` digest are
replaced by fixed placeholders. Without it every capture would produce a
diff and the lock would be noise.

**Stage two -- injection.** `build_document` reads those committed files
and attaches each as the `example` of the matching response. It reads
files, never a database, so `dump_openapi.py` remains runnable on any
machine with no environment of its own -- the property it was written to
have. A missing example file is a failure, not a silent omission;
otherwise the set of documented examples would quietly shrink.

The failure examples matter as much as the successes: `401`, `403`,
`404`, `422` (both an invalid parameter and a rejected cursor), `429` and
`504` come from the same harness, so the published problem bodies are
bodies the API has actually sent.

## Tests

New assertions in `tests/unit/test_api_contract.py`, which needs no
database and therefore runs in the same job as the contract lock:

- every operation carrying `security` publishes `401`, `403`, `429` and
  `500`
- no operation references `HTTPValidationError`, and the schema is gone
- every `4xx`/`5xx` outside `/oauth/token` references `Problem`
- `Problem`'s `type` enum equals the set of `TYPE_*` constants in
  `errors.py` -- the drift test
- `servers` is present and every entry is an absolute URL
- every route has an entry in `OPERATION_IDS`; every id is unique and
  camelCase
- every embedded example validates against the schema of the response it
  is attached to
- every catalogue entry publishes a non-empty column list, and every
  column type maps to a known wire type
- `/oauth/token`'s four error responses each carry the `OAuthError`
  schema

In `tests/repo/`:

- `test_api_examples.py`, the capture and lock described above
- a conditional-request test: a second request carrying the `ETag` as
  `If-None-Match` answers `304` with no body, and the `304` is not
  charged against the quota

## Runtime changes

Deliberately two, both required for the document to be true.

**`If-None-Match` -> `304`.** `_finish` already computes a weak ETag from
the request identity and the freshness stamp. The handler compares the
incoming `If-None-Match` against it and returns `304` when they match.
Because the stamp depends on the data, the ETag cannot always be computed
before the query; where it cannot, the saving is bandwidth rather than
database work, which is still the larger cost for a full page. A `304` is
not counted against the monthly quota, on the same principle that already
exempts `5xx`: the caller received no data.

**`interval` as an enum.** A `Literal` over `READABLE_INTERVALS`. The
refusal already exists; this publishes it.

## Files

New:

- `src/yfin/api/core/openapi.py`
- `docs/api/examples/*.json`
- `tests/repo/test_api_examples.py`

Changed:

- `src/yfin/api/core/docs.py` -- the three new introduction sections, the
  `servers` constants, the resource table's column count
- `src/yfin/api/core/config.py` -- `public_base_url`
- `src/yfin/api/app.py` -- install the document hook
- `src/yfin/api/routers/v1/market.py` -- conditional requests, the
  `interval` enum, parameter descriptions
- `src/yfin/api/routers/v1/datasets.py` -- `columns` on the catalogue
- `src/yfin/api/routers/oauth.py` -- the `OAuthError` schema, `TokenRequest`
- `src/yfin/api/schemas/common.py`, `schemas/market.py` -- field
  descriptions where missing
- `tests/unit/test_api_contract.py`
- `.github/workflows/ci.yml` -- the example lock in the database job
- `openapi.json` -- regenerated

## Out of scope

- A schema per dataset, in any form. Settled above.
- `x-logo`, `termsOfService`: no real value exists to publish.
- Webhooks and callbacks: the API has none.
- `Deprecation`/`Sunset` headers at runtime. The policy is published; the
  mechanism waits for something to deprecate.
- Any change to authentication, rate limiting or the data itself.

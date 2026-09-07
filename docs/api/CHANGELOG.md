# API changelog

Changes to the published contract, newest first. `openapi.json` is the
contract; this says what moved and whether it can break you.

Additive changes -- a new field, a new resource, a new optional parameter
-- happen without a `/v1` bump. Tolerate fields you do not know. A removal
or an incompatible change would arrive under a new prefix, and the old
operations would be marked `deprecated` and carry `Deprecation` and
`Sunset` headers first.

## Unreleased

### Added

- Every operation now publishes the errors it can answer, as RFC 9457
  problem documents on `application/problem+json`. The `type` values are
  enumerated per status, so a `404` does not claim it might answer
  `quota_exceeded`. `type` is an opaque token; do not dereference it.
- Response headers are published: `RateLimit-*`, `X-Quota-*`,
  `Retry-After`, `Cache-Control`, `Vary`, `ETag`, `X-Data-As-Of`,
  `X-Request-Id`, `WWW-Authenticate` and the fixed security headers.
- `servers`, `info.summary`, `info.contact` and `info.license`.
- Conditional requests. The five read operations answer `If-None-Match`
  with `304`. A `304` still counts against your rate and quota: it saves
  bandwidth and a query, not a request.
- `GET /v1/datasets` returns `columns` for every resource -- name, wire
  type and nullability -- so the shape of the generic `dict` rows is
  discoverable without provoking it.
- The introduction gained sections on errors, caching and versioning.
- Operation descriptions, and descriptions on every query parameter.

### Changed

- **Operation ids are camelCase**: `listBars`, `getSymbol`, `readDataset`
  and so on, replacing generated names like
  `list_bars_v1_symbols__symbol__bars_get`. **Generated clients will see
  renamed methods.** Taken now, at 1.0.0 with no published consumers,
  because it is permanent once anyone has generated against it.
- `interval` on `GET /v1/symbols/{symbol}/bars` is published as an
  enumeration instead of a string. The refusal is unchanged; the document
  now lists the values.
- `ETag` is derived from the response body, so it changes when the data
  does. It was a hash of the request alone, which made it useless as a
  validator -- and would have pinned a revalidating client to one page.
- `/oauth/token` answers **every** failure in the RFC 6749 shape,
  including a missing form field, an internal error and a cancelled
  query. Those three used to come back as problem documents with no
  `error` field for an OAuth2 client library to read.

### Removed

- `HTTPValidationError` and `ValidationError`. The API has never sent
  either: every `422` is a problem document.

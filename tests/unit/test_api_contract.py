"""The published contract: the committed document and what it promises.

The contract is only meaningful if changing it is visible. `openapi.json`
is generated from the code and committed, so a renamed Pydantic field
shows up as a reviewable diff instead of surfacing when a client breaks.
The check runs here as well as in CI, so it fails on the machine where
the change was made rather than a push later.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import schemathesis
from fastapi.testclient import TestClient
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings

from yfin.api.app import create_app
from yfin.api.core.config import ApiSettings
from yfin.core.families import DataFamily, scope_for

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMITTED = REPO_ROOT / "openapi.json"


def api_settings() -> ApiSettings:
    return ApiSettings(
        jwt_signing_key="x" * 32,
        jwt_kid="k1",
        jwt_issuer="yfin-api",
        jwt_audience="yfin-api",
        docs_enabled=True,
        cors_origins="",
        trusted_proxies="",
    )


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return create_app(api_settings()).openapi()


def test_the_committed_document_is_CURRENT(document: dict[str, Any]) -> None:
    """A contract change has to be a deliberate, reviewable diff."""
    import scripts.dump_openapi as dumper  # noqa: PLC0415 - path-dependent import

    assert COMMITTED.exists(), "run python scripts/dump_openapi.py"
    assert COMMITTED.read_text(encoding="utf-8") == dumper.render(document), (
        "openapi.json is stale; run python scripts/dump_openapi.py and review the diff"
    )


def test_the_client_credentials_flow_is_published(document: dict[str, Any]) -> None:
    """This is what makes the Authorize button in /docs work, and what
    tells a client library where to get a token."""
    scheme = document["components"]["securitySchemes"]["clientCredentials"]
    flow = scheme["flows"]["clientCredentials"]
    assert flow["tokenUrl"] == "/oauth/token"
    assert set(flow["scopes"]) == {scope_for(family) for family in DataFamily}


def test_every_v1_operation_declares_a_scope(document: dict[str, Any]) -> None:
    """The declaration in the document and the check in the code are the
    same Security() call, so an endpoint cannot advertise one scope and
    require another. What this catches is an endpoint declaring none."""
    undeclared = [
        f"{method.upper()} {path}"
        for path, operations in document["paths"].items()
        if path.startswith("/v1")
        for method, operation in operations.items()
        if not operation.get("security")
    ]
    assert undeclared == []


def test_the_token_endpoint_does_NOT_use_the_problem_schema(
    document: dict[str, Any],
) -> None:
    """RFC 6749 defines its own error body and every OAuth2 client library
    parses that shape. A problem document would leave them with no `error`
    field -- and a schema-conformance test would not notice, because the
    document would still match what we published."""
    responses = document["paths"]["/oauth/token"]["post"]["responses"]
    assert "401" in responses
    assert "problem" not in json.dumps(responses).lower()


def test_health_is_reachable_without_a_token(document: dict[str, Any]) -> None:
    assert not document["paths"]["/health"]["get"].get("security")


# --- schema conformance -----------------------------------------------------

_schema = schemathesis.openapi.from_dict(create_app(api_settings()).openapi())


@_schema.parametrize()
@hypothesis_settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
def test_no_operation_answers_with_an_unhandled_error(case: Any) -> None:
    """Fuzzes every operation and asserts the response matches what we
    published.

    Every route here is authenticated, so these calls get 401 or 422 --
    which is the point: the interesting failure is a 500, i.e. a request
    shape that reaches code expecting something else. An unauthenticated
    fuzz is exactly the traffic a public API receives first.
    """
    app = create_app(api_settings())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.request(
            case.method,
            case.formatted_path,
            params=case.query,
            headers=case.headers,
            json=case.body if case.body is not schemathesis.core.NOT_SET else None,
        )

    assert response.status_code < 500, (
        f"{case.method} {case.formatted_path} answered {response.status_code}"
    )


def test_no_published_scope_grants_access_to_NOTHING() -> None:
    """A scope in the document is a promise. Three of them used to grant a
    client an empty catalogue and a 404 on everything, which is a contract
    that does not hold."""
    from yfin.api.storage.catalog import CATALOG
    from yfin.core.families import DataFamily

    # Families the hand-written endpoints serve directly.
    curated = {DataFamily.REFERENCE, DataFamily.BARS, DataFamily.FUNDAMENTALS}
    served = {entry.family for entry in CATALOG.values()} | curated
    assert set(DataFamily) - served == set()


#: Tables that must never be readable, and why. Operational rows are not
#: product data: `proxies` carries credentials, `settings` carries the
#: pipeline's configuration, the gate tables are internal bookkeeping, and
#: the sync audit is an operator's record of our own runs.
NEVER_EXPOSED = {
    "proxies",
    "settings",
    "asof_state",
    "domain_asof_state",
    "discovery_asof_state",
    "sync_runs",
    "sync_run_items",
    "bar_gaps",
    "bar_rescales",
    "intraday_scope",
    "alembic_version",
}


def test_no_operational_table_is_reachable() -> None:
    """A dataset could point an exposure at any table its `produces` lists,
    and `sec_filings` already lists two. Nothing stops a future one from
    naming an operational table except this."""
    from yfin.api.storage.catalog import CATALOG

    exposed = {entry.table.name for entry in CATALOG.values()}
    assert exposed & NEVER_EXPOSED == set()


def test_the_api_owns_tables_are_not_readable_either() -> None:
    """Client ids, secret hashes, plans and usage counters are the API's
    own bookkeeping. A generic surface over them would hand one client the
    credentials of another."""
    from yfin.api.storage.catalog import CATALOG

    exposed = {entry.table.name for entry in CATALOG.values()}
    assert {name for name in exposed if name.startswith("api_")} == set()


def test_both_documentation_views_render() -> None:
    """Swagger UI is for trying an endpoint, ReDoc for reading the whole
    contract. Both render from /openapi.json, so neither can show
    something the API does not serve."""
    with TestClient(create_app(api_settings())) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 200, path


def test_the_documentation_can_be_turned_off() -> None:
    """A deployment that does not want its surface published should be
    able to withhold it without also losing the API."""
    settings = api_settings().model_copy(update={"docs_enabled": False})
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/health").status_code == 200


def test_the_document_lists_EVERY_resource(document: dict[str, Any]) -> None:
    """Fifty-five resources sit behind one path, so a reader opening ReDoc
    would otherwise see a generic endpoint and no way to learn what is
    available. The table is generated from the catalogue the API serves
    from, so it cannot list something that does not exist or miss
    something that does."""
    from yfin.api.storage.catalog import CATALOG

    text = document["info"]["description"]
    missing = [name for name in CATALOG if f"`{name}`" not in text]
    assert missing == [], f"not documented: {missing}"


def test_every_documented_resource_states_its_scope(document: dict[str, Any]) -> None:
    """A caller has to be able to tell, without trying, which token they
    need for a given resource."""
    from yfin.api.storage.catalog import CATALOG

    text = document["info"]["description"]
    for scope in {entry.scope for entry in CATALOG.values()}:
        assert f"`{scope}`" in text


def test_the_introduction_covers_what_callers_get_wrong(document: dict[str, Any]) -> None:
    """Cursors, decimal-as-string and the two date columns are the three
    things a client hits once and then has to be told about."""
    text = document["info"]["description"]
    for topic in ("next_cursor", "session_date", "local_date", "client_credentials"):
        assert topic in text


# --- the schema half of the contract ----------------------------------------
#
# Everything below guards `core/openapi.py`. The module's tables have to be
# maintained by hand; these are what make forgetting fail here rather than
# in a client six months from now.


def test_the_document_is_openapi_31(document: dict[str, Any]) -> None:
    """`license.identifier` and the `["string", "null"]` type arrays are 3.1
    only. A dependency downgrade would emit a document that is illegal
    rather than merely different, and nothing else would notice."""
    assert document["openapi"].startswith("3.1")


def test_every_route_is_named(document: dict[str, Any]) -> None:
    """A route missing from the map keeps FastAPI's generated id, which
    becomes a client method called `list_bars_v1_symbols__symbol__bars_get`.
    That is permanent once someone has generated against it."""
    from yfin.api.core.openapi import OPERATION_IDS, walk_routes

    app = create_app(api_settings())
    routed = {
        (method.lower(), route.path)
        for route in walk_routes(app.routes)
        for method in (getattr(route, "methods", None) or ())
        if route.path.startswith(("/v1", "/oauth", "/health"))
    }
    assert routed - set(OPERATION_IDS) == set()


def test_metrics_is_NOT_in_the_document(document: dict[str, Any]) -> None:
    """`/metrics` is operational, not part of what a client is promised:
    it publishes internal handler names and its format is Prometheus's to
    change, not ours.

    Explicit, because nothing else here would notice. Every path check
    above filters on the `("/v1", "/oauth", "/health")` prefixes, so a
    `/metrics` that leaked into the document would pass all of them and
    then be a route we had promised to keep.
    """
    from yfin.api.core.openapi import walk_routes

    assert "/metrics" not in document["paths"]
    # And it really is mounted: an assertion that only checked the absence
    # would still pass if the endpoint were never installed at all.
    app = create_app(api_settings())
    assert "/metrics" in {route.path for route in walk_routes(app.routes)}


def test_operation_ids_are_camel_case_and_unique(document: dict[str, Any]) -> None:
    ids = [
        operation["operationId"]
        for operations in document["paths"].values()
        for operation in operations.values()
    ]
    assert len(ids) == len(set(ids))
    assert [i for i in ids if not re.fullmatch(r"[a-z][A-Za-z0-9]*", i)] == []


def test_the_route_and_the_document_agree_on_the_id() -> None:
    """The id is set on the route, not patched into the finished document,
    so `route.operation_id` and the contract cannot drift -- and the example
    files, which are named after it, can be found from either."""
    from yfin.api.core.openapi import OPERATION_IDS, walk_routes

    app = create_app(api_settings())
    for route in walk_routes(app.routes):
        for method in getattr(route, "methods", None) or ():
            expected = OPERATION_IDS.get((method.lower(), route.path))
            if expected is not None:
                assert route.operation_id == expected


def test_every_operation_publishes_the_statuses_it_can_answer(
    document: dict[str, Any],
) -> None:
    from yfin.api.core.openapi import ERROR_STATUSES

    for operations in document["paths"].values():
        for operation in operations.values():
            operation_id = operation["operationId"]
            published = {int(code) for code in operation["responses"]}
            expected = {200, *ERROR_STATUSES[operation_id]}
            # 304 is not asserted from a table here on purpose: whether an
            # operation can answer one is settled against the running
            # handler in `tests/repo/test_api_headers.py`, not against the
            # declaration that produced this document.
            assert published - {304} == expected, operation_id


def test_the_validation_error_schemas_are_GONE(document: dict[str, Any]) -> None:
    """The API answers every 422 through `_validation_error`, which builds a
    problem document. Publishing FastAPI's default body described a
    response no code path can produce."""
    assert "HTTPValidationError" not in document["components"]["schemas"]
    assert "ValidationError" not in document["components"]["schemas"]
    assert "ValidationError" not in json.dumps(document)


def test_every_error_carries_a_narrowed_problem_schema(document: dict[str, Any]) -> None:
    """Narrowed per status, not one enum of all thirteen types: a 404 that
    documented it might answer `quota_exceeded` would be a contract a
    generated client is entitled to believe."""
    from yfin.api.core.errors import PROBLEM_MEDIA_TYPE
    from yfin.api.core.openapi import PROBLEM_VARIANTS, RANGE_VARIANT, RANGED, TOKEN_OPERATION

    for operations in document["paths"].values():
        for operation in operations.values():
            if operation["operationId"] == TOKEN_OPERATION:
                continue
            for code, response in operation["responses"].items():
                if int(code) < 400:
                    continue
                content = response["content"]
                assert set(content) == {PROBLEM_MEDIA_TYPE}, (operation["operationId"], code)
                expected = (
                    RANGE_VARIANT[0]
                    if operation["operationId"] in RANGED and code == "422"
                    else PROBLEM_VARIANTS[int(code)][0]
                )
                assert content[PROBLEM_MEDIA_TYPE]["schema"]["$ref"].endswith(f"/{expected}")


def test_the_token_endpoints_errors_carry_the_oauth_schema(
    document: dict[str, Any],
) -> None:
    responses = document["paths"]["/oauth/token"]["post"]["responses"]
    for code, response in responses.items():
        if int(code) < 400:
            continue
        content = response["content"]
        assert set(content) == {"application/json"}, code
        assert content["application/json"]["schema"]["$ref"].endswith("/OAuthError")


def test_ALL_TYPES_covers_every_error_type_the_code_defines() -> None:
    """An explicit tuple, not a scan of the module's globals: reflection
    would absorb any future name starting `TYPE_` into the published enum
    without anyone deciding to."""
    from yfin.api.core import errors

    declared = {
        value
        for name, value in vars(errors).items()
        if name.startswith("TYPE_") and isinstance(value, str)
    }
    assert set(errors.ALL_TYPES) == declared


def test_every_error_type_has_a_status_that_can_carry_it() -> None:
    """A type nothing publishes is a type a client cannot prepare for.
    `range_too_large` was exactly that until it was given to bars."""
    from yfin.api.core.errors import ALL_TYPES
    from yfin.api.core.openapi import PROBLEM_VARIANTS, RANGE_VARIANT

    placed: set[str] = set()
    for _, types in (*PROBLEM_VARIANTS.values(), RANGE_VARIANT):
        placed |= set(types)
    assert placed == set(ALL_TYPES)


#: Every header the API can put on a response, gathered from the code that
#: sets it. The document may publish a subset; it may not invent one.
def _emittable_headers() -> set[str]:
    from yfin.api.core.middleware import SECURITY_HEADERS

    return {
        *SECURITY_HEADERS,
        "X-Request-Id",
        "RateLimit-Limit",
        "RateLimit-Remaining",
        "RateLimit-Reset",
        "X-Quota-Limit",
        "X-Quota-Remaining",
        "X-Quota-Reset",
        "Retry-After",
        "Cache-Control",
        "Vary",
        "ETag",
        "X-Data-As-Of",
        "WWW-Authenticate",
        "Pragma",
    }


def test_no_published_header_is_one_the_code_cannot_send(
    document: dict[str, Any],
) -> None:
    published = {
        name
        for operations in document["paths"].values()
        for operation in operations.values()
        for response in operation["responses"].values()
        for name in response.get("headers", {})
    }
    assert published - _emittable_headers() == set()


def test_the_document_says_where_the_api_is(document: dict[str, Any]) -> None:
    from yfin.api.core.openapi import PRODUCTION_URL

    servers = document["servers"]
    assert servers[0]["url"] == PRODUCTION_URL
    # The relative entry is what lets /docs on a laptop call itself rather
    # than production.
    assert servers[-1]["url"] == "/"


def test_the_token_url_stays_relative(document: dict[str, Any]) -> None:
    """Deliberate, and reaffirmed after review: an absolute tokenUrl would
    make a developer's local /docs authenticate against production. In 3.1 a
    relative one resolves against the document's own URL."""
    flow = document["components"]["securitySchemes"]["clientCredentials"]["flows"]
    assert flow["clientCredentials"]["tokenUrl"] == "/oauth/token"


def test_the_security_scheme_explains_itself(document: dict[str, Any]) -> None:
    scheme = document["components"]["securitySchemes"]["clientCredentials"]
    assert scheme.get("description")


def test_every_operation_has_a_summary_and_a_description(
    document: dict[str, Any],
) -> None:
    """FastAPI takes both from the handler's docstring, so deleting one
    empties the contract silently."""
    for operations in document["paths"].values():
        for operation in operations.values():
            assert operation.get("summary"), operation["operationId"]
            assert operation.get("description"), operation["operationId"]


def test_the_licence_is_published_the_way_31_allows(document: dict[str, Any]) -> None:
    """`identifier` and `url` are mutually exclusive; publishing both is an
    invalid document, and the SPDX id is the more useful of the two."""
    licence = document["info"]["license"]
    assert licence["identifier"] == "AGPL-3.0-or-later"
    assert "url" not in licence


def test_withholding_the_docs_does_not_change_the_contract() -> None:
    """A deployment that does not publish its surface must still BE the
    application the committed document describes."""
    settings = api_settings().model_copy(update={"docs_enabled": False})
    assert create_app(settings).openapi() == create_app(api_settings()).openapi()


def test_the_document_is_a_VALID_openapi_document(document: dict[str, Any]) -> None:
    """A machine check for the classes of mistake a reader misses: a
    licence with both `identifier` and `url`, `example` alongside
    `examples`, a header object shaped like a parameter. Every one of those
    was proposed at some point while this contract was being written."""
    from openapi_spec_validator import validate  # noqa: PLC0415 - optional dev dependency

    validate(document)


def test_the_token_endpoint_answers_a_missing_field_in_the_OAUTH_shape() -> None:
    """The endpoint's docstring has always promised RFC 6749 errors, and
    until now the promise held only for the failures its own handler wrote.
    A missing form field reached the app-wide validation handler and came
    back as problem+json -- with no `error` field for a client library to
    read, and no test to notice, because the document still matched what we
    published."""
    with TestClient(create_app(api_settings()), raise_server_exceptions=False) as client:
        response = client.post("/oauth/token", data={})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"] == "invalid_request"
    assert "error_description" in body


def test_the_interval_type_matches_what_storage_can_resolve() -> None:
    """`ReadableInterval` is spelled out because a Literal's arguments have
    to be visible statically, so it is a second copy of the tuple. This is
    what keeps the copy honest."""
    import typing

    from yfin.models import READABLE_INTERVALS, ReadableInterval

    assert typing.get_args(ReadableInterval) == READABLE_INTERVALS


def test_the_bars_interval_is_published_as_a_choice(document: dict[str, Any]) -> None:
    """It was an unconstrained string, so a caller learned the list by
    guessing at it."""
    from yfin.models import READABLE_INTERVALS

    parameters = document["paths"]["/v1/symbols/{symbol}/bars"]["get"]["parameters"]
    interval = next(p for p in parameters if p["name"] == "interval")
    assert tuple(interval["schema"]["enum"]) == READABLE_INTERVALS
    assert interval["description"]


def test_every_resource_publishes_its_columns() -> None:
    """The generic route serves `dict[str, Any]` and will keep doing so, so
    the catalogue is the only place a caller can learn what a row carries.
    A resource with no columns would leave that surface undescribed."""
    from yfin.api.routers.v1.datasets import CatalogEntryOut
    from yfin.api.storage.catalog import CATALOG

    for entry in CATALOG.values():
        published = CatalogEntryOut.of(entry)
        assert published.columns, entry.name
        # Against what the route SELECTS, not against the table: a
        # resource may hide a column, and the two lists must agree on
        # exactly which.
        assert [c.name for c in published.columns] == [
            column.name for column in entry.served_columns
        ]


def test_every_exposed_column_has_a_published_wire_type() -> None:
    """`wire_type` raises on a type it has not been taught, and this is
    where that raise is meant to happen -- not on the first request to
    /v1/datasets after someone adds a JSONB column."""
    from yfin.api.storage.catalog import CATALOG
    from yfin.storage.wire import wire_type

    known = {
        "string",
        "integer",
        "boolean",
        "string (decimal)",
        "string (date-time)",
        "string (date)",
    }
    for entry in CATALOG.values():
        for column in entry.table.columns:
            assert wire_type(column) in known, f"{entry.table.name}.{column.name}"


def test_an_unteachable_column_type_is_refused() -> None:
    """Emitting "unknown" would publish a shape nobody had checked."""
    import sqlalchemy as sa

    from yfin.storage.wire import wire_type

    table = sa.Table("t", sa.MetaData(), sa.Column("blob", sa.LargeBinary()))
    with pytest.raises(ValueError, match="no published wire type"):
        wire_type(table.c.blob)


def test_the_introduction_documents_every_error_type(document: dict[str, Any]) -> None:
    """A `type` a client can receive but cannot look up is a type they will
    guess at. The schema publishes the shape; only the prose can say what
    to DO about each one."""
    from yfin.api.core.errors import ALL_TYPES

    text = document["info"]["description"]
    assert [t for t in ALL_TYPES if f"`{t}`" not in text] == []


def test_the_introduction_covers_caching_and_versioning(
    document: dict[str, Any],
) -> None:
    text = document["info"]["description"]
    for topic in ("If-None-Match", "X-Data-As-Of", "CHANGELOG", "deprecated"):
        assert topic in text, topic


def test_an_example_exists_for_everything_the_table_requires() -> None:
    """The presence check lives here, without a database, because the
    injection deliberately does not raise.

    Failing inside `finalise` would take /openapi.json and /docs down in
    production over a documentation file, and would deadlock CI: only the
    database job can produce one, while the job without a database would
    refuse to start without it. Here it costs nothing and still blocks the
    merge.
    """
    from yfin.api.core.openapi import REQUIRED_EXAMPLES, example_path

    missing = [
        path.name
        for operation, statuses in REQUIRED_EXAMPLES.items()
        for status, names in statuses.items()
        for name in names
        if not (path := example_path(operation, status, name)).is_file()
    ]
    assert missing == [], (
        f"capture them: pytest -m repo tests/repo/test_api_examples.py --snapshot-update"
        f" -- missing {missing}"
    )


def test_every_example_matches_the_schema_it_is_published_under(
    document: dict[str, Any],
) -> None:
    """An example that does not validate is worse than none: a reader
    copies it, and a generated client's tests fail against it."""
    from jsonschema import Draft202012Validator  # noqa: PLC0415 - dev dependency

    schemas = document["components"]["schemas"]
    for operations in document["paths"].values():
        for operation in operations.values():
            for code, response in operation["responses"].items():
                for media in response.get("content", {}).values():
                    for name, example in media.get("examples", {}).items():
                        # The document's own `components` travels with the
                        # schema, so `#/components/schemas/X` resolves --
                        # 2020-12 honours siblings of `$ref`, which older
                        # drafts did not.
                        validator = Draft202012Validator(
                            {**media["schema"], "components": {"schemas": schemas}}
                        )
                        errors = sorted(validator.iter_errors(example["value"]), key=str)
                        assert errors == [], (
                            f"{operation['operationId']}.{code}.{name}: {errors[0]}"
                        )


def test_the_examples_are_published_as_a_MAP(document: dict[str, Any]) -> None:
    """`examples`, never the singular `example`: 3.1 deprecates the latter
    on a Media Type Object, forbids using both, and it could not carry the
    two 422 bodies a reader has to be able to tell apart."""
    for operations in document["paths"].values():
        for operation in operations.values():
            for response in operation["responses"].values():
                for media in response.get("content", {}).values():
                    assert "example" not in media
    assert "invalid_cursor" in (
        document["paths"]["/v1/datasets/{name}"]["get"]["responses"]["422"]["content"][
            "application/problem+json"
        ]["examples"]
    )


def test_the_example_check_would_NOTICE_a_broken_example(
    document: dict[str, Any],
) -> None:
    """A validation test that silently validates nothing is worse than no
    test. This asserts the schema resolution above actually resolves."""
    from jsonschema import Draft202012Validator  # noqa: PLC0415 - dev dependency

    schemas = document["components"]["schemas"]
    validator = Draft202012Validator(
        {"$ref": "#/components/schemas/Problem", "components": {"schemas": schemas}}
    )
    assert list(validator.iter_errors({"type": "no_such_type", "title": 1}))
    assert not list(
        validator.iter_errors({"type": "not_found", "title": "x", "status": 404})
    )


def test_every_parameter_carries_an_example(document: dict[str, Any]) -> None:
    """A reader should be able to press "Try it out" and get a response,
    not first invent a symbol, a cursor and a date range."""
    missing = [
        f"{operation['operationId']}.{parameter['name']}"
        for operations in document["paths"].values()
        for operation in operations.values()
        for parameter in operation.get("parameters", ())
        if "example" not in parameter
    ]
    assert missing == []


def test_the_token_form_shows_what_to_post(document: dict[str, Any]) -> None:
    body = document["paths"]["/oauth/token"]["post"]["requestBody"]
    media = next(iter(body["content"].values()))
    assert media["examples"]["client_credentials"]["value"]["grant_type"] == (
        "client_credentials"
    )


def test_the_documentation_pages_load_nothing_from_a_THIRD_party_beacon() -> None:
    """FastAPI's default favicon is served from `fastapi.tiangolo.com`, so
    every reader of the contract would announce to another project's server
    that they had opened it."""
    with TestClient(create_app(api_settings())) as client:
        for path in ("/docs", "/redoc"):
            assert "fastapi.tiangolo.com" not in client.get(path).text, path


def test_the_documentation_pages_are_not_IN_the_contract(
    document: dict[str, Any],
) -> None:
    """They are how the contract is read, not part of it. Being in the
    document would also put them in the operation-id and response tables,
    which describe the API."""
    for path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
        assert path not in document["paths"]


def test_the_introduction_says_what_this_api_does_NOT_serve(
    document: dict[str, Any],
) -> None:
    """Live ticks are collected into `live_ticks` and published to Kafka,
    and no dataset exposes them. A reader who knows the pipeline exists
    would otherwise go looking for an endpoint that was never there."""
    text = document["info"]["description"]
    assert "Live ticks are not served here" in text
    assert "ACME" in text


def test_a_hidden_column_is_served_nowhere() -> None:
    """`hidden` has to reach BOTH the query and the catalogue, or the
    document describes a row the route does not send -- or worse, the route
    sends a column the document never mentioned."""
    from yfin.api.routers.v1.datasets import CatalogEntryOut
    from yfin.api.storage.catalog import CATALOG

    for entry in CATALOG.values():
        hidden = set(entry.exposure.hidden)
        if not hidden:
            continue
        served = {column.name for column in entry.served_columns}
        assert served & hidden == set(), entry.name
        published = {column.name for column in CatalogEntryOut.of(entry).columns}
        assert published == served, entry.name


def test_a_hidden_column_cannot_also_be_paged_on() -> None:
    """Filtering or sorting on a column a caller never receives leaves them
    holding a cursor they cannot reason about."""
    from yfin.datasets.exposure import ApiExposure

    exposure = ApiExposure(
        family=DataFamily.DISCOVERY,
        table="screens",
        sort_key=("screen_key",),
        hidden=("screen_key",),
    )
    with pytest.raises(ValueError, match="hidden but also used"):
        exposure.validate(dataset_name="x", produces=("screens",))


def test_the_screen_keys_are_discoverable() -> None:
    """Three resources take `screen_key` as a filter, and until this
    resource existed nothing told a caller which keys there are. It was
    unpublishable only because the same row carries the query definition."""
    from yfin.api.storage.catalog import CATALOG

    entry = CATALOG["screens"]
    served = {column.name for column in entry.served_columns}
    assert "screen_key" in served
    assert "title" in served
    assert "definition_json" not in served


def test_a_route_that_declares_no_contract_cannot_produce_a_document() -> None:
    """The declaration is required, not defaulted.

    Defaulting it to "meters nothing, caches nothing" is how a new route
    would publish no rate headers while metering every request -- the
    document quietly untrue about the one thing a client bills against.
    Three tests used to be believed to guard this; each compared the
    document against the table that generated it, so none of them could
    fail. This one fails at document build.
    """
    from yfin.api.core.openapi import CONTRACT_KEY, _apply

    with pytest.raises(ValueError, match="openapi_extra"):
        _apply({"responses": {}}, "listSymbols")

    # And accepts it when present, so the guard is about absence only.
    operation: dict[str, Any] = {
        "responses": {},
        CONTRACT_KEY: {"metered": True, "cached": True, "conditional": True},
    }
    _apply(operation, "listSymbols")
    assert CONTRACT_KEY not in operation, "the internal flag must not be published"
    assert "304" in operation["responses"]

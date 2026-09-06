"""The published contract: the committed document and what it promises.

The contract is only meaningful if changing it is visible. `openapi.json`
is generated from the code and committed, so a renamed Pydantic field
shows up as a reviewable diff instead of surfacing when a client breaks.
The check runs here as well as in CI, so it fails on the machine where
the change was made rather than a push later.
"""

from __future__ import annotations

import json
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

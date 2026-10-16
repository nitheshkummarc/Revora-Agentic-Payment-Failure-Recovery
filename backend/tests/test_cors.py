"""CORS scoping.

The web app is served from a different origin than the API, so some
allowance is required. What matters is that it is an allowance and not a
wildcard: a public demo with `*` invites any page on the internet to read the
endpoint. Nothing here is credentialed, so a wildcard would not
have leaked anything, but the narrower list costs nothing and these tests stop
it drifting back.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import DEFAULT_CORS_ORIGINS, app

client = TestClient(app)

WEB_ORIGIN = "http://localhost:5173"
FOREIGN_ORIGIN = "https://evil.example.com"


def test_the_web_app_origin_is_allowed():
    response = client.get("/health", headers={"Origin": WEB_ORIGIN})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == WEB_ORIGIN


@pytest.mark.parametrize("origin", DEFAULT_CORS_ORIGINS)
def test_every_default_origin_is_allowed(origin):
    """Both ports and both spellings of loopback. A browser treats localhost
    and 127.0.0.1 as distinct origins, so covering one is not covering both."""
    response = client.get("/health", headers={"Origin": origin})
    assert response.headers["access-control-allow-origin"] == origin


def test_an_unlisted_origin_is_not_granted_access():
    """The request still succeeds -- CORS is enforced by the browser, not the
    server -- but no allow-origin header comes back, so a browser refuses to
    hand the response to the page."""
    response = client.get("/health", headers={"Origin": FOREIGN_ORIGIN})
    assert "access-control-allow-origin" not in response.headers


def test_the_allowance_is_not_a_wildcard():
    """The specific regression this guards: `allow_origins=["*"]`."""
    assert "*" not in DEFAULT_CORS_ORIGINS
    response = client.get("/health", headers={"Origin": FOREIGN_ORIGIN})
    assert response.headers.get("access-control-allow-origin") != "*"


def test_preflight_offers_only_get_and_post():
    """GET for reading results, POST for the agent demo's run endpoint, which
    acts only on a sandbox gateway built per request. Nothing that edits or
    deletes is offered."""
    response = client.options(
        "/api/agent/run",
        headers={
            "Origin": WEB_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )
    allowed = response.headers.get("access-control-allow-methods", "")
    assert "GET" in allowed and "POST" in allowed
    for verb in ("PUT", "DELETE", "PATCH"):
        assert verb not in allowed


def test_credentials_are_not_allowed():
    """Nothing here is authenticated, so no cookie or auth header should ever
    ride along on a cross-origin request."""
    response = client.get("/health", headers={"Origin": WEB_ORIGIN})
    assert response.headers.get("access-control-allow-credentials") != "true"

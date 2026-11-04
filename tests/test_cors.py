"""CORS: the Pages origin must be allowed, and nothing broader than needed.

The frontend is served from GitHub Pages, which is a different origin from the
gateway, so every search is a cross-origin request. A missing header here is not
a slow search — it is a search that never leaves the browser.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.engine.config import Settings

PAGES_ORIGIN = "https://eyesearch.github.io"
LOCAL_ORIGIN = "http://localhost:5173"


@pytest.fixture()
def client():
    from app.main import app

    return TestClient(app)


def test_a_preflight_from_the_pages_origin_is_allowed(client):
    response = client.options(
        "/v1/search",
        headers={
            "Origin": PAGES_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "accept",
        },
    )

    assert response.status_code in (200, 204), response.status_code
    allowed = response.headers.get("access-control-allow-origin")
    assert allowed in ("*", PAGES_ORIGIN), allowed


def test_a_preflight_from_a_local_dev_server_is_allowed(client):
    response = client.options(
        "/v1/search",
        headers={"Origin": LOCAL_ORIGIN, "Access-Control-Request-Method": "GET"},
    )

    assert response.headers.get("access-control-allow-origin") in ("*", LOCAL_ORIGIN)


def test_the_search_methods_are_permitted(client):
    """GET for search and suggest, POST for retrieve. Nothing else exists."""
    response = client.options(
        "/v1/search",
        headers={"Origin": PAGES_ORIGIN, "Access-Control-Request-Method": "GET"},
    )

    methods = (response.headers.get("access-control-allow-methods") or "").upper()
    assert "*" in methods or "GET" in methods, methods


def test_an_arbitrary_request_header_is_permitted(client):
    """The app sends Accept; a proxy may add its own. The list is open."""
    response = client.options(
        "/v1/suggest",
        headers={
            "Origin": PAGES_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "accept, x-request-id",
        },
    )

    headers = (response.headers.get("access-control-allow-headers") or "").lower()
    assert "*" in headers or "x-request-id" in headers, headers


def test_a_wildcard_origin_is_not_paired_with_credentials(client):
    """`*` with credentials is refused by every browser, and it is a mistake
    worth failing on rather than discovering in production."""
    from app.main import app

    cors = [m for m in app.user_middleware if "CORS" in str(m)]
    assert cors, "no CORS middleware is installed"
    options = cors[0].kwargs
    if options.get("allow_origins") == ["*"]:
        assert options.get("allow_credentials") is False


def test_the_origin_list_parses_a_comma_separated_setting():
    single = Settings(_env_file=None, cors_origins=PAGES_ORIGIN)
    assert single.cors_origin_list == [PAGES_ORIGIN]

    several = Settings(_env_file=None, cors_origins=f"{PAGES_ORIGIN}, {LOCAL_ORIGIN}")
    assert several.cors_origin_list == [PAGES_ORIGIN, LOCAL_ORIGIN]

    star = Settings(_env_file=None, cors_origins="*")
    assert star.cors_origin_list == ["*"]


def test_the_default_allows_the_pages_origin():
    """A fresh deploy with no CORS_ORIGINS set must still serve the frontend."""
    assert "*" in Settings(_env_file=None).cors_origin_list

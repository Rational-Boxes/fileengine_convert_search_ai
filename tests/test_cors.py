"""CORS: enabled only when CSAI_CORS_ORIGINS is set; reflects allowed origins."""
from fastapi.testclient import TestClient

from convert_search_ai.app import build_app
from convert_search_ai.config import Config


def _client(origins):
    cfg = Config()
    cfg.cors_origins = origins
    return TestClient(build_app(cfg))


def test_cors_headers_present_for_allowed_origin():
    c = _client(["http://localhost:3000"])
    # Preflight for a credentialed POST /search from the SPA origin.
    r = c.options(
        "/search",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,x-tenant",
        },
    )
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"

    # A simple GET also gets the allow-origin header reflected.
    r2 = c.get("/healthz", headers={"Origin": "http://localhost:3000"})
    assert r2.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_disabled_by_default():
    c = _client([])
    r = c.get("/healthz", headers={"Origin": "http://localhost:3000"})
    assert "access-control-allow-origin" not in r.headers

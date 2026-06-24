"""Unit tests for the FastAPI app — run anywhere (no services needed)."""
from fastapi.testclient import TestClient

from convert_search_ai.app import build_app


def test_healthz():
    client = TestClient(build_app())
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "convert_search_ai"


def test_readyz_reports_checks():
    # With no services configured/reachable, readiness is 503 and reports which
    # dependency is down — never raises.
    client = TestClient(build_app())
    r = client.get("/readyz")
    assert r.status_code in (200, 503)
    body = r.json()
    assert set(body["checks"]) == {"core", "ldap"}
    assert body["ready"] == (r.status_code == 200)

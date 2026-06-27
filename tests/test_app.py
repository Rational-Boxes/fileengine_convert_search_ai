"""Unit tests for the FastAPI app — run anywhere (no services needed)."""
from fastapi.testclient import TestClient

import convert_search_ai.app as appmod
from convert_search_ai.app import build_app


def test_create_app_loads_dotenv_before_building(monkeypatch):
    # The ASGI factory must load .env BEFORE constructing the config, so launches
    # that skip main() still see CSAI_* settings (the cause of the spurious
    # "No module named anthropic" when .env wasn't loaded). build_app stays pure.
    seq = []
    monkeypatch.setattr(appmod, "load_dotenv", lambda *a, **k: seq.append("env"))
    monkeypatch.setattr(appmod, "build_app", lambda *a, **k: seq.append("build") or "APP")
    assert appmod.create_app() == "APP"
    assert seq == ["env", "build"]


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

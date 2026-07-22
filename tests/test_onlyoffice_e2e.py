"""Opt-in E2E against a REAL ONLYOFFICE Document Server.

Validates the service-to-service JWT seam end to end: our ``sign_onlyoffice_jwt`` is
wire-compatible with the running Document Server, and JWT is enforced. Uses the
Document Server's ConvertService as a probe (no gRPC core / interactive editing
needed): a request pointing at an unresolvable document URL returns a *download*
error (not a token error) only if the token was accepted first.

Opt-in (talks to a real service):
  CSAI_ONLYOFFICE_E2E=1
  CSAI_ONLYOFFICE_DOCSERVER_URL=http://localhost:8080   (default)
  CSAI_ONLYOFFICE_JWT_SECRET=<the Document Server's JWT_SECRET>
  pytest tests/test_onlyoffice_e2e.py -q
"""
import json
import os
import urllib.request

import pytest

from convert_search_ai.onlyoffice import sign_onlyoffice_jwt

_DS = os.environ.get("CSAI_ONLYOFFICE_DOCSERVER_URL", "http://localhost:8080").rstrip("/")
_SECRET = os.environ.get("CSAI_ONLYOFFICE_JWT_SECRET", "")


def _opt_in() -> bool:
    return os.environ.get("CSAI_ONLYOFFICE_E2E", "").strip().lower() in ("1", "true", "yes", "on")


def _skip_reason() -> str:
    if not _opt_in():
        return "opt-in only — set CSAI_ONLYOFFICE_E2E=1 (talks to a real Document Server)"
    try:
        with urllib.request.urlopen(f"{_DS}/healthcheck", timeout=5) as r:
            if "true" not in r.read().decode().lower():
                return "Document Server healthcheck did not return true"
    except Exception as e:
        return f"Document Server unreachable at {_DS}: {e.__class__.__name__}"
    return ""


pytestmark = pytest.mark.skipif(bool(_skip_reason()), reason=_skip_reason())


def _convert(body: dict, *, sign: bool):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if sign and _SECRET:
        body = {**body, "token": sign_onlyoffice_jwt(_SECRET, body)}
        headers["Authorization"] = "Bearer " + sign_onlyoffice_jwt(_SECRET, {"payload": body})
    req = urllib.request.Request(f"{_DS}/ConvertService.ashx",
                                 data=json.dumps(body).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _req(key: str) -> dict:
    # An unresolvable URL so the fetch fails FAST (DNS) — we only care whether the
    # token got us past auth to the download stage.
    return {"async": False, "filetype": "docx", "outputtype": "pdf", "key": key,
            "title": "probe.docx", "url": "http://does-not-exist.invalid/x.docx"}


def test_docserver_healthy():
    with urllib.request.urlopen(f"{_DS}/healthcheck", timeout=5) as r:
        assert "true" in r.read().decode().lower()


def test_jwt_is_enforced():
    # No token → the Document Server rejects with the token error (-8).
    assert _convert(_req("e2e-unsigned"), sign=False).get("error") == -8


def test_our_signed_token_is_accepted():
    if not _SECRET:
        pytest.skip("set CSAI_ONLYOFFICE_JWT_SECRET to the Document Server's JWT_SECRET")
    # Signed with our own signer → the token is accepted; the request reaches the
    # download stage and fails there (not a token error). error != -8 proves the
    # JWT contract is wire-compatible with the real Document Server.
    err = _convert(_req("e2e-signed"), sign=True).get("error")
    assert err != -8, f"signed request was rejected as a token error: {err}"

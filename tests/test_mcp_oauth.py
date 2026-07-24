# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Unit tests for MCP OAuth client-credentials token fetch + cache (offline)."""
import json
from types import SimpleNamespace

import pytest

import convert_search_ai.mcp_client as mc
from convert_search_ai import mcp_oauth


def _integ(**over):
    base = dict(id="i1", token_url="https://auth.example.com/token",
                oauth_client_id="client-1", oauth_scope="mcp.read")
    base.update(over)
    return SimpleNamespace(**base)


# ------------------------------- token cache --------------------------------
def test_cache_fetches_once_then_serves_cached():
    cache = mcp_oauth.OAuthTokenCache()
    calls = {"n": 0}

    def fetch(url, cid, secret, scope, *, timeout_s):
        calls["n"] += 1
        return f"tok-{calls['n']}", 3600

    integ = _integ()
    t1 = cache.get_token(integ, "secret", fetch=fetch, now=1000)
    t2 = cache.get_token(integ, "secret", fetch=fetch, now=1100)  # within TTL
    assert t1 == "tok-1" and t2 == "tok-1" and calls["n"] == 1


def test_cache_refreshes_before_expiry():
    cache = mcp_oauth.OAuthTokenCache()
    calls = {"n": 0}

    def fetch(url, cid, secret, scope, *, timeout_s):
        calls["n"] += 1
        return f"tok-{calls['n']}", 100   # expires_in=100

    integ = _integ()
    cache.get_token(integ, "s", fetch=fetch, now=1000)          # expiry=1100
    # at now=1080, 1080+30 >= 1100 → refresh
    t2 = cache.get_token(integ, "s", fetch=fetch, now=1080)
    assert t2 == "tok-2" and calls["n"] == 2


def test_rotating_secret_busts_the_cache():
    cache = mcp_oauth.OAuthTokenCache()
    calls = {"n": 0}

    def fetch(url, cid, secret, scope, *, timeout_s):
        calls["n"] += 1
        return f"tok-{secret}", 3600

    integ = _integ()
    a = cache.get_token(integ, "old-secret", fetch=fetch, now=1000)
    b = cache.get_token(integ, "new-secret", fetch=fetch, now=1001)  # different secret
    assert a == "tok-old-secret" and b == "tok-new-secret" and calls["n"] == 2


def test_changing_token_url_busts_the_cache():
    cache = mcp_oauth.OAuthTokenCache()
    calls = {"n": 0}

    def fetch(url, cid, secret, scope, *, timeout_s):
        calls["n"] += 1
        return "tok", 3600

    cache.get_token(_integ(token_url="https://a/token"), "s", fetch=fetch, now=1000)
    cache.get_token(_integ(token_url="https://b/token"), "s", fetch=fetch, now=1000)
    assert calls["n"] == 2


def test_invalidate_forces_refetch():
    cache = mcp_oauth.OAuthTokenCache()
    calls = {"n": 0}

    def fetch(url, cid, secret, scope, *, timeout_s):
        calls["n"] += 1
        return "tok", 3600

    integ = _integ()
    cache.get_token(integ, "s", fetch=fetch, now=1000)
    cache.invalidate("i1")
    cache.get_token(integ, "s", fetch=fetch, now=1001)
    assert calls["n"] == 2


# --------------------------- HTTP token exchange ----------------------------
class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_fetch_client_credentials_token_parses_response(monkeypatch):
    monkeypatch.setattr(mc, "validate_endpoint", lambda u: u)  # skip DNS/SSRF in unit test
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = req.data.decode()
        seen["auth"] = req.get_header("Authorization")
        return _Resp({"access_token": "abc123", "token_type": "Bearer", "expires_in": 1200})

    monkeypatch.setattr(mcp_oauth.urllib.request, "urlopen", fake_urlopen)
    token, expires = mcp_oauth.fetch_client_credentials_token(
        "https://auth.example.com/token", "client-1", "shh", "mcp.read")
    assert token == "abc123" and expires == 1200
    assert "grant_type=client_credentials" in seen["body"] and "scope=mcp.read" in seen["body"]
    assert seen["auth"].startswith("Basic ")  # client auth via HTTP Basic


def test_fetch_raises_without_access_token(monkeypatch):
    monkeypatch.setattr(mc, "validate_endpoint", lambda u: u)
    monkeypatch.setattr(mcp_oauth.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp({"error": "invalid_client"}))
    with pytest.raises(mcp_oauth.McpOAuthError):
        mcp_oauth.fetch_client_credentials_token("https://a/token", "c", "s")


def test_fetch_rejects_non_public_token_url():
    # Real SSRF guard: a private/loopback token_url is refused (no network).
    with pytest.raises(mcp_oauth.McpOAuthError):
        mcp_oauth.fetch_client_credentials_token("https://127.0.0.1/token", "c", "s")

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

"""Auth coordination: accept http_bridge bearer tokens via introspection."""
import json

from convert_search_ai import bridge_auth
from convert_search_ai.bridge_auth import BridgeTokenVerifier
from convert_search_ai.http_auth import resolve_identity
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.token_store import TokenStore


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self):
        return json.dumps(self._body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(body, status=200, calls=None):
    def f(req, timeout=None):
        if calls is not None:
            calls.append(req)
        return _Resp(status, body)
    return f


def test_disabled_when_no_url():
    v = BridgeTokenVerifier("")
    assert v.enabled is False
    assert v.verify("tok", "default") is None


def test_verify_resolves_identity_and_forwards_headers(monkeypatch):
    calls = []
    monkeypatch.setattr(bridge_auth.urllib.request, "urlopen", _fake_urlopen(
        {"active": True, "user": "alice", "tenant": "issued", "roles": ["r1", "r2"]}, calls=calls))
    v = BridgeTokenVerifier("http://bridge:8090/", ttl_seconds=60)
    ident = v.verify("tok", "acme")
    assert ident is not None
    assert ident.user == "alice" and ident.roles == ["r1", "r2"] and ident.authenticated
    assert ident.tenant == "acme"                  # request tenant wins over issue-time
    assert len(calls) == 1
    req = calls[0]
    assert req.full_url == "http://bridge:8090/v1/auth/introspect"
    assert req.get_header("Authorization") == "Bearer tok"
    assert req.get_header("X-tenant") == "acme"    # urllib normalizes header case


def test_verify_caches_within_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(bridge_auth.urllib.request, "urlopen",
                        _fake_urlopen({"active": True, "user": "a"}, calls=calls))
    v = BridgeTokenVerifier("http://b", ttl_seconds=60)
    assert v.verify("t", "d").user == "a"
    assert v.verify("t", "d").user == "a"
    assert len(calls) == 1                          # second served from cache


def test_inactive_token_returns_none(monkeypatch):
    monkeypatch.setattr(bridge_auth.urllib.request, "urlopen",
                        _fake_urlopen({"active": False}))
    assert BridgeTokenVerifier("http://b").verify("t", "d") is None


def test_unreachable_bridge_returns_none(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("connection refused")
    monkeypatch.setattr(bridge_auth.urllib.request, "urlopen", boom)
    assert BridgeTokenVerifier("http://b").verify("t", "d") is None


def test_resolve_identity_falls_back_to_bridge_for_unknown_token():
    store = TokenStore()

    class FakeBridge:
        def __init__(self):
            self.calls = []

        def verify(self, token, tenant):
            self.calls.append((token, tenant))
            return Identity(user="bob", roles=["x"], tenant=tenant, authenticated=True)

    b = FakeBridge()
    out = resolve_identity("Bearer unknown", "acme", None, store, b)
    assert out is not None and out.user == "bob" and out.tenant == "acme"
    assert b.calls == [("unknown", "acme")]


def test_resolve_identity_prefers_local_store_over_bridge():
    store = TokenStore()
    tok = store.issue(Identity(user="local", roles=[], tenant="t", authenticated=True))

    class FakeBridge:
        def verify(self, *a):
            raise AssertionError("bridge must not be consulted for our own token")

    out = resolve_identity(f"Bearer {tok}", "acme", None, store, FakeBridge())
    assert out.user == "local" and out.tenant == "acme"


def test_resolve_identity_without_bridge_rejects_unknown_token():
    # Backward-compatible: no bridge configured -> unknown bearer is just invalid.
    assert resolve_identity("Bearer nope", "t", None, TokenStore()) is None

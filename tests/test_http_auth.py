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

"""Unit tests for per-request credential resolution."""
import base64
from dataclasses import replace

import convert_search_ai.http_auth as ha
from convert_search_ai.config import Config
from convert_search_ai.http_auth import decode_basic, extract_tenant, resolve_identity
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.token_store import TokenStore


def test_decode_basic():
    hdr = "Basic " + base64.b64encode(b"bob:s3cr3t").decode()
    assert decode_basic(hdr) == ("bob", "s3cr3t")
    assert decode_basic("Bearer x") is None
    assert decode_basic("Basic !!notb64") is None


def test_extract_tenant():
    assert extract_tenant({"x-tenant": "acme"}, "anything", "default") == "acme"
    assert extract_tenant({}, "acme.example.com", "default") == "acme"
    assert extract_tenant({}, "www.example.com", "default") == "default"
    assert extract_tenant({}, "localhost:8092", "default") == "default"


def test_resolve_bearer_scopes_to_request_tenant():
    store = TokenStore()
    ident = Identity(user="svc", roles=["readers"], tenant="default", authenticated=True)
    tok = store.issue(ident)
    assert resolve_identity(f"Bearer {tok}", "acme", None, store) == replace(ident, tenant="acme")
    assert resolve_identity("Bearer bad", "acme", None, store) is None
    assert resolve_identity("", "acme", None, store) is None


def test_resolve_basic_binds_via_ldap(monkeypatch):
    cfg = Config()

    def fake_auth(config, user, password):
        return Identity(user=user, roles=["users"], tenant=config.tenant,
                        authenticated=(password == "right"))

    monkeypatch.setattr(ha, "authenticate", fake_auth)
    good = "Basic " + base64.b64encode(b"alice:right").decode()
    bad = "Basic " + base64.b64encode(b"alice:wrong").decode()
    out = resolve_identity(good, "t1", cfg, TokenStore())
    assert out.authenticated and out.user == "alice" and out.tenant == "t1"
    assert resolve_identity(bad, "t1", cfg, TokenStore()) is None

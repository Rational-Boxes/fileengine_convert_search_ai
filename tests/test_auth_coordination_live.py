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

"""``@live`` cross-service auth coordination: a bearer token minted by the HTTP
bridge authenticates against convert_search_ai via the bridge's
``/v1/auth/introspect`` — one login spans both services.

Skips unless the bridge is reachable and can mint a token for the agent identity
(``FILEENGINE_CSAI_USER/PASSWORD``). Needs only the bridge (and its LDAP); no
Postgres, since it exercises identity resolution (``/whoami``) only.
"""
import base64
import json
import os
import urllib.request

import pytest

from convert_search_ai.config import Config

BRIDGE_URL = os.environ.get("CSAI_BRIDGE_URL", "http://localhost:8090").rstrip("/")


def _mint_bridge_token(user: str, password: str):
    req = urllib.request.Request(BRIDGE_URL + "/v1/auth/token", method="POST")
    req.add_header("Authorization",
                   "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r).get("token")


def _skip_reason() -> str:
    cfg = Config()
    if not cfg.agent_user or not cfg.agent_password:
        return "agent credentials not set (FILEENGINE_CSAI_USER/PASSWORD)"
    try:
        return "" if _mint_bridge_token(cfg.agent_user, cfg.agent_password) \
            else "bridge issued no token"
    except Exception as e:
        return f"bridge unreachable: {e.__class__.__name__}"


_SKIP = _skip_reason()
pytestmark = pytest.mark.skipif(bool(_SKIP), reason=_SKIP or "live")


def _client(bridge_url: str):
    from fastapi.testclient import TestClient
    from convert_search_ai.app import build_app
    cfg = Config()
    cfg.bridge_url = bridge_url
    return TestClient(build_app(cfg)), cfg


def test_bridge_token_accepted_via_introspection():
    client, cfg = _client(BRIDGE_URL)
    tok = _mint_bridge_token(cfg.agent_user, cfg.agent_password)
    r = client.get("/whoami", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert r.json()["user"] == cfg.agent_user


def test_x_tenant_is_honored_through_coordination():
    client, cfg = _client(BRIDGE_URL)
    tok = _mint_bridge_token(cfg.agent_user, cfg.agent_password)
    r = client.get("/whoami", headers={"Authorization": f"Bearer {tok}", "X-Tenant": "acme"})
    assert r.status_code == 200 and r.json()["tenant"] == "acme"


def test_coordination_off_rejects_bridge_token():
    client, cfg = _client("")            # CSAI_BRIDGE_URL unset → coordination disabled
    tok = _mint_bridge_token(cfg.agent_user, cfg.agent_password)
    r = client.get("/whoami", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401          # not one of CSAI's own tokens, bridge off

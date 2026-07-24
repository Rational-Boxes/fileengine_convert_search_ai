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

"""Integration smoke tests — require LDAP + the gRPC core (``@live``).

These confirm the M0 plumbing works end to end: the agent authenticates against
LDAP, and an identity-bound gRPC client can reach the core. They skip cleanly
when services or credentials are absent."""
from conftest import live


@live
def test_agent_authenticates(config):
    from convert_search_ai.core_client import agent_identity
    ident = agent_identity(config)
    assert ident.authenticated
    assert ident.user == config.agent_user


@live
def test_identity_bound_client_reaches_core(config):
    import grpc
    from convert_search_ai.core_client import agent_identity, client_for

    ident = agent_identity(config)
    mf = client_for(ident, config)
    try:
        # The channel should become ready against the live core.
        grpc.channel_ready_future(mf.channel).result(timeout=5)
    finally:
        mf.close()


@live
def test_readyz_ok_when_services_up(config):
    from fastapi.testclient import TestClient
    from convert_search_ai.app import build_app

    client = TestClient(build_app(config))
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["checks"] == {"core": True, "ldap": True}

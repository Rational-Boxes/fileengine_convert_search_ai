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

"""The in-cluster text API: a service asserting whose behalf it acts.

MCP is the caller this exists for. It authenticates its own users and holds an
ACL-enforced core client, but no credential CSAI accepts — so it names the
principal over a secret-authenticated channel and CSAI applies its own READ gate
to that name. What is trusted is the NAME, never the access."""
import pytest
from fastapi.testclient import TestClient


SECRET = "shared-internal-secret"


class _Search:
    """Stands in for SearchService: records who it was asked about."""

    def __init__(self, text="# Title\n\nbody", raises=None):
        self.text = text
        self.raises = raises
        self.asked = []

    def get_text(self, identity, file_uid):
        self.asked.append((identity.user, tuple(identity.roles), identity.tenant, file_uid))
        if self.raises:
            raise self.raises
        return self.text, False


def _client(secret=SECRET, search=None):
    from convert_search_ai.api import router
    from convert_search_ai.config import Config
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    cfg = Config()
    cfg.internal_secret = secret
    app.state.config = cfg
    app.state.search = search or _Search()
    return TestClient(app, raise_server_exceptions=False), app.state.search


_BODY = {"user": "jo@example.com", "roles": ["administrators"], "tenant": "acme"}
_URL = "/internal/documents/f1/text"


def test_returns_the_markdown_for_the_asserted_principal():
    client, search = _client()
    r = client.post(_URL, json=_BODY, headers={"X-Internal-Auth": SECRET})
    assert r.status_code == 200
    assert r.json()["text"] == "# Title\n\nbody"
    assert r.json()["tenant"] == "acme"
    # The gate ran for the NAMED user, not for the calling service.
    assert search.asked == [("jo@example.com", ("administrators",), "acme", "f1")]


def test_a_wrong_secret_is_refused_and_asks_nothing():
    client, search = _client()
    r = client.post(_URL, json=_BODY, headers={"X-Internal-Auth": "wrong"})
    assert r.status_code == 403
    assert search.asked == []


def test_no_secret_header_is_refused():
    client, search = _client()
    assert client.post(_URL, json=_BODY).status_code == 403
    assert search.asked == []


def test_an_unset_secret_disables_the_route_rather_than_opening_it():
    """/csai/ is proxied as a whole prefix, so an unconfigured deployment must
    not leave this reachable — the mistake /ingest/reconcile once made."""
    client, search = _client(secret="")
    r = client.post(_URL, json=_BODY, headers={"X-Internal-Auth": ""})
    assert r.status_code == 404
    assert search.asked == []


def test_the_principal_is_required():
    client, _ = _client()
    for body in ({"tenant": "acme"}, {"user": "jo@example.com"}, {}):
        r = client.post(_URL, json=body, headers={"X-Internal-Auth": SECRET})
        assert r.status_code == 400


def test_a_denied_read_is_403_not_content():
    """The assertion names a user; it does not grant them anything."""
    client, _ = _client(search=_Search(raises=PermissionError("f1")))
    r = client.post(_URL, json=_BODY, headers={"X-Internal-Auth": SECRET})
    assert r.status_code == 403


def test_a_file_with_no_extracted_text_is_404():
    client, _ = _client(search=_Search(raises=FileNotFoundError("f1")))
    r = client.post(_URL, json=_BODY, headers={"X-Internal-Auth": SECRET})
    assert r.status_code == 404

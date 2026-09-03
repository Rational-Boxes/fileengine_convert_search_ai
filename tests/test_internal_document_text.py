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


class _Hit:
    def __init__(self, uid, name):
        self.file_uid, self.name, self.snippet, self.score = uid, name, "…snip…", 0.5


class _Search:
    """Stands in for SearchService: records who it was asked about."""

    def __init__(self, text="# Title\n\nbody", raises=None, hits=None):
        self.text = text
        self.raises = raises
        self.hits = hits if hits is not None else [_Hit("f1", "a.docx")]
        self.asked = []
        self.searched = []

    def get_text(self, identity, file_uid):
        self.asked.append((identity.user, tuple(identity.roles), identity.tenant, file_uid))
        if self.raises:
            raise self.raises
        return self.text, False

    def search(self, identity, query, *, limit=20, fuzzy=True):
        self.searched.append((identity.user, tuple(identity.roles), identity.tenant,
                              query, limit, fuzzy))
        if self.raises:
            raise self.raises
        return list(self.hits)


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


# --- internal search ---------------------------------------------------------
#
# Same trust, and the case where it matters most: a hit list discloses what
# exists and roughly what it says, so the per-hit permission filter has to run
# here, for the named principal, rather than being taken on trust from a caller.

_SEARCH_URL = "/internal/search"


def test_search_runs_as_the_asserted_principal():
    client, search = _client()
    r = client.post(_SEARCH_URL, json=dict(_BODY, query="leed", limit=5, fuzzy=False),
                    headers={"X-Internal-Auth": SECRET})
    assert r.status_code == 200
    assert r.json()["hits"] == [
        {"file_uid": "f1", "name": "a.docx", "snippet": "…snip…", "score": 0.5}]
    assert search.searched == [
        ("jo@example.com", ("administrators",), "acme", "leed", 5, False)]


def test_search_defaults_match_the_public_route():
    client, search = _client()
    client.post(_SEARCH_URL, json=dict(_BODY, query="leed"),
                headers={"X-Internal-Auth": SECRET})
    assert search.searched[0][4:] == (20, True)


def test_search_needs_the_secret_and_asks_nothing_without_it():
    client, search = _client()
    assert client.post(_SEARCH_URL, json=dict(_BODY, query="leed"),
                       headers={"X-Internal-Auth": "wrong"}).status_code == 403
    assert client.post(_SEARCH_URL, json=dict(_BODY, query="leed")).status_code == 403
    assert search.searched == []


def test_search_is_off_when_no_secret_is_configured():
    client, search = _client(secret="")
    r = client.post(_SEARCH_URL, json=dict(_BODY, query="leed"),
                    headers={"X-Internal-Auth": ""})
    assert r.status_code == 404
    assert search.searched == []


def test_search_requires_the_principal():
    client, _ = _client()
    r = client.post(_SEARCH_URL, json={"query": "leed"}, headers={"X-Internal-Auth": SECRET})
    assert r.status_code == 400


def test_a_rejected_query_is_400_like_the_public_route():
    """Empty or over-long queries are guard failures, not server errors."""
    from convert_search_ai.guards import GuardError
    client, _ = _client(search=_Search(raises=GuardError("query is empty")))
    r = client.post(_SEARCH_URL, json=dict(_BODY, query=""), headers={"X-Internal-Auth": SECRET})
    assert r.status_code == 400

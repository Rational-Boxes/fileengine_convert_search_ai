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

"""Conversation persistence — REST CRUD + WS turn persistence (in-memory fake store)."""
from conftest import live_db
from fastapi.testclient import TestClient

from convert_search_ai.app import build_app
from convert_search_ai.config import Config
from convert_search_ai.ldap_auth import Identity


class FakeChat:
    def answer(self, identity, *, message, system_prompt="", history=None, k=8, web_search=None,
               conversation_id=None):
        yield {"type": "token", "text": f"Answer to: {message}"}
        yield {"type": "citations", "citations": [{"marker": 1, "kind": "doc", "file_uid": "f1"}]}


class FakeConversationStore:
    """In-memory stand-in matching ConversationStore's interface."""
    def __init__(self):
        self.data: dict = {}  # cid -> {user, title, messages[]}
        self._n = 0

    def list(self, tenant, user, *, limit=200):
        return [{"id": cid, "title": c["title"], "updated_at": "2026-01-01T00:00:00"}
                for cid, c in self.data.items() if c["user"] == user]

    def create(self, tenant, user, *, title=""):
        self._n += 1
        cid = f"c{self._n}"
        self.data[cid] = {"user": user, "title": title, "messages": []}
        return cid

    def get(self, tenant, user, cid):
        c = self.data.get(cid)
        if not c or c["user"] != user:
            return None
        return {"id": cid, "title": c["title"], "messages": c["messages"]}

    def owns(self, tenant, user, cid):
        c = self.data.get(cid)
        return bool(c and c["user"] == user)

    def append(self, tenant, user, cid, role, content, citations=None):
        c = self.data.get(cid)
        if not c or c["user"] != user:
            return False
        c["messages"].append({"role": role, "content": content, "citations": citations or []})
        return True

    def set_title_if_empty(self, tenant, user, cid, title):
        c = self.data.get(cid)
        if c and c["user"] == user and not c["title"]:
            c["title"] = title

    def delete(self, tenant, user, cid):
        c = self.data.get(cid)
        if c and c["user"] == user:
            del self.data[cid]
            return True
        return False


def _app(convos):
    app = build_app(Config(), chat=FakeChat(), conversations=convos)
    return app, TestClient(app)


def _hdr(app, user="alice"):
    tok = app.state.token_store.issue(
        Identity(user=user, roles=[], tenant="default", authenticated=True))
    return {"Authorization": f"Bearer {tok}"}


def test_create_list_get_delete():
    convos = FakeConversationStore()
    app, c = _app(convos)
    h = _hdr(app)
    cid = c.post("/conversations", json={"title": "hi"}, headers=h).json()["id"]
    assert any(x["id"] == cid for x in c.get("/conversations", headers=h).json()["conversations"])
    got = c.get(f"/conversations/{cid}", headers=h).json()
    assert got["id"] == cid and got["messages"] == []
    assert c.delete(f"/conversations/{cid}", headers=h).status_code == 200
    assert c.get(f"/conversations/{cid}", headers=h).status_code == 404


def test_conversations_are_per_user():
    convos = FakeConversationStore()
    app, c = _app(convos)
    cid = c.post("/conversations", json={}, headers=_hdr(app, "alice")).json()["id"]
    bob = _hdr(app, "bob")
    assert all(x["id"] != cid for x in c.get("/conversations", headers=bob).json()["conversations"])
    assert c.get(f"/conversations/{cid}", headers=bob).status_code == 404
    assert c.delete(f"/conversations/{cid}", headers=bob).status_code == 404


def _drain(ws):
    evs = []
    while True:
        e = ws.receive_json()
        evs.append(e)
        if e["type"] == "done":
            return evs


@live_db  # persists the turn to Postgres (CSAI_PG_*)
def test_ws_persists_turn_and_returns_conversation_id():
    convos = FakeConversationStore()
    app, c = _app(convos)
    tok = app.state.token_store.issue(
        Identity(user="alice", roles=[], tenant="default", authenticated=True))
    with c.websocket_connect(f"/chat?token={tok}") as ws:
        ws.send_json({"message": "hello world"})
        evs = _drain(ws)
    conv = [e for e in evs if e["type"] == "conversation"]
    assert conv and conv[0]["id"]
    cid = conv[0]["id"]
    msgs = convos.data[cid]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "hello world"
    assert "Answer to: hello world" in msgs[1]["content"]
    assert msgs[1]["citations"][0]["kind"] == "doc"      # citations persisted
    assert convos.data[cid]["title"] == "hello world"    # auto-titled


def test_ws_resumes_existing_conversation_without_retitling():
    convos = FakeConversationStore()
    app, c = _app(convos)
    cid = convos.create("default", "alice", title="existing")
    tok = app.state.token_store.issue(
        Identity(user="alice", roles=[], tenant="default", authenticated=True))
    with c.websocket_connect(f"/chat?token={tok}") as ws:
        ws.send_json({"message": "follow up", "conversation_id": cid})
        _drain(ws)
    assert [m["role"] for m in convos.data[cid]["messages"]] == ["user", "assistant"]
    assert convos.data[cid]["title"] == "existing"

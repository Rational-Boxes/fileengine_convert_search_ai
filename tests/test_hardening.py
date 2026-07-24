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

"""Tests that guards + audit are wired into the search and chat services."""
import json

import pytest

from convert_search_ai import audit
from convert_search_ai.chat import ChatService
from convert_search_ai.config import Config
from convert_search_ai.guards import GuardError
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.permissions import PermissionGate
from convert_search_ai.search import SearchService


def _id():
    return Identity(user="alice", tenant="default", authenticated=True)


def _read(path):
    return [json.loads(ln[len("audit "):]) for ln in path.read_text().splitlines()
            if ln.startswith("audit ")]


class FakeRepo:
    def __init__(self, rows=None, text=None):
        self.rows = rows or []
        self._text = text

    def query(self, tenant, q, *, fetch, fuzzy):
        return list(self.rows)

    def get_text(self, tenant, uid):
        return self._text


class FakeMF:
    def __init__(self, allowed):
        self.allowed = set(allowed)

    def check_permission(self, uid, perm, tenant=None):
        return uid in self.allowed

    def close(self):
        pass


def test_search_emits_audit(tmp_path):
    audit.configure(str(tmp_path / "a.log"))
    rows = [{"file_uid": "a", "name": "A", "snippet": "s", "score": 1.0}]
    svc = SearchService(Config(), repo=FakeRepo(rows), gate=PermissionGate(300),
                        client_factory=lambda i: FakeMF(["a"]))
    svc.search(_id(), "hello")
    e = _read(tmp_path / "a.log")[-1]
    assert e["action"] == "search" and e["hits"] == 1 and e["candidates"] == 1


def test_get_text_truncates_and_audits(monkeypatch, tmp_path):
    monkeypatch.setenv("CSAI_MAX_TEXT_BYTES", "10")
    audit.configure(str(tmp_path / "a.log"))
    svc = SearchService(Config(), repo=FakeRepo(text="x" * 100), gate=PermissionGate(300),
                        client_factory=lambda i: FakeMF(["f"]))
    text, truncated = svc.get_text(_id(), "f")
    assert truncated and len(text.encode("utf-8")) <= 10
    assert _read(tmp_path / "a.log")[-1] == {**_read(tmp_path / "a.log")[-1],
                                             "action": "document_text", "result": "ok",
                                             "file_uid": "f", "truncated": True}


class RecordingRetriever:
    def __init__(self):
        self.k = None

    def retrieve(self, identity, message, k=8):
        self.k = k
        return []


class EchoChat:
    def stream(self, messages, system=None):
        yield "ok"


def test_chat_caps_k_and_audits(monkeypatch, tmp_path):
    monkeypatch.setenv("CSAI_MAX_CHAT_K", "2")
    audit.configure(str(tmp_path / "a.log"))
    rk = RecordingRetriever()
    list(ChatService(Config(), retriever=rk, chat=EchoChat()).answer(_id(), message="hi", k=50))
    assert rk.k == 2
    assert _read(tmp_path / "a.log")[-1]["action"] == "chat"


def test_chat_rejects_empty_message():
    svc = ChatService(Config(), retriever=RecordingRetriever(), chat=EchoChat())
    with pytest.raises(GuardError):
        list(svc.answer(_id(), message="   "))

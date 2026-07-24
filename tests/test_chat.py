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

"""Unit tests for the RAG ChatService (fake retriever + capturing chat provider)."""
from convert_search_ai.chat import ChatService
from convert_search_ai.config import Config
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.vectorstore import RetrievedChunk


class FakeRetriever:
    def __init__(self, chunks):
        self.chunks = chunks

    def retrieve(self, identity, message, k=8):
        return list(self.chunks)


class CapturingChat:
    def __init__(self):
        self.system = None
        self.messages = None

    def stream(self, messages, system=None):
        self.system = system
        self.messages = messages
        yield "Hello "
        yield "world"


def _id():
    return Identity(user="u", tenant="default", authenticated=True)


def test_answer_streams_tokens_then_unique_citations():
    chunks = [RetrievedChunk("fA", 0, "alpha", 0.1),
              RetrievedChunk("fA", 1, "beta", 0.2),
              RetrievedChunk("fB", 0, "gamma", 0.3)]
    cc = CapturingChat()
    events = list(ChatService(Config(), retriever=FakeRetriever(chunks), chat=cc)
                  .answer(_id(), message="hi", system_prompt="Be brief."))

    assert "".join(e["text"] for e in events if e["type"] == "token") == "Hello world"
    cites = [e for e in events if e["type"] == "citations"][0]["citations"]
    assert [c["file_uid"] for c in cites] == ["fA", "fB"]   # unique, order preserved

    # conversation system prompt seeded + retrieved context embedded
    assert "Be brief." in cc.system and "alpha" in cc.system and "Context" in cc.system
    assert cc.messages[-1] == {"role": "user", "content": "hi"}


def test_answer_with_history_and_no_context():
    cc = CapturingChat()
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}]
    events = list(ChatService(Config(), retriever=FakeRetriever([]), chat=cc)
                  .answer(_id(), message="now", history=history))
    assert [e for e in events if e["type"] == "citations"][0]["citations"] == []
    assert cc.messages == history + [{"role": "user", "content": "now"}]
    assert "no relevant context" in cc.system


# --------------------------------------------------------------------------- #
# Marker-driven report save (the streamed body is diverted to a file)
# --------------------------------------------------------------------------- #

class _Entry:
    def __init__(self, uid, name, is_dir=True):
        self.uid, self.name, self._dir = uid, name, is_dir

    @property
    def is_container(self):
        return self._dir


class _FakeMF:
    def __init__(self):
        self.tree = {"": []}
        self.files = {}
        self.puts = []
        self._n = 0

    def dir(self, uid, tenant=None, **kw):
        return self.tree.get(uid, [])

    def mkdir(self, parent, name, tenant=None, **kw):
        self._n += 1
        uid = f"d{self._n}"
        self.tree.setdefault(parent, []).append(_Entry(uid, name))
        self.tree[uid] = []
        return uid

    def touch(self, parent, name, tenant=None, **kw):
        self._n += 1
        uid = f"f{self._n}"
        self.files[uid] = name
        return uid

    def put(self, uid, data, tenant=None, **kw):
        self.puts.append((uid, data))
        return 1.0

    def close(self):
        pass


class _MarkerChat:
    """Streams a marker-wrapped report (no tool call)."""
    def __init__(self, *chunks):
        self._chunks = chunks

    def stream(self, messages, system=None):
        for c in self._chunks:
            yield c


def test_report_mode_saves_to_the_user_pinned_target(monkeypatch):
    from convert_search_ai import llm_tools
    mf = _FakeMF()
    monkeypatch.setattr(llm_tools, "_default_client", lambda identity, config: mf)
    dest = mf.mkdir("", "Reports")                                  # user pre-chose this folder
    chat = _MarkerChat(
        'Sure — here it is.\n[[SAVE_REPORT title="Q3"]]\n',         # content-only marker
        "# Q3 Report\n\nRevenue is **up**.\n\n- north\n- south\n",
        "[[/SAVE_REPORT]]\nDone!")
    events = list(ChatService(Config(), retriever=FakeRetriever([]), chat=chat).answer(
        _id(), message="write a report",
        report_target={"folder_uid": dest, "filename": "q3", "path": "/Reports"}))
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "Saved the report to /Reports/q3.html" in text          # deterministic confirmation
    # a report_saved event drives the SPA's "Open report" preview link
    saved_ev = next(e for e in events if e["type"] == "report_saved")
    assert saved_ev["name"] == "q3.html" and saved_ev["uid"] == mf.puts[-1][0]
    assert mf.files[mf.puts[-1][0]] == "q3.html"                   # written into the pinned folder
    saved = mf.puts[-1][1].decode()
    assert "<h1>" in saved and "<strong>up</strong>" in saved      # markdown rendered to HTML


def test_no_report_target_means_no_save(monkeypatch):
    # §3a: outside report mode the model can no longer save a report anywhere, even
    # if it emits a marker block.
    from convert_search_ai import llm_tools
    mf = _FakeMF()
    monkeypatch.setattr(llm_tools, "_default_client", lambda identity, config: mf)
    chat = _MarkerChat('[[SAVE_REPORT title="X"]]\nsome body\n[[/SAVE_REPORT]]')
    events = list(ChatService(Config(), retriever=FakeRetriever([]), chat=chat)
                  .answer(_id(), message="hi"))                    # no report_target
    assert not mf.puts                                             # nothing written
    assert not any(e["type"] == "report_saved" for e in events)


def test_marker_cutoff_without_closing_still_saves_with_note(monkeypatch):
    from convert_search_ai import llm_tools
    mf = _FakeMF()
    monkeypatch.setattr(llm_tools, "_default_client", lambda identity, config: mf)
    dest = mf.mkdir("", "Reports")
    chat = _MarkerChat('[[SAVE_REPORT title="Big"]]\n# Partial\n\nGot cut off mid-stream')
    events = list(ChatService(Config(), retriever=FakeRetriever([]), chat=chat).answer(
        _id(), message="report",
        report_target={"folder_uid": dest, "filename": "big", "path": "/Reports"}))
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "Saved the report to /Reports/big.html" in text
    assert "cut off" in text                                       # truncation noted
    assert mf.puts

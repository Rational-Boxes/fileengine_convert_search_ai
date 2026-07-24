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

"""ChatService web-search tool loop (offline, via the ToolEcho + fake providers)."""
from types import SimpleNamespace

from convert_search_ai.chat import ChatService
from convert_search_ai.config import Config
from convert_search_ai.providers.chat import EchoChatProvider, ToolEchoChatProvider
from convert_search_ai.vectorstore import RetrievedChunk


class FakeRetriever:
    def __init__(self, chunks):
        self.chunks = chunks

    def retrieve(self, identity, message, k=8):
        return list(self.chunks)


def _id():
    return SimpleNamespace(user="u", tenant="t")


def _cfg(**over):
    c = Config()
    c.web_search_provider = "fake"
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _chunks(n):
    return [RetrievedChunk(f"f{i}", 0, f"chunk {i}", 0.1) for i in range(n)]


def _run(cfg, chat, chunks, **kw):
    svc = ChatService(cfg, retriever=FakeRetriever(chunks), chat=chat)
    return list(svc.answer(_id(), message="what is new?", **kw))


def test_web_search_off_by_default_is_plain_rag():
    # Default config: web search disabled -> no tools, today's behavior, even with
    # a tool-capable provider.
    events = _run(Config(), ToolEchoChatProvider(), _chunks(2))
    assert not any(e["type"] in ("tool_call", "tool_result") for e in events)
    cites = [e for e in events if e["type"] == "citations"][0]["citations"]
    assert all(c["kind"] == "doc" for c in cites)


def test_tool_loop_runs_and_merges_web_citations():
    cfg = _cfg(web_search_enabled=True, web_search_default=True, web_search_results=3)
    events = _run(cfg, ToolEchoChatProvider(), _chunks(2))
    types = [e["type"] for e in events]
    assert "tool_call" in types and "tool_result" in types
    assert any(e.get("name") == "web_search" for e in events if e["type"] == "tool_call")

    cites = [e for e in events if e["type"] == "citations"][0]["citations"]
    docs = [c for c in cites if c["kind"] == "doc"]
    webs = [c for c in cites if c["kind"] == "web"]
    assert len(docs) == 2 and len(webs) == 3
    # web markers continue contiguously after the document markers
    assert [c["marker"] for c in cites] == [1, 2, 3, 4, 5]
    assert all(c["url"].startswith("https://") for c in webs)


def test_per_message_flag_overrides_default_on():
    cfg = _cfg(web_search_enabled=True, web_search_default=True)
    events = _run(cfg, ToolEchoChatProvider(), _chunks(1), web_search=False)
    assert not any(e["type"] == "tool_call" for e in events)


def test_non_tool_provider_falls_back_to_plain_rag():
    # Provider can't do tools -> web search silently unavailable (no error).
    cfg = _cfg(web_search_enabled=True, web_search_default=True)
    events = _run(cfg, EchoChatProvider(), _chunks(1))
    assert not any(e["type"] == "tool_call" for e in events)
    assert any(e["type"] == "token" for e in events)

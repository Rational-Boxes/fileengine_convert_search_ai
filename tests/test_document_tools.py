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

"""Unit tests for the RAG + direct-interrogation tools: document_search (locate
files by content) and get_document_text (windowed read of a file's extracted
Markdown). Both wrap the permission-gated SearchService."""
from convert_search_ai.config import Config
from convert_search_ai.llm_tools import (
    DocumentSearchTool, GetDocumentTextTool, ToolContext, build_tools,
)


class _Ident:
    user = "u@x"
    tenant = "default"
    roles = ["users"]


def _ctx():
    return ToolContext(identity=_Ident(), config=Config())


class _Hit:
    def __init__(self, file_uid, name, snippet, score=1.0):
        self.file_uid, self.name, self.snippet, self.score = file_uid, name, snippet, score


class _FakeSearch:
    """Stand-in for SearchService: `search` returns Hit-likes; `get_text` returns
    (text, truncated) or raises (a stored Exception instance, or FileNotFoundError
    for an unknown uid)."""
    def __init__(self, hits=None, texts=None):
        self._hits = hits or []
        self._texts = texts or {}
        self.calls = []

    def search(self, identity, query, *, limit=20, fuzzy=True):
        self.calls.append(("search", query, limit))
        return self._hits[:limit]

    def get_text(self, identity, file_uid):
        self.calls.append(("get_text", file_uid))
        v = self._texts.get(file_uid)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise FileNotFoundError(file_uid)
        return v, False


# --- document_search --------------------------------------------------------
def test_document_search_lists_matches_with_uids():
    s = _FakeSearch(hits=[_Hit("f1", "Q3.md", "north revenue up 12%"),
                          _Hit("f2", "notes.md", "south flat")])
    out = DocumentSearchTool(s).run({"query": "revenue"}, _ctx())
    assert "Q3.md" in out.text and "file_uid=f1" in out.text
    assert "notes.md" in out.text and "file_uid=f2" in out.text


def test_document_search_no_matches():
    out = DocumentSearchTool(_FakeSearch(hits=[])).run({"query": "zzz"}, _ctx())
    assert "No indexed documents matched" in out.text


def test_document_search_respects_limit():
    s = _FakeSearch(hits=[_Hit(f"f{i}", f"d{i}", "x") for i in range(20)])
    DocumentSearchTool(s).run({"query": "x", "limit": 3}, _ctx())
    assert s.calls[-1] == ("search", "x", 3)


# --- get_document_text ------------------------------------------------------
def test_get_document_text_windows_the_text():
    body = "".join(str(i % 10) for i in range(100))   # 100 chars
    s = _FakeSearch(texts={"f1": body})
    out = GetDocumentTextTool(s).run({"file_uid": "f1", "offset": 10, "length": 20}, _ctx())
    assert body[10:30] in out.text
    assert "characters 10" in out.text and "of 100" in out.text
    assert "offset=30" in out.text                    # points to the next window


def test_get_document_text_end_of_document():
    s = _FakeSearch(texts={"f1": "short"})
    out = GetDocumentTextTool(s).run({"file_uid": "f1"}, _ctx())
    assert "short" in out.text and "end of document" in out.text


def test_get_document_text_caps_window():
    s = _FakeSearch(texts={"f1": "a" * 100000})
    out = GetDocumentTextTool(s, max_window=1000).run({"file_uid": "f1", "length": 999999}, _ctx())
    assert "characters 0" in out.text and "of 100000" in out.text
    window = out.text.split(":\n\n", 1)[1]            # text after the header
    assert window == "a" * 1000                       # window capped to max_window


def test_get_document_text_permission_denied():
    s = _FakeSearch(texts={"f1": PermissionError("f1")})
    out = GetDocumentTextTool(s).run({"file_uid": "f1"}, _ctx())
    assert "do not have permission" in out.text


def test_get_document_text_not_indexed():
    out = GetDocumentTextTool(_FakeSearch(texts={})).run({"file_uid": "f9"}, _ctx())
    assert "no extracted text" in out.text


def test_get_document_text_requires_file_uid():
    out = GetDocumentTextTool(_FakeSearch()).run({}, _ctx())
    assert "file_uid is required" in out.text


# --- build_tools wiring -----------------------------------------------------
def test_build_tools_adds_doc_tools_only_with_search():
    cfg = Config()
    names_without = {t.name for t in build_tools(cfg, include_web=False, search=None)}
    names_with = {t.name for t in build_tools(cfg, include_web=False, search=_FakeSearch())}
    assert "document_search" not in names_without
    assert "get_document_text" not in names_without
    assert {"document_search", "get_document_text"} <= names_with
    assert "list_folders" in names_with               # folder tool present when doc feature on

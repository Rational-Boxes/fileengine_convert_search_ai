"""Unit tests for the create_document chat tool (writes an HTML report to
FileEngine as the user). Uses an in-memory fake ManagedFiles."""
import pytest

from convert_search_ai.config import Config
from convert_search_ai.llm_tools import (
    CreateDocumentTool, ListFoldersTool, ToolContext, build_tools,
    wrap_html_document, _norm_path, _safe_name,
)


class _Entry:
    def __init__(self, uid, name, is_dir=True):
        self.uid, self.name, self._dir = uid, name, is_dir

    @property
    def is_container(self):
        return self._dir


class FakeMF:
    """Mimics the ManagedFiles bits the tool uses: dir/mkdir/touch/put/close."""
    def __init__(self):
        self.tree = {"": [_Entry("rep", "Reports")]}   # root has a Reports folder
        self.files = {}      # uid -> name
        self.puts = []       # (uid, bytes)
        self.closed = False
        self._n = 0

    def dir(self, uid, tenant=None, **kw):
        return self.tree.get(uid, [])

    def mkdir(self, parent, name, tenant=None, **kw):
        self._n += 1
        uid = f"dir{self._n}"
        self.tree.setdefault(parent, []).append(_Entry(uid, name))
        self.tree[uid] = []
        return uid

    def touch(self, parent, name, tenant=None, **kw):
        self._n += 1
        uid = f"file{self._n}"
        self.files[uid] = name
        return uid

    def put(self, uid, data, tenant=None, **kw):
        self.puts.append((uid, data))
        return 1.0

    def close(self):
        self.closed = True


class _Ident:
    user, tenant, roles = "alice", "default", []


def _tool(mf):
    return CreateDocumentTool(client_factory=lambda identity, config: mf)


def _ctx():
    return ToolContext(identity=_Ident(), config=Config())


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

def test_writes_html_report_to_existing_folder():
    mf = FakeMF()
    out = _tool(mf).run(
        {"path": "/Reports", "filename": "q3", "title": "Q3",
         "html": "<h1>Q3</h1><p>Body</p>"}, _ctx())
    assert "/Reports/q3.html" in out.text
    uid, data = mf.puts[-1]
    assert mf.files[uid] == "q3.html"
    assert data.startswith(b"<!doctype html>")     # wrapped into a full document
    assert b"<h1>Q3</h1>" in data
    assert mf.closed is True                        # client cleaned up


def test_appends_html_extension_only_when_missing():
    mf = FakeMF()
    _tool(mf).run({"path": "/Reports", "filename": "report.html",
                   "html": "<p>x</p>"}, _ctx())
    assert mf.files[mf.puts[-1][0]] == "report.html"   # not report.html.html


# --------------------------------------------------------------------------- #
# Destination confirmation / folder creation
# --------------------------------------------------------------------------- #

def test_missing_folder_asks_instead_of_writing():
    mf = FakeMF()
    out = _tool(mf).run({"path": "/Nope", "filename": "x", "html": "<p>y</p>"}, _ctx())
    assert "does not exist" in out.text and "create_folders" in out.text
    assert mf.puts == []                            # nothing written


def test_create_folders_makes_missing_path():
    mf = FakeMF()
    out = _tool(mf).run(
        {"path": "/New/Sub", "filename": "x", "html": "<p>y</p>",
         "create_folders": True}, _ctx())
    assert "/New/Sub/x.html" in out.text
    assert len(mf.puts) == 1
    # both path segments were created
    names = {e.name for entries in mf.tree.values() for e in entries}
    assert {"New", "Sub"} <= names


def test_root_path_writes_at_top_level():
    mf = FakeMF()
    out = _tool(mf).run({"path": "/", "filename": "top", "html": "<p>y</p>"}, _ctx())
    assert "/top.html" in out.text
    assert mf.puts and mf.files[mf.puts[-1][0]] == "top.html"


# --------------------------------------------------------------------------- #
# Validation + safety
# --------------------------------------------------------------------------- #

def test_requires_filename_and_some_content():
    mf = FakeMF()
    # no filename -> error
    assert "error" in _tool(mf).run({"path": "/", "filename": "", "html": "x"}, _ctx()).text
    # filename but no html AND no inline reply to fall back to -> error
    assert "error" in _tool(mf).run({"path": "/", "filename": "a"}, _ctx()).text
    assert mf.puts == []


# --------------------------------------------------------------------------- #
# Reply fallback — save the report the model wrote inline (no html arg)
# --------------------------------------------------------------------------- #

def test_saves_inline_reply_when_html_omitted():
    mf = FakeMF()
    ctx = _ctx()
    ctx.answer_text = "# Q3 Report\n\nRevenue is **up**.\n\n- north\n- south\n"
    out = _tool(mf).run({"path": "/Reports", "filename": "q3"}, ctx)   # no html arg
    assert "/Reports/q3.html" in out.text
    saved = mf.puts[-1][1].decode()
    assert "<h1>" in saved and "<strong>up</strong>" in saved and "<li>north</li>" in saved


def test_explicit_html_arg_takes_precedence_over_reply():
    mf = FakeMF()
    ctx = _ctx()
    ctx.answer_text = "fallback text that should be ignored"
    _tool(mf).run({"path": "/Reports", "filename": "q3", "html": "<h2>Explicit</h2>"}, ctx)
    saved = mf.puts[-1][1].decode()
    assert "<h2>Explicit</h2>" in saved and "fallback text" not in saved


def test_markdown_to_html_converts_and_passes_through_html():
    from convert_search_ai.llm_tools import markdown_to_html
    assert "<h1>" in markdown_to_html("# Title")
    assert "<table>" in markdown_to_html("| a | b |\n|---|---|\n| 1 | 2 |")
    assert markdown_to_html("<h1>already</h1>") == "<h1>already</h1>"


def test_filename_cannot_traverse_paths():
    # Path separators / traversal are stripped from the file name.
    assert _safe_name("../../etc/passwd") == "etcpasswd"
    assert _safe_name("a/b\\c.html") == "abc.html"
    assert _safe_name("   ") == ""


def test_oversized_report_is_rejected():
    mf = FakeMF()
    tool = CreateDocumentTool(max_bytes=100, client_factory=lambda i, c: mf)
    out = tool.run({"path": "/", "filename": "big", "html": "<p>" + "x" * 500 + "</p>"}, _ctx())
    assert "too large" in out.text
    assert mf.puts == []


def test_write_failure_is_fail_soft():
    mf = FakeMF()

    def boom(*a, **k):
        raise RuntimeError("core read-only")
    mf.put = boom
    out = _tool(mf).run({"path": "/Reports", "filename": "x", "html": "<p>y</p>"}, _ctx())
    assert "error" in out.text.lower()
    assert mf.closed is True            # still cleaned up


# --------------------------------------------------------------------------- #
# Document wrapping + tool registration
# --------------------------------------------------------------------------- #

def test_wrap_passes_through_complete_documents():
    full = "<!DOCTYPE html><html><body>already</body></html>"
    assert wrap_html_document("T", full) == full
    wrapped = wrap_html_document("My Title", "<p>body</p>")
    assert wrapped.startswith("<!doctype html>")
    assert "<title>My Title</title>" in wrapped
    assert "<p>body</p>" in wrapped


def test_build_tools_includes_document_tools_without_web():
    names = {t.name for t in build_tools(Config(), include_web=False)}
    assert {"list_folders", "create_document"} <= names


def test_build_tools_can_disable_document_tools(monkeypatch):
    monkeypatch.setenv("CSAI_CHAT_DOCUMENT_TOOL", "false")
    names = {t.name for t in build_tools(Config(), include_web=False)}
    assert "create_document" not in names and "list_folders" not in names


# --------------------------------------------------------------------------- #
# list_folders — filesystem exploration
# --------------------------------------------------------------------------- #

def _browse_mf():
    mf = FakeMF()
    mf.tree = {
        "": [_Entry("p", "Projects"), _Entry("r", "Reports"),
             _Entry("n", "notes.txt", is_dir=False)],
        "p": [_Entry("a", "Alpha"), _Entry("b", "Beta")],
    }
    return mf


def _lister(mf):
    return ListFoldersTool(client_factory=lambda identity, config: mf)


def test_list_folders_lists_subfolders_and_files():
    out = _lister(_browse_mf()).run({"path": "/"}, _ctx())
    assert "Projects" in out.text and "Reports" in out.text
    assert "notes.txt" in out.text


def test_list_folders_drills_into_subfolder():
    out = _lister(_browse_mf()).run({"path": "/Projects"}, _ctx())
    assert "Alpha" in out.text and "Beta" in out.text


def test_list_folders_missing_path_is_guided_not_fatal():
    out = _lister(_browse_mf()).run({"path": "/Nope"}, _ctx())
    assert "does not exist" in out.text


def test_list_folders_defaults_to_root():
    out = _lister(_browse_mf()).run({}, _ctx())
    assert "Contents of /" in out.text


def test_norm_path_drops_traversal_and_blanks():
    assert _norm_path("//Projects/../Alpha/") == "/Projects/Alpha"
    assert _norm_path("") == "/"
    assert _norm_path("/") == "/"

"""Unit tests for the document-save layer: folder exploration (list_folders),
the SAVE_REPORT stream-marker parser, and the shared save helper that writes a
report into FileEngine as the user. Saving is marker-driven (no save tool)."""
from convert_search_ai.config import Config
from convert_search_ai.llm_tools import (
    ListFoldersTool, ToolContext, build_tools, markdown_to_html, parse_report_markers,
    report_location, save_report_document, wrap_html_document, ReportSaveError,
    _norm_path, _safe_name,
)


class _Entry:
    def __init__(self, uid, name, is_dir=True):
        self.uid, self.name, self._dir = uid, name, is_dir

    @property
    def is_container(self):
        return self._dir


class FakeMF:
    """Mimics the ManagedFiles bits used: dir/mkdir/touch/put/close."""
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


def _ctx():
    return ToolContext(identity=_Ident(), config=Config())


# --------------------------------------------------------------------------- #
# Shared save helper (used by the marker path)
# --------------------------------------------------------------------------- #

def test_save_report_document_writes_and_creates_folders():
    mf = FakeMF()
    uid, loc, n = save_report_document(
        _Ident(), Config(), path="/New/Deep", filename="r", title="R",
        body="# H\n\n**b**", create_folders=True, client_factory=lambda i, c: mf)
    assert loc == report_location("/New/Deep", "r") == "/New/Deep/r.html"
    assert n > 0
    saved = mf.puts[-1][1].decode()
    assert saved.startswith("<!doctype html>") and "<h1>" in saved and "<strong>b</strong>" in saved
    assert mf.closed is True
    names = {e.name for entries in mf.tree.values() for e in entries}
    assert {"New", "Deep"} <= names


def test_save_report_document_appends_html_extension_only_when_missing():
    mf = FakeMF()
    _, loc, _ = save_report_document(_Ident(), Config(), path="/Reports", filename="report.html",
                                     title="", body="<p>x</p>", create_folders=False,
                                     client_factory=lambda i, c: mf)
    assert loc == "/Reports/report.html" and mf.files[mf.puts[-1][0]] == "report.html"


def test_save_report_document_missing_folder_raises():
    mf = FakeMF()
    try:
        save_report_document(_Ident(), Config(), path="/Nope", filename="r", title="R",
                             body="x", create_folders=False, client_factory=lambda i, c: mf)
        assert False, "expected ReportSaveError"
    except ReportSaveError as e:
        assert e.kind == "missing_folder" and "/Nope" in e.missing_path


def test_save_report_document_rejects_empty_and_oversized():
    mf = FakeMF()
    for body in ("", "   "):
        try:
            save_report_document(_Ident(), Config(), path="/Reports", filename="r", title="",
                                 body=body, create_folders=False, client_factory=lambda i, c: mf)
            assert False
        except ReportSaveError as e:
            assert e.kind == "empty"
    try:
        save_report_document(_Ident(), Config(), path="/Reports", filename="r", title="",
                             body="x" * 5000, create_folders=False,
                             client_factory=lambda i, c: mf, max_bytes=100)
        assert False
    except ReportSaveError as e:
        assert e.kind == "too_large"
    assert mf.puts == []


def test_filename_cannot_traverse_paths():
    assert _safe_name("../../etc/passwd") == "etcpasswd"
    assert _safe_name("a/b\\c.html") == "abc.html"
    assert _safe_name("   ") == ""


# --------------------------------------------------------------------------- #
# SAVE_REPORT stream markers
# --------------------------------------------------------------------------- #

def test_parse_report_markers_extracts_target_and_body():
    text = ('preamble\n[[SAVE_REPORT path="/A/B" file="rep" title="My Report"]]\n'
            '# Body\n\ntext\n[[/SAVE_REPORT]]\npostamble')
    reps = parse_report_markers(text)
    assert len(reps) == 1
    r = reps[0]
    assert (r.path, r.filename, r.title, r.complete) == ("/A/B", "rep", "My Report", True)
    assert r.body == "# Body\n\ntext"          # preamble/postamble excluded


def test_parse_report_markers_cutoff_is_incomplete():
    reps = parse_report_markers('[[SAVE_REPORT path="/A" file="x"]]\n# Partial')
    assert len(reps) == 1 and reps[0].complete is False and reps[0].body == "# Partial"


def test_parse_report_markers_ignores_blocks_missing_file_or_body():
    assert parse_report_markers('[[SAVE_REPORT path="/A"]]\nbody\n[[/SAVE_REPORT]]') == []  # no file
    assert parse_report_markers('[[SAVE_REPORT file="x"]]\n[[/SAVE_REPORT]]') == []          # empty body


def test_markdown_to_html_converts_and_passes_through_html():
    assert "<h1>" in markdown_to_html("# Title")
    assert "<table>" in markdown_to_html("| a | b |\n|---|---|\n| 1 | 2 |")
    assert markdown_to_html("<h1>already</h1>") == "<h1>already</h1>"


def test_wrap_passes_through_complete_documents():
    full = "<!DOCTYPE html><html><body>already</body></html>"
    assert wrap_html_document("T", full) == full
    wrapped = wrap_html_document("My Title", "<p>body</p>")
    assert wrapped.startswith("<!doctype html>") and "<title>My Title</title>" in wrapped


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
    assert "Projects" in out.text and "Reports" in out.text and "notes.txt" in out.text


def test_list_folders_drills_into_subfolder():
    out = _lister(_browse_mf()).run({"path": "/Projects"}, _ctx())
    assert "Alpha" in out.text and "Beta" in out.text


def test_list_folders_missing_path_is_guided_not_fatal():
    out = _lister(_browse_mf()).run({"path": "/Nope"}, _ctx())
    assert "does not exist" in out.text


def test_list_folders_defaults_to_root():
    assert "Contents of /" in _lister(_browse_mf()).run({}, _ctx()).text


def test_norm_path_drops_traversal_and_blanks():
    assert _norm_path("//Projects/../Alpha/") == "/Projects/Alpha"
    assert _norm_path("") == "/" and _norm_path("/") == "/"


# --------------------------------------------------------------------------- #
# Tool set — saving is marker-driven, no save tool is offered
# --------------------------------------------------------------------------- #

def test_build_tools_offers_only_folder_exploration_for_documents():
    names = {t.name for t in build_tools(Config(), include_web=False)}
    assert "list_folders" in names
    assert "create_document" not in names      # obsolete tool removed


def test_build_tools_can_disable_document_feature(monkeypatch):
    monkeypatch.setenv("CSAI_CHAT_DOCUMENT_TOOL", "false")
    names = {t.name for t in build_tools(Config(), include_web=False)}
    assert "list_folders" not in names

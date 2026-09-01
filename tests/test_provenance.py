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

"""Chat provenance log (design_documents/CHAT_WITH_AI.md §4.6): PII redaction,
record + HTML building, attaching the hidden-child `chatlog` to a saved report,
and the chat-side threading of the provenance context."""
import json
from types import SimpleNamespace

from convert_search_ai import llm_tools, provenance
from convert_search_ai.app_links import resolve_app_url
from convert_search_ai.chat import ChatService
from convert_search_ai.config import Config
from convert_search_ai.llm_tools import save_report_document
from convert_search_ai.providers.chat import EchoChatProvider


class _Entry:
    def __init__(self, uid, name, is_dir=True):
        self.uid, self.name, self._dir = uid, name, is_dir

    @property
    def is_container(self):
        return self._dir


class _Info:
    def __init__(self, uid, name, version):
        self.uid, self.name, self.version = uid, name, version


class FakeMF:
    """dir/mkdir/touch/put/stat/close — enough for save_report_document + provenance."""

    def __init__(self):
        self.tree = {"": [_Entry("rep", "Reports")]}   # root has a Reports folder
        self.files = {}       # uid -> (name, version)
        self.children = {}     # parent_uid -> {name: uid}  (files + hidden children)
        self.puts = {}         # uid -> bytes
        self.closed = False
        self._n = 0

    def dir(self, uid, tenant=None, **kw):
        base = list(self.tree.get(uid, []))
        base += [_Entry(u, n, is_dir=False) for n, u in self.children.get(uid, {}).items()]
        return base

    def mkdir(self, parent, name, tenant=None, **kw):
        self._n += 1
        uid = f"dir{self._n}"
        self.tree.setdefault(parent, []).append(_Entry(uid, name))
        self.tree[uid] = []
        return uid

    def touch(self, parent, name, tenant=None, **kw):
        self._n += 1
        uid = f"file{self._n}"
        self.files[uid] = (name, "20260102_030405")
        self.children.setdefault(parent, {})[name] = uid
        return uid

    def put_stream(self, uid, chunks, tenant=None, **kw):
        # The real client re-splits chunks so no gRPC message is
        # oversized; a stub only has to reassemble them.
        joined = b"".join(
            c.encode() if isinstance(c, str) else bytes(c) for c in chunks)
        return self.put(uid, joined, **kw)

    def put(self, uid, data, tenant=None, **kw):
        self.puts[uid] = data
        return 1.0

    def stat(self, uid, tenant=None, **kw):
        name, version = self.files.get(uid, (uid, "v1"))
        return _Info(uid, name, version)

    def close(self):
        self.closed = True


class _Ident:
    user, tenant, roles = "alice@example.com", "acme", []


def _id():
    return SimpleNamespace(user="u", tenant="t")


# ---------------------------------------------------------------- PII redaction
def test_redact_pii_scrubs_pii_but_keeps_web_and_non_pii_numbers():
    t = provenance.redact_pii(
        "email me a.b@x.com, call (555) 123-4567, ssn 123-45-6789, card 4111 1111 1111 1111 — "
        "see https://example.com/stats about 2024 revenue of 1200000")
    assert "[redacted-email]" in t and "a.b@x.com" not in t
    assert "[redacted-phone]" in t
    assert "[redacted-ssn]" in t and "123-45-6789" not in t
    assert "[redacted-card]" in t and "4111 1111 1111 1111" not in t
    # public web content + ordinary numbers are preserved
    assert "https://example.com/stats" in t
    assert "1200000" in t and "2024" in t


def test_redact_pii_handles_empty():
    assert provenance.redact_pii("") == ""
    assert provenance.redact_pii(None) == ""


# ------------------------------------------------------------------ the record
def test_build_record_assembles_redacted_transcript():
    rec = provenance.build_record({
        "user": "alice@example.com", "tenant": "acme", "model": "m", "provider": "p",
        "conversation_id": "c1",
        "history": [{"role": "user", "content": "reach me at bob@y.com"},
                    {"role": "assistant", "content": "ok"}],
        "message": "make a report",
        "answer_text": "[[SAVE_REPORT ...]]the report[[/SAVE_REPORT]]",
        "citations": [{"marker": 1, "kind": "doc", "file_uid": "f1"}],
    }, report_uid="r1", report_version="v9")
    assert rec["schema"] == provenance.SCHEMA and rec["pii_redacted"] is True
    assert rec["chatted_by"] == "alice@example.com" and rec["tenant"] == "acme"
    assert rec["report"] == {"uid": "r1", "version": "v9", "title": "", "location": ""}
    assert rec["conversation_id"] == "c1"
    # transcript = 2 history + this turn's user + assistant answer, PII-redacted
    roles = [m["role"] for m in rec["transcript"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert "[redacted-email]" in rec["transcript"][0]["content"]  # bob@y.com scrubbed
    assert rec["citations"] == [{"marker": 1, "kind": "doc", "file_uid": "f1"}]


# -------------------------------------------------------------- HTML rendering
def test_render_html_embeds_roundtrippable_json_and_transcript():
    rec = provenance.build_record(
        {"user": "u", "tenant": "t", "message": "q", "answer_text": "a", "citations": []},
        report_uid="r1", report_version="v1")
    h = provenance.render_html(rec).decode("utf-8")
    assert "Chat provenance log" in h and 'type="application/json"' in h
    start = h.index('type="application/json">') + len('type="application/json">')
    payload = h[start:h.index("</script>", start)]
    assert json.loads(payload)["schema"] == provenance.SCHEMA


def test_render_html_renders_markdown_and_passes_through_html():
    rec = provenance.build_record(
        {"user": "u", "tenant": "t", "message": "**bold** point",
         "answer_text": "<p>raw <em>html</em></p>", "citations": []},
        report_uid="r1", report_version="v1")
    h = provenance.render_html(rec).decode("utf-8")
    # Markdown in a turn is formatted…
    assert "<strong>bold</strong>" in h
    # …and raw HTML the model produced renders rather than showing as escaped text.
    assert "<em>html</em>" in h


# --------------------------------------------------------------- attach to file
def test_attach_provenance_writes_chatlog_child():
    mf = FakeMF()
    mf.files["r1"] = ("report.html", "20260102_030405")
    name = provenance.attach_provenance(mf, "r1", "acme", {
        "user": "u", "tenant": "acme", "message": "q", "answer_text": "a", "citations": []})
    assert name and name.endswith("-chatlog.html")
    child_uid = mf.children["r1"][name]          # a hidden child under the report
    assert b"Chat provenance" in mf.puts[child_uid]


def test_attach_provenance_linkifies_file_refs_in_transcript_and_sources():
    uid = "5a23e207-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
    mf = FakeMF()
    mf.files["r1"] = ("report.html", "20260102_030405")
    mf.files[uid] = ("Budget.xlsx", "v1")
    name = provenance.attach_provenance(mf, "r1", "acme", {
        "user": "u", "tenant": "acme", "message": "q",
        "answer_text": f"the number is in (file {uid}) per the sheet",
        "citations": [{"marker": 1, "kind": "doc", "file_uid": uid}],
    }, base_url="https://app.example.com")
    out = mf.puts[mf.children["r1"][name]].decode("utf-8")
    # The embedded JSON audit record keeps raw data verbatim; the VISIBLE transcript
    # is what gets linkified — so scope the "no raw ref" check to the visible HTML.
    visible = out.split('<script id="provenance-record"')[0]
    # transcript ref → absolute, named deep-link; raw "(file <uid>)" is gone
    assert f'href="https://app.example.com/files?file={uid}&amp;tenant=acme"' in visible
    assert "📄 Budget.xlsx" in visible
    assert f"(file {uid})" not in visible
    # the sources list resolves the cited document to a name too (no raw uid)
    assert "document " + uid not in visible


def test_attach_provenance_is_best_effort_on_error():
    class Boom(FakeMF):
        def stat(self, *a, **k):
            raise RuntimeError("core unavailable")

    assert provenance.attach_provenance(Boom(), "x", "t", {}) is None  # logged, not raised


# ------------------------------------- integration: save_report_document + prov
def test_save_report_document_attaches_provenance_child():
    mf = FakeMF()
    prov = {"user": "alice@example.com", "tenant": "acme", "model": "m", "provider": "p",
            "history": [], "message": "make it", "answer_text": "here it is", "citations": []}
    uid, _loc, _n = save_report_document(
        _Ident(), Config(), path="/Reports", filename="r", title="R",
        body="# hi", create_folders=True, client_factory=lambda i, c: mf, provenance=prov)
    child_names = list(mf.children.get(uid, {}).keys())
    assert any(cn.endswith("-chatlog.html") for cn in child_names)


def test_save_report_document_has_no_provenance_by_default():
    mf = FakeMF()
    uid, _loc, _n = save_report_document(
        _Ident(), Config(), path="/Reports", filename="r", title="R",
        body="# hi", create_folders=True, client_factory=lambda i, c: mf)
    assert not any(cn.endswith("-chatlog.html") for cn in mf.children.get(uid, {}))


# ------------------------------------- integration: the ACTIVE app URL in links
REF = "5a23e207-1c2d-4e5f-8a9b-0c1d2e3f4a5b"


def _save_with_app_url(mf, app_url, monkeypatch, config=None, provenance=None):
    """Save a report whose body cites a file, with the docx step stubbed out so the
    linkified HTML is what lands in storage and can be asserted on."""
    monkeypatch.setattr(llm_tools, "_html_to_docx", lambda html, **kw: html.encode())
    mf.files[REF] = ("Q3 budget.xlsx", "v1")
    return save_report_document(
        _Ident(), config or Config(), path="/Reports", filename="r", title="R",
        body=f"<p>See (file {REF}).</p>", create_folders=True,
        client_factory=lambda i, c: mf, app_url=app_url, provenance=provenance)


def test_report_refs_are_complete_urls_from_the_active_app_url(monkeypatch):
    # A report leaves the browser (docx / PDF / mail), so a relative "/files?…" is a
    # dead link; the app URL the chat ran on makes every reference complete.
    mf = FakeMF()
    uid, _loc, _n = _save_with_app_url(mf, "https://files.example.com/app", monkeypatch)
    assert f'href="https://files.example.com/app/files?file={REF}&amp;tenant=acme"' \
        in mf.puts[uid].decode()


def test_configured_public_app_url_is_the_fallback_for_non_chat_callers(monkeypatch):
    # The chat path resolves the app URL per request (app_links.resolve_app_url, which
    # already folds the operator override in); a caller with no request context still
    # gets complete links from the configured value, per tenant.
    mf = FakeMF()
    cfg = Config()
    cfg.public_app_url = "https://{tenant}.example.com"
    uid, _loc, _n = _save_with_app_url(mf, "", monkeypatch, config=cfg)
    assert 'href="https://acme.example.com/files?' in mf.puts[uid].decode()


def test_two_tenants_on_different_domains_each_get_their_own_link_host(monkeypatch):
    # End-to-end for a multi-domain deployment: resolve the app URL from the door the
    # request arrived on, then save. Each tenant's report links to the FQDN that
    # tenant's chat was served on — a subdomain for one, a vanity domain for the other.
    monkeypatch.setattr(llm_tools, "_html_to_docx", lambda html, **kw: html.encode())
    docs = {}
    for tenant, host in (("acme", "acme.example.com"), ("globex", "files.globex.io")):
        mf = FakeMF()
        mf.files[REF] = ("Q3 budget.xlsx", "v1")
        app_url = resolve_app_url(Config(), {"host": host, "x-forwarded-proto": "https"},
                                  tenant=tenant)
        uid, _loc, _n = save_report_document(
            SimpleNamespace(user="u", tenant=tenant), Config(), path="/Reports",
            filename="r", title="R", body=f"<p>See (file {REF}).</p>", create_folders=True,
            client_factory=lambda i, c, mf=mf: mf, app_url=app_url)
        docs[tenant] = mf.puts[uid].decode()
    assert f'href="https://acme.example.com/files?file={REF}&amp;tenant=acme"' in docs["acme"]
    assert f'href="https://files.globex.io/files?file={REF}&amp;tenant=globex"' in docs["globex"]


def test_provenance_log_links_use_the_same_active_app_url(monkeypatch):
    # The chat log is stored alongside the report and read the same way, so its
    # references must be complete too.
    mf = FakeMF()
    prov = {"user": "alice@example.com", "tenant": "acme", "model": "m", "provider": "p",
            "history": [], "message": f"summarise (file {REF})", "answer_text": "done",
            "citations": [{"marker": 1, "kind": "doc", "file_uid": REF}]}
    uid, _loc, _n = _save_with_app_url(mf, "https://files.example.com", monkeypatch,
                                       provenance=prov)
    child = next(u for n, u in mf.children[uid].items() if n.endswith("-chatlog.html"))
    assert 'href="https://files.example.com/files?' in mf.puts[child].decode()


# --------------------------------------------- chat-side context + threading
def test_prov_builds_context_from_chat_state():
    svc = ChatService(Config(), retriever=SimpleNamespace(retrieve=lambda *a, **k: []),
                      chat=EchoChatProvider())
    prov = svc._prov(_id(), "seed prompt", "conv7", [{"role": "user", "content": "x"}],
                     "hello", [{"marker": 1, "kind": "doc", "file_uid": "f"}])
    assert prov["user"] == "u" and prov["tenant"] == "t"
    assert prov["conversation_id"] == "conv7" and prov["system_prompt"] == "seed prompt"
    assert prov["message"] == "hello" and prov["history"] == [{"role": "user", "content": "x"}]
    assert prov["citations"] == [{"marker": 1, "kind": "doc", "file_uid": "f"}]


def test_save_marked_reports_threads_provenance(monkeypatch):
    captured = {}

    def fake_save(identity, config, **kw):
        captured.update(kw)
        return ("uid1", "/Reports/r.html", 5)

    monkeypatch.setattr(llm_tools, "save_report_document", fake_save)
    svc = ChatService(Config(), retriever=SimpleNamespace(retrieve=lambda *a, **k: []),
                      chat=EchoChatProvider())
    answer = '[[SAVE_REPORT path="/Reports" file="r" title="R"]]body[[/SAVE_REPORT]]'
    prov = svc._prov(_id(), "", "c1", [], "make it", [])
    list(svc._save_marked_reports(_id(), answer, [], prov))
    assert captured.get("provenance") is not None
    assert captured["provenance"]["user"] == "u"
    assert captured["provenance"]["answer_text"] == answer  # the full assistant answer


def test_answer_threads_the_active_app_url_to_the_saved_report(monkeypatch):
    # End of the chain: api.py resolves the app URL per turn and it must reach the
    # save, or the document's references fall back to relative paths again.
    captured = {}

    def fake_save(identity, config, **kw):
        captured.update(kw)
        return ("uid1", "/Reports/r.html", 5)

    monkeypatch.setattr(llm_tools, "save_report_document", fake_save)
    answer = '[[SAVE_REPORT path="/Reports" file="r" title="R"]]body[[/SAVE_REPORT]]'
    streamer = SimpleNamespace(stream=lambda messages, system=None: iter([answer]))
    svc = ChatService(Config(), retriever=SimpleNamespace(retrieve=lambda *a, **k: []),
                      chat=streamer)
    list(svc.answer(_id(), message="write it",
                    report_target={"folder_uid": "f1", "filename": "r", "path": "/Reports"},
                    app_url="https://files.example.com/app"))
    assert captured.get("app_url") == "https://files.example.com/app"

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

"""LLM tool layer for chat-with-documents (WEB_SEARCH_TOOL_PLAN §6).

A ``Tool`` is a name + JSON-schema + ``run()``. P1 ships the ``web_search`` tool and
its pluggable backend; the provider tool-calling loop that actually *invokes* tools
lands in P2. ``build_tools(config)`` returns the enabled tools — empty unless
``CSAI_WEB_SEARCH_ENABLED`` is set (web search is OFF by default)."""
from __future__ import annotations

import html as _htmllib
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, NamedTuple, Optional
from urllib.parse import urlparse

from . import audit, guards
from .config import Config

log = logging.getLogger("convert_search_ai.llm_tools")


@dataclass
class ToolContext:
    """Per-answer context handed to a tool: who is asking, plus a ``sources``
    accumulator the chat loop reads back to build citations, and ``answer_text``
    — the assistant's reply streamed so far this turn, from which a SAVE_REPORT
    marker block is extracted and saved after the stream completes."""
    identity: object
    config: Config
    sources: List[dict] = field(default_factory=list)  # {kind:"web", url, title}
    answer_text: str = ""
    saved: List[str] = field(default_factory=list)  # locations already written this turn (dedupe)
    # Per-call consent gate for MCP tools (MCP_INTEGRATIONS §6): a callable
    # ``(ConsentRequest) -> bool``. None ⇒ no channel ⇒ MCP tools deny (fail-closed).
    consent: Optional[object] = None


@dataclass
class ToolOutput:
    """A tool's result: ``text`` is fed back to the model as the tool result;
    ``sources`` are the structured citations this call contributed."""
    text: str
    sources: List[dict] = field(default_factory=list)


class Tool(ABC):
    name: str = "tool"
    description: str = ""
    # JSON schema for the arguments object (provider-agnostic; the P2 loop wraps it
    # into each provider's tool envelope).
    schema: dict = {"type": "object", "properties": {}}

    @abstractmethod
    def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        ...


class WebSearchTool(Tool):
    """Search the public internet via the configured ``WebSearchProvider``."""

    name = "web_search"
    description = (
        "Search the public internet for current, external, or general-knowledge "
        "information that is NOT in the user's documents. Returns titled result "
        "snippets with their source URLs. Use only when the provided document "
        "context is insufficient to answer.")
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The web search query."},
        },
        "required": ["query"],
    }

    def __init__(self, provider, *, max_results: int = 5, max_chars: int = 4000,
                 max_query_chars: int = 1000):
        self.provider = provider
        self.max_results = max_results
        self.max_chars = max_chars
        self.max_query_chars = max_query_chars

    def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        args = args or {}
        try:
            query = guards.check_query(str(args.get("query", "")), self.max_query_chars)
        except guards.GuardError as e:
            return ToolOutput(text=f"(web_search error: {e})")
        k = guards.cap_limit(int(args.get("max_results", self.max_results)), self.max_results)
        results = self.provider.search(query, k=k) or []

        # Audit the SHAPE only — never the query text (audit.py contract). The
        # `web` flag records that a query was sent to a third-party engine.
        audit.record(action="web_search", user=getattr(ctx.identity, "user", ""),
                     tenant=getattr(ctx.identity, "tenant", ""), result="ok",
                     provider=getattr(self.provider, "provider_id", "?"),
                     results=len(results), web=True)

        if not results:
            return ToolOutput(text="No web results found.")

        blocks, added, total = [], [], 0
        for r in results:
            block = self._format(r)
            if blocks and total + len(block) > self.max_chars:
                break
            blocks.append(block)
            total += len(block)
            src = {"kind": "web", "url": r.url, "title": r.title, "snippet": r.snippet}
            added.append(src)
            ctx.sources.append(src)
        return ToolOutput(text="\n\n".join(blocks), sources=added)

    @staticmethod
    def _format(r) -> str:
        domain = urlparse(r.url).netloc or r.url
        head = r.title or domain
        return f"{head} ({domain})\n{r.snippet}\nSource: {r.url}"


def _default_page_fetch(url: str, *, max_bytes: int, timeout: float):
    from .webfetch import fetch_text
    return fetch_text(url, max_bytes=max_bytes, timeout=timeout)


class FetchPageTool(Tool):
    """Fetch and read the full text of a single public web page (SSRF-guarded)."""

    name = "fetch_page"
    description = (
        "Fetch and read the full text of a single public web page by its https URL "
        "(e.g. a URL returned by web_search) when the search snippet is not enough. "
        "Only public https pages can be read.")
    schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The https URL to fetch."},
        },
        "required": ["url"],
    }

    def __init__(self, *, fetcher=None, max_bytes: int = 2_000_000,
                 timeout_ms: int = 5000, max_chars: int = 4000):
        self._fetch = fetcher or _default_page_fetch
        self.max_bytes = max_bytes
        self.timeout = max(1.0, timeout_ms / 1000.0)
        self.max_chars = max_chars

    def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        url = str((args or {}).get("url", "")).strip()
        if not url:
            return ToolOutput(text="(fetch_page error: url is required)")
        result = self._fetch(url, max_bytes=self.max_bytes, timeout=self.timeout)
        audit.record(action="fetch_page", user=getattr(ctx.identity, "user", ""),
                     tenant=getattr(ctx.identity, "tenant", ""),
                     result="ok" if result else "error", web=True)
        if not result:
            return ToolOutput(
                text=f"Could not read {url} (blocked, non-text, or unavailable).")
        title, text = result
        text = text[:self.max_chars]
        src = {"kind": "web", "url": url, "title": title, "snippet": text}
        ctx.sources.append(src)
        return ToolOutput(text=text, sources=[src])


# --------------------------------------------------------------------------- #
# Report saving — write a chat-generated report into FileEngine (marker-driven)
# --------------------------------------------------------------------------- #

# Characters disallowed in a single file/folder name (path separators, control,
# and the Windows-reserved set) so a name can't escape its folder.
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_REPORT_CSS = """
  :root { color-scheme: light; }
  body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         line-height: 1.5; color: #1a1a1a; max-width: 50rem; margin: 2.5rem auto;
         padding: 0 1.5rem; }
  h1, h2, h3, h4 { line-height: 1.25; margin: 1.4em 0 0.5em; color: #11233b; }
  h1 { font-size: 1.9rem; border-bottom: 2px solid #e2e6ea; padding-bottom: .3em; }
  table { border-collapse: collapse; width: 100%; margin: 1em 0; }
  th, td { border: 1px solid #cdd3da; padding: .5em .7em; text-align: left; }
  th { background: #f3f5f7; }
  code, pre { font-family: ui-monospace, Menlo, Consolas, monospace; }
  pre { background: #f6f8fa; padding: 1em; overflow-x: auto; border-radius: 6px; }
  blockquote { border-left: 4px solid #d0d7de; margin: 1em 0; padding: .2em 1em; color: #444; }
  a { color: #0b5cad; }
  @media print { body { margin: 0; max-width: none; } }
""".strip()


def _safe_name(name: str) -> str:
    """A single, safe file/folder name (no path traversal). Empty if unusable."""
    name = _BAD_NAME.sub("", (name or "").strip()).strip(". ")
    return name[:200]


_HTMLISH = re.compile(
    r"\s*<(?:!doctype|html|h[1-6]|p|div|table|ul|ol|section|article|body|pre|blockquote)\b",
    re.IGNORECASE)


def _looks_like_html(s: str) -> bool:
    return bool(_HTMLISH.match(s or ""))


# Inline file references the model emits as "(file <uid>)" (the convention set in
# chat.py's _INSTRUCTIONS_FILE_REFS). Matched conservatively — hex + dashes, ≥8 chars
# (file uids are UUIDs) — so ordinary parenthetical prose is never touched. This is
# the report-side twin of the chat UI's utils/fileRefs.ts.
_FILE_REF_RE = re.compile(r"\(file\s+([0-9a-fA-F-]{8,})\)")


def _linkify_file_refs(html: str, mf, tenant: str, base_url: str = "", cache: dict = None) -> str:
    """Rewrite '(file <uid>)' references in report/provenance HTML into file
    deep-links that show each file's name, resolved via the core AS THE USER (``mf``
    is the caller's already-open, identity-bound client).

    ``base_url`` (the SPA's public origin) makes the link ABSOLUTE
    (``https://app/files?file=…&tenant=…``) so it survives PDF rendering and copying
    the report to an external site; empty ⇒ a relative ``/files?…`` (dev). It may
    carry a ``{tenant}`` placeholder — substituted with this report's tenant — so a
    multi-tenant deployment that gives each tenant its own host
    (``https://{tenant}.example.com``) links to the RIGHT tenant's URL; a plain origin
    instead carries the tenant only in the ``?tenant=`` query. ``cache`` lets a caller
    share one uid→name map across several calls (the provenance log linkifies many
    turns).

    Best-effort: a uid that can't be resolved (deleted / no access) degrades to a
    plain '📄 file' label rather than a broken link, and any failure returns the HTML
    unchanged — linkifying must never block saving the report."""
    if not html or "(file " not in html:
        return html
    # Resolve the per-tenant host (no-op for a plain origin or the relative fallback).
    base = base_url.replace("{tenant}", tenant) if base_url else ""
    resolved: dict = cache if cache is not None else {}

    def name_of(uid):
        if uid not in resolved:
            try:
                info = mf.stat(uid, tenant=tenant)
                resolved[uid] = getattr(info, "name", "") or ""
            except Exception:
                resolved[uid] = ""
        return resolved[uid]

    def repl(m):
        uid = m.group(1)
        name = name_of(uid)
        label = _htmllib.escape(f"📄 {name}" if name else "📄 file")
        if not name:
            # No reliable target (unresolved) — show a label, not a dead link.
            return f"({label})"
        href = _htmllib.escape(f"{base}/files?file={uid}&tenant={tenant}", quote=True)
        return f'(<a href="{href}">{label}</a>)'

    try:
        return _FILE_REF_RE.sub(repl, html)
    except Exception:
        return html


def markdown_to_html(text: str) -> str:
    """Render report text to HTML. The model usually writes the inline report as
    Markdown; convert it so the saved document keeps its formatting. Already-HTML
    text passes through. Falls back to escaped <pre> if the markdown lib is absent."""
    text = text or ""
    if _looks_like_html(text):
        return text
    try:
        import markdown as _md
        return _md.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    except Exception:
        return "<pre>" + _htmllib.escape(text) + "</pre>"


def wrap_html_document(title: str, body: str) -> str:
    """Wrap a model-supplied HTML *body* in a styled, printable full document. If
    the model already returned a complete document, it is used unchanged."""
    body = body or ""
    low = body.lstrip()[:200].lower()
    if low.startswith("<!doctype") or low.startswith("<html"):
        return body
    safe_title = _htmllib.escape(title or "Report")
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{safe_title}</title>\n<style>\n{_REPORT_CSS}\n</style>\n</head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def _resolve_folder(mf, path: str, tenant: str, *, create: bool):
    """Resolve a '/'-separated folder ``path`` to a container UID, walking from the
    root. Raises ``FileNotFoundError`` (carrying the missing path) when a segment
    is absent and ``create`` is false; otherwise mkdir's the missing segments."""
    from ._client import ManagedFiles  # noqa: F401 (ensures fileengine importable)
    try:
        from fileengine import ROOT_UID
    except Exception:
        ROOT_UID = ""
    segments = [s for s in (path or "").replace("\\", "/").split("/") if s.strip()]
    uid = ROOT_UID
    walked: List[str] = []
    for seg in segments:
        seg_clean = _safe_name(seg)
        if not seg_clean:
            continue
        entries = mf.dir(uid, tenant=tenant) or []
        match = next((e for e in entries if e.is_container and e.name == seg_clean), None)
        if match is not None:
            uid = match.uid
        elif create:
            uid = mf.mkdir(uid, seg_clean, tenant=tenant)
        else:
            raise FileNotFoundError("/" + "/".join(walked + [seg_clean]))
        walked.append(seg_clean)
    return uid


def _default_client(identity, config):
    from .core_client import client_for
    return client_for(identity, config)


def _close(mf) -> None:
    close = getattr(mf, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _norm_path(path: str) -> str:
    """A clean '/'-rooted display path from a user/model-supplied path string."""
    segs = [_safe_name(s) for s in (path or "").replace("\\", "/").split("/")]
    segs = [s for s in segs if s]
    return "/" + "/".join(segs)


def report_location(path: str, filename: str) -> str:
    """The canonical '/'-rooted location a report saves to (used for dedupe + UX)."""
    safe = _safe_name(filename or "")
    name = safe if safe.lower().endswith((".html", ".htm")) else safe + ".html"
    segs = [s for s in (path or "").replace("\\", "/").split("/") if s.strip()]
    return "/".join(["", *segs, name])


class ReportSaveError(Exception):
    """A user-actionable failure saving a report. ``kind`` is one of
    ``empty`` | ``too_large`` | ``missing_folder`` | ``write``."""
    def __init__(self, kind: str, message: str, missing_path: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.missing_path = missing_path


def _is_permission_error(e) -> bool:
    s = str(e).lower()
    return any(w in s for w in ("permission", "denied", "not allowed", "forbidden"))


def save_report_document(identity, config, *, path: str, filename: str, title: str,
                         body: str, create_folders: bool, client_factory=None,
                         max_bytes: int = 5_000_000, provenance: dict = None,
                         folder_uid: str = None):
    """Persist a report (Markdown or HTML ``body``) as an HTML file in the user's
    storage, written *as the user*. Returns ``(uid, location, byte_len)``; raises
    :class:`ReportSaveError`. Used by the marker-driven save path (chat.py).

    Destination: when ``folder_uid`` is given (the user pinned it in the UI — see
    GENERATE_REPORT_TO_TARGET), the report is written straight into that folder UID
    (no name-walk, no mkdir); otherwise ``path`` is resolved by name from the root.
    If a file of the same name already exists in the target folder, a **new version**
    of that exact file is written; otherwise the file is created.

    When ``provenance`` (the chat context) is given, a chat-provenance log is
    attached as a hidden child of the report (best-effort — see provenance.py)."""
    client_factory = client_factory or _default_client
    safe = _safe_name(filename or "")
    if not safe:
        raise ReportSaveError("empty", "a filename is required")
    body = (body or "").strip()
    if not body:
        raise ReportSaveError("empty", "no report content to save")
    html = body if _looks_like_html(body) else markdown_to_html(body)
    name = safe if safe.lower().endswith((".html", ".htm")) else safe + ".html"
    tenant = getattr(identity, "tenant", "") or getattr(config, "tenant", "")
    base_url = getattr(config, "public_app_url", "") or ""
    mf = client_factory(identity, config)
    ref_cache: dict = {}
    try:
        # Rewrite the model's "(file <uid>)" references into named, absolute file
        # deep-links (resolved as the user) before wrapping + size-checking the
        # document — absolute so they survive PDF export / external hosting.
        html = _linkify_file_refs(html, mf, tenant, base_url, ref_cache)
        document = wrap_html_document((title or safe).strip(), html).encode("utf-8")
        if len(document) > max_bytes:
            raise ReportSaveError("too_large", "the report is too large to save")
        if folder_uid is not None:
            parent = folder_uid          # UID-anchored: the user chose this exact folder
        else:
            try:
                parent = _resolve_folder(mf, path or "/", tenant, create=create_folders)
            except FileNotFoundError as miss:
                raise ReportSaveError("missing_folder", f"the folder '{miss}' does not exist",
                                      missing_path=str(miss))
        # Overwrite as a new version when a file with this name already exists in the
        # target folder; else create it. Either way the report lands at the exact
        # file the user picked (versioning is a core feature — history is preserved).
        existing = None
        try:
            for e in (mf.dir(parent, tenant=tenant) or []):
                if not getattr(e, "is_container", False) and e.name == name:
                    existing = e.uid
                    break
        except Exception:
            existing = None
        uid = existing or mf.touch(parent, name, tenant=tenant)
        mf.put(uid, document, tenant=tenant)
        if provenance is not None:
            from . import provenance as _prov
            _prov.attach_provenance(mf, uid, tenant, {
                **provenance,
                "report_title": (title or safe).strip(),
                "report_location": report_location(path, filename),
            }, base_url=base_url, ref_cache=ref_cache)
    except ReportSaveError:
        raise
    except Exception as e:  # WriteUnavailable / permission / … — surface as save error
        kind = "denied" if _is_permission_error(e) else "write"
        raise ReportSaveError(kind, f"could not save the report: {e}")
    finally:
        _close(mf)
    return uid, report_location(path, filename), len(document)


# Stream markers the model wraps a report in so the app can divert a copy of the
# streamed body into a file — the destination travels in the START marker, set
# BEFORE the report is generated. Closing marker OR stream cutoff triggers the save.
_REPORT_BLOCK = re.compile(
    r"\[\[\s*SAVE_REPORT\b([^\]]*?)\]\](.*?)(\[\[\s*/\s*SAVE_REPORT\s*\]\]|\Z)",
    re.IGNORECASE | re.DOTALL)
_ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


class MarkedReport(NamedTuple):
    path: str
    filename: str
    title: str
    body: str
    complete: bool   # False when the closing marker was missing (truncated/cut off)


def parse_report_markers(text: str) -> List["MarkedReport"]:
    """Every ``[[SAVE_REPORT path=… file=… title=…]] … [[/SAVE_REPORT]]`` block in
    ``text``. An unclosed block (cutoff) is captured to the end with complete=False."""
    out: List[MarkedReport] = []
    for m in _REPORT_BLOCK.finditer(text or ""):
        attrs = dict(_ATTR.findall(m.group(1) or ""))
        body = (m.group(2) or "").strip()
        filename = attrs.get("file") or attrs.get("filename") or ""
        # Only the body is required. In the user‑pinned report flow (GENERATE_REPORT_
        # TO_TARGET) the marker is content‑only — no path/file — and the destination
        # comes from the caller, so an absent filename is expected there.
        if not body:
            continue
        out.append(MarkedReport(
            path=attrs.get("path") or "/",
            filename=filename,
            title=attrs.get("title") or "",
            body=body,
            complete=bool(m.group(3).strip())))
    return out


def strip_report_markers(text: str) -> str:
    """Remove just the marker delimiters from displayed text (keep the body)."""
    text = re.sub(r"\[\[\s*SAVE_REPORT\b[^\]]*?\]\]", "", text or "", flags=re.IGNORECASE)
    return re.sub(r"\[\[\s*/\s*SAVE_REPORT\s*\]\]", "", text, flags=re.IGNORECASE)


class ListFoldersTool(Tool):
    """Browse the user's file storage (as the user) so the model can find and
    suggest an appropriate place to save a document — and see whether a folder
    already exists before offering to create one."""

    name = "list_folders"
    description = (
        "Browse the user's file storage to find an appropriate place to save a "
        "document. Lists the sub-folders (and a sample of files) directly under a "
        "folder path. Use '/' for the top level, then drill into promising folders. "
        "Call this BEFORE suggesting where to save a report so your suggestion "
        "matches the user's actual folder layout.")
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description":
                     "Folder path to list, e.g. '/' (top level), '/Projects', "
                     "'Reports/2026'. Defaults to '/'."},
        },
    }

    def __init__(self, *, max_entries: int = 200, client_factory=None):
        self.max_entries = max_entries
        self._client_factory = client_factory or _default_client

    def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        path = str((args or {}).get("path", "/") or "/")
        disp = _norm_path(path)
        tenant = getattr(ctx.identity, "tenant", "") or getattr(ctx.config, "tenant", "")
        mf = self._client_factory(ctx.identity, ctx.config)
        try:
            try:
                folder = _resolve_folder(mf, path, tenant, create=False)
            except FileNotFoundError as miss:
                return ToolOutput(text=(
                    f"The folder '{miss}' does not exist. List a parent folder (e.g. "
                    f"'/') to see what's available, or offer to create it."))
            try:
                entries = mf.dir(folder, tenant=tenant) or []
            except Exception as e:
                return ToolOutput(text=f"(list_folders error: could not list {disp}: {e})")
        finally:
            _close(mf)

        folders = sorted(e.name for e in entries if getattr(e, "is_container", False))
        files = sorted(e.name for e in entries if not getattr(e, "is_container", False))
        audit.record(action="list_folders", user=getattr(ctx.identity, "user", ""),
                     tenant=tenant, result="ok", folders=len(folders), files=len(files))

        if not folders and not files:
            return ToolOutput(text=f"{disp} is empty (no sub-folders or files).")
        lines = [f"Contents of {disp}:"]
        if folders:
            shown = folders[:self.max_entries]
            lines.append("Folders: " + ", ".join(shown)
                         + (f" (+{len(folders) - len(shown)} more)" if len(folders) > len(shown) else ""))
        if files:
            shown = files[:min(20, self.max_entries)]
            lines.append("Files: " + ", ".join(shown)
                         + (f" (+{len(files) - len(shown)} more)" if len(files) > len(shown) else ""))
        return ToolOutput(text="\n".join(lines))


class DocumentSearchTool(Tool):
    """Locate the user's indexed documents by content terms (permission-gated).

    RAG auto-retrieves context for the user's message; this tool lets the model go
    further — deliberately searching for a file the initial context didn't surface,
    then reading it with get_document_text. Returns matching files (name + file_uid
    + snippet), not full text."""

    name = "document_search"
    description = (
        "Search the user's indexed documents by content terms to FIND relevant "
        "files. Returns matching documents with a name, a file_uid, and a short "
        "snippet — not the full text. Use this when the automatically-provided "
        "context does not contain what you need: search for the topic, then read the "
        "most relevant file with get_document_text. Only files the user may read are "
        "returned.")
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Content search terms."},
            "limit": {"type": "integer",
                      "description": "Max documents to return (default 10)."},
        },
        "required": ["query"],
    }

    def __init__(self, search_service, *, default_limit: int = 10):
        self._search = search_service
        self._default_limit = default_limit

    def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        query = str((args or {}).get("query", "") or "")
        try:
            limit = int((args or {}).get("limit") or self._default_limit)
        except (TypeError, ValueError):
            limit = self._default_limit
        try:
            hits = self._search.search(ctx.identity, query, limit=limit, fuzzy=True)
        except guards.GuardError as e:
            return ToolOutput(text=f"(document_search error: {e})")
        except Exception as e:
            return ToolOutput(text=f"(document_search error: {e})")
        if not hits:
            return ToolOutput(text=f"No indexed documents matched '{query}'.")
        lines = [f"{len(hits)} document(s) matched '{query}' (read one with "
                 f"get_document_text using its file_uid):"]
        for h in hits:
            snippet = " ".join((getattr(h, "snippet", "") or "").split())
            lines.append(f"- {getattr(h, 'name', '') or '(unnamed)'} "
                         f"[file_uid={getattr(h, 'file_uid', '')}]\n  {snippet}")
        return ToolOutput(text="\n".join(lines))


class GetDocumentTextTool(Tool):
    """Read a window of one indexed document's extracted text (permission-gated).

    The deep-interrogation half of RAG + direct read: fetch the Markdown the search
    index holds for a specific file_uid, sliced to an [offset, offset+length)
    character window so the model can page through a large document rather than
    pull the whole thing. The reply states the total length and the window bounds
    so the model can request the next range."""

    name = "get_document_text"
    description = (
        "Read the extracted text (Markdown) of ONE indexed document by its file_uid, "
        "to interrogate details the automatic context did not include. Returns a "
        "character window: pass 'offset' (start, default 0) and 'length' (default "
        "4000, capped) to page through a long document — the response reports the "
        "total length and the window returned so you can request the next range. Get "
        "a file_uid from document_search or a citation. Only readable files are "
        "returned.")
    schema = {
        "type": "object",
        "properties": {
            "file_uid": {"type": "string", "description": "The document's file_uid."},
            "offset": {"type": "integer",
                       "description": "Start character offset into the text (default 0)."},
            "length": {"type": "integer",
                       "description": "Number of characters to return (default 4000; capped)."},
        },
        "required": ["file_uid"],
    }

    def __init__(self, search_service, *, default_window: int = 4000, max_window: int = 20000):
        self._search = search_service
        self._default_window = default_window
        self._max_window = max_window

    def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        file_uid = str((args or {}).get("file_uid", "") or "").strip()
        if not file_uid:
            return ToolOutput(text="(get_document_text error: file_uid is required)")
        try:
            offset = max(0, int((args or {}).get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            length = int((args or {}).get("length") or self._default_window)
        except (TypeError, ValueError):
            length = self._default_window
        length = max(1, min(length, self._max_window))
        try:
            text, _truncated = self._search.get_text(ctx.identity, file_uid)
        except PermissionError:
            return ToolOutput(text=f"(get_document_text: you do not have permission to read {file_uid})")
        except FileNotFoundError:
            return ToolOutput(text=(f"(get_document_text: no extracted text for {file_uid} — "
                                    f"it may not be indexed)"))
        except Exception as e:
            return ToolOutput(text=f"(get_document_text error: {e})")
        text = text or ""
        total = len(text)
        if offset >= total and total > 0:
            return ToolOutput(text=(f"Document {file_uid} is {total} characters; offset {offset} "
                                    f"is past the end. Request a smaller offset."))
        window = text[offset:offset + length]
        end = offset + len(window)
        more = (f" — more follows; call again with offset={end} for the next window"
                if end < total else " — end of document")
        header = f"Document {file_uid}: characters {offset}–{end} of {total}{more}:\n\n"
        return ToolOutput(text=header + window)


def build_tools(config: Config, *, include_web: bool = True, search=None,
                mcp: Optional[List[Tool]] = None) -> List[Tool]:
    """The tools to expose to the model for this deployment.

    Report saving is handled by the SAVE_REPORT stream markers (see chat.py), not a
    tool. The web tools are added only when web search is enabled AND ``include_web``
    (the chat layer passes the per-turn opt-in); fetch_page needs its own extra
    enable. The document tools (folder browse + content search + windowed read) are
    added when the document feature is enabled; the search/read pair needs a
    ``search`` SearchService to reach the index (per-user permission-gated).

    ``mcp`` is the tenant's already-resolved MCP tools (McpToolProvider.tools_for,
    which is identity-scoped); they are appended last and are consent-gated at call
    time (MCP_INTEGRATIONS §6)."""
    tools: List[Tool] = []
    if include_web and getattr(config, "web_search_enabled", False):
        from .providers import make_web_search_provider
        tools.append(WebSearchTool(
            make_web_search_provider(config),
            max_results=getattr(config, "web_search_results", 5),
            max_chars=getattr(config, "web_max_chars", 4000),
            max_query_chars=getattr(config, "max_query_chars", 1000)))
        if getattr(config, "web_fetch_pages", False):
            tools.append(FetchPageTool(
                max_bytes=getattr(config, "web_fetch_max_bytes", 2_000_000),
                timeout_ms=getattr(config, "web_timeout_ms", 5000),
                max_chars=getattr(config, "web_max_chars", 4000)))
    if getattr(config, "chat_document_tool_enabled", True):
        # Folder exploration so the model can browse the user's storage.
        tools.append(ListFoldersTool())
        # RAG + direct interrogation: find files by content, then read a range.
        if search is not None:
            tools.append(DocumentSearchTool(
                search, default_limit=getattr(config, "chat_doc_search_limit", 10)))
            tools.append(GetDocumentTextTool(
                search,
                default_window=getattr(config, "chat_doc_text_window", 4000),
                max_window=getattr(config, "chat_doc_text_max_window", 20000)))
    # Tenant-managed MCP tools last (already resolved + namespaced; consent-gated).
    if mcp:
        tools.extend(mcp)
    return tools

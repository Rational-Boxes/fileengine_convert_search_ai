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


def save_report_document(identity, config, *, path: str, filename: str, title: str,
                         body: str, create_folders: bool, client_factory=None,
                         max_bytes: int = 5_000_000, provenance: dict = None):
    """Persist a report (Markdown or HTML ``body``) as an HTML file in the user's
    storage, written *as the user*. Returns ``(uid, location, byte_len)``; raises
    :class:`ReportSaveError`. Used by the marker-driven save path (chat.py).

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
    document = wrap_html_document((title or safe).strip(), html).encode("utf-8")
    if len(document) > max_bytes:
        raise ReportSaveError("too_large", "the report is too large to save")
    tenant = getattr(identity, "tenant", "") or getattr(config, "tenant", "")
    mf = client_factory(identity, config)
    try:
        try:
            parent = _resolve_folder(mf, path or "/", tenant, create=create_folders)
        except FileNotFoundError as miss:
            raise ReportSaveError("missing_folder", f"the folder '{miss}' does not exist",
                                  missing_path=str(miss))
        uid = mf.touch(parent, name, tenant=tenant)
        mf.put(uid, document, tenant=tenant)
        if provenance is not None:
            from . import provenance as _prov
            _prov.attach_provenance(mf, uid, tenant, {
                **provenance,
                "report_title": (title or safe).strip(),
                "report_location": report_location(path, filename),
            })
    except ReportSaveError:
        raise
    except Exception as e:  # WriteUnavailable, PermissionDenied, … — surface as save error
        raise ReportSaveError("write", f"could not save the report: {e}")
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
        if not filename or not body:
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


def build_tools(config: Config, *, include_web: bool = True) -> List[Tool]:
    """The tools to expose to the model for this deployment.

    Document saving is handled by the SAVE_REPORT stream markers (see chat.py),
    not a tool, so only the folder-exploration tool is offered for documents. The
    web tools are added only when web search is enabled AND ``include_web`` (the
    chat layer passes the per-turn opt-in); fetch_page needs its own extra enable."""
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
        # Folder exploration so the model can browse + suggest a destination; the
        # save itself is deterministic via the SAVE_REPORT markers.
        tools.append(ListFoldersTool())
    return tools

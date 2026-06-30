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
from typing import List, Optional
from urllib.parse import urlparse

from . import audit, guards
from .config import Config

log = logging.getLogger("convert_search_ai.llm_tools")


@dataclass
class ToolContext:
    """Per-answer context handed to a tool: who is asking, plus a ``sources``
    accumulator the chat loop reads back to build citations, and ``answer_text``
    — the assistant's reply streamed so far this turn, so create_document can save
    the report the model wrote inline even when it omits the ``html`` argument."""
    identity: object
    config: Config
    sources: List[dict] = field(default_factory=list)  # {kind:"web", url, title}
    answer_text: str = ""


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
# create_document — save a chat-generated report into FileEngine
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


def _looks_like_html(s: str) -> bool:
    low = s.lstrip()[:200].lower()
    return low.startswith("<!doctype") or low.startswith("<html") or "<p>" in low or "<h1" in low


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


class CreateDocumentTool(Tool):
    """Save a report generated from the conversation as a formatted HTML document
    in the user's FileEngine storage (written *as the user*, so ACLs apply)."""

    name = "create_document"
    description = (
        "Save a report you produced in this conversation as a formatted document in "
        "the user's file storage (an HTML file with an automatic PDF preview). "
        "IMPORTANT: writing a report in your chat reply does NOT save it — you must "
        "call this tool. The simplest reliable way: write the full report in your "
        "reply, then call create_document with just 'path' and 'filename' — your "
        "reply's report content is saved automatically (you can omit 'html'). "
        "Optionally pass 'html' to save specific HTML instead of your reply. Confirm "
        "the destination folder + file name with the user before calling. Returns "
        "the saved file's location.")
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description":
                     "Destination folder path in the user's storage, e.g. '/Reports' "
                     "or 'Projects/Q3'. Use '/' for the top level."},
            "filename": {"type": "string", "description":
                         "File name without extension ('.html' is appended), e.g. "
                         "'q3-summary'."},
            "title": {"type": "string", "description":
                      "Report title (used as the document <title> and heading)."},
            "html": {"type": "string", "description":
                     "OPTIONAL. The report body as HTML/Markdown. If omitted, the "
                     "report you wrote in your reply this turn is saved — prefer "
                     "omitting it and writing the report in your reply."},
            "create_folders": {"type": "boolean", "description":
                               "Create the destination folder (and any missing "
                               "parents) if it does not exist. Only set true after "
                               "the user confirms creating a new folder."},
        },
        "required": ["path", "filename"],
    }

    def __init__(self, *, max_bytes: int = 5_000_000, client_factory=None):
        self.max_bytes = max_bytes
        self._client_factory = client_factory or _default_client

    def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        args = args or {}
        user = getattr(ctx.identity, "user", "")
        tenant = getattr(ctx.identity, "tenant", "") or getattr(ctx.config, "tenant", "")
        filename = _safe_name(str(args.get("filename", "")))
        arg_html = str(args.get("html") or "")
        # Fall back to the report the model wrote in its reply this turn — the common
        # case is the model shows the report in chat and calls this with only a
        # destination. Convert that (usually Markdown) to HTML so formatting survives.
        used_fallback = False
        if arg_html.strip():
            body = arg_html
        else:
            reply = (getattr(ctx, "answer_text", "") or "").strip()
            body = markdown_to_html(reply) if reply else ""
            used_fallback = bool(reply)
        # Diagnostic: every invocation, with sizes + whether the reply fallback was
        # used — so a "no file written" never lacks a trace.
        log.info("create_document called: filename=%r path=%r html_arg_len=%d "
                 "reply_len=%d fallback=%s", filename, args.get("path"), len(arg_html),
                 len(getattr(ctx, "answer_text", "") or ""), used_fallback)
        if not filename or not str(body).strip():
            audit.record(action="create_document", user=user, tenant=tenant,
                         result="error", reason="missing_content",
                         html_arg_len=len(arg_html), reply_len=len(getattr(ctx, "answer_text", "") or ""))
            return ToolOutput(text=(
                "(create_document error: no report content to save. Write the report "
                "in your reply and call create_document again with 'path' and "
                "'filename', or pass the report in the 'html' argument.)"))
        title = str(args.get("title", "") or filename).strip()
        path = str(args.get("path", "") or "/")
        create = bool(args.get("create_folders", False))

        name = filename if filename.lower().endswith((".html", ".htm")) else filename + ".html"
        document = wrap_html_document(title, str(body)).encode("utf-8")
        if len(document) > self.max_bytes:
            audit.record(action="create_document", user=user, tenant=tenant,
                         result="error", reason="too_large", bytes=len(document))
            return ToolOutput(text="(create_document error: the report is too large to save)")

        mf = self._client_factory(ctx.identity, ctx.config)
        try:
            try:
                parent = _resolve_folder(mf, path, tenant, create=create)
            except FileNotFoundError as miss:
                return ToolOutput(text=(
                    f"The folder '{miss}' does not exist. Ask the user to confirm the "
                    f"destination, or call create_document again with create_folders=true "
                    f"to create it."))
            uid = mf.touch(parent, name, tenant=tenant)
            mf.put(uid, document, tenant=tenant)
        except Exception as e:  # WriteUnavailable, PermissionDenied, etc. — fail soft
            audit.record(action="create_document", user=getattr(ctx.identity, "user", ""),
                         tenant=tenant, result="error")
            return ToolOutput(text=f"(create_document error: could not save the report: {e})")
        finally:
            _close(mf)

        audit.record(action="create_document", user=user, tenant=tenant, result="ok",
                     bytes=len(document), source="reply" if used_fallback else "html_arg")
        loc = "/".join(["", *(s for s in path.replace("\\", "/").split("/") if s.strip()), name])
        return ToolOutput(text=(
            f"Saved the report to {loc} (file id {uid}). A PDF preview is being "
            f"generated automatically. Let the user know it's ready in their files."))


def build_tools(config: Config, *, include_web: bool = True) -> List[Tool]:
    """The tools to expose to the model for this deployment.

    ``create_document`` is included whenever it's enabled (default on). The web
    tools are added only when web search is enabled AND ``include_web`` (the chat
    layer passes the per-turn opt-in); fetch_page needs its own extra enable."""
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
        # Exploration tool first so the model can browse + suggest a location, then
        # the writer.
        tools.append(ListFoldersTool())
        tools.append(CreateDocumentTool(
            max_bytes=getattr(config, "chat_document_max_bytes", 5_000_000)))
    return tools

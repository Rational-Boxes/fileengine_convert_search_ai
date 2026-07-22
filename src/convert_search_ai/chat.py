"""RAG chat-with-documents (M3) + optional web-search tool (WEB_SEARCH_TOOL_PLAN).

Retrieve permission-scoped document context for the user's message, build a prompt
seeded by the conversation-specific system prompt, and stream the answer from the
configured ChatProvider. When web search is enabled (off by default) and the
provider supports tools, the model may additionally call the ``web_search`` tool;
document and web sources share one ``[n]`` citation numbering.

``answer`` is a sync generator of event dicts:

  {"type": "token", "text": "..."}       streamed answer deltas
  {"type": "tool_call", "name", "args"}  the model invoked a tool (e.g. web_search)
  {"type": "tool_result", "name"}        a tool returned
  {"type": "citations", "citations": [{"marker", "kind", "file_uid"|"url", "title"?}]}
"""
from __future__ import annotations

from typing import Iterator, List, Optional
from urllib.parse import urlparse

from . import audit, guards
from .config import Config
from .llm_tools import ToolContext, build_tools
from .retrieval import Retriever
from .vectorstore import RetrievedChunk

_INSTRUCTIONS = (
    "Answer using ONLY the provided context excerpts from the user's documents. "
    "If the answer is not in the context, say you don't know. Cite the excerpts "
    "you use by their [n] marker."
)

_INSTRUCTIONS_WEB = (
    "Answer using the provided context excerpts from the user's documents. If they "
    "are insufficient or the question needs current/external information, you may "
    "call the web_search tool. Prefer the user's documents when they conflict with "
    "the web. Cite every claim by its [n] marker — document and web sources share "
    "one numbering — and make clear which statements come from the web."
)

# Appended (non-report mode) when the document read/search tools are available:
# RAG auto-retrieval is only a first pass, so let the model interrogate the source
# documents directly instead of giving up when the excerpts fall short.
_INSTRUCTIONS_DOC_TOOLS = (
    "The Context below is auto-retrieved (RAG) and may be incomplete or miss details "
    "buried inside a document. Do NOT say you don't know until you have looked. When "
    "the Context is missing or thin on what the user asks, follow this workflow:\n"
    "  1. Call document_search with the key terms to find which file(s) contain the "
    "information (it returns file names + file_uids).\n"
    "  2. Call get_document_text on the most relevant file_uid to READ its actual "
    "content; page through long files with offset/length until you find the detail.\n"
    "  3. Answer from what you read, naming the document(s) you used.\n"
    "Prefer searching-then-reading a real document over guessing or giving up. You "
    "may iterate (search again, read another file) as needed."
)

# Appended when tenant-configured MCP tools (mcp__<integration>__<tool>) are present.
# These reach EXTERNAL systems the tenant admin registered; each call is gated by an
# explicit user consent prompt, so use them deliberately (MCP_INTEGRATIONS §6).
_INSTRUCTIONS_MCP = (
    "Additional tools named 'mcp__<integration>__<tool>' connect to external systems "
    "your organization configured (e.g. a ticketing system or internal API). Use them "
    "when they are the right way to answer or act on the user's request. Each such "
    "call requires the user's explicit approval before it runs, and may have real "
    "side effects — call one only when it clearly serves what the user asked, and "
    "explain what you intend to do."
)

# Appended when the document tools are available. Workflow: explore folders,
# confirm the destination FIRST, then stream the report wrapped in SAVE_REPORT
# markers — the app diverts the marked body to a file automatically (no fragile
# giant tool-argument needed).
_INSTRUCTIONS_DOCUMENT = (
    "If the user asks to save, export, or turn this conversation (or its results) "
    "into a document or report:\n"
    "1. Use the list_folders tool to explore the user's folders (start at '/' and "
    "drill in) so your suggestion matches their layout.\n"
    "2. Decide the destination FIRST: propose a specific folder + file name and get "
    "the user's confirmation (offer to create a folder, e.g. 'Reports', if none "
    "fits). Settle the destination before writing the report.\n"
    "3. Then write the report wrapped in these markers, with the confirmed "
    "destination in the opening marker:\n"
    "   [[SAVE_REPORT path=\"/Confirmed/Folder\" file=\"report-name\" title=\"Report Title\"]]\n"
    "   ...the full report as Markdown or HTML (headings, paragraphs, tables, lists)...\n"
    "   [[/SAVE_REPORT]]\n"
    "Everything between the markers is saved automatically to that path as an HTML "
    "document with a PDF preview — this is the ONLY way to save; there is no save "
    "tool to call. Put the ENTIRE report between the markers: write the opening "
    "marker, then the complete report, then the closing [[/SAVE_REPORT]] marker — do "
    "not place report content after the closing marker, and do not emit the markers "
    "around just a preamble. Do not claim a report is saved unless you actually "
    "emitted the full marked block."
)

# Injected (in place of _INSTRUCTIONS_DOCUMENT) when the caller pinned a destination
# via the "Generate report" action (GENERATE_REPORT_TO_TARGET). The user already
# chose the folder + filename; the model produces ONLY content and must not name a
# destination — one content‑only SAVE_REPORT block.
_INSTRUCTIONS_REPORT_TARGET = (
    "Your ONLY task right now is to WRITE A REPORT of this conversation. Do not ask "
    "questions, do not browse folders or call any tools, do not narrate what you are "
    "doing ('gathering information', etc.), and do not answer conversationally — the "
    "user has already chosen where to save it. Synthesize the conversation above (and "
    "any Context excerpts below, citing them by their [n] marker where you use them) "
    "into a complete, well-structured report.\n"
    "Output ONLY the report, wrapped in a single pair of markers, with NO text before "
    "the opening marker or after the closing one:\n"
    "   [[SAVE_REPORT title=\"A short, descriptive title\"]]\n"
    "   ...the full report as Markdown: headings, paragraphs, tables, lists...\n"
    "   [[/SAVE_REPORT]]\n"
    "Write the opening marker, then the ENTIRE report, then the closing "
    "[[/SAVE_REPORT]] marker. Do NOT put any folder, path, or filename in the marker "
    "— only a title. Begin your response with '[[SAVE_REPORT'."
)

# Always appended in research mode (answer AND report). The Context labels each file
# by a uid as "(file <uid>)", and the file tools return uids too; left to itself each
# model refers to files differently (one settled on "(file <uid>)", another might emit
# a bare uid or the name). Standardize the convention so the interface can reliably
# rewrite it into a named link — both in the live chat (utils/fileRefs.ts) and in the
# saved report HTML (llm_tools._linkify_file_refs).
_INSTRUCTIONS_FILE_REFS = (
    "Referring to files: each file is identified by a uid, shown in the Context and "
    "returned by the file tools in the form '(file <uid>)'. Whenever you point to a "
    "specific file in your answer, write the reference in exactly that literal form — "
    "'(file <uid>)' with the file's real uid, and nothing else: no bare or reformatted "
    "uids, no invented ids, no Markdown link, and do not write the file name yourself. "
    "CRITICAL: reproduce the uid IN FULL, character-for-character, exactly as given — "
    "it is a long identifier (e.g. '9798c571-c617-42cd-bb82-097f1073f93d'). Never "
    "shorten, truncate, abbreviate, or elide any part of it, and never use '…' or "
    "'...' or a prefix; a partial uid will not resolve and the link will break. "
    "The interface automatically replaces each '(file <uid>)' with a link showing the "
    "file's current name, so the reference always stays correct. This is separate from, "
    "and does not replace, the [n] citation markers."
)


class ChatService:
    def __init__(self, config: Config, *, retriever: Optional[Retriever] = None, chat=None,
                 search=None, mcp=None):
        self.config = config
        self.retriever = retriever or Retriever(config)
        self._chat = chat
        # SearchService for the document_search / get_document_text tools (RAG +
        # LLM-controlled direct interrogation). None ⇒ those two tools are omitted.
        self._search = search
        # McpToolProvider (MCP_INTEGRATIONS): resolves the tenant's enabled external
        # tools per turn. None ⇒ MCP integrations are off (no external tools).
        self._mcp = mcp

    @property
    def chat(self):
        if self._chat is None:
            from .providers import make_chat_provider
            self._chat = make_chat_provider(self.config)
        return self._chat

    def answer(self, identity, *, message: str, system_prompt: str = "",
               history: Optional[List[dict]] = None, k: int = 8,
               web_search: Optional[bool] = None,
               conversation_id: Optional[str] = None,
               report_target: Optional[dict] = None,
               consent=None) -> Iterator[dict]:
        msg = guards.check_query(message, self.config.max_query_chars)
        k = guards.cap_k(k, self.config.max_chat_k)
        chunks = self.retriever.retrieve(identity, msg, k=k)
        chunks, trimmed = guards.trim_context(chunks, self.config.max_context_chars)

        # Report mode is a focused "write the report now" task — offer NO tools so
        # the model can't wander off "gathering information" and answer briefly
        # instead of writing (the destination is already fixed by the user).
        tools = [] if report_target is not None else self._select_tools(identity, web_search)
        messages = list(history or []) + [{"role": "user", "content": msg}]
        system = self._build_system(system_prompt, chunks, tools=tools, report_target=report_target)
        doc_citations = self._doc_citations(chunks)

        if not tools:
            answer_parts: List[str] = []
            for delta in self.chat.stream(messages, system=system):
                answer_parts.append(delta)
                yield {"type": "token", "text": delta}
            # Save the report ONLY when the user pinned a destination (report mode).
            if report_target is not None:
                prov = self._prov(identity, system_prompt, conversation_id, history, msg, doc_citations)
                yield from self._save_marked_reports(identity, "".join(answer_parts), [], prov,
                                                     report_target=report_target)
            self._audit(identity, chunks, doc_citations, trimmed, web_searches=0)
            yield {"type": "citations", "citations": doc_citations}
            return

        # --- tool loop ---------------------------------------------------------
        ctx = ToolContext(identity=identity, config=self.config, consent=consent)
        tool_citations: List[dict] = []   # web + mcp sources, sharing the [n] numbering
        counters = {"marker": len(chunks), "searches": 0}  # tool markers continue after docs
        tools_by_name = {t.name: t for t in tools}

        def execute(name: str, args: dict) -> str:
            tool = tools_by_name.get(name)
            if tool is None:
                return f"(unknown tool: {name})"
            counters["searches"] += 1
            out = tool.run(args or {}, ctx)
            if not out.sources:
                return out.text
            lines = []
            for s in out.sources:
                counters["marker"] += 1
                m = counters["marker"]
                if s.get("kind") == "mcp":
                    # An MCP tool call is a bibliographic source too: record it as a
                    # citation and prefix its result with the [n] marker so the model
                    # can attribute the answer to the external tool.
                    tool_citations.append({"marker": m, "kind": "mcp",
                                           "integration": s.get("integration", ""),
                                           "tool": s.get("tool", "")})
                    label = s.get("label") or (f"{s.get('integration', '')} · "
                                               f"{s.get('tool', '')}").strip(" ·")
                    lines.append(f"[{m}] {label}\n{out.text}")
                else:
                    tool_citations.append({"marker": m, "kind": "web",
                                           "url": s["url"], "title": s.get("title", "")})
                    head = s.get("title") or s["url"]
                    lines.append(f"[{m}] {head} ({urlparse(s['url']).netloc})\n"
                                 f"{s.get('snippet', '')}\nSource: {s['url']}")
            return "\n\n".join(lines)

        specs = [{"name": t.name, "description": t.description, "schema": t.schema} for t in tools]
        answer_parts: List[str] = []
        for ev in self.chat.run_tools(messages, system=system, tools=specs, execute=execute,
                                      max_iterations=self.config.web_max_iterations):
            et = ev.get("type")
            if et == "text":
                answer_parts.append(ev.get("text", ""))
                # Keep the running reply on the context so the marked report can be
                # extracted and saved after the stream completes.
                ctx.answer_text = "".join(answer_parts)
                yield {"type": "token", "text": ev.get("text", "")}
            elif et == "tool_call":
                yield {"type": "tool_call", "name": ev.get("name"), "args": ev.get("args")}
            elif et == "tool_result":
                yield {"type": "tool_result", "name": ev.get("name")}

        # Save the report ONLY when the user pinned a destination (report mode).
        citations = doc_citations + tool_citations
        if report_target is not None:
            prov = self._prov(identity, system_prompt, conversation_id, history, msg, citations)
            yield from self._save_marked_reports(identity, ctx.answer_text, ctx.saved, prov,
                                                 report_target=report_target)

        self._audit(identity, chunks, citations, trimmed, web_searches=counters["searches"])
        yield {"type": "citations", "citations": citations}

    # ----------------------------------------------------------------- helpers
    def _prov(self, identity, system_prompt, conversation_id, history, message, citations) -> dict:
        """Assemble the chat context handed to a saved report so it can attach a
        provenance log (who chatted + transcript + grounding). See provenance.py."""
        return {
            "user": getattr(identity, "user", ""),
            "tenant": getattr(identity, "tenant", ""),
            "model": getattr(self.config, "chat_model", ""),
            "provider": getattr(self.config, "chat_provider", ""),
            "system_prompt": system_prompt or "",
            "conversation_id": conversation_id,
            "history": history or [],
            "message": message,
            "citations": citations,
        }

    def _save_marked_reports(self, identity, answer_text: str, saved: List[str],
                             provenance: Optional[dict] = None,
                             report_target: Optional[dict] = None):
        """Write the ``[[SAVE_REPORT …]] … [[/SAVE_REPORT]]`` block the model streamed
        to the file the **user pinned** in the UI (``report_target`` — folder UID +
        filename; the marker is content‑only). The closing marker OR a stream cutoff
        triggers the save; no tool call required. Emits a ``report_saved`` event (for
        the "Open report" preview link) plus a confirmation token. ``saved`` dedupes
        locations already written this turn."""
        from .llm_tools import (ReportSaveError, parse_report_markers,
                                report_location, save_report_document)
        pinned = report_target or {}
        for rep in parse_report_markers(answer_text):
            # The destination is the user's pinned target, never the model's marker.
            path = str(pinned.get("path", "") or "")
            filename = str(pinned.get("filename", "") or "")
            folder_uid = pinned.get("folder_uid")
            loc = report_location(path, filename)
            if loc in saved:
                continue
            try:
                uid, loc, nbytes = save_report_document(
                    identity, self.config, path=path, filename=filename,
                    title=rep.title, body=rep.body, create_folders=False,
                    folder_uid=folder_uid,
                    max_bytes=getattr(self.config, "chat_document_max_bytes", 5_000_000),
                    provenance=({**provenance, "answer_text": answer_text}
                                if provenance is not None else None))
            except ReportSaveError as e:
                audit.record(action="save_report", user=identity.user, tenant=identity.tenant,
                             result="error", reason=e.kind)
                yield {"type": "token",
                       "text": f"\n\n⚠ Could not save the report to {loc}: {e.message}\n"}
                continue
            saved.append(loc)
            audit.record(action="save_report", user=identity.user, tenant=identity.tenant,
                         result="ok", bytes=nbytes, truncated=not rep.complete)
            # Structured event first (drives the SPA's "Open report" preview link),
            # then the human‑readable confirmation token.
            yield {"type": "report_saved", "uid": uid,
                   "name": loc.rsplit("/", 1)[-1], "path": loc}
            note = "" if rep.complete else (" (note: the report may have been cut off before "
                                            "completion — regenerate if it looks incomplete)")
            yield {"type": "token", "text": (
                f"\n\n✅ Saved the report to {loc} (file {uid}){note}. A PDF preview is "
                f"being generated.\n")}
            # Only the first marked block is the report; ignore any extras in report mode.
            break

    def _select_tools(self, identity, web_search: Optional[bool]):
        """Decide which tools to offer this turn. Requires provider tool support.
        Web tools need the global enable + per-message opt-in (or the configured
        default); the folder-exploration tool is offered whenever the document
        feature is enabled (saving itself is marker-driven, not a tool); the tenant's
        enabled MCP tools (identity-scoped, consent-gated) are resolved per turn."""
        if not getattr(self.chat, "supports_tools", False):
            return []
        include_web = False
        if getattr(self.config, "web_search_enabled", False):
            include_web = (self.config.web_search_default if web_search is None
                           else bool(web_search))
        mcp_tools = []
        if self._mcp is not None and getattr(self.config, "mcp_enabled", False):
            try:
                mcp_tools = self._mcp.tools_for(identity)
            except Exception:  # fail open — MCP problems never break the chat
                import logging
                logging.getLogger("convert_search_ai.chat").warning(
                    "MCP tool resolution failed; continuing without external tools",
                    exc_info=True)
                mcp_tools = []
        return build_tools(self.config, include_web=include_web, search=self._search,
                           mcp=mcp_tools)

    @staticmethod
    def _doc_citations(chunks: List[RetrievedChunk]) -> List[dict]:
        seen, out = set(), []
        for i, c in enumerate(chunks):
            if c.file_uid not in seen:
                seen.add(c.file_uid)
                out.append({"marker": i + 1, "kind": "doc", "file_uid": c.file_uid})
        return out

    def _audit(self, identity, chunks, citations, trimmed, *, web_searches: int) -> None:
        audit.record(action="chat", user=identity.user, tenant=identity.tenant, result="ok",
                     retrieved=len(chunks), citations=len(citations),
                     web_searches=web_searches, context_trimmed=trimmed)

    def _build_system(self, system_prompt: str, chunks: List[RetrievedChunk],
                      *, tools: Optional[List] = None,
                      report_target: Optional[dict] = None) -> str:
        context = ("\n\n".join(f"[{i + 1}] (file {c.file_uid})\n{c.text}" for i, c in enumerate(chunks))
                   if chunks else "(no relevant context found)")
        names = {getattr(t, "name", "") for t in (tools or [])}
        parts = []
        if system_prompt and system_prompt.strip():
            parts.append(system_prompt.strip())
        # In report mode the report directive REPLACES the retrieval "answer + cite"
        # instruction — otherwise the model produces a short grounded answer instead
        # of writing the report (GENERATE_REPORT_TO_TARGET). Outside report mode we do
        # NOT solicit any SAVE_REPORT block (the model no longer chooses where/whether
        # to save); list_folders remains available for general browsing.
        if report_target is not None:
            parts.append(_INSTRUCTIONS_REPORT_TARGET)
        else:
            parts.append(_INSTRUCTIONS_WEB if "web_search" in names else _INSTRUCTIONS)
            if "get_document_text" in names:
                parts.append(_INSTRUCTIONS_DOC_TOOLS)
            if any(n.startswith("mcp__") for n in names):
                parts.append(_INSTRUCTIONS_MCP)
        # The file-reference convention applies in both answer and report modes, so
        # the UI/report linkifier can turn "(file <uid>)" into a named link.
        parts.append(_INSTRUCTIONS_FILE_REFS)
        parts.append("Context:\n" + context)
        return "\n\n".join(parts)

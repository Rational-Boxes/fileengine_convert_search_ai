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

"""Chat provenance log for AI-generated reports (design_documents/CHAT_WITH_AI.md §4.6).

When a report is saved from a chat, attach a hidden-child **chatlog** artifact that
records *who* chatted, the *full* conversation transcript, the grounding citations,
and the model — so an AI-authored document carries an auditable provenance trail.

Decisions (per the feature doc):
  - **Full transcript**, untruncated.
  - **PII redacted** from the transcript (emails, phones, SSNs, card numbers); other
    content — including public web material — is kept verbatim.
  - **Lifespan follows the report**: it is a hidden child (rendition-shaped), so it
    inherits the report's ACL + cascade delete + read access for free.
  - Written **as the requesting user** (same identity/ManagedFiles as the report).

The child is one self-contained HTML file (`<version>-chatlog.html`) with the
machine-readable JSON record embedded in a `<script type="application/json">` tag,
so it both previews cleanly and is parseable.
"""
from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from .renditions import rendition_name

log = logging.getLogger("convert_search_ai.provenance")

PROVENANCE_FMT = "chatlog"
SCHEMA = "fileengine.chat_provenance.v1"

# ---------------------------------------------------------------- PII redaction
# Deliberately conservative — specific PII shapes only — so public web content
# (URLs, titles, statistics, prose) is preserved per the "keep web content"
# decision. Over-redaction of PII is acceptable; clobbering general text is not.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD = re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}\b")  # grouped 16-digit only
_PHONE = re.compile(r"(?<!\w)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\w)")


def redact_pii(text: str) -> str:
    """Scrub common personally identifiable information from a string, leaving all
    other content untouched."""
    if not text:
        return text or ""
    t = _EMAIL.sub("[redacted-email]", text)
    t = _SSN.sub("[redacted-ssn]", t)
    t = _CARD.sub("[redacted-card]", t)
    t = _PHONE.sub("[redacted-phone]", t)
    return t


# ------------------------------------------------------------------ the record
def build_record(prov: dict, *, report_uid: str, report_version: str) -> dict:
    """Assemble the provenance JSON record from the chat context. The transcript is
    history + this turn's user message + the assistant answer (which carried the
    report), each PII-redacted."""
    transcript = []
    for m in (prov.get("history") or []):
        transcript.append({"role": str(m.get("role", "")),
                           "content": redact_pii(str(m.get("content", "")))})
    transcript.append({"role": "user", "content": redact_pii(str(prov.get("message", "")))})
    transcript.append({"role": "assistant", "content": redact_pii(str(prov.get("answer_text", "")))})
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report": {
            "uid": report_uid,
            "version": report_version,
            "title": prov.get("report_title", ""),
            "location": prov.get("report_location", ""),
        },
        "chatted_by": prov.get("user", ""),
        "tenant": prov.get("tenant", ""),
        "conversation_id": prov.get("conversation_id"),
        "provider": prov.get("provider", ""),
        "model": prov.get("model", ""),
        "system_prompt": redact_pii(str(prov.get("system_prompt", "") or "")),
        "citations": prov.get("citations") or [],
        "transcript": transcript,
        "pii_redacted": True,
    }


# -------------------------------------------------------------- HTML rendering
def render_html(record: dict, *, linkify=None) -> bytes:
    """A self-contained, previewable HTML transcript with the JSON record embedded.

    ``linkify`` (optional) rewrites the model's '(file <uid>)' references — in the
    transcript and the sources list — into named file deep-links (see
    ``attach_provenance``); omit it to keep the raw text (used by unit tests)."""
    from .llm_tools import markdown_to_html  # lazy: avoids an import cycle
    e = html.escape
    rep = record.get("report", {})
    rows = []
    for m in record.get("transcript", []):
        role = e(m.get("role", ""))
        # Render each turn as Markdown → HTML, letting any raw HTML the model
        # produced (e.g. the report body) render rather than showing as escaped
        # text. Isolated downstream by the viewer (shadow DOM / iframe document).
        body = markdown_to_html(m.get("content", ""))
        if linkify:
            body = linkify(body)  # "(file <uid>)" → named deep-link
        rows.append(f'<div class="msg {role}"><div class="role">{role}</div>'
                    f'<div class="body">{body}</div></div>')
    cites = []
    for c in record.get("citations", []):
        marker = e(str(c.get("marker", "")))
        if c.get("kind") == "web":
            cites.append(f'<li>[{marker}] '
                         f'<a href="{e(c.get("url",""))}" rel="noopener">{e(c.get("title") or c.get("url",""))}</a></li>')
        else:
            # Resolve the cited document to a named deep-link too (never a raw uid).
            fu = c.get("file_uid", "")
            label = linkify(f"(file {fu})") if (linkify and fu) else f"document {e(fu)}"
            cites.append(f'<li>[{marker}] {label}</li>')
    cites_html = f'<ul class="cites">{"".join(cites)}</ul>' if cites else '<p class="muted">No citations.</p>'
    payload = json.dumps(record, ensure_ascii=False)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>Chat provenance — {e(rep.get('title',''))}</title>"
        "<style>"
        "body{font:14px/1.5 system-ui,sans-serif;max-width:820px;margin:24px auto;padding:0 16px;color:#1f2937}"
        "h1{font-size:18px}.meta{color:#6b7280;font-size:12px;margin-bottom:16px}"
        ".meta b{color:#374151}"
        ".msg{border:1px solid #e5e7eb;border-radius:10px;padding:10px 12px;margin:8px 0}"
        ".msg.user{background:#f9fafb}.msg.assistant{background:#fff}"
        ".role{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#6b7280;margin-bottom:4px}"
        ".cites{font-size:13px}.muted{color:#9ca3af}"
        ".note{margin-top:20px;font-size:12px;color:#9ca3af;border-top:1px solid #e5e7eb;padding-top:8px}"
        "</style></head><body>"
        "<h1>🧾 Chat provenance log</h1>"
        "<div class=\"meta\">"
        f"Report: <b>{e(rep.get('title',''))}</b> · "
        f"generated by <b>{e(record.get('chatted_by',''))}</b> "
        f"({e(record.get('tenant',''))}) at {e(record.get('generated_at',''))}<br>"
        f"Model: {e(record.get('provider',''))} / {e(record.get('model',''))}"
        f"{' · conversation ' + e(str(record.get('conversation_id'))) if record.get('conversation_id') else ''}"
        "</div>"
        "<h2 style=\"font-size:15px\">Conversation</h2>"
        f"{''.join(rows)}"
        "<h2 style=\"font-size:15px\">Sources</h2>"
        f"{cites_html}"
        "<p class=\"note\">Personally identifiable information has been redacted from "
        "this transcript; public web content is preserved. This log follows the "
        "report's access and lifespan.</p>"
        f"<script id=\"provenance-record\" type=\"application/json\">{payload}</script>"
        "</body></html>"
    ).encode("utf-8")


# --------------------------------------------------------------- attach to file
def attach_provenance(mf, report_uid: str, tenant: str, prov: dict,
                      *, base_url: str = "", ref_cache: dict = None) -> Optional[str]:
    """Write the chat-provenance hidden child for a just-saved report, as the same
    user. Best-effort: returns the child name, or None on failure (logged) — a
    provenance hiccup must never lose the report itself.

    ``base_url`` (the SPA origin) + ``ref_cache`` (a shared uid→name map) are passed
    through to linkify the transcript's '(file <uid>)' references into named file
    deep-links, consistent with the report body itself."""
    try:
        info = mf.stat(report_uid, tenant=tenant)
        version = getattr(info, "version", "") or "0"
        record = build_record(prov, report_uid=report_uid, report_version=version)
        name = rendition_name(version, PROVENANCE_FMT, "html")
        child = mf.touch(report_uid, name, tenant=tenant)
        # Reuse the report's linkifier so the chat log's file references resolve to
        # the same named deep-links as the report body.
        from .llm_tools import _linkify_file_refs  # lazy: avoids an import cycle
        cache = ref_cache if ref_cache is not None else {}
        linkify = lambda s: _linkify_file_refs(s, mf, tenant, base_url, cache)  # noqa: E731
        mf.put(child, render_html(record, linkify=linkify), tenant=tenant)
        return name
    except Exception as e:  # noqa: BLE001 — provenance is best-effort
        log.warning("chat provenance log not attached to report %s: %s", report_uid, e)
        return None

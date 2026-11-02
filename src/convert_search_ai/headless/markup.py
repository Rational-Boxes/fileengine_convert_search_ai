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

"""The marked-up PDF (EXTRACTION_PROMPT_SETS.md §5): the document with each
extracted value's evidence highlighted, a note on each highlight, and a legend
page. A derived copy, written as a version sidecar of the source file.

The quotes were taken from the stored Markdown, not the PDF, so an exact search
often misses: Markdown table pipes and emphasis, line and column breaks,
hyphenation, ligatures, typographic quotes and dashes. The locator tries, in
order:

1. ``exact``: the normalised quote, with PyMuPDF's own search;
2. ``words``: the quote's words as a consecutive run of the page's words
   (normalised the same way; a word hyphenated across a line counts as one),
   so line and column breaks do not matter;
3. ``cells``: a table-row quote (``Fire resistance | EI 60``) as its cells,
   each found on the page and all on one line band.

Pages are searched starting from the one where the quote's offset in the
Markdown suggests it lies, so repeated text resolves to the nearer occurrence.

Unlocated evidence is reported per field, never silently skipped. The value is
still valid (its quote was verified against the text); only the drawing failed.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import unicodedata
from typing import Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("convert_search_ai.headless.markup")

#: Distinct, readable on white, and not reliant on red/green alone.
PALETTE = ["#e8590c", "#1c7ed6", "#2f9e44", "#ae3ec9", "#f59f00", "#0ca678",
           "#d6336c", "#4263eb", "#74b816", "#7048e8"]

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = {ord("‘"): "'", ord("’"): "'", ord("“"): '"', ord("”"): '"'}
_MD = re.compile(r"(\*\*|__|`|^#+\s*|^\s*[-*+]\s+|^\s*\d+\.\s+)", re.M)


def normalise(text: str) -> str:
    """The comparison form of a quote or of page text."""
    text = unicodedata.normalize("NFKC", text or "")      # ligatures, full-width
    text = text.translate(_DASHES).translate(_QUOTES)
    text = _MD.sub(" ", text)
    text = text.replace("|", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


_EDGE_PUNCT = ".,;:!?()[]{}\"'"


def _tokens(text: str) -> List[str]:
    """Words for comparison, without the punctuation at their edges
    ("certificate." in the PDF, "certificate" in the quote). Inner punctuation
    stays: 13501-2, 1.1, EI-60."""
    out = []
    for t in re.split(r"[\s|]+", normalise(text)):
        t = t.strip(_EDGE_PUNCT)
        if t:
            out.append(t)
    return out


def _hex(colour: str) -> Tuple[float, float, float]:
    c = colour.lstrip("#")
    return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))


def colours_for(fields: Sequence[str], chosen: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    chosen = chosen or {}
    return {f: chosen.get(f) or PALETTE[i % len(PALETTE)] for i, f in enumerate(fields)}


# ------------------------------------------------------------------ locate

def _page_order(n_pages: int, ratio: Optional[float]) -> List[int]:
    if ratio is None or n_pages <= 1:
        return list(range(n_pages))
    start = min(n_pages - 1, max(0, int(ratio * n_pages)))
    order = [start]
    for d in range(1, n_pages):
        for p in (start - d, start + d):
            if 0 <= p < n_pages:
                order.append(p)
    return order


def _page_words(page) -> List[Tuple]:
    """(rect, token) per word, a line-end hyphenation joined with the next word."""
    import pymupdf
    raw = page.get_text("words")      # x0, y0, x1, y1, word, block, line, word_no
    out: List[Tuple] = []
    i = 0
    while i < len(raw):
        x0, y0, x1, y1, w = raw[i][:5]
        rect = pymupdf.Rect(x0, y0, x1, y1)
        if (w.endswith("-") and i + 1 < len(raw)
                and (raw[i + 1][5], raw[i + 1][6]) != (raw[i][5], raw[i][6])):
            nxt = raw[i + 1]
            for t in _tokens(w[:-1] + nxt[4]):
                out.append((rect | pymupdf.Rect(*nxt[:4]), t))
            i += 2
            continue
        for t in _tokens(w):
            out.append((rect, t))
        i += 1
    return out


def _find_run(words: List[Tuple], toks: List[str]) -> Optional[List]:
    if not toks:
        return None
    n = len(toks)
    for i in range(len(words) - n + 1):
        if all(words[i + k][1] == toks[k] for k in range(n)):
            return [w[0] for w in words[i:i + n]]
    return None


def _merge_lines(rects: List) -> List:
    """Word rectangles merged into one rectangle per text line."""
    import pymupdf
    lines: List = []
    for r in sorted(rects, key=lambda r: (round(r.y0, 0), r.x0)):
        for i, ln in enumerate(lines):
            if abs(ln.y0 - r.y0) < max(2.0, r.height * 0.5) and r.x0 - ln.x1 < r.height * 3:
                lines[i] = ln | r
                break
        else:
            lines.append(pymupdf.Rect(r))
    return lines


def _contiguous(rects: List) -> bool:
    """PyMuPDF's own search joins words in reading order whatever the layout,
    so "Fire resistance … EI 60" can match a label at the top of the page and a
    value at the bottom. A real match is one passage: successive pieces on the
    same line or the next."""
    for a, b in zip(rects, rects[1:]):
        if b.y0 - a.y1 > max(a.height, b.height) * 1.5:
            return False
    return True


def locate(doc, quote: str, ratio: Optional[float] = None) -> Optional[dict]:
    """``{"page": index, "rects": [...], "method": ...}`` or None."""
    if not quote or not quote.strip():
        return None
    needle = normalise(quote)
    toks = _tokens(quote)
    cells = [c for c in (quote.split("|") if "|" in quote else []) if c.strip()]
    for p in _page_order(len(doc), ratio):
        page = doc[p]
        hits = page.search_for(needle) if needle else []
        if not hits and quote.strip() != needle:
            hits = page.search_for(re.sub(r"\s+", " ", quote.replace("|", " ")).strip())
        if hits and _contiguous(list(hits)):
            return {"page": p, "rects": list(hits), "method": "exact"}
        words = _page_words(page)
        run = _find_run(words, toks)
        if run:
            lines = _merge_lines(run)
            if _contiguous(sorted(lines, key=lambda r: r.y0)):
                return {"page": p, "rects": lines, "method": "words"}
        if len(cells) >= 2:
            found = [_find_run(words, _tokens(c)) for c in cells]
            if all(found):
                ys = [sum(r.y0 + r.y1 for r in f) / (2 * len(f)) for f in found]
                h = max(r.height for f in found for r in f)
                if max(ys) - min(ys) <= h:
                    rects = [r for f in found for r in f]
                    return {"page": p, "rects": _merge_lines(rects), "method": "cells"}
    return None


def _value_rects(page, region_rects: List, value) -> List:
    """The value's own words inside the located evidence (so the eye lands on it)."""
    if value is None or isinstance(value, bool):
        return []
    text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
    toks = _tokens(text)
    if not toks:
        return []
    words = [(r, t) for r, t in _page_words(page)
             if any(r.intersects(reg) for reg in region_rects)]
    run = _find_run(words, toks)
    return _merge_lines(run) if run else []


# ------------------------------------------------------------------ draw

def _fmt(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, list):
        return ", ".join(map(str, v))
    return str(v)


def mark_up(pdf: bytes, data: Dict[str, dict], fields: List[dict], *, text_len: int,
            title: str, legend: Dict[str, str], colours: Optional[Dict[str, str]] = None
            ) -> Tuple[bytes, Dict[str, dict]]:
    """(marked-up PDF bytes, per-field report).

    ``data`` is an extraction item's ``data``; ``fields`` the field definitions
    in order (``name``, optional ``label``); ``text_len`` the stored Markdown's
    length (to turn an offset into a page estimate); ``legend`` extra lines
    (prompt set, model, version, time)."""
    import pymupdf
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        names = [f["name"] for f in fields]
        palette = colours_for(names, colours)
        report: Dict[str, dict] = {}
        for f in fields:
            name = f["name"]
            label = f.get("label") or name
            v = data.get(name) or {}
            if not v.get("found"):
                report[name] = {"located": False, "reason": "not_found"}
                continue
            ev = v.get("evidence") or {}
            offset = ev.get("offset")
            ratio = (offset / text_len) if isinstance(offset, int) and text_len else None
            hit = locate(doc, ev.get("quote") or "", ratio)
            if hit is None:
                report[name] = {"located": False, "reason": "not_located"}
                continue
            page = doc[hit["page"]]
            colour = _hex(palette[name])
            unit = f" {v['unit']}" if v.get("unit") else ""
            note = f"{label}: {_fmt(v.get('value'))}{unit}"
            annot = page.add_highlight_annot(hit["rects"])
            annot.set_colors(stroke=colour)
            annot.set_opacity(0.35)
            annot.set_info(title=title, content=note)
            annot.update()
            for r in _value_rects(page, hit["rects"], v.get("value")):
                u = page.add_underline_annot(r)
                u.set_colors(stroke=colour)
                u.set_info(title=title, content=note)
                u.update()
            report[name] = {"located": True, "page": hit["page"] + 1, "method": hit["method"]}
        _legend(doc, fields, data, report, palette, title, legend)
        return doc.tobytes(garbage=3, deflate=True), report
    finally:
        doc.close()


def _legend(doc, fields, data, report, palette, title, legend) -> None:
    import pymupdf
    w, h = (doc[0].rect.width, doc[0].rect.height) if len(doc) else (595, 842)
    page = doc.new_page(width=w, height=h)
    y = 50
    page.insert_text((40, y), title, fontsize=14, fontname="helv")
    y += 20
    for k, v in legend.items():
        page.insert_text((40, y), f"{k}: {v}", fontsize=9, fontname="helv",
                         color=(0.35, 0.35, 0.35))
        y += 13
    y += 10
    for f in fields:
        if y > h - 50:
            page = doc.new_page(width=w, height=h)
            y = 50
        name = f["name"]
        v = data.get(name) or {}
        r = report.get(name, {})
        if v.get("found"):
            unit = f" {v['unit']}" if v.get("unit") else ""
            value = _fmt(v.get("value")) + unit
            where = (f"page {r['page']}" if r.get("located")
                     else "found in the text, not located on a page")
        else:
            value, where = "not stated in the document", ""
        page.draw_rect(pymupdf.Rect(40, y - 8, 50, y + 2), color=_hex(palette[name]),
                       fill=_hex(palette[name]))
        page.insert_text((56, y), f"{f.get('label') or name}: {value}", fontsize=10,
                         fontname="helv")
        if where:
            page.insert_text((56, y + 11), where, fontsize=8, fontname="helv",
                             color=(0.45, 0.45, 0.45))
            y += 11
        y += 18


def legend_lines(*, prompt_set: Optional[dict], model: str, version: str) -> Dict[str, str]:
    out = {}
    if prompt_set and prompt_set.get("name"):
        rev = prompt_set.get("revision")
        out["Prompt set"] = prompt_set["name"] + (f" (revision {rev})" if rev else "")
    out["Model"] = model or ""
    out["Document version"] = version or ""
    out["Extracted"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return out


# ------------------------------------------------------------------ source + sidecar

class MarkupStore:
    """Reads the PDF to mark up and writes the marked-up copy as a version
    sidecar (§5.1, §5.2), as csai's service identity like the cache sidecar.

    Which PDF: the source itself if it is a PDF, else csai's ``pdf`` rendition
    of that version (office documents, ODT included). Nothing is converted
    beside the source. Over ``max_bytes`` the PDF is not read at all."""

    def __init__(self, agent_client_factory, max_bytes: int):
        self._agent = agent_client_factory
        self.max_bytes = int(max_bytes)

    @staticmethod
    def _close(mf) -> None:
        close = getattr(mf, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _read(self, mf, uid: str, version: str, tenant: str) -> Optional[bytes]:
        buf, total = [], 0
        for chunk in mf.get_stream(uid, version=version, tenant=tenant):
            total += len(chunk)
            if total > self.max_bytes:
                return None
            buf.append(chunk)
        return b"".join(buf)

    def source(self, file_uid: str, version: str, name: str, tenant: str) -> Tuple[Optional[bytes], str]:
        """(pdf bytes, "") or (None, reason): ``no_pdf`` | ``too_large`` | ``error``."""
        from ..renditions import rendition_name
        mf = self._agent()
        try:
            if (name or "").lower().endswith(".pdf"):
                data = self._read(mf, file_uid, version, tenant)
                return (data, "") if data is not None else (None, "too_large")
            want = rendition_name(version, "pdf", "pdf")
            for e in mf.dir(file_uid, tenant=tenant) or []:
                if e.name == want:
                    data = self._read(mf, e.uid, "", tenant)
                    return (data, "") if data is not None else (None, "too_large")
            return None, "no_pdf"
        except Exception:
            log.warning("could not read the PDF of %s@%s", file_uid, version, exc_info=True)
            return None, "error"
        finally:
            self._close(mf)

    def existing(self, file_uid: str, version: str, fmt: str, tenant: str) -> Optional[Tuple[str, str]]:
        from ..renditions import rendition_name
        name = rendition_name(version, fmt, "pdf")
        mf = self._agent()
        try:
            uid = next((e.uid for e in mf.dir(file_uid, tenant=tenant) or [] if e.name == name), None)
            return (uid, name) if uid else None
        except Exception:
            return None
        finally:
            self._close(mf)

    def write(self, file_uid: str, version: str, fmt: str, data: bytes, tenant: str) -> Tuple[str, str]:
        """(sidecar uid, name); an existing one for this version and set is
        replaced by a new version of itself."""
        from ..renditions import rendition_name
        name = rendition_name(version, fmt, "pdf")
        mf = self._agent()
        try:
            uid = next((e.uid for e in mf.dir(file_uid, tenant=tenant) or [] if e.name == name), None)
            if uid is None:
                uid = mf.touch(file_uid, name, tenant=tenant)
            mf.put_stream(uid, [data], tenant=tenant)
            return uid, name
        finally:
            self._close(mf)


class TestMarkups:
    """Builder test runs store nothing (§4.3), so a test's marked-up PDF is
    held in memory for a short while and fetched once by the editor who ran
    it."""

    def __init__(self, ttl_s: int = 600, max_items: int = 50):
        import threading
        self.ttl_s, self.max_items = ttl_s, max_items
        self._items: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    def put(self, owner: str, data: bytes) -> str:
        import secrets
        import time
        token = secrets.token_urlsafe(18)
        with self._lock:
            now = time.time()
            self._items = {k: v for k, v in self._items.items() if v[2] > now}
            while len(self._items) >= self.max_items:
                self._items.pop(next(iter(self._items)))
            self._items[token] = (owner, data, now + self.ttl_s)
        return token

    def get(self, owner: str, token: str) -> Optional[bytes]:
        import time
        with self._lock:
            item = self._items.get(token)
            if not item or item[0] != owner or item[2] < time.time():
                return None
            return item[1]

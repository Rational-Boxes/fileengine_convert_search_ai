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

"""Structured extraction from documents (HEADLESS_RESEARCH_API.md §3.1).

Not chat. Each document in scope is read whole — its stored Markdown, in
consecutive windows when long — and asked the same schema, independently, so a
value buried in one table of one datasheet is not missed and a value from the
wrong product's datasheet is not borrowed.

Every value comes back wrapped::

    {"value": ..., "unit": ..., "evidence": {"quote": ..., "offset": n}, "found": true}

and the wrapper is enforced here, not trusted from the model:

- ``found: false`` when the document does not state it — never a guess;
- a value whose quote cannot be found in the document is DROPPED (counted), so
  an invented value never reaches the caller; ``offset`` is where the quote sits
  in the stored Markdown, which the caller can check;
- each value is validated against the caller's schema for that field, with one
  repair round trip when the reply is malformed or a value does not validate.

The stored text is read, never re-converted. If it is older than the file it is
re-indexed first; a file with no usable text is ``skipped`` with the reason,
never reported as "nothing found". Markdown is the only input until multimodal
support exists (owner, 2026-10-29)."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional

from .. import audit
from ..providers.base import collect_usage

log = logging.getLogger("convert_search_ai.headless.extract")

PROMPT_VERSION = "extract.v1"

#: Statuses that mean the stored row has no usable text for this version.
_NO_TEXT_STATUSES = {"unsupported", "error"}
#: Statuses that mean conversion has not finished; re-index before reading.
_PENDING_STATUSES = {"pending", "converting"}

_MAX_DEPTH = 12          # folder recursion bound


class ExtractRequestError(ValueError):
    """The request itself is unusable (400)."""


# --------------------------------------------------------------------- request

@dataclass
class ExtractRequest:
    schema: dict
    folder_uid: Optional[str] = None
    files: List[str] = field(default_factory=list)
    recursive: bool = False
    extensions: List[str] = field(default_factory=list)
    name_glob: str = ""
    instructions: str = ""
    cache: str = "use"            # use | refresh

    @property
    def fields(self) -> List[str]:
        return list((self.schema.get("properties") or {}).keys())


def parse_request(body: dict, config) -> ExtractRequest:
    """Validate a POST /v1/extract body."""
    if not isinstance(body, dict):
        raise ExtractRequestError("body must be a JSON object")
    folder = body.get("folder_uid")
    files = body.get("files")
    if (folder is None) == (files is None):
        raise ExtractRequestError("give exactly one target: folder_uid or files")
    max_files = int(getattr(config, "headless_files_per_run", 200))
    if files is not None:
        if not isinstance(files, list) or not all(isinstance(f, str) and f.strip() for f in files):
            raise ExtractRequestError("files must be a list of document ids")
        seen, ordered = set(), []
        for f in files:
            f = f.strip()
            if f not in seen:
                seen.add(f)
                ordered.append(f)
        if not ordered:
            raise ExtractRequestError("files is empty")
        if len(ordered) > max_files > 0:
            raise ExtractRequestError(f"at most {max_files} files per run")
        files = ordered
    elif not isinstance(folder, str):
        raise ExtractRequestError("folder_uid must be a string ('' is the root)")

    schema = body.get("schema")
    _check_schema(schema, config)

    instructions = body.get("instructions") or ""
    if not isinstance(instructions, str):
        raise ExtractRequestError("instructions must be a string")
    cap = int(getattr(config, "headless_max_input_chars", 20000))
    if len(instructions) + len(json.dumps(schema)) > cap:
        raise ExtractRequestError(f"schema and instructions exceed {cap} characters")

    match = body.get("match") or {}
    if not isinstance(match, dict):
        raise ExtractRequestError("match must be an object")
    exts = [str(e).lower() if str(e).startswith(".") else "." + str(e).lower()
            for e in (match.get("extensions") or [])]
    cache = body.get("cache", "use")
    if cache not in ("use", "refresh"):
        raise ExtractRequestError("cache must be 'use' or 'refresh'")
    return ExtractRequest(schema=schema, folder_uid=folder if files is None else None,
                          files=files or [], recursive=bool(body.get("recursive", False)),
                          extensions=exts, name_glob=str(match.get("name") or ""),
                          instructions=instructions, cache=cache)


def _check_schema(schema, config) -> None:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    if not isinstance(schema, dict):
        raise ExtractRequestError("schema must be a JSON Schema object")
    props = schema.get("properties")
    if schema.get("type") != "object" or not isinstance(props, dict) or not props:
        raise ExtractRequestError("schema must be an object schema with properties")
    limit = int(getattr(config, "headless_max_schema_fields", 60))
    if len(props) > limit:
        raise ExtractRequestError(f"schema has more than {limit} fields")
    for name in props:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]{0,63}", name):
            raise ExtractRequestError(f"field name {name!r} is not allowed")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        raise ExtractRequestError(f"schema is not valid JSON Schema: {e.message}")


def fingerprint(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()[:32]


def cache_key(req: ExtractRequest, model: str) -> str:
    """One question: the schema, the instructions, the model, the prompt."""
    return fingerprint({"schema": req.schema, "instructions": req.instructions,
                        "model": model, "prompt": PROMPT_VERSION})


# ---------------------------------------------------------------------- prompt

_SYSTEM = (
    "You extract specific facts from ONE document for a program. Answer with a "
    "single JSON object and nothing else: no prose, no Markdown fences.\n"
    "The object has exactly one key per requested field. Each value is an object:\n"
    '  {"found": true|false, "value": <the value>, "unit": <unit or null>, '
    '"evidence": {"quote": "<exact text copied from the document>"}}\n'
    "Rules:\n"
    "- Use ONLY this document. If it does not state a field, return "
    '{"found": false, "value": null, "unit": null, "evidence": null}. Never guess, '
    "infer from general knowledge, or use another product's figures.\n"
    "- evidence.quote must be copied character for character from the document "
    "(a short span that states the value, e.g. a table row or sentence). It is "
    "checked against the document; a value whose quote is not found is discarded.\n"
    "- value must match the field's schema. Put a unit of measure in unit, not in "
    "value, when the field is numeric.\n"
    "- If the document covers several products, use the one the instructions "
    "name; if none is named and they differ, return found: false."
)


def build_prompt(req: ExtractRequest, name: str, window: str, part: int, parts: int):
    """(system, messages) for one window of one document."""
    fields = {k: v for k, v in (req.schema.get("properties") or {}).items()}
    lines = ["Fields to extract (JSON Schema for each value):",
             json.dumps(fields, ensure_ascii=False, indent=1)]
    if req.instructions.strip():
        lines += ["", "Instructions from the requester:", req.instructions.strip()]
    where = f" (part {part} of {parts})" if parts > 1 else ""
    lines += ["", f"Document: {name}{where}", "<<<DOCUMENT", window, "DOCUMENT>>>"]
    return _SYSTEM, [{"role": "user", "content": "\n".join(lines)}]


def windows(text: str, size: int) -> List[str]:
    """Consecutive windows of about ``size`` characters, cut at line breaks so a
    table row is not split; a little overlap so a value at a cut is still seen."""
    if size <= 0 or len(text) <= size:
        return [text]
    out, start, overlap = [], 0, min(2000, size // 10)
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            nl = text.rfind("\n", start + size // 2, end)
            if nl > start:
                end = nl
        out.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


# --------------------------------------------------------------------- checking

def parse_json(text: str) -> dict:
    """The JSON object in a model reply (tolerates fences or a stray preamble)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t)
    try:
        obj = json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a < 0 or b <= a:
            raise ValueError("no JSON object in the reply")
        obj = json.loads(t[a:b + 1])
    if not isinstance(obj, dict):
        raise ValueError("the reply is not a JSON object")
    return obj


_SEP = r"[\s|*_`>#\-:]+"


def locate(quote: str, text: str) -> Optional[int]:
    """Where ``quote`` occurs in ``text``, tolerant of whitespace, case and the
    Markdown table/emphasis characters a model drops when quoting a table row.
    None if it is not there (or is too short to mean anything)."""
    words = [w for w in re.split(_SEP, quote or "") if w]
    if not words or len("".join(words)) < 2:
        return None
    pattern = _SEP.join(re.escape(w) for w in words)
    m = re.search(pattern, text, re.IGNORECASE)
    return m.start() if m else None


def _empty() -> dict:
    return {"found": False, "value": None, "unit": None, "evidence": None}


def check(raw: dict, req: ExtractRequest, text: str):
    """Enforce the wrapper on one reply. Returns (data, dropped, problems):
    ``data`` per field; ``dropped`` the fields whose quote was not found;
    ``problems`` what to ask the model to fix (shape or schema errors)."""
    from jsonschema import Draft202012Validator
    props = req.schema.get("properties") or {}
    data, dropped, problems = {}, [], []
    for name, sub in props.items():
        entry = raw.get(name)
        if entry is None:
            problems.append(f"missing field {name!r}")
            data[name] = _empty()
            continue
        if not isinstance(entry, dict):
            problems.append(f"field {name!r} must be an object with found/value/unit/evidence")
            data[name] = _empty()
            continue
        value = entry.get("value")
        if not entry.get("found") or value is None:
            data[name] = _empty()
            continue
        errors = sorted(Draft202012Validator(sub).iter_errors(value), key=lambda e: e.path)
        if errors:
            problems.append(f"field {name!r}: {errors[0].message}")
            data[name] = _empty()
            continue
        ev = entry.get("evidence") if isinstance(entry.get("evidence"), dict) else {}
        quote = str(ev.get("quote") or "")
        offset = locate(quote, text)
        if offset is None:
            dropped.append(name)
            data[name] = {**_empty(), "dropped": "no_evidence"}
            continue
        unit = entry.get("unit")
        data[name] = {"found": True, "value": value,
                      "unit": unit if isinstance(unit, str) and unit.strip() else None,
                      "evidence": {"quote": quote, "offset": offset}}
    unknown = [k for k in raw if k not in props]
    if unknown:
        problems.append(f"unexpected fields {unknown!r}")
    return data, dropped, problems


def merge(parts: List[dict], fields: Iterable[str]) -> dict:
    """Combine per-window results: the first window that found a field wins; a
    field dropped for lack of evidence everywhere stays marked dropped."""
    out = {}
    for f in fields:
        found = next((p[f] for p in parts if p.get(f, {}).get("found")), None)
        if found is not None:
            out[f] = found
        elif any(p.get(f, {}).get("dropped") for p in parts):
            out[f] = {**_empty(), "dropped": "no_evidence"}
        else:
            out[f] = _empty()
    return out


def counts(data: dict) -> dict:
    return {"found": sum(1 for v in data.values() if v.get("found")),
            "not_found": sum(1 for v in data.values()
                             if not v.get("found") and not v.get("dropped")),
            "dropped_no_evidence": sum(1 for v in data.values() if v.get("dropped"))}


# --------------------------------------------------------------------- service

@dataclass
class Target:
    file_uid: str
    name: str = ""
    version: str = ""
    skipped: str = ""             # reason, when this target cannot be read


class Extractor:
    """Runs one extraction request for one identity.

    Collaborators are injected so the engine runs against fakes in tests:
    ``client_for(identity)`` a user-bound core client, ``gate`` the permission
    gate (READ + existence), ``store`` the document store (``get_text_row``),
    ``convert(file_uid, tenant)`` the re-index path (or None), ``cache`` the
    sidecar cache, ``chat`` the chat provider, ``meter`` the usage meter."""

    def __init__(self, config, *, client_for, gate, store, chat, cache=None,
                 convert: Optional[Callable[[str, str], object]] = None, meter=None):
        self.config = config
        self.client_for = client_for
        self.gate = gate
        self.store = store
        self.chat = chat
        self.cache = cache
        self.convert = convert
        self.meter = meter
        self.truncated = False

    # --- targets
    def resolve(self, identity, req: ExtractRequest) -> List[Target]:
        mf = self.client_for(identity)
        try:
            if req.files:
                return [self._named(mf, identity, uid) for uid in req.files]
            return self._folder(mf, identity, req)
        finally:
            close = getattr(mf, "close", None)
            if callable(close):
                close()

    def _named(self, mf, identity, uid: str) -> Target:
        # One reason for missing and unreadable, so the route cannot be used to
        # learn which ids exist; the gate checks existence as well as READ.
        if not self.gate.can_read(mf, identity, uid):
            return Target(uid, skipped="not found or not readable")
        try:
            info = mf.stat(uid, tenant=identity.tenant)
        except Exception:
            return Target(uid, skipped="not found or not readable")
        if info.is_dir:
            return Target(uid, info.name, skipped="not a document")
        return Target(uid, info.name, info.version)

    def _folder(self, mf, identity, req: ExtractRequest) -> List[Target]:
        cap = int(getattr(self.config, "headless_files_per_run", 200))
        out: List[Target] = []
        queue = [(req.folder_uid or "", 0)]
        # Look for one document past the cap, so the plan can say the folder had
        # more than one run reads (rather than cutting it short silently).
        while queue and len(out) <= cap:
            uid, depth = queue.pop(0)
            try:
                entries = mf.dir(uid, tenant=identity.tenant) or []
            except Exception:
                if depth == 0:
                    raise ExtractRequestError("folder not found or not readable")
                continue
            for e in sorted(entries, key=lambda e: e.name.lower()):
                if e.is_container:
                    if req.recursive and depth < _MAX_DEPTH:
                        queue.append((e.uid, depth + 1))
                    continue
                if not self._matches(req, e.name):
                    continue
                # Folder mode: an unreadable file is left out silently, as search
                # does — the caller did not name it.
                if not self.gate.can_read(mf, identity, e.uid):
                    continue
                try:
                    info = mf.stat(e.uid, tenant=identity.tenant)
                except Exception:
                    continue
                out.append(Target(e.uid, info.name, info.version))
                if len(out) > cap:
                    break
        self.truncated = len(out) > cap
        return out[:cap]

    @staticmethod
    def _matches(req: ExtractRequest, name: str) -> bool:
        low = name.lower()
        if req.extensions and not any(low.endswith(x) for x in req.extensions):
            return False
        if req.name_glob and not fnmatch.fnmatch(low, req.name_glob.lower()):
            return False
        return True

    # --- one document
    def _text(self, identity, t: Target):
        """(text, skip_reason). Re-indexes stale or pending text first."""
        row = self.store.get_text_row(identity.tenant, t.file_uid)
        stale = (row is None or row.status in _PENDING_STATUSES
                 or row.source_version != t.version)
        if stale and self.convert is not None:
            try:
                self.convert(t.file_uid, identity.tenant)
            except Exception:
                log.warning("re-index of %s failed", t.file_uid, exc_info=True)
            row = self.store.get_text_row(identity.tenant, t.file_uid)
        if row is None or row.source_version != t.version:
            return None, "text not yet indexed for this version"
        if row.status in _NO_TEXT_STATUSES or not (row.content_md or "").strip():
            return None, "no text"
        return row.content_md, ""

    def extract_one(self, identity, req: ExtractRequest, t: Target) -> dict:
        """One ``item``. Never raises for a per-file problem."""
        base = {"file_uid": t.file_uid, "name": t.name, "version": t.version}
        if t.skipped:
            return {**base, "skipped": t.skipped}
        text, why = self._text(identity, t)
        if text is None:
            return {**base, "skipped": why}
        model = getattr(self.chat, "model_id", "")
        key = cache_key(req, model)
        if self.cache is not None and req.cache == "use":
            hit = self.cache.get(t.file_uid, t.version, identity.tenant, key)
            if hit and isinstance(hit.get("data"), dict):
                if self.meter is not None:
                    self.meter.add_cache_hit(identity, int(hit.get("tokens") or 0))
                return {**base, "data": hit["data"], "counts": counts(hit["data"]),
                        "valid": True, "cached": True, "model": hit.get("model", model),
                        "created_at": hit.get("created_at", "")}

        spent = [0]

        def sink(i, o):
            spent[0] += i + o

        parts, problems_left = [], []
        chunks = windows(text, int(getattr(self.config, "headless_extract_window_chars", 60000)))
        with collect_usage(sink):
            for n, chunk in enumerate(chunks, 1):
                data, problems = self._ask(req, t.name, chunk, n, len(chunks), text)
                parts.append(data)
                problems_left += problems
        if self.meter is not None:
            self.meter.add_tokens(identity, spent[0])
        data = merge(parts, req.fields)
        valid = not problems_left
        item = {**base, "data": data, "counts": counts(data), "valid": valid,
                "cached": False, "model": model, "tokens": spent[0]}
        if not valid:
            item["problems"] = problems_left[:10]
        elif self.cache is not None:
            import datetime as _dt
            self.cache.put(t.file_uid, t.version, identity.tenant, key, {
                "schema_hash": fingerprint(req.schema),
                "instructions_hash": fingerprint(req.instructions),
                "model": model, "provider": getattr(self.config, "chat_provider", ""),
                "prompt_version": PROMPT_VERSION,
                "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "created_by": identity.user, "client_key_id": identity.client_key_id,
                "tokens": spent[0], "data": data})
        return item

    def _ask(self, req, name, chunk, part, parts, text):
        """One window: ask, check, and repair once if the reply is malformed.
        Quotes are located in the whole document, so offsets are document
        offsets whichever window found them."""
        system, messages = build_prompt(req, name, chunk, part, parts)
        reply = self.chat.complete_json(messages, system=system)
        try:
            data, _dropped, problems = check(parse_json(reply), req, text)
        except ValueError as e:
            data, problems = {f: _empty() for f in req.fields}, [str(e)]
        if not problems:
            return data, []
        repair = messages + [
            {"role": "assistant", "content": reply or ""},
            {"role": "user", "content": "That reply cannot be used: " + "; ".join(problems[:8])
             + ". Answer again with only the corrected JSON object."}]
        reply2 = self.chat.complete_json(repair, system=system)
        try:
            data2, _d, problems2 = check(parse_json(reply2), req, text)
        except ValueError as e:
            return data, [str(e)]
        # A field the model still leaves out after the repair claims no value, so
        # it is "not found" — never a wrong value. Measured on the production
        # model: a document with none of the fields often comes back as {} even
        # when asked again; flagging that invalid would re-ask (and re-bill) on
        # every request, because invalid results are not cached. Wrong types and
        # unreadable replies stay invalid.
        return data2, [p for p in problems2 if not p.startswith("missing field")]

    # --- the run
    def run(self, identity, req: ExtractRequest, emit: Callable[[str, dict], None],
            cancelled: Callable[[], bool] = lambda: False,
            on_item: Optional[Callable[[dict], None]] = None) -> dict:
        self.truncated = False
        targets = self.resolve(identity, req)
        plan = {"files": [{"file_uid": t.file_uid, "name": t.name, "version": t.version}
                          for t in targets if not t.skipped],
                "skipped": [{"file_uid": t.file_uid, "reason": t.skipped}
                            for t in targets if t.skipped],
                "truncated": self.truncated}
        if self.truncated:
            # The folder holds more matching documents than one run reads; the
            # caller narrows the request (match, a subfolder) or names ids.
            plan["limit"] = int(getattr(self.config, "headless_files_per_run", 200))
        emit("plan", plan)
        summary = {"files": len(targets), "extracted": 0, "cached": 0, "skipped": 0,
                   "failed": 0, "found": 0, "not_found": 0, "dropped_no_evidence": 0,
                   "tokens": 0, "cancelled": False, "truncated": self.truncated}
        budget_out = False
        state = self._usage_state(identity)
        for t in targets:
            if cancelled():
                summary["cancelled"] = True
                break
            if not t.skipped and self.meter is not None and (
                    budget_out or self.meter.tokens_exhausted(identity)):
                budget_out = True
                item = {"file_uid": t.file_uid, "name": t.name, "version": t.version,
                        "skipped": "budget"}
            else:
                try:
                    item = self.extract_one(identity, req, t)
                except Exception as e:
                    log.warning("extraction of %s failed", t.file_uid, exc_info=True)
                    item = {"file_uid": t.file_uid, "name": t.name, "version": t.version,
                            "error": str(e)[:300]}
            self._tally(summary, item)
            audit.record(action="extract_item", user=identity.user, tenant=identity.tenant,
                         result=_result(item), file_uid=t.file_uid, version=t.version,
                         client_key_id=identity.client_key_id,
                         cached=bool(item.get("cached")), tokens=int(item.get("tokens") or 0),
                         **(item.get("counts") or {}))
            if on_item is not None:
                on_item(item)
            emit("error" if "error" in item else "item", item)
            if int(item.get("tokens") or 0) > 0 and state is not None:
                # Tell the caller when this run moves the key into warning or
                # exceeded, not only at the end (§3.8).
                st = self._usage_status(identity)
                if st is not None and st.get("state") != state:
                    state = st.get("state")
                    emit("usage", st)
        return summary

    def _usage_status(self, identity) -> Optional[dict]:
        if self.meter is None:
            return None
        try:
            return self.meter.status(identity)
        except Exception:
            return None

    def _usage_state(self, identity) -> Optional[str]:
        st = self._usage_status(identity)
        return st.get("state") if st else None

    @staticmethod
    def _tally(summary: dict, item: dict) -> None:
        if "error" in item:
            summary["failed"] += 1
        elif item.get("skipped"):
            summary["skipped"] += 1
        else:
            summary["extracted"] += 1
            summary["cached"] += 1 if item.get("cached") else 0
            for k, v in (item.get("counts") or {}).items():
                summary[k] = summary.get(k, 0) + v
            summary["tokens"] += int(item.get("tokens") or 0)


def _result(item: dict) -> str:
    if "error" in item:
        return "error"
    if item.get("skipped"):
        return "skipped"
    return "ok" if item.get("valid", True) else "invalid"

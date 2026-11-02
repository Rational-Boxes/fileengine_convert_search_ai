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

"""The extraction models a deployment offers (EXTRACTION_PROMPT_SETS.md §4.4).

``CSAI_EXTRACT_MODELS`` lists them; ``CSAI_EXTRACT_DEFAULT_MODEL`` names the
default. A prompt set, an application or an imported file names a PREFERRED
model; one this deployment does not list falls back to the default (owner,
2026-11-02). A model choice is never an error, and the fallback is never
hidden: every result carries ``{"requested", "used", "fallback"}``.

An entry with no provider details uses the chat provider's (``CSAI_CHAT_*``),
which covers every model one OpenAI-compatible endpoint serves. Provider keys
are named by environment variable (``api_key_env``) and never appear in the
list, the API or the UI.

A broken list (bad JSON, duplicate ids, a default that is not listed) makes
:meth:`ModelRegistry.from_config` raise, and the app refuses to start rather
than run with a list nobody configured."""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from typing import List, Optional, Tuple

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_ALLOWED = {"id", "label", "model", "provider", "base_url", "max_tokens", "json_mode",
            "api_key_env"}


class ModelConfigError(ValueError):
    pass


class ModelRegistry:
    def __init__(self, config, entries: List[dict], default_id: str, implicit: bool = False):
        self.config = config
        self.entries = entries
        self.by_id = {e["id"]: e for e in entries}
        self.default_id = default_id
        self.implicit = implicit          # no list configured: the chat model alone
        self._providers: dict = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config) -> "ModelRegistry":
        raw = (getattr(config, "extract_models", "") or "").strip()
        chat_model = getattr(config, "chat_model", "") or ""
        if not raw:
            entry = {"id": chat_model or "default", "label": chat_model or "default",
                     "model": chat_model, "_chat": True}
            return cls(config, [entry], entry["id"], implicit=True)
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise ModelConfigError(f"CSAI_EXTRACT_MODELS is not valid JSON: {e}")
        if not isinstance(data, list) or not data:
            raise ModelConfigError("CSAI_EXTRACT_MODELS must be a non-empty JSON list")
        entries, seen = [], set()
        for i, e in enumerate(data):
            if not isinstance(e, dict):
                raise ModelConfigError(f"CSAI_EXTRACT_MODELS[{i}] is not an object")
            unknown = set(e) - _ALLOWED
            if unknown:
                raise ModelConfigError(f"CSAI_EXTRACT_MODELS[{i}]: unknown keys "
                                       f"{sorted(unknown)} (keys go in api_key_env)")
            mid, model = e.get("id"), e.get("model")
            if not isinstance(mid, str) or not _ID.fullmatch(mid):
                raise ModelConfigError(f"CSAI_EXTRACT_MODELS[{i}]: bad id {mid!r}")
            if mid in seen:
                raise ModelConfigError(f"CSAI_EXTRACT_MODELS: id {mid!r} appears twice")
            if not isinstance(model, str) or not model.strip():
                raise ModelConfigError(f"CSAI_EXTRACT_MODELS[{i}] ({mid}): model is required")
            seen.add(mid)
            entries.append({**e, "label": str(e.get("label") or mid)})
        default = (getattr(config, "extract_default_model", "") or "").strip() or entries[0]["id"]
        if default not in seen:
            raise ModelConfigError(f"CSAI_EXTRACT_DEFAULT_MODEL {default!r} is not in "
                                   "CSAI_EXTRACT_MODELS")
        return cls(config, entries, default)

    # ------------------------------------------------------------------ choice

    def public(self) -> List[dict]:
        """What the builder and desktop see: no provider details, no keys."""
        return [{"id": e["id"], "label": e["label"], "default": e["id"] == self.default_id}
                for e in self.entries]

    def resolve(self, preferred: Optional[str]) -> Tuple[dict, dict]:
        """(entry to use, report). The report is ``{"requested", "used",
        "fallback"}``; ``requested`` is None when nothing was asked for."""
        preferred = (preferred or "").strip() or None
        entry = self.by_id.get(preferred) if preferred else None
        fallback = preferred is not None and entry is None
        if entry is None:
            entry = self.by_id[self.default_id]
        return entry, {"requested": preferred, "used": entry["id"], "fallback": fallback}

    def is_known(self, model_id: Optional[str]) -> bool:
        return bool(model_id) and model_id in self.by_id

    # ------------------------------------------------------------------ providers

    def provider(self, entry: dict, default_chat):
        """The chat provider for ``entry``. The implicit entry, and an entry that
        names the chat model with no provider details of its own, reuse the
        app's chat provider (``default_chat``); others are built once."""
        own = {k for k in ("provider", "base_url", "api_key_env", "max_tokens", "json_mode")
               if entry.get(k) not in (None, "")}
        if entry.get("_chat") or (not own and entry["model"] == getattr(
                self.config, "chat_model", None)):
            return default_chat
        with self._lock:
            p = self._providers.get(entry["id"])
            if p is None:
                p = self._build(entry)
                self._providers[entry["id"]] = p
            return p

    def _build(self, entry: dict):
        from ..providers.factory import make_chat_provider
        overlay = copy.copy(self.config)
        overlay.chat_model = entry["model"]
        if entry.get("provider"):
            overlay.chat_provider = entry["provider"]
        if entry.get("base_url"):
            overlay.chat_base_url = entry["base_url"]
        if entry.get("api_key_env"):
            key = os.environ.get(entry["api_key_env"], "")
            overlay.chat_api_key = key
            overlay.anthropic_api_key_override = key
        if entry.get("max_tokens"):
            overlay.chat_max_tokens = int(entry["max_tokens"])
        if entry.get("json_mode") is not None:
            overlay.chat_json_mode = bool(entry["json_mode"])
        return make_chat_provider(overlay)


def registry(app) -> ModelRegistry:
    r = getattr(app.state, "extract_models", None)
    if r is None:
        r = ModelRegistry.from_config(app.state.config)
        app.state.extract_models = r
    return r

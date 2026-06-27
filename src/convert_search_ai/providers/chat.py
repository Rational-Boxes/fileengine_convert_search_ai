"""Chat provider implementations (lazy external imports)."""
from __future__ import annotations

import json
import os
from typing import Iterator, List, Optional

from .base import ChatProvider


def _last_user(messages: List[dict]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content", "")
            return c if isinstance(c, str) else ""
    return ""


class AnthropicChatProvider(ChatProvider):
    """Claude (Anthropic) — the ecosystem default and reference implementation."""

    supports_tools = True

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None,
                 max_tokens: int = 1024):
        self.model_id = model
        self.max_tokens = max_tokens
        self._key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def stream(self, messages: List[dict], *, system: Optional[str] = None) -> Iterator[str]:
        import anthropic
        client = anthropic.Anthropic(api_key=self._key)
        kwargs = dict(model=self.model_id, max_tokens=self.max_tokens, messages=messages)
        if system:
            kwargs["system"] = system
        with client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                yield text

    def run_tools(self, messages, *, system=None, tools=None, execute=None,
                  max_iterations=4) -> Iterator[dict]:
        import anthropic
        client = anthropic.Anthropic(api_key=self._key)
        anth_tools = [{"name": t["name"], "description": t["description"],
                       "input_schema": t["schema"]} for t in (tools or [])]
        msgs = list(messages)
        for _ in range(max(1, max_iterations)):
            kwargs = dict(model=self.model_id, max_tokens=self.max_tokens,
                          messages=msgs, tools=anth_tools)
            if system:
                kwargs["system"] = system
            with client.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    yield {"type": "text", "text": text}
                final = stream.get_final_message()
            tool_uses = [b for b in final.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                return
            msgs.append({"role": "assistant", "content": final.content})
            results = []
            for tu in tool_uses:
                yield {"type": "tool_call", "name": tu.name, "args": tu.input}
                text = execute(tu.name, tu.input) if execute else ""
                yield {"type": "tool_result", "name": tu.name}
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": text})
            msgs.append({"role": "user", "content": results})
        # Iterations exhausted — force a final, tool-free answer.
        kwargs = dict(model=self.model_id, max_tokens=self.max_tokens, messages=msgs)
        if system:
            kwargs["system"] = system
        with client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                yield {"type": "text", "text": text}


class OpenAICompatibleChatProvider(ChatProvider):
    """Any OpenAI-compatible chat endpoint — OpenAI, **Ollama**, vLLM, LM Studio,
    etc. — selected by ``base_url``. Requires the ``openai`` SDK (the client for
    all such endpoints). The system prompt is sent as a leading system message."""

    supports_tools = True

    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None,
                 max_tokens: int = 1024):
        self.model_id = model
        self.base_url = base_url or None
        self.max_tokens = max_tokens
        # Many local servers ignore the key but the SDK requires a non-empty one.
        self._key = api_key or os.environ.get("OPENAI_API_KEY") or "not-needed"

    def _client(self):
        from openai import OpenAI
        return OpenAI(api_key=self._key, base_url=self.base_url)

    def stream(self, messages: List[dict], *, system: Optional[str] = None) -> Iterator[str]:
        client = self._client()
        msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
        completion = client.chat.completions.create(
            model=self.model_id, messages=msgs, max_tokens=self.max_tokens, stream=True)
        for chunk in completion:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    def run_tools(self, messages, *, system=None, tools=None, execute=None,
                  max_iterations=4) -> Iterator[dict]:
        client = self._client()
        oa_tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["schema"]}}
            for t in (tools or [])]
        msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
        for _ in range(max(1, max_iterations)):
            completion = client.chat.completions.create(
                model=self.model_id, messages=msgs, tools=oa_tools,
                max_tokens=self.max_tokens, stream=True)
            text_acc, calls = "", {}  # tc.index -> {id, name, args}
            for chunk in completion:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    text_acc += delta.content
                    yield {"type": "text", "text": delta.content}
                for tc in (getattr(delta, "tool_calls", None) or []):
                    slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
            if not calls:
                return
            msgs.append({"role": "assistant", "content": text_acc or None, "tool_calls": [
                {"id": s["id"], "type": "function",
                 "function": {"name": s["name"], "arguments": s["args"]}}
                for s in calls.values()]})
            for s in calls.values():
                try:
                    args = json.loads(s["args"] or "{}")
                except ValueError:
                    args = {}
                yield {"type": "tool_call", "name": s["name"], "args": args}
                text = execute(s["name"], args) if execute else ""
                yield {"type": "tool_result", "name": s["name"]}
                msgs.append({"role": "tool", "tool_call_id": s["id"], "content": text})
        # Iterations exhausted — force a final, tool-free answer.
        completion = client.chat.completions.create(
            model=self.model_id, messages=msgs, max_tokens=self.max_tokens, stream=True)
        for chunk in completion:
            if chunk.choices and getattr(chunk.choices[0].delta, "content", None):
                yield {"type": "text", "text": chunk.choices[0].delta.content}


class EchoChatProvider(ChatProvider):
    """Offline dev/test chat: deterministic, no external calls. Echoes the last
    user message and notes whether retrieved context was supplied. No tools."""

    model_id = "echo"

    def stream(self, messages: List[dict], *, system: Optional[str] = None) -> Iterator[str]:
        last = _last_user(messages)
        has_ctx = bool(system and "context" in system.lower())
        yield f"[echo{' +context' if has_ctx else ''}] {last}"


class ToolEchoChatProvider(ChatProvider):
    """Offline, deterministic, **tool-using** chat for dev/tests. If a ``web_search``
    tool is offered, it calls it once with the last user message, then answers —
    exercising the whole ``run_tools`` loop without a real model or network."""

    model_id = "echo-tools"
    supports_tools = True

    def stream(self, messages: List[dict], *, system: Optional[str] = None) -> Iterator[str]:
        yield f"[echo-tools] {_last_user(messages)}"

    def run_tools(self, messages, *, system=None, tools=None, execute=None,
                  max_iterations=4) -> Iterator[dict]:
        last = _last_user(messages)
        names = [t["name"] for t in (tools or [])]
        if execute and "web_search" in names:
            yield {"type": "text", "text": "Searching the web. "}
            args = {"query": last}
            yield {"type": "tool_call", "name": "web_search", "args": args}
            execute("web_search", args)
            yield {"type": "tool_result", "name": "web_search"}
            yield {"type": "text", "text": f"[echo-tools+web] {last}"}
        else:
            yield {"type": "text", "text": f"[echo-tools] {last}"}

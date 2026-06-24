"""RAG chat-with-documents (M3).

Retrieve permission-scoped context for the user's message, build a prompt seeded
by the conversation-specific system prompt, stream the answer from the configured
ChatProvider, and emit citations (the source files of the retrieved context — all
readable by the user). ``answer`` is a sync generator of event dicts:

  {"type": "token", "text": "..."}      streamed answer deltas
  {"type": "citations", "citations": [{"file_uid", "marker"}]}
"""
from __future__ import annotations

from typing import Iterator, List, Optional

from . import audit, guards
from .config import Config
from .retrieval import Retriever
from .vectorstore import RetrievedChunk

_INSTRUCTIONS = (
    "Answer using ONLY the provided context excerpts from the user's documents. "
    "If the answer is not in the context, say you don't know. Cite the excerpts "
    "you use by their [n] marker."
)


class ChatService:
    def __init__(self, config: Config, *, retriever: Optional[Retriever] = None, chat=None):
        self.config = config
        self.retriever = retriever or Retriever(config)
        self._chat = chat

    @property
    def chat(self):
        if self._chat is None:
            from .providers import make_chat_provider
            self._chat = make_chat_provider(self.config)
        return self._chat

    def answer(self, identity, *, message: str, system_prompt: str = "",
               history: Optional[List[dict]] = None, k: int = 8) -> Iterator[dict]:
        msg = guards.check_query(message, self.config.max_query_chars)
        k = guards.cap_k(k, self.config.max_chat_k)
        chunks = self.retriever.retrieve(identity, msg, k=k)
        chunks, trimmed = guards.trim_context(chunks, self.config.max_context_chars)
        system = self._build_system(system_prompt, chunks)
        messages = list(history or []) + [{"role": "user", "content": msg}]

        for delta in self.chat.stream(messages, system=system):
            yield {"type": "token", "text": delta}

        seen, citations = set(), []
        for i, c in enumerate(chunks):
            if c.file_uid not in seen:
                seen.add(c.file_uid)
                citations.append({"file_uid": c.file_uid, "marker": i + 1})
        audit.record(action="chat", user=identity.user, tenant=identity.tenant, result="ok",
                     retrieved=len(chunks), citations=len(citations), context_trimmed=trimmed)
        yield {"type": "citations", "citations": citations}

    def _build_system(self, system_prompt: str, chunks: List[RetrievedChunk]) -> str:
        context = ("\n\n".join(f"[{i + 1}] (file {c.file_uid})\n{c.text}" for i, c in enumerate(chunks))
                   if chunks else "(no relevant context found)")
        parts = []
        if system_prompt and system_prompt.strip():
            parts.append(system_prompt.strip())
        parts.append(_INSTRUCTIONS)
        parts.append("Context:\n" + context)
        return "\n\n".join(parts)

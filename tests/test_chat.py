"""Unit tests for the RAG ChatService (fake retriever + capturing chat provider)."""
from convert_search_ai.chat import ChatService
from convert_search_ai.config import Config
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.vectorstore import RetrievedChunk


class FakeRetriever:
    def __init__(self, chunks):
        self.chunks = chunks

    def retrieve(self, identity, message, k=8):
        return list(self.chunks)


class CapturingChat:
    def __init__(self):
        self.system = None
        self.messages = None

    def stream(self, messages, system=None):
        self.system = system
        self.messages = messages
        yield "Hello "
        yield "world"


def _id():
    return Identity(user="u", tenant="default", authenticated=True)


def test_answer_streams_tokens_then_unique_citations():
    chunks = [RetrievedChunk("fA", 0, "alpha", 0.1),
              RetrievedChunk("fA", 1, "beta", 0.2),
              RetrievedChunk("fB", 0, "gamma", 0.3)]
    cc = CapturingChat()
    events = list(ChatService(Config(), retriever=FakeRetriever(chunks), chat=cc)
                  .answer(_id(), message="hi", system_prompt="Be brief."))

    assert "".join(e["text"] for e in events if e["type"] == "token") == "Hello world"
    cites = [e for e in events if e["type"] == "citations"][0]["citations"]
    assert [c["file_uid"] for c in cites] == ["fA", "fB"]   # unique, order preserved

    # conversation system prompt seeded + retrieved context embedded
    assert "Be brief." in cc.system and "alpha" in cc.system and "Context" in cc.system
    assert cc.messages[-1] == {"role": "user", "content": "hi"}


def test_answer_with_history_and_no_context():
    cc = CapturingChat()
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}]
    events = list(ChatService(Config(), retriever=FakeRetriever([]), chat=cc)
                  .answer(_id(), message="now", history=history))
    assert [e for e in events if e["type"] == "citations"][0]["citations"] == []
    assert cc.messages == history + [{"role": "user", "content": "now"}]
    assert "no relevant context" in cc.system

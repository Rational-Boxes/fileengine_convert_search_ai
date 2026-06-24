"""Unit tests for pluggable AI providers (offline implementations + factory)."""
import math

import pytest

from convert_search_ai.config import Config
from convert_search_ai.providers.chat import EchoChatProvider
from convert_search_ai.providers.embeddings import HashEmbeddingProvider
from convert_search_ai.providers.factory import make_chat_provider, make_embedding_provider


def test_hash_embeddings_deterministic_and_unit_norm():
    p = HashEmbeddingProvider(dimension=64)
    a = p.embed(["hello world"])
    b = p.embed(["hello world"])
    assert a == b                                  # deterministic
    assert len(a[0]) == 64
    assert abs(math.sqrt(sum(x * x for x in a[0])) - 1.0) < 1e-6
    assert p.embed(["different text"])[0] != a[0]
    assert p.embed_query("x") == p.embed(["x"])[0]


def test_echo_chat_includes_message_and_notes_context():
    out = "".join(EchoChatProvider().stream(
        [{"role": "user", "content": "hi there"}], system="Context: stuff"))
    assert "hi there" in out and "+context" in out


def test_factory_defaults_to_hash_and_resolves_echo(monkeypatch):
    assert make_embedding_provider(Config()).__class__.__name__ == "HashEmbeddingProvider"
    monkeypatch.setenv("CSAI_CHAT_PROVIDER", "echo")
    assert make_chat_provider(Config()).__class__.__name__ == "EchoChatProvider"


def test_factory_rejects_unknown(monkeypatch):
    monkeypatch.setenv("CSAI_EMBEDDING_PROVIDER", "bogus")
    with pytest.raises(ValueError):
        make_embedding_provider(Config())

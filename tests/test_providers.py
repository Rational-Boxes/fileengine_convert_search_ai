"""Unit tests for pluggable AI providers (offline implementations + factory)."""
import math

import pytest

import convert_search_ai.providers.chat as chat_mod
from convert_search_ai.config import Config
from convert_search_ai.providers.chat import AnthropicChatProvider, EchoChatProvider
from convert_search_ai.providers.embeddings import HashEmbeddingProvider
from convert_search_ai.providers.factory import make_chat_provider, make_embedding_provider


def test_missing_provider_sdk_raises_actionable_error(monkeypatch):
    # A bare "No module named anthropic" is replaced by an actionable message that
    # names the package AND flags the likely unloaded-.env cause for the default.
    def boom(name):
        raise ImportError(f"No module named {name!r}")
    monkeypatch.setattr(chat_mod.importlib, "import_module", boom)
    with pytest.raises(RuntimeError) as ei:
        list(AnthropicChatProvider().stream([{"role": "user", "content": "hi"}]))
    msg = str(ei.value)
    assert "anthropic" in msg and "pip install" in msg and "CSAI_CHAT_PROVIDER" in msg


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


def test_openai_compatible_chat_uses_base_url(monkeypatch):
    monkeypatch.setenv("CSAI_CHAT_PROVIDER", "openai")
    monkeypatch.setenv("CSAI_CHAT_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("CSAI_CHAT_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("CSAI_CHAT_API_KEY", "sk-test")
    p = make_chat_provider(Config())
    assert p.__class__.__name__ == "OpenAICompatibleChatProvider"
    assert p.model_id == "gpt-4o-mini" and p.base_url == "https://api.example.com/v1"
    assert p._key == "sk-test"
    # The token budget must be wired from config — a low default truncates the
    # create_document tool's HTML argument and the report fails to save.
    assert p.max_tokens == 8192                       # generous default
    monkeypatch.setenv("CSAI_CHAT_MAX_TOKENS", "5000")
    assert make_chat_provider(Config()).max_tokens == 5000


def test_ollama_chat_defaults_base_url_and_needs_no_key(monkeypatch):
    monkeypatch.setenv("CSAI_CHAT_PROVIDER", "ollama")
    monkeypatch.setenv("CSAI_CHAT_MODEL", "llama3.2")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = make_chat_provider(Config())
    assert p.base_url == "http://localhost:11434/v1" and p.model_id == "llama3.2"
    assert p._key == "not-needed"


def test_ollama_embeddings_default_base_url(monkeypatch):
    monkeypatch.setenv("CSAI_EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("CSAI_EMBEDDING_MODEL", "nomic-embed-text")
    p = make_embedding_provider(Config())
    assert p.__class__.__name__ == "OpenAIEmbeddingProvider"
    assert p.base_url == "http://localhost:11434/v1" and p.model_id == "nomic-embed-text"
    assert p.send_dimensions is False


def test_openai_embeddings_base_url_and_dimensions(monkeypatch):
    monkeypatch.setenv("CSAI_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("CSAI_EMBEDDING_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("CSAI_EMBEDDING_SEND_DIMENSIONS", "true")
    monkeypatch.setenv("CSAI_EMBEDDING_DIMENSION", "1024")
    p = make_embedding_provider(Config())
    assert p.base_url == "https://api.example.com/v1"
    assert p.send_dimensions is True and p.dimension == 1024

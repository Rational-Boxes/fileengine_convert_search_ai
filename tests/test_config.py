"""Unit tests for Config — run anywhere, no services needed."""
import importlib

from convert_search_ai.config import _strip_value


def test_strip_value_handles_inline_comments_and_quotes():
    # .env.example documents values with inline `# ...` comments; the loader must
    # not fold them into the value (regression: CSAI_EMBEDDING_DIMENSION=768 # ...).
    assert _strip_value("768          # MUST match the model's dim") == "768"
    assert _strip_value("https://api.deepinfra.com/v1/openai   # base url") == \
        "https://api.deepinfra.com/v1/openai"
    assert _strip_value("plain") == "plain"
    assert _strip_value('"quoted # not a comment"') == "quoted # not a comment"
    assert _strip_value("sha256#abc") == "sha256#abc"   # '#' without leading space kept


def _fresh_config(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg_mod = importlib.import_module("convert_search_ai.config")
    return cfg_mod.Config()


def test_defaults(monkeypatch):
    # Clear anything that would override the documented defaults.
    for k in ("FILEENGINE_GRPC_HOST", "FILEENGINE_GRPC_PORT", "CSAI_HTTP_PORT",
              "CSAI_PERMISSION_CACHE_TTL", "FILEENGINE_EVENTS_STREAM"):
        monkeypatch.delenv(k, raising=False)
    cfg = _fresh_config(monkeypatch)
    assert cfg.grpc_address == "localhost:50051"
    assert cfg.http_port == 8092
    assert cfg.permission_cache_ttl == 300            # <= 5 min per spec
    assert cfg.events_stream == "fileengine:events"   # matches the core publisher default
    assert cfg.chat_provider == "anthropic"


def test_env_override(monkeypatch):
    cfg = _fresh_config(
        monkeypatch,
        FILEENGINE_GRPC_HOST="core.internal",
        FILEENGINE_GRPC_PORT="6000",
        CSAI_PERMISSION_CACHE_TTL="60",
    )
    assert cfg.grpc_address == "core.internal:6000"
    assert cfg.permission_cache_ttl == 60


def test_redis_legacy_alias(monkeypatch):
    # Canonical FILEENGINE_REDIS_* wins over the legacy REDDIS_* alias.
    monkeypatch.setenv("REDDIS_HOST", "legacy-host")
    monkeypatch.setenv("FILEENGINE_REDIS_HOST", "canonical-host")
    cfg = _fresh_config(monkeypatch)
    assert cfg.redis_host == "canonical-host"


def test_pg_dsn(monkeypatch):
    cfg = _fresh_config(monkeypatch, CSAI_PG_DATABASE="csai_test", CSAI_PG_PORT="5434")
    assert "dbname=csai_test" in cfg.pg_dsn
    assert "port=5434" in cfg.pg_dsn

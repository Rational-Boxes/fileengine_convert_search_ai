"""Configuration for convert_search_ai, read from the environment.

A ``.env`` in the working directory is loaded automatically (without overriding
values already set in the environment), mirroring the FileEngine MCP server.
The ``FILEENGINE_*`` names are shared with the core / bridges / mcp; service-
specific knobs use the ``CSAI_*`` prefix."""
import os


def load_dotenv(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _first(*keys_and_default: str) -> str:
    """First non-empty env value among the given keys; last arg is the default."""
    *keys, default = keys_and_default
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    return default


def _bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self) -> None:
        # --- gRPC core (shared with the bridges / mcp) ---
        self.grpc_host = _env("FILEENGINE_GRPC_HOST", "localhost")
        self.grpc_port = _env("FILEENGINE_GRPC_PORT", "50051")
        self.grpc_address = f"{self.grpc_host}:{self.grpc_port}"

        # --- Tenant + the service's own agent identity ---
        # The agent identity is used only for indexing / rendition writes.
        # Retrieval is always evaluated as the *end user* — never this account.
        self.tenant = _env("FILEENGINE_CSAI_TENANT", "default")
        self.agent_user = _first("FILEENGINE_CSAI_USER", "FILEENGINE_LDAP_USER", "")
        self.agent_password = _first("FILEENGINE_CSAI_PASSWORD", "FILEENGINE_LDAP_PASSWORD", "")

        # --- LDAP — the auth/role authority (mirrors mcp + the bridges) ---
        self.ldap_uri = _env("FILEENGINE_LDAP_ENDPOINT", "ldap://localhost:1389")
        self.ldap_domain = _env("FILEENGINE_LDAP_DOMAIN", "dc=rationalboxes,dc=com")
        self.ldap_user_base = _env("FILEENGINE_LDAP_USER_BASE", "ou=users,dc=rationalboxes,dc=com")
        self.ldap_tenant_base = _env("FILEENGINE_LDAP_TENANT_BASE", "ou=tenants,dc=rationalboxes,dc=com")
        self.ldap_bind_dn = _env("FILEENGINE_LDAP_BIND_DN", "cn=admin,dc=rationalboxes,dc=com")
        self.ldap_bind_password = _env("FILEENGINE_LDAP_BIND_PASSWORD", "admin")

        # --- This service's own Postgres (its own DB; per-tenant partitioned) ---
        self.pg_host = _env("CSAI_PG_HOST", "localhost")
        self.pg_port = int(_env("CSAI_PG_PORT", "5432"))
        self.pg_database = _env("CSAI_PG_DATABASE", "convert_search_ai")
        self.pg_user = _env("CSAI_PG_USER", "fileengine_user")
        self.pg_password = _env("CSAI_PG_PASSWORD", "fileengine_password")

        # --- Event ingestion — consumes the core publisher's stream (EVENT_CONTRACT.md) ---
        self.redis_host = _first("FILEENGINE_REDIS_HOST", "REDDIS_HOST", "localhost")
        self.redis_port = int(_first("FILEENGINE_REDIS_PORT", "REDDIS_PORT", "6379"))
        self.redis_password = _first("FILEENGINE_REDIS_PASSWORD", "REDDIS_PASSWORD", "")
        self.redis_db = int(_first("FILEENGINE_REDIS_DB", "REDDIS_DB", "0"))
        self.events_stream = _env("FILEENGINE_EVENTS_STREAM", "fileengine:events")
        self.events_group = _env("CSAI_EVENTS_GROUP", "convert_search_ai")

        # --- HTTP / WebSocket surface ---
        self.http_host = _env("CSAI_HTTP_HOST", "127.0.0.1")
        self.http_port = int(_env("CSAI_HTTP_PORT", "8092"))
        # Browser CORS: comma-separated allowed origins for a SPA on another
        # origin (e.g. the FileEngine frontend). Empty = CORS disabled (no
        # browser cross-origin access). Mirrors the bridge's HTTP_CORS_ORIGIN.
        self.cors_origins = [
            o.strip() for o in _env("CSAI_CORS_ORIGINS", "").split(",") if o.strip()
        ]

        # --- Permission cache (DEVELOPMENT_PLAN §8): cap decisions to this many seconds ---
        self.permission_cache_ttl = int(_env("CSAI_PERMISSION_CACHE_TTL", "300"))

        # --- Bearer-token TTL for the /auth/token HTTP path ---
        self.token_ttl = int(_env("CSAI_TOKEN_TTL", "3600"))

        # --- Guardrails (production hardening) ---
        self.max_query_chars = int(_env("CSAI_MAX_QUERY_CHARS", "1000"))
        self.max_results = int(_env("CSAI_MAX_RESULTS", "100"))           # search hits cap
        self.max_chat_k = int(_env("CSAI_MAX_CHAT_K", "12"))              # RAG retrieval depth cap
        self.max_context_chars = int(_env("CSAI_MAX_CONTEXT_CHARS", "12000"))
        self.max_text_bytes = int(_env("CSAI_MAX_TEXT_BYTES", str(5 * 1024 * 1024)))
        self.db_statement_timeout_ms = int(_env("CSAI_DB_STATEMENT_TIMEOUT_MS", "5000"))
        self.audit_log_file = _env("CSAI_AUDIT_LOG_FILE", "")            # empty -> stderr

        # --- Pluggable AI providers (DEVELOPMENT_PLAN §7; chosen at deploy time) ---
        # Embeddings: hash (offline default) | voyage | openai | ollama | openai-compatible.
        # The openai/ollama/openai-compatible providers speak the OpenAI API, so any
        # OpenAI-compatible endpoint (OpenAI, Ollama, vLLM, …) works via *_BASE_URL.
        self.embedding_provider = _env("CSAI_EMBEDDING_PROVIDER", "")
        self.embedding_model = _env("CSAI_EMBEDDING_MODEL", "")
        self.embedding_dimension = int(_env("CSAI_EMBEDDING_DIMENSION", "1024"))
        self.embedding_base_url = _env("CSAI_EMBEDDING_BASE_URL", "")
        self.embedding_api_key = _first("CSAI_EMBEDDING_API_KEY", "OPENAI_API_KEY", "")
        # Only OpenAI-native models accept the `dimensions` param; off by default for
        # compatibility (Ollama etc. produce their model's native dimension).
        self.embedding_send_dimensions = _bool("CSAI_EMBEDDING_SEND_DIMENSIONS", False)

        # Chat: anthropic (default) | openai | ollama | openai-compatible | echo.
        self.chat_provider = _env("CSAI_CHAT_PROVIDER", "anthropic")
        self.chat_model = _env("CSAI_CHAT_MODEL", "claude-sonnet-4-6")
        self.chat_base_url = _env("CSAI_CHAT_BASE_URL", "")
        self.chat_api_key = _first("CSAI_CHAT_API_KEY", "OPENAI_API_KEY", "")

        # --- Web search tool (WEB_SEARCH_TOOL_PLAN; OFF by default) ---
        # The chat web_search tool. `enabled` is the master switch (off by default —
        # a search sends the query to a third party, see plan §9); `provider`
        # chooses the backend (DuckDuckGo by default, no API key). The tool-loop
        # wiring lands in P2; P1 ships the backend + tool only.
        self.web_search_provider = _env("CSAI_WEB_SEARCH_PROVIDER", "duckduckgo")
        self.web_search_enabled = _bool("CSAI_WEB_SEARCH_ENABLED", False)
        self.web_search_default = _bool("CSAI_WEB_SEARCH_DEFAULT", False)
        self.web_search_results = int(_env("CSAI_WEB_SEARCH_RESULTS", "5"))
        self.web_max_iterations = int(_env("CSAI_WEB_MAX_ITERATIONS", "3"))
        self.web_max_chars = int(_env("CSAI_WEB_MAX_CHARS", "4000"))
        self.web_timeout_ms = int(_env("CSAI_WEB_TIMEOUT_MS", "4000"))
        self.web_region = _env("CSAI_WEB_REGION", "wt-wt")
        self.web_safesearch = _env("CSAI_WEB_SAFESEARCH", "moderate")
        self.web_timelimit = _env("CSAI_WEB_TIMELIMIT", "")  # "" | d | w | m | y

        # PDF/Office → Markdown extraction backends, fidelity-ordered (the first
        # one installed AND yielding content wins; see plugins/pdf_backends).
        # Structure + tables are critical — pdftotext is only the last resort.
        self.pdf_backends = [
            b.strip() for b in
            _env("CSAI_PDF_BACKENDS", "docling,pymupdf4llm,pdfplumber,pdftotext").split(",")
            if b.strip()
        ]

        # Page-1 preview image sizes (longest edge, px) for document types
        # (PDF + Office): an icon-sized thumbnail and a larger inline preview.
        # Aligned with the image plugin's thumbnail/preview boxes.
        self.doc_thumbnail_px = int(_env("CSAI_DOC_THUMBNAIL_PX", "256"))
        self.doc_preview_px = int(_env("CSAI_DOC_PREVIEW_PX", "1280"))

        # Source/text preview: Pygments style for the colour-coded first-page PDF,
        # and how many leading lines to render (the rest is clipped — it's a
        # preview, the full text is still extracted for search).
        self.code_preview_style = _env("CSAI_CODE_PREVIEW_STYLE", "default")
        self.code_preview_head_lines = int(_env("CSAI_CODE_PREVIEW_HEAD_LINES", "120"))

        # --- Auth coordination with the core REST API (http_bridge) ---
        # When set, a bearer token issued by the bridge is accepted here too:
        # this service introspects it against the bridge's /v1/auth/introspect,
        # so one login (LDAP or OAuth, at the bridge) authenticates across both
        # services. Empty disables coordination (only this service's own
        # /auth/token bearer tokens + Basic auth are accepted).
        self.bridge_url = _env("CSAI_BRIDGE_URL", "").rstrip("/")
        self.bridge_introspect_ttl = int(_env("CSAI_BRIDGE_INTROSPECT_TTL", "60"))

    @property
    def pg_dsn(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.pg_database} "
            f"user={self.pg_user} password={self.pg_password}"
        )

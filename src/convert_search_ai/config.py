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

        # --- Permission cache (DEVELOPMENT_PLAN §8): cap decisions to this many seconds ---
        self.permission_cache_ttl = int(_env("CSAI_PERMISSION_CACHE_TTL", "300"))

        # --- Pluggable AI providers (DEVELOPMENT_PLAN §7; chosen at deploy time) ---
        self.embedding_provider = _env("CSAI_EMBEDDING_PROVIDER", "")   # e.g. voyage|openai|local
        self.embedding_model = _env("CSAI_EMBEDDING_MODEL", "")
        self.embedding_dimension = int(_env("CSAI_EMBEDDING_DIMENSION", "1024"))
        self.chat_provider = _env("CSAI_CHAT_PROVIDER", "anthropic")
        self.chat_model = _env("CSAI_CHAT_MODEL", "claude-sonnet-4-6")

        # PDF/Office → Markdown extraction backends, fidelity-ordered (the first
        # one installed AND yielding content wins; see plugins/pdf_backends).
        # Structure + tables are critical — pdftotext is only the last resort.
        self.pdf_backends = [
            b.strip() for b in
            _env("CSAI_PDF_BACKENDS", "docling,pymupdf4llm,pdfplumber,pdftotext").split(",")
            if b.strip()
        ]

    @property
    def pg_dsn(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.pg_database} "
            f"user={self.pg_user} password={self.pg_password}"
        )

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
            os.environ.setdefault(key.strip(), _strip_value(val))


def _strip_value(val: str) -> str:
    """Parse a dotenv value: honor a surrounding quote, else drop an inline
    `` # …`` comment (the .env.example template documents values inline).

    A value that is *entirely* a comment (``KEY=# note``) yields an empty string —
    otherwise the comment text becomes the value (e.g. ``CSAI_AUDIT_LOG_FILE=#
    empty -> stderr`` would be taken as a literal file path)."""
    val = val.strip()
    if val[:1] in ("'", '"'):
        q = val[0]
        end = val.find(q, 1)
        return val[1:end] if end != -1 else val[1:]
    if val.startswith("#"):           # whole value is a comment -> empty
        return ""
    hi = val.find(" #")
    if hi != -1:
        val = val[:hi]
    return val.strip()


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
        # Indexing must see ALL content so the vector index is complete; per-user
        # ACLs are enforced later, at retrieval time (PermissionGate). The core
        # grants this via the trusted ``system_admin`` role bypass, attached to the
        # indexing agent only. Disable only if a deployment deliberately wants
        # index-time ACL filtering (denied content then never enters the index, so
        # it is invisible to every user regardless of their own permissions).
        self.index_bypass_acl = _bool("CSAI_INDEX_BYPASS_ACL", True)

        # --- LDAP — the auth/role authority (mirrors mcp + the bridges) ---
        self.ldap_uri = _env("FILEENGINE_LDAP_ENDPOINT", "ldap://localhost:1389")
        # Read-only replica directory for failover (REPLICATION_FAILOVER.md). When the
        # master directory is unreachable, auth falls back to this replica. Failover is
        # OFF unless set (or FILEENGINE_LDAP_REPLICA_ENABLED -> ldap://localhost:1389).
        self.ldap_uri_replica = _env("FILEENGINE_LDAP_ENDPOINT_REPLICA", "")
        if not self.ldap_uri_replica and _bool("FILEENGINE_LDAP_REPLICA_ENABLED", False):
            self.ldap_uri_replica = "ldap://localhost:1389"
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

        # --- Read-only replica (disconnect fault tolerance; REPLICATION_FAILOVER.md) ---
        # The master (above) is the primary for all reads + writes. When it is
        # unreachable, reads fall back to this on-prem replica (read-only) and writes
        # are rejected. Failover is OFF unless a replica host is configured; the host
        # defaults to localhost when enabled. Replica creds default to the master's.
        self.pg_replica_host = _env("CSAI_PG_REPLICA_HOST", "")
        if not self.pg_replica_host and _bool("CSAI_PG_REPLICA_ENABLED", False):
            self.pg_replica_host = "localhost"
        self.pg_replica_port = int(_env("CSAI_PG_REPLICA_PORT", str(self.pg_port)))
        self.pg_replica_database = _env("CSAI_PG_REPLICA_DATABASE", self.pg_database)
        self.pg_replica_user = _env("CSAI_PG_REPLICA_USER", self.pg_user)
        self.pg_replica_password = _env("CSAI_PG_REPLICA_PASSWORD", self.pg_password)
        # Circuit-breaker cooldown before the primary is re-probed (seconds).
        self.failover_cooldown_s = int(_env("CSAI_FAILOVER_COOLDOWN_S", "30"))
        # Sleep/poll back-off when the core is read-only (writes rejected during a
        # primary-DB failover): the ingest worker pauses this long between retries
        # of its un-acked events instead of dropping them. See ingest.py.
        self.failover_poll_interval_s = float(_env("CSAI_FAILOVER_POLL_INTERVAL_S", "5"))

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

        # Public base URL of the SPA, used to build ABSOLUTE file deep-links baked
        # into generated report HTML (and the chat provenance log) so they still
        # resolve when the report is rendered to PDF or copied to an externally hosted
        # site — a relative "/files?…" would not. MULTI-TENANT: put a {tenant}
        # placeholder in the value (e.g. https://{tenant}.example.com) and each
        # report links to its OWN tenant's host, since tenants get unique URLs; a
        # plain origin instead carries the tenant only in the ?tenant= query. Empty ⇒
        # relative links (dev). NB: no CORS-origin fallback — that is a single global
        # origin, which would mislink every tenant to one host.
        self.public_app_url = _env("CSAI_PUBLIC_APP_URL", "").rstrip("/")

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
        # Max output tokens per completion. Must be generous: a saved report is
        # streamed inline (wrapped in SAVE_REPORT markers), so a small cap truncates
        # the report before its closing marker. 1024 was far too low for reports.
        self.chat_max_tokens = int(_env("CSAI_CHAT_MAX_TOKENS", "8192"))

        # --- Web search tool (WEB_SEARCH_TOOL_PLAN; OFF by default) ---
        # The chat web_search tool. `enabled` is the master switch (off by default —
        # a search sends the query to a third party, see plan §9); `provider`
        # chooses the backend (DuckDuckGo by default, no API key). The tool-loop
        # wiring lands in P2; P1 ships the backend + tool only.
        self.web_search_provider = _env("CSAI_WEB_SEARCH_PROVIDER", "duckduckgo")
        self.web_search_enabled = _bool("CSAI_WEB_SEARCH_ENABLED", False)
        self.web_search_default = _bool("CSAI_WEB_SEARCH_DEFAULT", False)
        self.web_search_results = int(_env("CSAI_WEB_SEARCH_RESULTS", "5"))
        # Cap on tool-loop rounds per answer. Governs ALL tools, not just web
        # search — incl. list_folders. Must be generous: a report workflow explores
        # folders (and may web-research) before streaming the report, and once the
        # cap is hit the loop forces a final answer. Too low and the model runs out
        # of rounds before producing the report.
        self.web_max_iterations = int(_first("CSAI_TOOL_MAX_ITERATIONS",
                                             "CSAI_WEB_MAX_ITERATIONS", "8"))
        self.web_max_chars = int(_env("CSAI_WEB_MAX_CHARS", "4000"))
        self.web_timeout_ms = int(_env("CSAI_WEB_TIMEOUT_MS", "4000"))
        self.web_region = _env("CSAI_WEB_REGION", "wt-wt")
        self.web_safesearch = _env("CSAI_WEB_SAFESEARCH", "moderate")
        self.web_timelimit = _env("CSAI_WEB_TIMELIMIT", "")  # "" | d | w | m | y
        # Optional fetch_page tool (read full page text; SSRF-guarded). Off by
        # default and only added when web search is also enabled.
        self.web_fetch_pages = _bool("CSAI_WEB_FETCH_PAGES", False)
        self.web_fetch_max_bytes = int(_env("CSAI_WEB_FETCH_MAX_BYTES", str(2 * 1024 * 1024)))

        # Document save feature: the model saves a report from the conversation as
        # an HTML document in the user's own storage (written as the user, so ACLs
        # apply; PDF rendition produced by the HTML→PDF converter). Saving is driven
        # by SAVE_REPORT stream markers (deterministic) plus a folder-exploration
        # tool — there is no save tool the model can misfire. On by default; caps
        # the saved report size.
        self.chat_document_tool_enabled = _bool("CSAI_CHAT_DOCUMENT_TOOL", True)
        self.chat_document_max_bytes = int(_env("CSAI_CHAT_DOCUMENT_MAX_BYTES", str(5 * 1024 * 1024)))
        # document_search / get_document_text tools (RAG + direct interrogation).
        self.chat_doc_search_limit = int(_env("CSAI_CHAT_DOC_SEARCH_LIMIT", "10"))
        self.chat_doc_text_window = int(_env("CSAI_CHAT_DOC_TEXT_WINDOW", "4000"))
        self.chat_doc_text_max_window = int(_env("CSAI_CHAT_DOC_TEXT_MAX_WINDOW", "20000"))

        # --- Tenant-managed MCP integrations (MCP_INTEGRATIONS.md; OFF by default) ---
        # A tenant admin registers external MCP servers whose tools the chat model may
        # call (with per-call user consent). `enabled` is the master switch; `secret_key`
        # (Fernet) is REQUIRED when enabled — it encrypts each integration's credential
        # at rest. stdio transport is never tenant-settable (arbitrary code execution);
        # `allow_stdio` only permits a deployment's own system-config servers (P3).
        self.mcp_enabled = _bool("CSAI_MCP_ENABLED", False)
        self.mcp_allow_stdio = _bool("CSAI_MCP_ALLOW_STDIO", False)
        self.mcp_max_integrations = int(_env("CSAI_MCP_MAX_INTEGRATIONS", "10"))
        self.mcp_tool_timeout_ms = int(_env("CSAI_MCP_TOOL_TIMEOUT_MS", "15000"))
        self.mcp_max_tool_output_chars = int(_env("CSAI_MCP_MAX_TOOL_OUTPUT_CHARS", "8000"))
        self.mcp_max_tools_per_integration = int(_env("CSAI_MCP_MAX_TOOLS", "50"))
        self.mcp_connect_cache_ttl = int(_env("CSAI_MCP_CONNECT_CACHE_TTL", "300"))
        # A tool call blocks this long for the user's approve/deny reply, then denies.
        self.mcp_consent_timeout_ms = int(_env("CSAI_MCP_CONSENT_TIMEOUT_MS", "120000"))
        self.mcp_secret_key = _env("CSAI_MCP_SECRET_KEY", "")
        # Signs the minimal user-claim header sent to an integration whose
        # `forward_identity` is on (opt-in, §7). Defaults to the shared bridge secret.
        self.mcp_identity_secret = _first("CSAI_MCP_IDENTITY_SECRET", "FILEENGINE_JWT_SECRET", "")
        self.mcp_identity_ttl = int(_env("CSAI_MCP_IDENTITY_TTL", "120"))

        # --- ONLYOFFICE in-browser editing (Phase 1.7 consumer; OFF by default) ---
        # Edit office documents in an embedded ONLYOFFICE Document Server; every save
        # writes back through the immutable versioned store as the *impersonated user*.
        self.onlyoffice_enabled = _bool("CSAI_ONLYOFFICE_ENABLED", False)
        # Public URL the browser loads the Document Server's api.js from (e.g.
        # http://localhost:8080, or an https tunnel for a remote SPA).
        self.onlyoffice_docserver_url = _env("CSAI_ONLYOFFICE_DOCSERVER_URL", "").rstrip("/")
        # Shared secret for the Document Server's JWT contract (config + callback).
        # Required when enabled — the Doc Server rejects an unsigned config with JWT on.
        self.onlyoffice_jwt_secret = _env("CSAI_ONLYOFFICE_JWT_SECRET", "")
        # Base URL the Document Server uses to reach CSAI for document download +
        # save callback. MUST be reachable *from inside the Doc Server container*
        # (host.docker.internal / a tunnel), NOT necessarily the same as the SPA's
        # origin. Empty ⇒ derive from the incoming request base URL (dev fallback).
        self.onlyoffice_callback_base = _env("CSAI_ONLYOFFICE_CALLBACK_BASE", "").rstrip("/")
        # Signs the scoped download/callback tokens that bind the impersonated user
        # to the editing session. Defaults to the shared bridge secret.
        self.onlyoffice_signing_secret = _first("CSAI_ONLYOFFICE_SIGNING_SECRET",
                                                "FILEENGINE_JWT_SECRET", "")
        # Scoped-token lifetime — must outlast a long editing session (12h default),
        # since the Doc Server may fire the save callback hours after config issuance.
        self.onlyoffice_session_ttl = int(_env("CSAI_ONLYOFFICE_SESSION_TTL", "43200"))
        # Cap on a written-back document (bytes).
        self.onlyoffice_max_bytes = int(_env("CSAI_ONLYOFFICE_MAX_BYTES", str(100 * 1024 * 1024)))

        # HTML → PDF conversion (for .html documents, incl. chat-generated reports).
        # Chromium headless gives full-CSS fidelity; LibreOffice is the fallback.
        self.html_chromium = _env("CSAI_HTML_CHROMIUM", "chromium-browser")
        self.html_pdf_timeout_s = int(_env("CSAI_HTML_PDF_TIMEOUT_S", "60"))

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
        # Max source lines rendered into the (now full-document, paginated) source/
        # markdown-fallback PDF. 0 = the entire file; set a positive cap only as a
        # safety valve for pathologically large files.
        self.code_preview_max_lines = int(_env("CSAI_CODE_PREVIEW_MAX_LINES", "0"))

        # --- 3D / BIM conversion + indexing (XEOKIT3D_PLUGIN design doc) ---
        # Convert open 3D/AEC formats (IFC, glTF/GLB, CityJSON, LAS/LAZ, STL, PLY)
        # to xeokit's XKT model rendition, and index every human-readable string
        # for search. Geometry needs Node + convert2xkt; IfcOpenShell/CxConverter
        # are optional, auto-detected, higher-fidelity IFC backends.
        self.threed_enabled = _bool("CSAI_3D_ENABLED", True)
        # auto = detect best installed IFC backend (cxconverter -> ifcopenshell ->
        # native web-ifc); or pin one / a comma-list: cxconverter|ifcopenshell|webifc.
        self.threed_ifc_backend = _env("CSAI_3D_IFC_BACKEND", "auto")
        self.threed_convert2xkt = _env("CSAI_3D_CONVERT2XKT", "convert2xkt")
        self.threed_ifcconvert = _env("CSAI_3D_IFCCONVERT", "ifcConvert")
        self.threed_cxconverter = _env("CSAI_3D_CXCONVERTER", "")  # path enables it
        self.threed_max_input_mb = int(_env("CSAI_3D_MAX_INPUT_MB", "512"))
        self.threed_timeout_s = int(_env("CSAI_3D_TIMEOUT_S", "600"))
        self.threed_extract_only = _bool("CSAI_3D_EXTRACT_ONLY", False)
        # CAD geometry backend (OpenCASCADE "DRAW" CLI). True-CAD/mesh formats —
        # STEP, IGES, BREP, OBJ, VRML — are read by DRAWEXE, tessellated, written
        # to glTF, then chained through convert2xkt → XKT (same final hop as the
        # other formats). When DRAWEXE is absent these are text-indexed only.
        self.threed_drawexe = _env("CSAI_3D_DRAWEXE", "DRAWEXE")
        # BRep tessellation quality for exact-geometry formats (STEP/IGES/BREP):
        # linear deflection is relative (fraction of each shape's bounding box) so
        # one value suits models of any scale; angular deflection is in degrees.
        self.threed_cad_deflection = _env("CSAI_3D_CAD_DEFLECTION", "0.001")
        self.threed_cad_angle = _env("CSAI_3D_CAD_ANGLE", "20")
        # STEP/IGES parts are routinely defined far from the world origin, which
        # leaves the xeokit camera framing empty space (geometry off-screen). Bake
        # a translation that moves the model's bounding-box centre to the origin so
        # the viewer gets a sane default view (also improves float precision).
        self.threed_cad_recenter = _bool("CSAI_3D_CAD_RECENTER", True)

        # --- Auth coordination with the core REST API (http_bridge) ---
        # When set, a bearer token issued by the bridge is accepted here too:
        # this service introspects it against the bridge's /v1/auth/introspect,
        # so one login (LDAP or OAuth, at the bridge) authenticates across both
        # services. Empty disables coordination (only this service's own
        # /auth/token bearer tokens + Basic auth are accepted).
        self.bridge_url = _env("CSAI_BRIDGE_URL", "").rstrip("/")
        self.bridge_introspect_ttl = int(_env("CSAI_BRIDGE_INTROSPECT_TTL", "60"))
        # Shared HS256 secret to verify the bridge's bearer JWTs locally (no
        # introspection round-trip). Same value the bridge signs with.
        self.jwt_secret = _env("FILEENGINE_JWT_SECRET", "")

    @property
    def pg_dsn(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.pg_database} "
            f"user={self.pg_user} password={self.pg_password}"
        )

    @property
    def pg_replica_enabled(self) -> bool:
        """Postgres read-only failover is active only when a replica is configured."""
        return bool(self.pg_replica_host)

    @property
    def pg_replica_dsn(self) -> str:
        return (
            f"host={self.pg_replica_host} port={self.pg_replica_port} "
            f"dbname={self.pg_replica_database} "
            f"user={self.pg_replica_user} password={self.pg_replica_password}"
        )

    @property
    def ldap_replica_enabled(self) -> bool:
        return bool(self.ldap_uri_replica)

# convert_search_ai — Tenant-Managed MCP Integrations for Chat

Status: **Design / proposal (not implemented).** This document specifies adding
**Model Context Protocol (MCP) client** support to the CSAI chat backend so a
tenant can equip the assistant with external tools, plus the **tenant-admin
management interface** to add and govern those integrations. It extends
[`CHAT_WITH_AI.md`](./CHAT_WITH_AI.md) (§2.3 Agentic tools) and reuses the tool loop
from [`WEB_SEARCH_TOOL_PLAN.md`](./WEB_SEARCH_TOOL_PLAN.md). Companion:
[`SPECIFICATION.md`](./SPECIFICATION.md), [`EVENT_CONTRACT.md`](./EVENT_CONTRACT.md).

---

## 1. Goal

Let a **tenant administrator** register external **MCP servers** (e.g. a ticketing
system, a CRM, an internal knowledge API) so the AI Research Chat can call their
tools mid-conversation — the same agentic loop that already runs `document_search`,
`get_document_text`, `list_folders`, and `web_search`, extended with tools
**discovered dynamically from each tenant's MCP servers**. Two parts:

1. **CSAI as an MCP client** (`chat.py` / `llm_tools.py` / a new `mcp_client.py`):
   connect to a tenant's enabled MCP servers, discover their tools, wrap each as a
   CSAI `Tool`, and expose them to the model in the tool loop.
2. **A management interface** (new CSAI admin API + frontend admin view): a
   tenant admin adds, edits, tests, enables/disables, and removes MCP integrations,
   scoped to their tenant.

```
  Tenant admin ──HTTPS──►  CSAI  /v1/admin/mcp-integrations   (CRUD, test, enable)
                                   │  store (per-tenant, secrets encrypted)
  end user (chat) ─WS /chat─► ChatService.answer ─► tool loop
                                   │  build_tools(...) + MCP tools discovered from
                                   │  the tenant's enabled integrations (cached)
                                   ▼
                          MCP client ──Streamable-HTTP──►  Tenant's MCP server(s)
                                        (auth = the integration's stored credential)
```

---

## 2. What we build on

- **Tool contract** (`llm_tools.py`): a `Tool` is `name` + `description` + JSON
  `schema` + `run(args, ctx) -> ToolOutput`. `build_tools(config, *, include_web,
  search)` returns the enabled tools; `ChatService._select_tools` chooses them per
  turn and the provider loop (`providers/chat.py::run_tools`, Anthropic/OpenAI)
  invokes them. MCP tools slot in as additional `Tool`s — no loop changes.
- **Per-turn context** (`ToolContext`): `identity`, `config`, plus a `sources`
  accumulator. MCP tool output can add citations here if it references documents.
- **Per-tenant isolation** (`schema.py`, `store.py`): tenant data lives in a
  Postgres schema (`tenant_<tenant>`, `search_path`-scoped). MCP integration config
  is a new per-tenant table (§4).
- **Identity + tenant-admin gating**: chat/admin requests carry a bridge-issued
  JWT (`api.py` `_identity`) with the caller's roles for the active tenant;
  `administrators` maps to `tenant_admin` (bridge H2). Admin endpoints gate on that
  role (mirrors ldap_manager's `require_tenant_admin` for `/v1/admin/*`).
- **The FileEngine MCP server** (`../mcp`, FastMCP over stdio + Streamable-HTTP) is
  our reference for MCP semantics; here CSAI is the **client**, not the server.
- **Secret-at-rest pattern**: ldap_manager encrypts TOTP secrets with Fernet; MCP
  credentials reuse that approach (§7).

**Gap:** the tool set is currently built-in and static. This adds a
tenant-configurable, dynamically-discovered tool source.

---

## 3. Design principles

1. **Tenant admin owns the integration; the assistant merely uses it.** An MCP tool
   runs with the **integration's stored credentials** (configured by the admin), not
   the end-user's core identity — the admin vouches for the server. The core's
   per-user ACLs do **not** gate external MCP calls (they are external systems), so
   this is an explicit trust boundary the admin accepts (§7). **Optionally**, per
   integration, a minimal end-user claim may be forwarded so the MCP server can do
   its own authorization — **opt-in, default off** (§4, §7).
2. **Every MCP tool call requires user consent.** Because MCP tools reach external
   systems and can have side effects, the assistant may **not** invoke one silently:
   the turn pauses, the user is asked to approve (or deny) the specific call, and the
   tool runs only on approval (§6). The user may "allow for this conversation" to
   avoid repeated prompts.
3. **Remote (HTTP) first; local (stdio) is deployment-only.** Tenant admins may add
   **Streamable-HTTP / SSE** MCP servers by URL. Spawning **stdio** subprocesses is
   arbitrary code execution on the CSAI host and is **NOT** available to tenant
   admins — only via system/deployment config (§7).
4. **Isolation + namespacing.** Integrations are tenant-scoped; a tenant's tools
   never appear in another tenant's chat. Every MCP tool is namespaced
   (`mcp__<integration_slug>__<tool>`) so it cannot shadow a built-in tool
   (`document_search`, …) or collide across integrations.
5. **Fail open on the chat, closed on the secret.** A down/misconfigured MCP server
   omits its tools and logs — it never breaks the chat. Secrets are encrypted at
   rest and never returned by the API.
6. **Observable + audited.** MCP tool calls surface as the existing `tool_call` /
   `tool_result` chat events (plus the consent prompt of §6); every call, consent
   decision, and admin config change is audited (content-free).

---

## 4. Data model — `mcp_integration` (per-tenant schema)

| Column | Type | Notes |
|---|---|---|
| `id` | uuid | primary key |
| `name` | text | admin-facing label; unique per tenant |
| `slug` | text | derived, url-safe; the tool-name namespace |
| `description` | text | optional |
| `transport` | text | `streamable-http` \| `sse` (stdio not tenant-settable) |
| `endpoint_url` | text | the MCP server URL (SSRF-checked, §7) |
| `auth_type` | text | `none` \| `bearer` \| `header` \| `oauth` |
| `secret_enc` | bytea | Fernet-encrypted token/secret; write-only via API |
| `headers` | jsonb | extra static headers (non-secret) |
| `enabled` | bool | off ⇒ not offered to the model |
| `allowed_tools` | jsonb | allowlist of tool names to expose; `null` = all discovered |
| `forward_identity` | bool | **default false**; when true, forward a minimal end-user claim (§7) so the server can authorize per-user |
| `created_by` / `created_at` / `updated_at` | text / ts | provenance |

Stored in the tenant's **existing** Postgres schema alongside `documents` /
`conversations` (`tenant_<tenant>.mcp_integration`) — the same DB section, not a new
store — so per-tenant isolation is automatic. Secrets are stored **only** encrypted;
`GET` responses omit `secret_enc` and expose a boolean `has_secret`. Consent is a
**runtime** decision (§6), not a column: MCP tool calls always prompt (the user may
remember the choice for the conversation).

---

## 5. Management API (CSAI, tenant-admin gated)

New router (`routers/mcp_admin.py`), all under `/v1/admin/mcp-integrations`, each
guarded by a `require_tenant_admin` dependency (rejects non-admins with 403; scopes
every query to `identity.tenant`):

| Method / path | Purpose |
|---|---|
| `GET  /v1/admin/mcp-integrations` | List the tenant's integrations (no secrets; `has_secret`, last-test status). |
| `POST /v1/admin/mcp-integrations` | Create — `{name, transport, endpoint_url, auth_type, secret?, headers?, allowed_tools?, enabled}`. |
| `GET  /v1/admin/mcp-integrations/{id}` | One integration (no secret). |
| `PUT  /v1/admin/mcp-integrations/{id}` | Update fields / rotate the secret / toggle `enabled`. |
| `DELETE /v1/admin/mcp-integrations/{id}` | Remove. |
| `POST /v1/admin/mcp-integrations/{id}/test` | **Connect + `list_tools()`** and return the discovered tool names/schemas (validation, no persistence) — powers the "Test connection" button. |

Validation on write: SSRF-check `endpoint_url` (§7), cap count at
`CSAI_MCP_MAX_INTEGRATIONS`, reject `transport=stdio`. Every mutation and every
`test` is audited (`action=mcp_admin`, content-free: which fields changed, not the
secret).

---

## 6. Chat integration (CSAI as MCP client)

New `mcp_client.py` wrapping the MCP Python SDK client:

- **Discovery / caching.** `McpToolProvider.tools_for(tenant)` loads the tenant's
  **enabled** integrations, connects to each (Streamable-HTTP), calls `list_tools()`,
  and returns a wrapped `Tool` per discovered (and `allowed_tools`-permitted) tool.
  Results are cached per `(tenant, integration, config-version)` with a TTL
  (`CSAI_MCP_CONNECT_CACHE_TTL`); a config change or `test` busts the cache. A server
  that errors/times out contributes **zero** tools and logs — never raises into the
  chat.
- **Wrapping.** Each MCP tool becomes a `Tool` with:
  - `name = f"mcp__{slug}__{tool_name}"` (collision-proof, §3),
  - `description` + `schema` copied from the MCP tool's `inputSchema`,
  - `run(args, ctx)` → **(a)** obtains user consent (below); on approval **(b)**
    opens/reuses a client session **authenticated with the integration's decrypted
    credential** (and, if the integration's `forward_identity` is set, a minimal
    end-user claim header — §7), calls `call_tool(tool_name, args)`, and returns the
    content as `ToolOutput.text` (truncated to `CSAI_MCP_MAX_TOOL_OUTPUT_CHARS`). A
    denied call returns a short "the user declined this action" result so the model
    can continue without it.
- **Consent flow (required, §3.2).** When the model calls an MCP tool, the tool loop
  **pauses** and CSAI emits a new `tool_consent_request` event
  (`{id, integration, tool, args_summary}`) over the WS. The SPA shows an approve /
  deny prompt (with "allow this tool for this conversation"); the client replies with
  a `tool_consent` message (`{id, decision, remember}`). The `run()` blocks on that
  reply up to `CSAI_MCP_CONSENT_TIMEOUT_MS`, then defaults to **deny**. A remembered
  approval is held in the turn/conversation state so the same tool isn't re-prompted.
  (New events are added to [`EVENT_CONTRACT.md`](./EVENT_CONTRACT.md).)
- **Wiring.** `build_tools(config, *, include_web, search, mcp=None)` appends the MCP
  tools when `config.mcp_enabled` and the tenant has enabled integrations;
  `ChatService._select_tools(identity)` resolves them per turn (identity carries the
  tenant). Report mode still offers **no** tools (unchanged).
- **System prompt.** When MCP tools are present, a short instruction lists the
  available external capabilities and reminds the model these are the tenant's
  configured integrations (analogous to `_INSTRUCTIONS_DOC_TOOLS`).
- **UX.** Existing `tool_call` / `tool_result` events already render a "using
  <tool>…" indicator; MCP tool names surface there. (A friendlier per-integration
  label is a follow-up.)

---

## 7. Security & correctness invariants

- **Trust boundary is explicit.** MCP tools execute against external systems with the
  **integration's** credentials — not the end user's core identity, and **not**
  gated by the core's per-user ACLs. Adding an integration means the tenant admin
  trusts that server for all of the tenant's chat users. The UI states this.
- **User consent is mandatory.** No MCP tool runs without an explicit per-call user
  approval (§6); a timeout or a closed socket defaults to deny. This is the primary
  guard against a prompt-injected model triggering an unwanted external side effect.
- **Identity forwarding is opt-in and minimal.** By default the end user's identity
  is **never** sent to an external MCP server. A tenant admin may set an
  integration's `forward_identity`, after which CSAI forwards only a **minimal**
  claim (e.g. the user's stable id/email + tenant) as a signed header — never core
  roles, tokens, or ACLs. Off by default; documented in the admin UI as a data-
  sharing choice.
- **No tenant-spawned processes.** `transport=stdio` is rejected by the admin API;
  subprocess MCP servers exist only if a deployment sets them in system config. This
  removes arbitrary-code-execution-by-tenant.
- **SSRF guard** on `endpoint_url` (reuse `webfetch.py`'s validator): HTTPS only,
  public IPs only, no internal/link-local/metadata hosts, redirects re-validated.
- **Secrets** are Fernet-encrypted at rest (`CSAI_MCP_SECRET_KEY`), never returned by
  the API (write-only; `GET` exposes only `has_secret`), and excluded from logs/audit.
- **Per-tenant isolation:** integrations live in the tenant schema; discovery/tools
  are keyed by `identity.tenant`; one tenant's tools can never reach another's chat.
- **Namespacing** prevents an MCP tool from shadowing a built-in (`document_search`,
  `list_folders`, `web_search`, `get_document_text`) or another integration.
- **Resource caps:** `CSAI_MCP_MAX_INTEGRATIONS` per tenant, per-tool-call timeout
  (`CSAI_MCP_TOOL_TIMEOUT_MS`), max discovered tools per integration, and output
  truncation (`CSAI_MCP_MAX_TOOL_OUTPUT_CHARS`) so a chatty tool can't blow the
  context or hang the turn.
- **Fail-safe:** an unreachable/erroring MCP server drops its tools and logs;
  the chat continues with the remaining tools.
- **Audit** (content-free): every MCP tool invocation (`action=mcp_tool`,
  integration + tool name + ok/error), every **consent decision**, and every admin
  mutation/test.
- **Consent is the side-effect gate** (§3.2, §6) — mandatory in P1. A per-integration
  `read_only` hint (to relax prompting for safe read tools) is an optional P2
  refinement, not a substitute for consent.

---

## 8. Frontend — management UI

A new **admin** view (gated by `auth.hasAccessLevel('admin')`), reachable from
Users & Roles → **Integrations** (or System). It talks to the CSAI admin API via a
new `mcpAdminService` (bearer JWT, same as the other CSAI calls):

- **List** the tenant's integrations: name, transport, endpoint host, enabled
  toggle, last-test status, and discovered-tool count.
- **Add / edit** a form: name, description, endpoint URL, auth type + secret
  (write-only field), extra headers, `allowed_tools` multiselect (populated from a
  **Test connection** call), enabled.
- **Test connection** button → `POST …/{id}/test` (or a dry-run on create) → shows
  the discovered tools so the admin can pick the allowlist and confirm reachability
  before enabling.
- **Delete** with a confirm modal.

No change to the chat view is required beyond the existing tool indicators; MCP tool
names appear there automatically.

---

## 9. Configuration

| Env | Default | Meaning |
|---|---|---|
| `CSAI_MCP_ENABLED` | `false` | master switch for MCP integrations |
| `CSAI_MCP_ALLOW_STDIO` | `false` | permit stdio servers **from system config only** (never the admin API) |
| `CSAI_MCP_MAX_INTEGRATIONS` | `10` | per-tenant cap |
| `CSAI_MCP_TOOL_TIMEOUT_MS` | `15000` | per tool call |
| `CSAI_MCP_MAX_TOOL_OUTPUT_CHARS` | `8000` | truncate tool output fed to the model |
| `CSAI_MCP_CONNECT_CACHE_TTL` | `300` | seconds to cache discovered tools per integration |
| `CSAI_MCP_CONSENT_TIMEOUT_MS` | `120000` | how long a tool call waits for user consent before defaulting to **deny** |
| `CSAI_MCP_SECRET_KEY` | — | Fernet key for `secret_enc` (required when enabled) |

---

## 10. Testing

- **Unit:** integration CRUD (tenant-scoped, secret write-only, SSRF rejection,
  stdio rejection, count cap); tool wrapping (namespacing, schema copy, output
  truncation, `allowed_tools` filter); discovery caching + fail-open (erroring server
  → 0 tools, no raise). MCP client mocked (no network).
- **Provider loop:** an MCP `Tool` is invoked and its result flows back like any
  other tool (extend the existing tool-loop tests).
- **Admin gating:** non-admin → 403; cross-tenant access denied.
- **Live (opt-in, `-m live`):** stand up a trivial MCP server (echo tool), register
  it, and drive a chat turn that calls it end to end.

---

## 11. Phasing

- **P1 (MVP):** data model + admin CRUD + Streamable-HTTP transport + `bearer`/`header`
  auth + discovery/wrapping/namespacing + fail-open + **mandatory per-call consent**
  (§6) + **opt-in `forward_identity`** (§7) + the frontend list/add/test UI and the
  in-chat consent prompt.
- **P2:** `allowed_tools` allowlist UX, connection/tool caching, output/timeout caps,
  richer tool-name labels, per-integration `read_only` (relax consent for safe reads),
  "remember for this conversation" polish.
- **P3:** OAuth auth type, deployment-level stdio servers (system config), and
  (deferred) global/system integrations shared across tenants (§12.4).

---

## 12. Resolved decisions

1. **User-identity forwarding — RESOLVED: opt-in, default off.** Per-integration
   `forward_identity` (§4) defaults to `false`; when enabled, CSAI forwards only a
   **minimal** signed user claim (stable id/email + tenant), never core roles/tokens/
   ACLs (§7). Included in **P1**.
2. **Store location — RESOLVED: CSAI's per-tenant schema.** The `mcp_integration`
   table lives in the existing CSAI tenant schema/section (§4) — not a new store and
   not ldap_manager. ldap_manager remains the identity authority.
3. **Consent for side effects — RESOLVED: mandatory per-call consent.** Every MCP
   tool call requires explicit in-chat user approval, with a default-deny timeout
   (§3.2, §6, §7). Included in **P1**. A per-integration `read_only` hint to relax
   prompting for safe reads is an optional **P2** refinement.
4. **Global (system) integrations — RESOLVED: deferred.** Not in the initial version;
   a possible follow-up (**P3**). P1 ships per-tenant integrations only.

---

## 13. Touched files (implementation map)

**convert_search_ai** — `mcp_client.py` (new: SDK client, discovery, tool wrapping),
`llm_tools.py` (`build_tools(..., mcp=…)`), `chat.py` (`_select_tools`, MCP system
instruction), `routers/mcp_admin.py` (new admin CRUD + test), `store.py`/`schema.py`
(the `mcp_integration` table + a small repo), `config.py` (§9 knobs), `crypto` helper
(Fernet for `secret_enc`), `app.py` (wire the router + MCP provider), `audit.py`
(new action names), `design_documents/EVENT_CONTRACT.md` (tool events unchanged; note
MCP tool names).

**frontend** — a new admin view + `mcpAdminService`, linked from the admin nav.

No core / bridge changes — MCP integrations are a CSAI-and-frontend concern; the
core remains the ACL authority for the user's own documents, independent of external
MCP tools.

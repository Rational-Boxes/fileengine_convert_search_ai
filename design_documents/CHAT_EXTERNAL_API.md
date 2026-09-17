# External chat API — a scoped, key-authenticated door onto the AI chat

> **Terminology.** The *door* is the REST surface an external system calls. The
> *external system* is a machine caller holding a minted API key — today, the
> public chat **proxy**. The *customer* is a person the proxy has identified and
> on whose behalf it is asking. CSAI authenticates only the first of these; it
> takes the customer's identity as an **assertion** from the proxy and bounds
> what that assertion can reach (§6.5).

**Status:** Design proposal — for review
**Scope (this document is normative for CSAI only):** `convert_search_ai` (the
door, the key store, the admin API), `frontend` (the tenant-admin panel),
`docker_unified` (nginx routing + config), `audit_service` (new action codes).
**No change to `file_engine_core`** and no change to the existing SPA chat
WebSocket. §13 states the contract the future **proxy service** must satisfy; it
does not design that service.

> **This feature ships off.** The whole of it — the door, the admin API, the
> frontend panel — is behind one deployment switch that is `false` by default and
> cannot be turned on from inside the product. Publishing part of a corpus to the
> public internet is a decision many organisations will never make, and for them
> this must be *absent*, not merely unconfigured. See §9.1.

> Expanded from the original sketch, kept verbatim as §1. §3 collects what is
> settled; §15 is what remains open.

---

## 1. The original sketch

> Implement a simple REST API using a generated access token, same as for the
> MCP and WebDAV doors, to allow an external system to drive the scoped AI chat
> conversation.
>
> A scoped user account will be created in FileEngine, this user will be the
> identity of an external system allowed access to the embedded AI chat. The goal
> is exposing the scoped chat to a proxy that can be on the Internet and publicly
> chat with the defined subset of the corpus. The plan for the proxy includes the
> ability for a customer to securely identify themselves, using the same secret
> request as the external share; an authenticated customer then has the context
> folder for their account included in the chat context.
>
> The API also needs to specify a text file in the corpus that is used as the
> system prompt for the conversation to set the AI agent "personality".

Everything below is the mechanism for that, plus the parts the sketch leaves
implicit: what "scoped" has to mean given how the core actually evaluates
permissions, where the conversation lives when the caller is a machine serving
many people, and how a door onto the tenant origin that answers unauthenticated
members of the public avoids becoming the most expensive thing in the deployment.

---

## 2. Goals & non-goals

**Goals**

1. **A machine-callable REST chat.** One request in, one grounded answer out,
   with citations — plus an SSE variant carrying the same events as the
   WebSocket, so the proxy can stream to a customer's browser.
2. **A scoped identity.** Every core call on this door is made as one named
   FileEngine account whose reachable corpus is a deliberately chosen subset.
3. **Confinement that does not depend on the caller.** The subset is pinned in
   the key record, enforced server-side, on retrieval *and* on every document
   tool. Nothing a caller sends can widen it.
4. **A personality that is a document.** The system prompt is a text file in the
   corpus, editable by an ordinary user with ordinary permissions, live without
   a redeploy.
5. **Two modes, one door.** *Public mode* answers an unidentified visitor from
   the key's base scope. *Identified mode* adds that customer's own folder to the
   scope for the turn — and only for that turn, and only if it sits where the key
   says customer folders sit. Which modes a key permits is part of its record.
6. **Resumable conversations without cross-customer leakage.** The proxy can
   continue a conversation it started, and can continue *only* the ones it holds
   a key for.
7. **Bounded cost.** A leaked key is a bounded loss, not an open LLM tap.
8. **Accountable.** Every turn is audited with the door and the asserted
   customer, content-free, on the same chain as everything else.
9. **Verifiable before it is live.** The identity is a real account a person can
   sign into. What that person sees when they browse as it *is* what the public
   will be able to ask about — so the door's exposure is reviewed by looking, not
   by reasoning about rules, and the key cannot be enabled until someone has
   (§6.1).

**Non-goals (v1)**

- **Not a general REST port of the chat.** No conversation listing, no folder
  browsing, no search endpoint, no document download, no report saving, no MCP
  integrations, no ONLYOFFICE. The door answers questions; it is not a second
  file API.
- **Not an authentication service for customers.** CSAI never sees a customer's
  email, never sends mail, never issues a code. That is entirely the proxy's job
  (§13), reusing `ldap_manager`'s `/internal/share/email-*` seam.
- **Not a replacement for the SPA chat.** The WebSocket at `/chat` is unchanged
  and keeps its client-supplied prompt/history semantics.
- **No user accounts for customers.** The scoped account is the only principal;
  customers are asserted references, never directory entries.

**Threat model.** Assume the key leaks — a proxy config in a git history, a log,
an environment dump. Every control is designed around *the key is eventually
public*: it names one principal, that principal reaches one subtree, the tools it
can drive are read-only, the budgets are finite, and revocation is a row update.
Assume also that the **proxy itself is compromised**: it can then assert any
`customer_ref` it likes, so the blast radius of that is deliberately one folder
per reference under one pinned root (§6.5) — not an arbitrary UID.

---

## 3. Decisions (locked unless §15 reopens them)

| Topic | Decision |
|---|---|
| Credential | A **minted API key**, `{key_id}.{secret}` — 128-bit id + 256-bit CSPRNG secret, base64url. Only `sha256(secret)` is stored; the plaintext is shown once at creation. The key row **is** the door configuration (§5.1). |
| Why not `/auth/token` | The MCP/WebDAV pattern would make the proxy hold the scoped account's **LDAP password**, and CSAI's `TokenStore` is process memory (`token_store.py`) — it does not survive a restart or span replicas. A minted key has no password behind it, is revocable without touching LDAP, and gives the configuration somewhere to live (§4.5). |
| Acting as the principal | **Trusted-upstream delegation**, as `core_client.client_for` already does: the scoped account's name and its **live LDAP roles** go in the `AuthenticationContext`. No password is stored anywhere. Admin roles are **stripped** before the call (§6.2). |
| Transport | **Both.** `POST /external/v1/chat` returns one JSON body; the same route with `Accept: text/event-stream` streams the WebSocket's event vocabulary (§7.3). |
| Confinement | **Core ACLs *and* a server-side scope pinned on the key**, enforced on RAG retrieval and on every document tool. A caller-supplied `scope_folders` is a **400**, not an ignored field (§6.3). |
| System prompt | A text document, **selected by the caller from a pinned `prompt_root` folder** (or the key's default). Read as the principal, size-capped, cached briefly. A caller-supplied `system_prompt` is a **400**. Unreadable prompt ⇒ the door is **down**, not personality-less (§6.4). |
| Customer context | The proxy asserts an opaque `customer_ref`; **CSAI resolves** `customer_root/<customer_ref>` itself. A folder UID from the caller is never accepted (§6.5). |
| Modes | **Public** (no `customer_ref`) and **identified** (`customer_ref` asserted) are both first-class, and each is independently enabled per key. Identified mode *adds* the customer's folder to the base scope; it never replaces it and can never widen beyond `customer_root` (§6.5). |
| Conversations | CSAI mints an opaque **resume key** the proxy stores per customer. It is the sole proof of ownership; there is no listing route on this door. The key is bound to the minting API key *and* to the `customer_ref` in force when it was minted (§6.6). |
| Tools | A **per-key allowlist**, defaulting to `document_search` + `get_document_text`. `list_folders` and `web_search` are opt-in. `save_report`, `fetch_page` and MCP integrations are **refused on this door** regardless of configuration; an MCP consent request auto-denies (§6.7). |
| Budgets | **Redis-backed**, per key and per customer — `redis>=5.0` and `FILEENGINE_REDIS_*` are already dependencies of this service. Per-process counters are rejected for the reason `OUTSIDE_SHARE_LINKS` §8.4 gives: one fresh allowance per replica (§6.8). |
| The scoped identity | An **existing, loginable account** the administrator **selects** — not a hidden `svc-*` worker, and not created by this feature. Being loginable is the point: the administrator signs in as it, sees exactly what it can reach, and only then selects it (§6.1, §8.1). CSAI never creates, modifies or provisions an account. |
| Attestation | **Minting requires it.** The administrator attests, at selection, that they signed in as the account and reviewed its reach; `attested_by` / `attested_at` are stored on the key row and the mint is audited with the principal named. There is no separate enable step — the review happens *before* selection, so the key is live when minted (§6.1, §11). |
| Key administration | **Tenant administrator** — `_ADMIN_ROLES` as in `routers/mcp_admin.py` — over REST plus a frontend panel; secret shown once. Minting a key publishes part of your own tenant, which is a tenant-level decision, and rotating a leaked one must not wait on a deploy (§7.2, §10). |
| Audit | New action codes with `actor = "external:<key_id>|<customer_ref>"`, mirroring the share pattern. Content-free: the question text is never logged (§11). |
| Failure disclosure | Unknown / revoked / expired / disabled / budget-exhausted return a **generic** shape to the caller with a machine-readable `code`; the real reason goes to audit (§6.9). |
| Routes | `/external/v1/*` (API key) and `/v1/admin/external-keys` (tenant-admin bearer), both on the existing `csai-app:8092`, reached publicly as `/csai/external/v1/*` (§7). |
| Master switch | `CSAI_EXTERNAL_API_ENABLED`, **default `false`**, set at deployment. Off ⇒ every route in §7 — public *and* admin — 404s, the capability reports unavailable, and the SPA panel does not render. A tenant administrator cannot enable the feature; that takes a deploy, deliberately (§9.1). |
| Core | **Unchanged.** No tables, no RPCs, no permission bit. |

---

## 4. What the current code makes load-bearing

This is not "expose `/chat` over REST". Five properties of the code as it stands
decide most of the design; each is a real constraint, not a preference.

### 4.1 The WebSocket is not the door

`api.py:297` is a `@router.websocket` handler with a long-lived receive loop, an
`anyio` memory stream bridging the blocking generator, and a concurrent control
reader for MCP consent replies. None of that survives a stateless request. The
external door is a **separate handler** that drives the same `ChatService.answer`
generator (`chat.py:174`) and drains it either into one response body or into an
SSE stream. The WS handler is left alone.

### 4.2 `scope_folder_uids` is a retrieval filter, not a boundary

`ChatService.answer` passes the scope to `Retriever.retrieve` and nowhere else
(`chat.py:186`). `document_search`, `get_document_text` and `list_folders` in
`llm_tools.py` receive no scope at all — they run against the whole tenant,
bounded only by `PermissionGate`. On the SPA that is correct: the scope is a
convenience for a user who could read those files anyway. On this door it would
be a **hole through which the model walks out of the subset on its second tool
call**. Threading containment into the tool layer is the single largest piece of
new work in this design (§6.3, M1).

### 4.3 Read-by-default means a fresh account is a tenant-wide reader

`AclManager` ships `default_read_ = true` with parent traversal: a principal with
no matching rule reads everything whose parent chain is readable. So "create a
scoped user account" does **not** produce a scoped user — it produces a reader of
the entire tenant unless explicit denies exist. This is why §6.3 is defence in
depth rather than a choice of mechanism; why key creation runs a pre-flight that
refuses a principal reading outside its scope; and above all why the identity is a
**real account someone signs into and browses** before the key is enabled (§6.1).
A rule set with default-read and inherited traversal is not something anyone can
verify by reading it. It is something you check by looking at what it produced.

### 4.4 The conversation store is keyed by `user_id` alone

`schema.py:120` keys `conversations` by `(tenant, user_id)` and
`ConversationStore.owns` checks exactly that. One scoped account serving many
customers means every customer's conversation is owned by the same `user_id` —
`GET /conversations` would hand any customer every other customer's chat. Hence:
**no listing route on this door**, and a per-conversation capability (the resume
key) instead of an ownership check (§6.6).

### 4.5 `TokenStore` is process memory

`token_store.py` holds `dict[str, (Identity, expiry)]` behind a lock. It is
correct for its purpose — one LDAP bind cached for an interactive session — and
wrong for a standing machine credential: a restart logs the proxy out, and two
replicas do not share tokens. The minted key is resolved from Postgres on each
request (with a short in-process cache keyed by `key_id`, §6.2), which has
neither problem.

---

## 5. Data model (CSAI per-tenant schema)

A new migration, `migrations/0002_external_api.sql`, plus the matching
`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE … ADD COLUMN IF NOT EXISTS` in
`schema.py` (the file provisions tenants directly; both must agree).

### 5.1 `external_api_keys`

```sql
CREATE TABLE IF NOT EXISTS "{schema}".external_api_keys (
    id                  TEXT        PRIMARY KEY,          -- key_id: 32 hex chars
    name                TEXT        NOT NULL,
    description         TEXT        NOT NULL DEFAULT '',
    secret_sha256       TEXT        NOT NULL,             -- hex sha256 of the secret half
    principal           TEXT        NOT NULL,             -- scoped account: uid OR mail
    scope_folder_uids   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    prompt_root_uid     TEXT        NOT NULL DEFAULT '',
    default_prompt_uid  TEXT        NOT NULL DEFAULT '',
    customer_root_uid   TEXT        NOT NULL DEFAULT '',
    tools               JSONB       NOT NULL DEFAULT '["document_search","get_document_text"]'::jsonb,
    modes               JSONB       NOT NULL DEFAULT '{"public":true,"identified":true}'::jsonb,
    budgets             JSONB       NOT NULL DEFAULT '{}'::jsonb,
    enabled             BOOLEAN     NOT NULL DEFAULT TRUE,
    attested_by         TEXT        NOT NULL,             -- who reviewed the principal's reach
    attested_at         TIMESTAMPTZ NOT NULL,
    created_by          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ,
    last_used_at        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_external_keys_enabled
    ON "{schema}".external_api_keys (enabled, revoked_at);
```

Notes that matter:

- **`principal` is matched as uid OR mail**, the filter
  `ldap_auth._authenticate_against` already uses across `ldap_user_base` and
  `ldap_service_base` (the platform hands services an email as their identity, so
  a uid-only filter silently matches nothing). **Any existing account in the
  tenant may be named** — there is no designating group. What bounds the choice is
  the administrator's review, recorded in `attested_by`, and the pre-flight that
  refuses a principal whose reach exceeds the declared scope (§6.1, §8.1).
- **`secret_sha256`, not encryption.** The secret is a bearer credential CSAI
  only ever needs to *compare*, so it is hashed, not Fernet-encrypted like the
  MCP integration secrets in `crypto.py`. Plain SHA-256 is sufficient because the
  secret is 256 bits of CSPRNG output, not a password.
- **`budgets`** — `{turns_per_minute, turns_per_day, turns_per_minute_per_visitor,
  turns_per_day_per_visitor, max_history_turns, max_k, max_context_chars}`. Absent
  keys fall back to the `CSAI_EXTERNAL_*` defaults (§9). "Visitor" is the
  `customer_ref` in identified mode and the `session_ref` in public mode (§6.8).
- **`tools`** is an allowlist of tool *names*; anything not on the refused list
  (§6.7) and not in this array is simply not offered to the model.
- **`attested_by` / `attested_at` are NOT NULL**, which is the schema saying that
  a key cannot exist without someone having claimed responsibility for what it
  publishes. They are set once, at mint, and carried through a rotate; an update
  that changes `principal` or widens `scope_folder_uids` refreshes them with the
  editing administrator (§6.1).
- **`modes`** decides which of §6.5's two modes this key serves. A key for a
  customer portal sets `{"public": false}` and every turn must then carry a
  `customer_ref`; a key for an open marketing bot sets `{"identified": false}` and
  `customer_ref` becomes a 400. Both true is the default.

### 5.2 Conversation resume

Rather than a second conversation table, the existing one gains three columns and
an index:

```sql
ALTER TABLE "{schema}".conversations ADD COLUMN IF NOT EXISTS external_key_id TEXT;
ALTER TABLE "{schema}".conversations ADD COLUMN IF NOT EXISTS resume_sha256   TEXT;
ALTER TABLE "{schema}".conversations ADD COLUMN IF NOT EXISTS customer_ref    TEXT;
CREATE INDEX IF NOT EXISTS idx_conversations_external
    ON "{schema}".conversations (external_key_id, id);
```

The **resume key** handed to the proxy is `{conversation_id}.{secret}` — the same
shape as the API key, for the same reason (the id makes the lookup a primary-key
hit; the secret is compared constant-time against `resume_sha256`). A
conversation minted on this door has `external_key_id` set and is therefore
**excluded from `GET /conversations`** on the SPA door as well: the scoped
account has no human behind it, but if someone ever logs in as it, they should
not get the customers' transcripts as a list.

Resuming requires all three to hold: the secret matches, `external_key_id`
matches the presenting key, and the asserted `customer_ref` equals the stored one
(empty matching empty). A resume key that leaks between customers is therefore
inert — it only works when re-presented with the same asserted identity.

### 5.3 Retention

Conversations minted on this door are deleted after
`CSAI_EXTERNAL_CONVERSATION_TTL_DAYS` (default **30**) of inactivity by the
existing reconcile sweep's schedule (`reconcile.py`); `DELETE /external/v1/
conversations/{resume_key}` deletes one immediately, which is what a proxy calls
when a customer asks to be forgotten. Revoking an API key does **not** delete its
conversations — it makes them unreachable, and the retention sweep collects them.

---

## 6. Behaviour

### 6.1 Selecting the account, minting the key

Two steps, in this order, and the order is the design: **the account's access is
audited before it is selected, not after it is published.**

**Step 1 — audit the account by signing in as it.** The administrator picks a
candidate from the tenant's existing accounts, logs into the SPA (or WebDAV) with
its credentials, and browses. Whatever they can see is precisely what a stranger
on the internet will be able to ask about, because the door makes every core call
as this principal (§6.2). This is the acceptance test for the whole feature, and
it is the reason the identity is a real loginable account rather than a hidden
`svc-*` worker like the ingest agents: §4.3's read-by-default with inherited
traversal produces a reachable set that nobody can verify by reading rules. You
verify it by looking at what the rules produced.

If the account reaches more than the public should see, the fix belongs here,
before anything is minted: tighten its ACLs — grant what should be public, deny
the rest — and browse again. A purpose-made account is usually the cleanest
answer, but this feature neither creates one nor requires it; it selects.

**Step 2 — select it, attest, and mint.** A tenant administrator (`_ADMIN_ROLES`
in `routers/mcp_admin.py`: `administrators` / `tenant_admin` / `system_admin`)
posts the key definition naming the reviewed account, together with an explicit
attestation — *"I have signed in as `<principal>` and reviewed what it can
reach"*. The attestation is not decoration: it is stored (`attested_by`,
`attested_at`), audited with the principal named (§11), and shown on the key ever
after, so "who published this account's reach, and when" is one query rather than
a reconstruction.

Before the row is written, CSAI runs a pre-flight **as the named principal** and
refuses the mint if any of these fail:

1. **The principal resolves** in LDAP (uid or mail, either base).
2. **Every `scope_folder_uids` entry exists and is READ-able** by the principal.
3. **`prompt_root_uid` / `default_prompt_uid` / `customer_root_uid`, when set,
   exist and are READ-able** by the principal.
4. **Containment sanity:** listing the filesystem root as the principal returns
   *only* entries inside the declared scope. If it returns more, the mint is
   refused with `principal_overreaches`, naming the first offending entry.

Check 4 carries real weight now that any account in the tenant may be named. An
ordinary colleague's account, or an administrator's, reaches most of the tenant —
so its root listing will not sit inside a narrow declared scope, and the mint is
refused rather than quietly publishing a person's working set. It is a smoke test
and not a proof: it walks the root, not the whole tree, and it cannot distinguish
"this account is meant to be broad" from "this scope is meant to be broad". Step 1
is the guarantee; the panel says so rather than letting a green pre-flight imply
more than it checked (§10).

The mint response contains the full `{key_id}.{secret}` **once**. It is never
retrievable again; a lost key is rotated, not recovered. The key is live on mint —
the review that would have gated an enable step has already happened, and a second
switch whose purpose nobody remembers is worse than no switch.

**The account's password is a real credential.** It opens every door that account
can reach — SPA, WebDAV, MCP, the bridges — so it stays with the people who
administer it and must never appear in the proxy's configuration. CSAI itself
never learns it: the door delegates by *name* (§6.2), exactly as a share-link
redemption delegates as its creator.

### 6.2 Authenticating a request

`Authorization: Bearer <key_id>.<secret>` on `/external/v1/*`. Resolution:

1. Split on the first `.`; a malformed value is a uniform 401.
2. Load the row by `key_id` from the request's tenant schema. Tenant comes from
   `X-Tenant` or the Host subdomain exactly as `http_auth.extract_tenant` already
   decides it — a key minted in one tenant's schema cannot be found from another.
3. `secrets.compare_digest(sha256(secret), row.secret_sha256)`.
4. Reject if `revoked_at`, `expires_at` in the past, or `enabled = false`.
5. Resolve the principal's **Identity** — user + live LDAP roles for this tenant —
   without a password. This needs a new `ldap_auth.identity_for(cfg, username,
   tenant)`: service bind, `(|(uid=…)(mail=…))` across both bases, then the
   existing `tenant_access.roles_in_tenant`. It is `_authenticate_against` with
   the user-bind step removed, and it is the same passwordless resolution
   `OUTSIDE_SHARE_LINKS` §6.3 specifies for a redeeming share link.
   **The principal is re-resolved here, not cached from mint time.** An account
   that has been deleted, or has lost the tenant role membership that makes it a
   member of anything, stops resolving — and every key naming it stops working.
   That is the account-level kill switch (the key-level one is `/revoke`), and it
   is the same reason `OUTSIDE_SHARE_LINKS` §6.3 re-evaluates a link creator's
   authority on every redemption rather than trusting creation-time state. Note
   what it implies in the other direction: **widening the account's ACLs widens
   the door**, live, with nothing in CSAI to notice (§15).
6. **Strip administrative roles** — `administrators`, `tenant_admin`,
   `system_admin` — from the resolved identity before it is used. `client_for`
   *adds* `tenant_admin` for members of `administrators` (`core_client.py`), which
   is right for a person in the SPA and catastrophic here; and `system_admin` is
   an outright ACL bypass in the core. A misconfigured key naming an admin
   account must degrade to that account's ordinary reach, not to everything.
   Because the door also refuses the mint in §6.1 check 4, this is the second of
   two independent barriers.

Steps 1–5 are cached in-process for `CSAI_EXTERNAL_KEY_CACHE_TTL` (default 60 s)
keyed by `key_id`, with the cache busted on revoke/update by the admin router —
the same `_bust` pattern `routers/mcp_admin.py:344` uses. **Revocation is
therefore effective within one TTL on every replica**, which the admin UI states
rather than implying instantaneity.

`last_used_at` is written at most once per minute per key (a debounced update, not
one write per turn).

### 6.3 Corpus confinement (the defining rule)

Three layers, in order of how much they are trusted:

**Layer 1 — core ACLs on the principal.** The real boundary. Denies are authored
by the operator; §6.1's pre-flight checks the obvious failure.

**Layer 2 — the pinned scope, enforced in CSAI.** The key's `scope_folder_uids`
expand to a **containment set** of file UIDs by the walk
`Retriever._resolve_scope_file_uids` already implements (`retrieval.py:94`) —
`mf.dir()` as the principal, level by level, bounded by `max_folders`. That set
is then applied at **four** places, three of which are new:

| Point | Today | On this door |
|---|---|---|
| RAG `ann_search` | `file_uids=` filter (`retrieval.py:74`) | unchanged, always populated |
| `document_search` | whole tenant | results filtered to the containment set before the permission gate |
| `get_document_text` | any indexed uid | a uid outside the set is `404 not_found` — **not** 403, which would confirm it exists |
| `list_folders` | any path | a root outside the scope is `404 not_found`; listings never show a parent |

`SearchService.search` gains an optional `file_uids` parameter threaded into
`DocumentSearchRepo.query` (`search.py:100`), and `ToolContext` gains the
containment set so `llm_tools.py` can enforce the other two. This is the work
§4.2 identifies.

**Layer 3 — the caller cannot participate.** `scope_folders`, `scope_folder_uids`
and `system_prompt` in an external request body are **400 `unsupported_field`**.
Silently ignoring them would let a proxy believe it had narrowed a scope it had
not.

**Cache and staleness.** The containment set is cached per
`(tenant, key_id, customer_ref)` for `CSAI_EXTERNAL_SCOPE_TTL` (default **60 s**)
and busted on core `file.moved` / `file.deleted` / `acl.changed` events through
the existing `cache_invalidation.py` subscriber. Two consequences, both stated
rather than discovered: a document added inside the scope becomes answerable
within one TTL, and a document moved *out* of the scope remains in a warm
containment set for at most one TTL — during which `PermissionGate.can_read`
still gates it live, so the residual exposure is "a file the principal may still
read that has been moved elsewhere", not a permission bypass.

### 6.4 The system prompt document

**Selection.** The request may carry `prompt` — a **filename** relative to the
key's `prompt_root_uid`, or a file UID that must resolve to a direct child of it.
Absent, the key's `default_prompt_uid` is used. With no `prompt_root_uid`
configured, only the default is available and any `prompt` value is a 400. This
lets one key serve several personalities (per product line, per language) without
the caller ever pointing at arbitrary content.

**Reading.** The bytes are fetched from the core **as the principal** via
`StreamFileDownload` — not via `SearchService.get_text`, because the extracted
Markdown only exists once the document has been ingested, and a personality that
does not work until an ingest sweep has run is a support call. MIME must be
`text/plain` or `text/markdown`; size is capped at
`CSAI_EXTERNAL_PROMPT_MAX_BYTES` (default **32768**). Cached per
`(key_id, prompt_uid)` for `CSAI_EXTERNAL_PROMPT_TTL` (default **60 s**), so
editing the document changes the personality within a minute and no deploy is
involved. That latency is the feature.

**Composition.** The document's text is **prepended** to what
`ChatService._build_system` already assembles — it does not replace it. The
retrieved context block, the citation rules and `_INSTRUCTIONS_DOC_TOOLS` must
survive, or the model stops citing and stops using the tools it was given. The
prompt document sets voice, subject-matter framing and refusal policy; it does
not get to turn off grounding.

**Failure is closed.** Missing, unreadable, wrong MIME, or over the cap ⇒
`503 prompt_unavailable`, and the turn does not run. There is deliberately no
fallback to a built-in personality: a public-facing bot that silently loses its
instructions — including whatever the operator wrote about what not to discuss —
is worse than one that is briefly down. The condition is audited and surfaced in
the admin panel as a per-key health state (§10).

### 6.5 Two modes: public and identified

Every turn is one of two shapes, and a key declares which of them it serves
(`modes`, §5.1):

**Public mode** — no `customer_ref`. The turn runs against the key's base scope
alone: the corpus an administrator signed in and reviewed in §6.1 step 2. This is
the unidentified visitor on the proxy's public page, and it is a first-class mode,
not a degraded one.

**Identified mode** — the proxy has verified a customer and asserts
`customer_ref`: an opaque string matching `^[A-Za-z0-9._-]{1,64}$`. CSAI resolves
it **itself** — `mf.dir(customer_root_uid)` as the principal, looking for a direct
child folder whose name equals `customer_ref` — and **adds** that folder to the
turn's scope roots before the containment set is built (§6.3). Resolution is
cached with the containment set.

Identified mode only ever *adds*. It cannot remove anything from the base scope
and cannot reach outside `customer_root`, so the worst a wrong `customer_ref`
does is show one customer another customer's folder — bad, bounded, and audited
(§11) — rather than opening the tenant.

A turn in a mode the key does not permit is a 400 `mode_not_permitted`: a
`customer_ref` on a public-only key, or its absence on an identified-only key.
Being explicit matters more than being lenient here, because both mistakes are
silent in the answer text — the customer just gets a reply that does not know
about them.

**Anonymous visitors still need a budget key.** Public mode has no
`customer_ref`, so without something else every anonymous visitor shares one
bucket and the first abuser degrades the page for everyone. The proxy therefore
passes `session_ref` — the same opaque shape, its own per-browser-session
identifier — and §6.8's per-visitor budgets key on `customer_ref or session_ref`.
It is not a credential and grants nothing; a caller that omits it gets the
per-key budget only, which is the degenerate case the proxy is expected to avoid.
`session_ref` is refused alongside `customer_ref` (400) rather than both being
accepted at once: a session that has identified itself is identified.

Three properties follow, and they are the reason it is done this way rather than
having the proxy pass a UID:

- A compromised proxy can name a **customer**, not a location. The worst it
  reaches is some other customer's folder under the same root — bad, and bounded
  — rather than any folder the principal can read.
- The mapping needs no synchronisation. Provisioning a customer is creating a
  folder with the right name; there is no second registry to drift.
- CSAI never learns who the customer is. `customer_ref` is opaque to it, and the
  proxy should make it opaque in fact (a salted hash of the verified email, not
  the email) so the audit trail carries a pseudonym — see §13.

**No folder ⇒ no failure.** An identified customer with no folder yet is normal.
The turn proceeds with the base scope and the response carries
`"customer_context": false` so the proxy can say so if it wants; the audit record
notes `customer_context: "missing"`. The resolved folder UID is **never** returned
to the caller.

### 6.6 Conversations and the resume key

A first turn without `conversation_key` creates a conversation, mints
`{conversation_id}.{secret}`, stores `sha256(secret)`, `external_key_id` and
`customer_ref`, and returns the resume key in the response. **It is returned
once**, in the response that created it; subsequent turns echo nothing.

A turn with `conversation_key` resumes: primary-key lookup, constant-time secret
compare, then the two bindings of §5.2 (same API key, same `customer_ref`). Any
mismatch is a uniform `404 conversation_not_found` — never 403, which would
confirm the conversation exists.

**History is server-side on this door.** The proxy does not send `history`; CSAI
reconstructs it from `conversation_messages`, newest `max_history_turns` (default
**12**) turns, oldest-first. This is the §4.2-of-`CHAT_WITH_AI` "server-side
context" item, scoped to this door only: the SPA's client-supplied history is
untouched. The reason it cannot wait is that the alternative is letting an
internet-facing caller hand the model an arbitrary transcript — a prompt-injection
lever handed out for free (§8.3). A `history` field in an external request is a
**400**.

Persistence is best-effort on the SPA door (`api.py:365`). Here it is **not**:
if the user turn cannot be persisted, the resume key would be a lie, so the turn
fails with `503 conversation_unavailable`.

### 6.7 Tools on this door

| Tool | Default | Configurable |
|---|---|---|
| `document_search` | on | yes |
| `get_document_text` | on | yes |
| `list_folders` | off | yes |
| `web_search` | off | yes (needs `CSAI_WEB_SEARCH_ENABLED`) |
| `fetch_page` | **refused** | no |
| `save_report` | **refused** | no |
| MCP integrations | **refused** | no |

The three refusals are structural, not conservative defaults. `save_report`
writes into the tenant's storage as the principal — a public conversation must
not be able to create files. `fetch_page` turns the door into a fetch proxy whose
target is chosen by a member of the public; the SSRF guard in `webfetch.py` makes
that survivable, not desirable. MCP tools are consent-gated on a human
(`consent.py`), and there is no human: the external door passes a `consent`
callback that **denies immediately** rather than waiting for
`CSAI_MCP_CONSENT_TIMEOUT_MS` and then denying, so a misconfiguration is a fast
"no" and not a two-minute hang.

`CSAI_TOOL_MAX_ITERATIONS` still bounds the loop; `max_k` and `max_context_chars`
from the key's budgets are applied through `guards.cap_k` / `guards.trim_context`
as they already are.

### 6.8 Budgets and rate limits

Counters live in **Redis** — already a dependency (`redis>=5.0`,
`FILEENGINE_REDIS_*` in `config.py:149`) — as fixed-window counters with the
window length as the TTL:

```
csai:ext:{tenant}:{key_id}:rpm           csai:ext:{tenant}:{key_id}:rpd
csai:ext:{tenant}:{key_id}:{visitor}:rpm csai:ext:{tenant}:{key_id}:{visitor}:rpd
```

Checked and incremented **before** the LLM call; a refusal costs no tokens.
Exceeding any bucket is `429` with `Retry-After` and
`code: "rate_limited"` — the one place the door is deliberately *specific* rather
than uniform, because a proxy needs to know to back off rather than retry.

**Redis unavailable ⇒ the door is closed** (`503 budget_unavailable`), matching
the fail-closed posture `OUTSIDE_SHARE_LINKS` §6.9 takes for the same reason: an
unmetered public LLM endpoint is a worse outage than a refused one. This is a new
hard dependency for this door only; the SPA chat is unaffected.

**Token accounting is approximate in v1.** `providers/chat.py` exposes only
`stream(...) -> Iterator[str]`; no provider surfaces usage. So a token ceiling
would have to be enforced on a `len(text)/4` estimate, which is worth having as a
metric and not worth having as a limit. v1 therefore **meters turns, not tokens**,
and M3 adds real usage reporting to the provider interface before any token
ceiling is enforced (§15).

### 6.9 Uniform failure

Every failure on `/external/v1/*` returns the same envelope:

```json
{ "error": "request could not be completed", "code": "<machine_code>", "request_id": "…" }
```

with HTTP 401 for any credential problem (unknown key, bad secret, revoked,
expired, disabled — all identical to the caller), 400 for a malformed or
unsupported field, 404 for anything not found *or* not reachable, 429 for
budgets, 503 for a dependency (prompt, Redis, conversation store). The `code`
distinguishes only what a correct proxy needs to act on
(`rate_limited`, `prompt_unavailable`, `unsupported_field`, `conversation_not_found`,
`unauthorized`); the *reason* a credential failed goes to audit only, and
`request_id` is what support correlates on.

---

## 7. Routes

### 7.1 External (API key)

`APIRouter(prefix="/external/v1", tags=["external-chat"])`, reached publicly as
`https://<tenant>.<base>/csai/external/v1/…` through the existing `/csai/`
location in `docker_unified/images/nginx/snippets/tenant.conf` — which already
sets `proxy_buffering off`, so SSE works without an nginx change. A dedicated
`limit_req` zone for this prefix is added as a coarse outer bound (§8.4).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/capabilities` | What this key permits: **which modes** (`public` / `identified`), available prompts, tools, budgets. Lets the proxy render the right UI — and refuse to offer a "sign in" affordance a key does not serve — without hard-coded assumptions. |
| `GET` | `/prompts` | The selectable prompt documents under `prompt_root_uid` — `{uid, name, title}` — resolved as the principal. Empty when no root is configured. |
| `POST` | `/chat` | One turn. Body: `{message, conversation_key?, customer_ref?, session_ref?, prompt?, k?}` — `customer_ref` for identified mode, `session_ref` for public mode, never both (§6.5). |
| `DELETE` | `/conversations/{conversation_key}` | Forget one conversation and its messages. |

**Deliberately absent:** conversation listing, `/search`, `/documents/{uid}/text`,
anything under `/internal/`, and every admin route. The door answers questions.

`POST /chat` response (non-streaming):

```json
{
  "answer": "…",
  "citations": [{"marker": 1, "kind": "doc", "file_uid": "…", "title": "…"}],
  "conversation_key": "…",          // only on the turn that created it
  "conversation_id": "…",
  "mode": "identified",
  "customer_context": true,
  "prompt": {"uid": "…", "name": "support-agent.md"},
  "request_id": "…"
}
```

Document citations carry `file_uid` and `title` as they do today. **The proxy
must not turn a `file_uid` into a link** for a member of the public — that UID
addresses a file in the tenant that the customer has no way to open and no
business knowing exists. §13 makes this the proxy's responsibility; CSAI's
`capabilities` response flags it (`"citations_are_internal": true`).

### 7.2 Admin (tenant-admin bearer)

`APIRouter(prefix="/v1/admin/external-keys", tags=["external-admin"])`, a direct
mirror of `routers/mcp_admin.py` down to `_require_admin` and `_bust`:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `` | List keys (never the secret; `last_used_at`, health state). |
| `POST` | `` | Mint — requires the §6.1 attestation, runs the pre-flight, returns the secret once. Live on creation. |
| `GET` | `/{id}` | One key. |
| `PUT` | `/{id}` | Update configuration (not the secret). Re-runs the pre-flight; a changed `principal` or a widened `scope_folder_uids` requires a fresh attestation and records the editing administrator. |
| `POST` | `/{id}/rotate` | New secret, same id and configuration; returns it once. |
| `POST` | `/{id}/disable` / `/{id}/enable` | Switch a key off and back on without revoking — the reversible pair, for taking a bot down during maintenance. Re-enabling does **not** re-attest; changing the principal or widening the scope does (§6.1). |
| `POST` | `/{id}/revoke` | Set `revoked_at`; terminal. Effective within the key cache TTL. |
| `DELETE` | `/{id}` | Delete the key row. Conversations remain until the retention sweep. |
| `POST` | `/{id}/test` | Dry-run: resolve the principal, expand the scope, read the default prompt, report counts and failures. |

`/{id}/test` is what turns "the bot says nothing useful" into a one-click answer;
it is the same affordance `mcp_admin`'s `test_integration` provides.

### 7.3 Streaming

`POST /external/v1/chat` with `Accept: text/event-stream` streams the **same event
names** the WebSocket emits (`CHAT_WITH_AI` §2.1), so a proxy that already speaks
one speaks the other:

```
event: token      data: {"text":"…"}
event: tool_call  data: {"name":"document_search"}
event: citations  data: {"citations":[…]}
event: meta       data: {"conversation_key":"…","conversation_id":"…","customer_context":true}
event: done       data: {}
event: error      data: {"code":"…","error":"…"}
```

Differences from the socket, all forced by the transport: `tool_consent_request`
is never emitted (§6.7); `meta` carries what the JSON body would have carried
alongside the answer and is sent **before** `done`; a comment line (`: keepalive`)
every `CSAI_EXTERNAL_SSE_KEEPALIVE_S` (default 15) keeps intermediaries from
closing an idle stream while the model thinks. The implementation drains the same
`ChatService.answer` generator in a threadpool, exactly as `_stream_answer`
(`api.py:392`) does, minus the consent reader.

---

## 8. Security

### 8.1 The selected account — deliberately a real one

It is **not** a hidden `svc-*` worker under `ou=services`. The ingest agents are
hidden because nothing should ever look at what they see; this account is the
opposite — **it is the audit point for the entire feature**, and it earns that
role only by being loginable. An administrator signs in as it and browses; what
they see is what the internet will be able to ask about. Nothing else in this
design produces that assurance: the pre-flight walks one level (§6.1 check 4), the
containment set is a cache, and the ACL rules that actually decide are spread
across a tree nobody reads in full. Browsing the account *is* reading them, and it
is the review that precedes selection (§6.1 step 1).

The consequences of that choice, each of which has to be carried rather than
wished away:

- **It holds a password**, and that password opens every door the account can
  reach — SPA, WebDAV, MCP, the bridges. It belongs to the administrators who
  audit and must never appear in the proxy's configuration. CSAI never learns it
  (§6.2 delegates by name), so nothing about this door depends on it.
- **It is visible** in user pickers and ACL dialogs, unlike the workers. That is
  wanted: granting it access is an ordinary ACL grant on an ordinary account, and
  seeing it listed is a reminder that it exists.
- **It needs a tenant role to authenticate at all** — a member of nothing fails
  `_authenticate_against` — but should hold no role beyond that. §6.2 strips
  `administrators` / `tenant_admin` / `system_admin` from the delegated identity
  regardless, which matters more for a real account than it would for a service
  one, precisely because a real account is the kind someone might casually put in
  an admin group.
- **It must never be a member of `share_external`** — it has no business minting
  outside share links, and a door that answers strangers should not also be able
  to mint credentials.

### 8.1.1 Accepted risk: any account may be selected

Selection is not restricted to a designated group or OU. Any existing account in
the tenant may be named as a key's principal, and the controls against naming the
wrong one are, in order:

1. **The review** (§6.1 step 1) — the administrator has to sign in as the account
   and look at what it reaches before selecting it.
2. **The attestation** (`attested_by` / `attested_at`, and the principal named in
   the `external_key_create` audit event) — the choice is attributable, and "which
   accounts have ever been published this way" is one query over the audit chain.
3. **The pre-flight** (§6.1 check 4) — an account whose root listing exceeds the
   declared scope is refused, which is the ordinary shape of a colleague's or an
   administrator's account and therefore catches the accidental case.
4. **Admin-role stripping** (§6.2) — naming a privileged account still does not
   convey its privilege through this door.

What is *not* covered, stated plainly rather than left to be discovered: a tenant
administrator who deliberately names a real person's narrowly-scoped account, and
attests, publishes that person's reach to the internet, and nothing in CSAI stops
them. This is a considered trade — a designating group was weighed and declined in
favour of not requiring a deployment action to stand up a bot — and it means the
tenant-administrator role is trusted with publication. Deployments that do not
want that trust in tenant hands have the control that covers it: leave
`CSAI_EXTERNAL_API_ENABLED` off (§9.1).

### 8.2 Key strength and storage

128-bit id + 256-bit secret from `secrets.token_urlsafe`; `sha256(secret)` at
rest; constant-time comparison; the plaintext exists in one response body and
nowhere else. Keys are never logged — `audit.py`'s header already forbids
logging tokens, and the audit record carries `key_id` only, which is not a
credential on its own.

### 8.3 Prompt-injection posture

This door takes input from the public and feeds it to a model that can call
tools, so the usual hand-waving is not enough. The concrete controls:

- **The caller cannot set the system prompt.** It is a document under a folder an
  admin pinned (§6.4).
- **The caller cannot supply history.** It is reconstructed server-side (§6.6),
  so a crafted "previous turn" cannot be injected.
- **The caller cannot widen scope.** Containment is enforced on every tool (§6.3).
- **No tool can write, fetch, or reach a third-party server** (§6.7).

What remains is the genuine residual: **retrieved document content is untrusted
input**. A document inside the scope that contains instructions will be read by
the model as part of its context. Nothing in v1 fixes that — it is the
`CHAT_WITH_AI` §4.4 "treat retrieved text as untrusted" item, and it matters more
here than anywhere else because the corpus is deliberately published to
strangers. The mitigation available today is procedural and belongs in the admin
panel copy: **the scope of an external key is published material; treat its
contents as world-readable, and review what goes into it.**

### 8.4 Abuse controls

Layered, outermost first: an nginx `limit_req` zone on
`/csai/external/` (a coarse per-IP bound that costs the service nothing), the
Redis per-key and per-visitor budgets (§6.8), `guards.check_query` on message
length, `cap_k`, `trim_context`, `CSAI_TOOL_MAX_ITERATIONS`, and
`CSAI_CHAT_MAX_TOKENS` on the answer. The per-visitor buckets matter as much as
the per-key ones: without them one abusive visitor consumes the whole tenant's
daily budget and the proxy looks broken to everyone else — and in public mode,
where there is no customer to blame, that is the *only* thing separating one
scripted browser tab from the day's entire allowance.

### 8.5 What the door never exposes

No folder UIDs (except a `file_uid` inside a citation, §7.1), no paths, no
listing of conversations, no other customer's `customer_ref`, no principal name,
no tenant internals, no distinction between "does not exist" and "not permitted",
and no indication of which of the credential failures occurred.

---

## 9. Configuration (`CSAI_EXTERNAL_*`)

### 9.1 The master switch

`CSAI_EXTERNAL_API_ENABLED` defaults to **`false`**, and while it is false this
feature does not exist:

- every route in §7 — `/external/v1/*` **and** `/v1/admin/external-keys` — returns
  404, not 403, so nothing confirms the surface is there;
- `GET /v1/capabilities` reports `external_api: {available: false}`, and the SPA
  hides the admin panel through the capability-detection path it already uses to
  hide absent services;
- no table is provisioned and no key can be minted, so there is nothing to leave
  lying around if the switch is later flipped off again.

**It is a deployment setting, not a product setting.** No administrator — tenant
or otherwise — can turn this on from inside the application; it takes a change to
the stack configuration and a deploy. That is the point rather than an
inconvenience. Everything else in this document is an *internal* control: who may
mint a key, which account it names, what it may reach. This switch is the one
control that answers a different question — *should this organisation have an
internet-facing door onto its documents at all?* — and that question belongs to
whoever owns the deployment, not to whoever administers a tenant inside it.

For many deployments the answer is simply no, and **private instances serving
higher-security organisations are the clearest case**: a single-tenant or on-prem
installation exists precisely because its documents are not supposed to be
reachable from the public internet, and for it this feature is not a capability
that went unconfigured — it is a risk that was declined. A document store with an
authenticated user population has one clean boundary. This feature deliberately
puts a hole in that boundary and then fills the hole with review steps (§6.1),
confinement (§6.3) and budgets (§6.8). Those controls are worth having; they are
not worth *needing* if you were never going to publish anything.

So the switch is not only a default, it is an answer such a deployment can point
at. The routes are absent, no table is provisioned, no key can exist, and the
feature contributes no attack surface to review — which is a materially different
statement from "the feature is present but no keys are configured", and it is the
statement a security review of a private instance actually needs. An organisation
that never sets the variable should never have to reason about any of the rest of
this document.

The consequence to plan for: turning the switch off with keys already live is an
immediate outage for every proxy pointed at the deployment. It is an intentional
kill switch — the coarsest one this feature has, above per-key `/revoke` and above
the account-level stop of §6.2 — and the admin panel says what it will break.

### 9.2 Knobs

Per-key `budgets` override these; these are the deployment defaults and the
ceilings a key cannot exceed.

| Env var | Default | Purpose |
|---|---|---|
| `CSAI_EXTERNAL_API_ENABLED` | `false` | **Master switch (§9.1).** Off ⇒ the public routes, the admin routes and the SPA panel all cease to exist. |
| `CSAI_EXTERNAL_KEY_CACHE_TTL` | `60` | Key + identity resolution cache (s). Bounds revocation latency. |
| `CSAI_EXTERNAL_SCOPE_TTL` | `60` | Containment-set + customer-folder cache (s). |
| `CSAI_EXTERNAL_PROMPT_TTL` | `60` | System-prompt document cache (s). |
| `CSAI_EXTERNAL_PROMPT_MAX_BYTES` | `32768` | Prompt document size cap. |
| `CSAI_EXTERNAL_MAX_HISTORY_TURNS` | `12` | Server-side history depth. |
| `CSAI_EXTERNAL_TURNS_PER_MINUTE` | `30` | Per key. |
| `CSAI_EXTERNAL_TURNS_PER_DAY` | `5000` | Per key. |
| `CSAI_EXTERNAL_VISITOR_TURNS_PER_MINUTE` | `6` | Per `(key, customer_ref or session_ref)`. |
| `CSAI_EXTERNAL_VISITOR_TURNS_PER_DAY` | `200` | Per `(key, customer_ref or session_ref)`. |
| `CSAI_EXTERNAL_MAX_K` | `8` | Retrieval depth ceiling on this door (below `CSAI_MAX_CHAT_K`). |
| `CSAI_EXTERNAL_SSE_KEEPALIVE_S` | `15` | SSE comment interval. |
| `CSAI_EXTERNAL_CONVERSATION_TTL_DAYS` | `30` | Inactive-conversation retention. |

That the switch defaults off is not ceremony: the edge proxies `/csai/` as a whole
prefix, so anything mounted here is on the public internet unless something stops
it — which is precisely how `/ingest/reconcile` became an open denial-of-service
lever (`api.py`, the `_require_internal` header). A feature whose entire purpose is
to be reachable from the internet has no business being reachable by default.

---

## 10. Frontend (tenant-admin panel)

A **External chat keys** panel beside the existing MCP integrations admin,
reachable by the same roles. It shows, per key: name, principal, scope folders
(as paths, resolved for display), prompt root + default prompt, customer root,
tool allowlist, budgets, `last_used_at`, and a **health state** derived from
`POST /{id}/test` — green, or a specific failure (`principal unresolvable`,
`prompt unreadable`, `scope empty`, `principal overreaches`).

**The whole panel is absent when the feature is off** (§9.1) — it renders only
when `/v1/capabilities` reports `external_api.available`, through the same
capability path that already hides absent services.

The **principal is chosen from a picker** of the tenant's existing accounts, never
typed — a typo must not become a silently different identity. The picker shows,
beside each account, the roles it holds and flags the ones in an administrative
group, because "this is a person's admin account" is exactly what the selecting
administrator needs to notice and exactly what a uid on its own hides.

**Minting is gated on the attestation**, which is the feature's review step and
not a confirmation dialog for its own sake: *"Sign in as `<principal>` and browse.
Everything you can see there is what the public will be able to ask about. I have
reviewed it."* The key cannot be created without it, the attesting administrator
and timestamp are shown on the key from then on, and a later edit that changes the
principal or widens the scope asks again.

Three further pieces of copy carry decisions that are otherwise invisible:

- On the secret, at creation: shown once, cannot be recovered, rotate if lost.
- On revocation: effective within `CSAI_EXTERNAL_KEY_CACHE_TTL` seconds across
  all replicas — not instantly.
- On the scope, prominently: **everything inside it should be treated as
  published**. §8.3's residual is a procedural control, and procedural controls
  only work if they are written where the person choosing the folder reads them.

The scope, prompt-root and customer-root pickers are the existing folder picker
the "Limit to folders" chat tool already uses.

---

## 11. Audit events

Emitted through `audit.record` (`audit.py:45`) onto the same stream, content-free:

| Action | When | Notable fields |
|---|---|---|
| `external_chat` | a turn completes | `key_id`, `customer_ref`, `customer_context` (`present`/`missing`/`n/a`), `conversation_id`, `prompt_uid`, `chunks`, `citations`, `tools_used`, `result` |
| `external_chat_denied` | 401 / 429 / 503 | `key_id`, `customer_ref`, `reason` (the real one), `result: denied` |
| `external_key_create` | a key is minted, i.e. a corpus is published | `key_id`, **`principal`**, `scope_folder_uids`, `modes`, `attested: true`, actor = the attesting administrator. **This is the record of who reviewed the account and published its reach** — the principal is named in the event itself so "which accounts have been exposed this way" is one query over the chain, not a join against a mutable table |
| `external_key_update` / `_rotate` / `_disable` / `_enable` / `_revoke` / `_delete` | admin action | `key_id`, `changed_fields`, `reattested` where applicable, actor = the administrator |
| `external_conversation_delete` | forget request | `key_id`, `conversation_id` |

`actor` on the first two is `external:<key_id>|<customer_ref>` — the door and the
pseudonymous human who walked through it — with the principal in `detail`. This
is the `share:<link_uid>|<verified_email>` shape from `OUTSIDE_SHARE_LINKS` §4.3,
and it carries the same consequence: **the core's own events attribute everything
to the scoped account**, so the audit chain is the sole custodian of the fact that
a request came from outside. Querying the core for "who read this file" will show
the scoped account, many times, and cannot be asked otherwise.

**The question text is never logged.** `audit.py`'s invariant holds here and
matters more: on this door the questions are third-party content from members of
the public. The retained transcript in `conversation_messages` is a separate
store with its own retention (§5.3), which the proxy's privacy notice must
disclose (§13).

Metrics (`metrics.py`, unchanged pattern): `csai_external_turns_total{key_id,result}`,
`csai_external_denied_total{reason}`, `csai_external_turn_seconds`,
`csai_external_prompt_cache_hits_total`, `csai_external_scope_size`.

---

## 12. Testing

The invariants that have no second line of defence behind them, and therefore
must be tests rather than intentions:

1. **Containment on every tool.** A document outside the scope is unreachable via
   `document_search`, `get_document_text` *and* `list_folders` — asserted
   separately for each, because each is enforced in a different place (§6.3).
2. **Caller cannot widen.** `scope_folders`, `system_prompt`, `history` each
   produce a 400, not a silent ignore.
3. **Admin roles are stripped** before any core call, including the
   `administrators → tenant_admin` promotion `client_for` performs.
4. **Pre-flight refuses an overreaching principal** (§6.1 check 4) against a
   fixture tree with default-read ACLs.
4b. **Minting requires the attestation.** A create call without it is refused, and
   a successful one persists `attested_by` / `attested_at` and emits
   `external_key_create` **naming the principal** (§11) — the record the whole
   review step exists to produce.
4c. **The principal is re-resolved per request, not cached from mint.** Deleting
   the account, or removing its last tenant role, stops every key naming it
   (§6.2) — the account-level kill switch.
4d. **The master switch removes the feature.** With `CSAI_EXTERNAL_API_ENABLED`
   false, every `/external/v1/*` *and* `/v1/admin/external-keys` route returns
   **404** (not 403), a previously valid key authenticates nothing, and
   `/v1/capabilities` reports it unavailable (§9.1).
5. **Resume-key binding.** A valid resume key presented with a different
   `customer_ref`, or by a different API key, is a 404 — and the conversation is
   not touched.
6. **No cross-customer leakage.** Customer A's turn never retrieves from B's
   folder, with `customer_root` containing both.
6b. **Modes are enforced both ways.** A `customer_ref` on a public-only key and a
   turn without one on an identified-only key are each 400 `mode_not_permitted`;
   a public-mode turn retrieves from the base scope and nothing else; an
   identified-mode turn retrieves from base *plus* the customer folder, with the
   base still present.
7. **Prompt fail-closed.** Deleting the prompt document produces 503, not a
   personality-less answer.
8. **Budgets are shared across replicas** — two app instances against one Redis
   enforce one allowance (the trap §6.8 names).
9. **Revocation** stops the next request after the cache TTL, and `/revoke`
   busts the cache immediately on the replica that served it.
10. **Uniform failure**: unknown key, wrong secret, revoked key and disabled key
    produce byte-identical responses.
11. **SSE** emits `meta` before `done`, keepalives on a slow turn, and a clean
    `error` event when the provider fails mid-stream.

---

## 13. The proxy contract (non-normative)

The proxy is a separate service project, started once the CSAI side above is
settled. This section is the seed for its design document, not that document.

**What it must do**

0. **Serve two modes, and make the difference visible.** *Public mode* for an
   unidentified visitor — the key's base scope, the corpus reviewed in §6.1 step
   2 — and *identified mode* once a customer has proved who they are, which adds
   their own folder to that context. The proxy owns the transition between them:
   the offer to identify, the challenge, and the switch. Two things follow. It
   must pass a `session_ref` for anonymous sessions or every visitor shares one
   budget (§6.5); and it should select a **different prompt document** per mode
   (`GET /prompts`, §7.1) — a bot that can see a customer's file should introduce
   itself differently from one that cannot, and that is a document edit rather
   than a code change.
1. **Identify the customer** using the share-link mechanism: `POST` an email to
   `ldap_manager`'s `/internal/share/email-challenge`, then
   `/internal/share/email-verify` (`OUTSIDE_SHARE_LINKS` §6.9), guarded by the
   shared internal secret. It owns the recipient allowlist question — whether
   *anyone* may request a code or only known customers. **The allowlist decision
   in `OUTSIDE_SHARE_LINKS` applies with full force here**: accepting an
   arbitrary address makes an internet caller the chooser of a destination for
   tenant-branded mail, i.e. an open relay with the deployment's sending
   reputation behind it. An unauthenticated public chat implies an *open*
   audience, so the proxy must reconcile those two before it writes a line of
   code — this is the largest unresolved question in the whole feature (§15).
2. **Hold the API key** as a server-side secret. It never reaches a browser.
3. **Derive `customer_ref` as a pseudonym** — an HMAC of the verified email under
   a proxy-held key, not the email. CSAI stores it in `conversations` and in the
   audit stream; it should be unlinkable to a person without the proxy.
4. **Store the resume key** per customer session and present it with the same
   `customer_ref` every turn (§6.6).
5. **Strip internal references from citations.** `file_uid`s address files the
   customer cannot open and should not know exist. Render document citations as
   titles, or map them to a public URL the proxy owns — never pass the UID
   through to a browser.
6. **Rate-limit its own users** before calling CSAI. CSAI's budgets are the
   backstop, not the first line; hitting them means every customer of that key is
   already degraded.
7. **Serve its own privacy notice.** Conversations are retained server-side for
   `CSAI_EXTERNAL_CONVERSATION_TTL_DAYS`; the customer must be told.
8. **Fail visibly.** A `prompt_unavailable` or `budget_unavailable` from CSAI is
   an operator problem, not a customer one — it belongs in the proxy's alerting.

**What it must never be trusted for.** CSAI treats the proxy as authenticated,
not as correct. The `customer_ref` is an assertion, which is why it names a
customer rather than a folder (§6.5); the scope, prompt set and tools come from
the key row, not the request; and every retrieval is still gated by the
principal's live core permissions. A fully compromised proxy reaches the scoped
subset and one customer folder per reference — which is exactly the set the key
was minted to publish.

---

## 14. Implementation stages

1. **M0 — the switch, the key, the door.** `CSAI_EXTERNAL_API_ENABLED` first and
   wired everywhere (§9.1) — public routes, admin routes, capability reporting —
   so no later stage can land a surface that is reachable by default.
   Then: `external_api_keys` + migration; `ExternalKeyStore` on the
   `McpIntegrationStore` pattern; the admin router (mint / list / update / rotate
   / disable / enable / revoke / delete / test) with the attestation requirement
   and the §6.1 pre-flight; `ldap_auth.identity_for` (passwordless resolution +
   admin-role stripping, re-resolved per request); the `/external/v1` router with
   `capabilities` and a non-streaming `POST /chat` that uses the *existing* scope
   filter only.
   **Tests:** 3, 4, 4b, 4c, 4d, 9, 10 from §12.
2. **M1 — containment.** Thread the containment set through `SearchService.search`,
   `DocumentSearchRepo.query`, `ToolContext`, `get_document_text` and
   `list_folders`; the caller-cannot-widen rejections; the scope cache and its
   event-driven busting. This is the milestone the design actually stands on.
   **Tests:** 1, 2.
3. **M2 — prompt, modes, conversations.** The prompt document (read, cache,
   compose, fail-closed) and `GET /prompts`; **public and identified modes** and
   the per-key `modes` gate; `customer_ref` resolution under `customer_root`;
   resume keys and server-side history; `DELETE /conversations`; retention.
   **Tests:** 5, 6, 6b, 7.
4. **M3 — budgets, streaming, operations.** Redis buckets per key and per
   customer; 429 with `Retry-After`; fail-closed on a Redis outage; the SSE
   variant; metrics; the nginx `limit_req` zone; provider usage reporting so a
   later token ceiling has something real to count.
   **Tests:** 8, 11.
5. **M4 — the admin panel.** The frontend surface of §10: the capability-gated
   render, the account picker with its role flags, the attestation gate on mint,
   the health state, and the "treat this scope as published" copy.

Each stage must land with an empty diff against `file_engine_core`; that is a
mechanical check, not an intention.

---

## 15. Still open

1. **Who may request a verification code on the proxy?** Settled in outline,
   open in detail. Public mode (§6.5) resolves the awkward half — a stranger can
   chat without identifying at all — so identification is no longer the price of
   entry and the recipient set can be *closed* without making the bot useless to
   the public. What remains is the proxy's own question: the share-link design
   forbids mailing a code to an arbitrary typed address outright (open relay with
   the deployment's sending reputation, `OUTSIDE_SHARE_LINKS` §6.9), so the proxy
   needs a registry of who its customers are. The obvious source is the one that
   already exists — a customer has a folder under `customer_root`, or they do not
   — but reaching it means the proxy asking CSAI whether an address maps to a
   provisioned customer, and this document deliberately exposes no such route
   (§7.1). Whether to add one, or to have the proxy keep its own registry, is the
   first decision of the proxy project.
2. **Token ceilings.** Deferred to M3+ pending provider usage reporting (§6.8).
   Until then a runaway key is bounded by turn counts, not by spend.
3. **Per-key provider/model choice.** A cheap model for the public door and a
   better one for staff is an obvious want, and is a small addition to the key
   row — but it interacts with the per-tenant provider selection already listed
   in `CHAT_WITH_AI` §4.4. Left out of v1 to avoid two half-built mechanisms.
4. **Prompt-injection from corpus content** (§8.3). Procedural in v1. A
   structural answer belongs to the platform-wide item in `CHAT_WITH_AI` §4.4.
5. **Multiple keys per principal.** Allowed by the schema; whether the admin UI
   should encourage one key per proxy deployment (easy rotation) or one per
   audience is unsettled.
6. **Drift in the account's own access.** A widened scope or a changed principal
   re-asks for attestation (§6.1, §7.2), but the account's ACLs live outside
   CSAI: granting it read on a new folder widens the door immediately, with
   nothing here to notice and nobody re-attesting. The available signals are
   weak — subscribing to core `acl.changed` events for the principal would flag
   grants but not inherited ones, and a periodic re-run of the §6.1 check-4
   root walk would catch coarse widening at the cost of a scheduled job that
   reports on a thing no one asked about. Left unsolved rather than half-solved;
   in the meantime §10's copy tells the operator that the scope is published
   material and that changing what the account can read changes what is
   published.

---

## 16. References

CSAI: `api.py` (the WS door this parallels), `chat.py` (`ChatService.answer`),
`retrieval.py` (`_resolve_scope_file_uids`), `search.py`, `llm_tools.py`,
`permissions.py`, `guards.py`, `http_auth.py`, `token_store.py`,
`ldap_auth.py` / `tenant_access.py`, `core_client.py`, `mcp_store.py`,
`routers/mcp_admin.py`, `schema.py`, `config.py`, `audit.py`.
Design: [CHAT_WITH_AI](./CHAT_WITH_AI.md), [SPECIFICATION](./SPECIFICATION.md),
[MCP_INTEGRATIONS](./MCP_INTEGRATIONS.md),
[GENERATE_REPORT_TO_TARGET](./GENERATE_REPORT_TO_TARGET.md).
Cross-repo: `share_service/design_documents/OUTSIDE_SHARE_LINKS.md` (the external
door this borrows its delegation, uniform-failure and audit-actor shapes from),
`docker_unified/images/nginx/snippets/tenant.conf`.

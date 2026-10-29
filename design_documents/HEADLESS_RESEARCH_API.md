# Headless research API (desktop applications)

Status: **investigation** (branch `research/headless-research-api`, opened 2026-10-29).
Nothing here is implemented yet.

## 1. Goal

Desktop applications (Reviera first) drive the research tooling without a
person in a browser. In headless mode it must:

- do **focused, structured fact extraction** from the documents in a specific
  folder. This is the strongest case and the main mode. Reviera pulls given
  values (manufacturer, model, dimensions, fire rating, EPD figures,
  certificate numbers and expiry dates, ...) from datasheets and
  sustainability certificates to populate model metadata;
- still produce **reports**, by the **existing technique** (owner,
  2026-10-29): the report always lives in FileEngine, so its provenance
  sidecar is kept; the desktop application downloads the file;
- **stream** progress and results (SSE), with a non-streaming option for
  simple callers.

Desktop apps sign in with generated credentials: API key → `/v1/auth/token` →
session bearer, renewed while the key is live (http_bridge 1.9.66).

## 2. What exists (reviewed 2026-10-29, csai master 81343b3)

**Usable as is**

- `ChatService.answer()` (`chat.py`) is a transport-neutral generator of events:
  `token`, `tool_call`, `tool_result`, `citations`, `report_saved`, plus `error`
  from the transport. It already does permission-scoped retrieval, folder
  scoping (`scope_folder_uids`), the document search/read tools, optional web
  search, MCP tools and pinned-destination reports (`report_target`).
- Authentication: `resolve_identity` → `BridgeTokenVerifier` verifies http_bridge
  HS256 tokens locally (`jwt_verify.py`) and checks the jti revocation list, so
  API-key session tokens already work here. Tenant binding is enforced.

**Shaped for the browser**

| # | Gap | Where |
|---|---|---|
| 1 | Only transport is `WS /chat`. Errors are frames, not statuses. A client disconnect does not stop the provider call (the worker thread runs to completion). | `api.py` `chat`, `_stream_answer` |
| 2 | No structured output. Every prompt asks for prose with `[n]` markers and `(file <uid>)` references meant for the SPA linkifier. Providers have no JSON mode or schema parameter. | `chat.py` `_INSTRUCTIONS*`, `providers/chat.py` |
| 3 | A report is only produced into FileEngine (as `.docx`) when a folder and filename are pinned. The draft cannot be returned to the caller. | `_save_marked_reports`, `llm_tools.save_report_document` |
| 4 | MCP tool calls block on an interactive `tool_consent` reply (120 s timeout, then deny). A headless caller would stall on every external tool. | `consent.py`, `_stream_answer.read_control` |
| 5 | No per-user or per-key rate limit or token budget. `CSAI_MAX_QUERY_CHARS` = 1000 is too small for headless prompts that carry instructions or data. | `config.py`, `guards.py` |
| 6 | Every turn is persisted to the user's one conversation list; there is no notion of which client made it, so automated runs would mix into the user's research sessions. | `_begin_turn` / `_end_turn`, `conversations` table |

Production model: `openai-compatible` → DeepInfra, `deepseek-ai/DeepSeek-V4-Flash`
(scripts `roles/convert_search_ai/defaults/main.yml`). Whether it honours
`response_format` (`json_object` / `json_schema`) must be measured (§5).

## 3. Proposed shape

New routes over the existing services; `WS /chat` is unchanged for the SPA.
Every route answers `text/event-stream` (default) or `application/json`
(wait, return the final result only).

### 3.1 Extraction — `POST /csai/v1/extract` (the main mode)

Extraction is not chat. RAG top-k retrieval across the tenant is the wrong
tool: a value buried in one table of one datasheet is easy to miss, and a
value from the wrong product's datasheet is worse than none. Extraction works
**per document**: each file in scope is read whole (its extracted Markdown,
paged when long) and asked the same schema, independently.

**Source text.** Each file is read from csai's own stored Markdown,
`documents.content_md`: the whole document, extracted at index time with its
table structure kept as Markdown tables. Nothing is converted again at
request time. `documents.source_version` is the FileEngine version that text
came from, and becomes the result's version stamp.

**Stale or missing text.** Indexing is asynchronous, so the stored text can
lag the file. Before reading, compare `source_version` with the file's current
version (`Stat`):

- current: extract;
- behind, or `status` pending: re-index that file first (the ingest path
  already exists), then extract; the `item` is late, not wrong;
- `status` failed, or no text (`content_md` NULL; for example a scanned
  certificate with no text layer): `skipped` with the reason, never an empty
  "nothing found".

**Markdown is the input, by decision** (owner, 2026-10-29): extraction reads
`content_md` and nothing else until advanced multimodal model support exists.
A file with no usable text (a scan with no text layer) is reported as
`skipped: no text` so the caller knows to check it by hand; there is no OCR or
image pass in this work.

```json
{
  "folder_uid": "<uid>",
  "recursive": false,
  "files": ["<uid>"],
  "match": { "extensions": [".pdf"], "name": "*datasheet*" },
  "schema": { "...": "JSON Schema of the values wanted" },
  "instructions": "optional guidance (units, which product, ...)"
}
```

**Target: a folder or a list of document ids, both supported** (owner,
2026-10-29). Exactly one per request; both or neither is a 400.

- `folder_uid`: every file in the folder (`recursive` for subfolders),
  optionally narrowed by `match`. Renditions and other hidden children are
  never included.
- `files`: explicit document ids, which may sit in different folders (the
  documents a model element already links to). Order is kept, duplicates are
  dropped, and an id the caller cannot use is reported as `skipped:
  not found or not readable`, never silently dropped: the caller named it.
  One reason for both, so the route cannot be used to probe which ids exist.
  Check existence explicitly as well as READ: the core's `CheckPermission`
  answers true for an id that does not exist.

The schema describes the values; the service wraps each one with evidence, so
the caller never has to ask for it:

```json
{ "fire_rating": { "value": "EI 60", "unit": null,
                   "evidence": { "quote": "Fire resistance: EI 60", "page": 3 },
                   "found": true } }
```

SSE events:

| event | data |
|---|---|
| `plan` | the files in scope (uid, name, version), so a client can show progress |
| `item` | one file's result: `{file_uid, name, version, data, valid, skipped?}` |
| `error` | `{file_uid?, error, kind}`. A per-file error does not end the run |
| `done` | summary: counts of extracted, not found, skipped, failed |

Rules:

- **Absent means `found: false`, never a guess.** Each value needs a
  supporting quote from that document. A value without one is dropped, and
  the drop is counted.
- **Validated** against the schema, with one repair attempt per file.
- **Version-stamped.** Each `item` names the version it was read from, so
  Reviera can tell when a datasheet has been revised since a value was taken.
- Only files the caller can READ; unreadable ones are skipped silently,
  exactly as search does.
- No web search and no MCP tools: the answer comes from the documents only.
- Cancelling (client disconnect) stops after the current file.
- Results are not written anywhere by the service. Reviera decides what
  becomes model metadata.

### 3.2 Reports — the existing technique

Reports use today's pinned-destination report mode unchanged: the model writes
`[[SAVE_REPORT ...]]`, the service saves a `.docx` with its provenance sidecar
into the pinned folder, and emits `report_saved {uid, name, path}`. The desktop
application then downloads the file through the REST door
(`GET /v1/files/{uid}/content`). The SSE route only needs to carry the same
events; the client relies on `report_saved`, not on the streamed tokens (which
include the markers and a server-written confirmation line).

### 3.3 Questions — `POST /csai/v1/research`

The existing chat (answer mode) over SSE, for a desktop "ask about these
documents" panel. Same body as a `WS /chat` frame. Lowest priority of the
three.

### 3.4 Authentication: shadows the REST door

Decided (owner, 2026-10-29): the headless routes authenticate exactly as the
REST door does, with the **same generated key:secret**. There is no second
credential and no csai-specific sign-in:

1. The desktop application exchanges its key at the bridge:
   `POST /api/v1/auth/token` with Basic `fesk_…:fesks_…` returns a session
   bearer, issued only for a key that holds the `api` scope.
2. It sends that bearer to csai (`Authorization: Bearer …`), exactly as to the
   REST door.
3. It renews it at the bridge (`POST /api/v1/auth/refresh`); the bridge asks
   ldap_manager whether the key is still live, so revoking or rotating the key
   ends csai access at the next refresh, as it does for REST.

csai already verifies bridge tokens locally (HS256, shared secret), binds them
to their tenant, and checks the jti revocation list, so a signed-out session is
refused here too. What this work adds:

- Basic `fesk_…` sent straight to csai is refused with a pointer to the bridge's
  token route, instead of being tried as an LDAP bind (today it would just fail
  as a wrong password).
- The audit record carries the key id (`skid`) from the token.
- End-to-end test: key → bridge token → `/csai/v1/extract` works; refreshed
  token works; signed-out token is refused; a key without the `api` scope never
  gets a token.
- Authentication is checked when a request starts. A long extraction may
  stream past the token's expiry, as a long REST download does; each new
  request needs a live token.

If keys later need research access without full REST access, a separate `ai`
scope would be checked here from a claim the bridge adds (§4, decision 2).

### 3.5 Review in the web application

Decided (owner, 2026-10-29): headless runs are **kept**, and are reviewable in
the web application, but **out of the user's research sessions**: under a
folded list per headless application.

**What is an application.** The generated key the desktop application signed
in with. One key, one fold, labelled with the key's name as the user gave it on
the credentials card ("Reviera — office laptop"). A user with two desktop
installs, each with its own key, sees two folds.

**Storage** (additive, same per-tenant tables):

- `conversations` gains `origin` (`interactive` default | `headless`),
  `client_key_id` (`skid`) and `client_label`.
- An extraction run is stored as one conversation: the request (target,
  schema, instructions) as the first message, then one message per file
  `item` with its JSON result, evidence and version stamp, then the summary.
  A report run or a question is stored as today's turns.
- `GET /conversations` keeps returning **interactive only** by default, so the
  research sidebar is unchanged; `?origin=headless` returns headless runs
  grouped by `client_key_id`, with label, run count and last run.

**The label.** csai sees only the key id in the token. The bridge adds the
key's name as a claim (`skname`) when it issues a key session, and csai stores
it with each run. The fold shows the latest label; a revoked or deleted key
keeps its fold under its last name, marked revoked, so its history stays
reviewable.

**Web application** (frontend): the research sidebar gets an *Applications*
section, collapsed by default, one fold per key. Opening a run shows it
read-only: request, per-file results as a table (value, evidence quote,
document link, version), and any report as its saved-file link. A headless run
is not continued as a chat.

**Ownership.** A key session acts as its owner, so runs belong to that user,
like their chats; nobody else sees them.

### 3.6 Accountability: the platform's conventions, unchanged

Decided (owner, 2026-10-29): headless use follows the existing conventions;
it does not get looser rules because no one is watching.

**Attribution.** Every run is attributed to the key's owner *and* the key:
`user`, `tenant`, `client_key_id` (`skid`), `client_label` (`skname`). Core
calls are made as the owner, as for a chat. A report written from a headless
run carries `revised_by` = the owner in the core and the key in its sidecar.

**Audit** (`audit.record`, the secret-free access log): as detailed as
possible about shape and outcome, never about content.

- per run: `action=extract` (or `research`), result, key id and label, target
  kind (folder uid, or count of ids), file count, schema hash and field
  names, model and provider, counts (extracted, not found, dropped for no
  evidence, skipped, failed), duration;
- per file: `action=extract_item`, `file_uid`, the version read, result, and
  per-file counts;
- never: values, evidence quotes, file names, the caller's instructions, or
  tokens. Files are referenced by uid only (the redaction line: identifiable
  information from file content or file names stays out of the audit).

**Provenance.**

- Reports: the existing chatlog sidecar, unchanged, plus `origin: headless`,
  the key id and label. The sidecar is still a hidden child of the report, so
  it lives and dies with it.
- Extraction runs: the stored run is the provenance record, holding the same
  facts the chatlog holds: who (owner and key), model and provider, the prompt
  version, the request (target, schema, instructions), each file's version
  read, and each value with its evidence quote. PII is redacted in stored
  quotes, as in the chatlog transcript; the live response to the caller is not
  redacted.

**Retention: kept.** Data is retained by default, so headless runs are kept;
there is no expiry. Unlike a chat, a headless run is **not deletable from the
web application**: it is the record of what an automated client did as the
user. (Resolves §4 decision 4, which is withdrawn.)

**Erasure reaches runs.** Erasure is the one sanctioned way content leaves.
Per-file results are stored keyed by `file_uid` (a `headless_results` table,
not free text in a message), so when a source file is erased, csai's erasure
handler removes that file's values and quotes from every run and leaves a
tombstone (`file_uid`, `erased`, `erasure_id`): the record that the run read
the file survives, its content does not.

**Finding: interactive chats are not reached by erasure today.** `Store.erase`
deletes the file's `documents` and `chunks` rows and records it in
`erased_documents`, but stored chat messages (`conversation_messages`) that
quote the file are untouched. That is an existing gap, separate from this work;
the headless storage above is shaped so it does not repeat it.

### 3.7 Extraction cache: a sidecar on the source file

Decided (owner, 2026-10-29): extracted values are cached **in a sidecar of the
source file**, so the same question about the same version of the same
document is never paid for twice.

**Shape.** One hidden child per source version, by the existing rendition
convention: `<version>-extract.json` (`rendition_name(version, "extract",
"json")`; `extract` joins `_KNOWN_FMTS`). Inside, one entry per question:

```json
{ "schema": "fileengine.extract_cache.v1",
  "file_uid": "...", "version": "20261029_120000.123",
  "entries": {
    "<key>": { "schema_hash": "...", "instructions_hash": "...",
               "model": "...", "provider": "...", "prompt_version": "extract.v1",
               "created_at": "...", "created_by": "<owner>", "client_key_id": "...",
               "data": { "...": "values with evidence and found, as returned" } } } }
```

`key` = hash of (schema, instructions, model, prompt version). A different
model or a revised prompt is a different question and does not hit an old
answer.

**Lookup.** Before calling the model for a file: read its current version's
sidecar; on a key hit, return the entry as the `item` with `cached: true` and
its original `model` and `created_at`. No model call, no tokens. On a miss,
extract, then add the entry. `"cache": "use"` (default) or `"refresh"`
(re-extract and replace that entry, for when an answer looks wrong).

**Why this place.** It follows the file:

- version-keyed: a revised datasheet is a new version with no sidecar yet, so
  stale values are never served; superseded versions' sidecars are pruned by
  the existing `prune_old_versions`, as other renditions are (the history of
  what was extracted stays in the headless run records, §3.5);
- access follows the file's ACL: anyone who can READ the datasheet can reuse
  what was read from it, and no one else;
- lifetime follows the file: it is a hidden child, so erasure of the source
  file takes it too (verify that `EraseFile` destroys hidden children with the
  file, §5);
- one user's run warms the cache for every other user and application in the
  tenant reading the same document with the same question.

**Writing.** As csai's own service identity through `RenditionWriter`, as
renditions are written, so a caller with only READ still gets their result
cached. Read access is still checked as the caller before any lookup.
Concurrent runs adding entries to the same sidecar may race; the loser's entry
is simply missing and is recomputed next time (no wrong answer, just a repeat
cost). Each write is a new version of the sidecar, so earlier entries are
retained in its history.

**Accountability.** A cache hit is still a run: it is audited
(`extract_item`, `cached=true`) and stored in the run record like any other
result; only the model call is skipped. The audit counts hits, so the tokens
saved are visible.

**Check in the web application:** the SPA lists renditions it recognises; it
must treat `extract` as non-previewable, like `chatlog`.

### 3.8 Common behaviour

- MCP tools omitted by default (headless cannot answer a consent prompt).
- Larger headless input cap; rate limit and token budget (§4).
- Audit and provenance per §3.6.
- `X-Accel-Buffering: no` on SSE; check the `/csai/` proxy read timeout.

## 4. Open decisions (owner)

1. Rate limits and budget: per user, per API key, or both?
2. A separate `ai` scope on generated credentials (research access without
   full REST access), now or later? Today `api` covers both.
3. ~~Cache extractions?~~ Resolved by §3.7 (the file's sidecar).
4. ~~Retention for headless runs~~: resolved by §3.6 (kept; not deletable;
   erasure reaches them).

## 5. Investigation tasks

- [ ] Measure DeepSeek V4 Flash on DeepInfra: does `response_format`
      `json_object` / `json_schema` work? How often is output invalid without
      enforcement, and how often does it invent a value that is not in the
      document (the evidence rule catches these: measure how many)?
- [ ] Same for the Anthropic provider (tool-forced output as the schema route).
- [ ] Long documents: page through the extracted text per file, or retrieve
      within the one file first? Measure cost per datasheet.
- [ ] Cancellation: close the provider stream from the worker thread when the
      SSE client goes away (OpenAI SDK `completion.close()`).
- [ ] nginx: does the `/csai/` location buffer or time out SSE today?
- [ ] Confirm `EraseFile` destroys a file's hidden children (the extraction
      cache sidecar) along with it.
- [ ] Prototype `/v1/extract` and an end-to-end test driven by an API-key
      session token over a fixture folder of datasheets, as Reviera would.

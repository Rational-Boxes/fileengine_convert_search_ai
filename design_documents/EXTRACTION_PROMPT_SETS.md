# Extraction prompt sets, folder automation and the marked-up PDF

Status: **plan, agreed** (2026-11-02). Nothing implemented. Spans
convert_search_ai (csai), folder_actions (FA), frontend and scripts. The
owner's decisions are recorded in §9.

## 1. Goal

1. A **prompt builder**: a person defines, once, *what to extract* from a kind
   of document (fields, types, units, instructions) as a named, versioned
   **prompt set**, tries it on real documents, and reuses it everywhere.
2. **Folder automation, trigger**: a folder can be told "when a file's text
   extraction completes, run prompt set X (or the set its classification
   picks)".
3. **Folder automation, act**: when that extraction completes, file the
   document and act on the extracted values, interpreted through the prompt
   set's schema (typed comparisons, metadata writes, filing, review, notify).
4. **Marked-up PDF**: the extractor also produces a PDF with every extracted
   value's evidence highlighted, stored as a sidecar of the source file.

The marked-up rendition is a **PDF** (owner, 2026-11-02: "ODF" in the
request was a typo). ODT *sources* are covered: they are marked up through
their PDF rendition (§5.2).

## 2. What exists

### 2.1 `/home/telendry/code/ai/classifier_and_extractor` (reviewed 2026-11-02)

A small FastAPI service plus a browser "Workbench". Implemented:

| Piece | What it really is | Port? |
|---|---|---|
| Classifier (`lib/classifier.py`) | Fuzzy n-gram term matching (rapidfuzz Levenshtein, `*`/`?`/`#` wildcards, digits exact), weighted sums, raw scores, no threshold | **Already ported**: FA `classifier.py` `document_classifier_simple`, with `classifier_set`/`classifier`/`classifier_term` tables and an admin panel. Nothing to do. |
| Extractor definition | `extractors(name, prompt)` + `extractor_fields(name, description)`; a field's "type" is only the wording of its description; field order not guaranteed | The *idea* (a question + named fields with descriptions) and the YAML import format. Not the storage. |
| Prompt template | One hard-coded string; confidence, explanation, per-field `{value, citation[]}`, `"unknown"` for missing. **Bug**: `$query` is replaced inside `$query_question`, so the question never reaches the model intact (`prompt_builder.py:59-61`) | No. csai's `extract.v1` prompt is stricter (found:false, quote checked). |
| Context | pgvector top-20 chunks with the *prompt* as the query, else 1500-word windows where the **first window with found:true wins** (later windows' fields are lost) | No. csai reads the whole stored Markdown in windows and merges per field. |
| Evidence | Quote strings only; never checked against the text; no page/offset | No. csai drops any value whose quote is not in the document and records its offset. |
| Marked-up PDF (`api/pdf_markup/highlight_pdf.py`) | PyMuPDF `page.search_for(quote)` on every page, plain yellow highlight, no per-field colour, no note, no legend; unfound quotes silently skipped; quotes de-duplicated with `set()` so the field is lost | The approach (PyMuPDF search + highlight annotations), rebuilt (§5). |
| Non-PDF to PDF | Converts beside the source as `<name>.pdf` (can **overwrite** a real `x.pdf` next to `x.docx`); ODT via pandoc only | No. csai already writes a `pdf` rendition for office documents. |
| Per-extractor model | `llm_models` row pinned per extractor (sync service route ignores it) | Yes, as an optional field on a prompt set (§9 D5). |
| Post-extraction actions | **None** (only a webhook POST of the result) | n/a; FA provides this (§6). |

Also noted: unused heavy dependencies (marker-pdf, surya-ocr, torch), an
"anthropic" provider with no code path, no OCR, one unittest file
(`testing.py`).

### 2.2 csai (master 2533196, 1.9.69)

- `POST /v1/extract` (`headless/extract.py`): JSON Schema in, per-document
  whole-text extraction, values wrapped `{found, value, unit, evidence:{quote,
  offset}}`, quote verified against the stored Markdown (dropped if absent),
  per-field validation with one repair round, version-stamped, SSE item per
  file. Prompt `extract.v1`.
- Cache sidecar `<version>-extract.json` (`headless/cache.py`), keyed by
  hash(schema, instructions, model, prompt version), written as csai's service
  identity, pruned with the version.
- `conversion.complete` on `fileengine:events` (`emit.py`) with `renditions`
  (`"text"` when Markdown exists, `"pdf"` when a PDF rendition was written).
  No `parent_uid`, no `name`.
- Rendition names `<version>-<fmt>.<ext>`; `fmt` must be in `_KNOWN_FMTS` and
  never contain `-` (`renditions.py`).
- Per-key usage meter (requests/min, tokens/day, concurrency) for API keys.

**Finding (production, verified in the 1.9.69 image):** PyMuPDF is not
usable. The image installs the `pdf-pymupdf` extra, which pulls
`pymupdf4llm` 2.0. That install has metadata only and no module, so both
`import pymupdf4llm` and `import pymupdf` fail. csai's `pymupdf4llm` PDF
backend has therefore been silently skipped in production (falls through to
pdfplumber). The marked-up PDF needs `pymupdf` as an explicit, pinned
dependency (AGPL, same licence as FileEngine).

### 2.3 folder_actions

- Plug-ins: `ActionPlugin` with `type_name`, `supported_events`, a pydantic
  `ConfigModel`, `config_fields()` (generic `FieldDescriptor` form, incl.
  `options_source` for pick-lists), `execute(event, cfg, ctx) -> ActionResult`.
- Bindings in FA's own Postgres (`action_binding`: folder, recursive,
  on_events, mime_types, config), run log `action_run` keyed
  `(event_id, binding_id)` (idempotency + audit).
- Runs as FA's **service principal**, not the rule owner.
- `sorter` already binds `conversion.complete`, reads text via csai
  `GET /documents/{uid}/text`, classifies, moves. It is the template for
  §6.1.
- `webhook` already applies a response's `move_to` and `metadata{}`: the
  existing "act on values" contract.
- Gaps that matter here:
  - `CoreClient` has no `copy`, `rename` or move-with-new-name (the
    python_interface client has them).
  - Retryable failures are only re-read on consumer **restart** (no
    XAUTOCLAIM, no retry count, no dead letter). An extraction that is
    rate-limited would wait for a restart.
  - Every action runs **inline** in the single consumer. A 20–60 s extraction
    would stall every other folder's events.
  - The loop guard covers only `auto_moves` plug-ins on `file.moved` by FA's
    own actor. A copy makes a new uid that gets its own `conversion.complete`.
  - Secret splitting is special-cased to `action_type == "webhook"`.
  - `on_events` is not checked against the plug-in's `supported_events`.
  - The frontend form emits shapes the models don't accept, to be confirmed
    before reuse: notify `recipients` (list of dicts vs `list[str]`), webhook
    `context` (key/value rows vs dict), webhook auth (flat keys vs `auth{}`).

## 3. Shape of the solution

```
 prompt builder (SPA) ──► csai prompt_set store (versioned) ◄── POST /v1/extract {prompt_set}
                                   │                              (desktop / headless)
 file uploaded ─► csai conversion ─► conversion.complete ─► FA "extract" action
                                                                │  POST csai /v1/extract/jobs (202)
                                         csai extraction worker ◄┘
                                         ├─ <version>-extract.json   (values, cached)
                                         ├─ <version>-extractpdf_<set>.pdf (marked-up PDF)
                                         └─ extraction.complete ─► FA "extraction rules" action
                                                                    ├─ write metadata
                                                                    ├─ file (move/copy/rename)
                                                                    ├─ raise review / notify
                                                                    └─ flag missing required
```

Three choices carry the design:

1. **Prompt sets live in csai** (§9 D1). csai owns extraction, the cache key,
   the model and the prompt version; the desktop API and FA both refer to a set
   by id. FA keeps only a reference, as it does for classifier sets.
2. **Extraction runs as a csai job, not inside FA's consumer.** FA's action
   asks and returns at once; csai's worker does the slow part; completion is an
   event. This removes the inline-blocking and restart-only-retry problems for
   this feature without rebuilding FA's consumer.
3. **Act on `extraction.complete`, not on the extract action's return.** Two
   plug-ins chained by an event, so the act step also works for extractions
   started elsewhere (§9 D4) and its runs are logged and replayable on their
   own.

## 4. Prompt sets and the prompt builder

### 4.1 Model

A prompt set is a named definition with immutable **revisions**. A run always
records the revision it used.

```json
{
  "id": "ps_7f3c…", "name": "Door datasheet", "description": "…",
  "revision": 3, "status": "published",          // draft | published | retired
  "applies_to": {"classifier_set_id": "…", "labels": ["door_datasheet"]},  // optional
  "instructions": "If several door types are listed, use the one named in the file name.",
  "model": "deepseek-v4-flash",                  // id from CSAI_EXTRACT_MODELS (§4.4); null = default
  "fields": [
    {"name": "fire_rating", "label": "Fire rating", "type": "enum",
     "enum": ["EI 30", "EI 60", "EI 90", "EI 120"], "required": true,
     "description": "Fire resistance classification to EN 13501-2"},
    {"name": "width", "label": "Leaf width", "type": "number", "unit": "mm",
     "minimum": 300, "description": "Door leaf width"},
    {"name": "manufacturer", "type": "string", "required": true},
    {"name": "certificate_expiry", "type": "date"},
    {"name": "fsc_certified", "type": "boolean"}
  ],
  "markup": {"enabled": true, "colours": {"fire_rating": "#e8590c"}},  // optional
  "created_by": "…", "created_at": "…"
}
```

- **Field types**: string, text, number, integer, boolean, date (ISO 8601),
  enum, and list-of-string. No nested objects in v1; they do not map onto
  metadata keys or rule conditions cleanly.
- **Compilation**: a revision compiles to the JSON Schema `/v1/extract` already
  takes (`date` → `{"type":"string","format":"date"}`, enum → `enum`, unit and
  label kept as annotations), so the extractor, its validation, repair and
  cache are reused unchanged. `required` is not sent to the model (it must
  still answer found:false); it is used by the rules (§6.2).
- **Storage**: csai tenant schema, `prompt_sets` (id, name, status,
  current_revision) + `prompt_set_revisions` (set_id, revision, body jsonb,
  schema_hash, created_by, created_at). Additive, self-applied like the
  headless tables.
- **Permissions**: tenant-wide objects. Create and edit for tenant admins and
  a new `extraction_editors` role (D2); everyone in the tenant can list and
  use published sets.
- **API** (csai): `GET/POST /v1/prompt-sets`, `GET/PUT /v1/prompt-sets/{id}`
  (a PUT of a published set creates a new draft revision),
  `POST /v1/prompt-sets/{id}/publish`, `GET /v1/prompt-sets/{id}/revisions`,
  `POST /v1/prompt-sets/{id}/test` (§4.3), import/export (§4.6).
- `/v1/extract` gains `{"prompt_set": "ps_…", "revision": n?}` as an
  alternative to an inline `schema` + `instructions`. The cache key already
  covers schema and instructions, so a set's revisions cache separately.

### 4.2 Builder UI (frontend)

A "Prompt sets" admin panel next to Classifier sets:

- a field table (name, label, type, unit, enum values, required, description)
  with reordering; names validated against csai's field-name rule;
- instructions text; optional "applies to" classifier labels;
- a generated-schema view (read-only) and the exact prompt the model will see
  for a chosen document, so nothing is hidden;
- a **model** dropdown filled from the configured list (§4.4), showing the
  deployment default first; a set whose model has been removed from the list
  is flagged "model no longer available" and cannot be published until one is
  chosen;
- a **Test** panel, the same operation as the classifier editor's (owner,
  2026-11-02): **paste text** into a box *or* **choose a stored file**
  (the same `NodeBrowser` picker, kept across runs and set switches, so
  re-testing after an edit needs no re-picking), then **Run test** (§4.3).
  The result shows per-field value, unit, quote (highlighted in the text
  shown), found/not found, dropped values and repair rounds, tokens used and
  the model. For a stored PDF or a document with a PDF rendition, **View
  marked-up PDF** opens the result (§5) in the existing PDF.js viewer.
  Another model can be picked for the test alone, to compare models on the
  same input without changing the set;
- revision history with diff; publish; retire, optionally naming the set that
  replaces it.

### 4.3 Test runs

`POST /v1/prompt-sets/{id}/test` takes **one** of:

- `{"text": "…"}`: pasted text, treated as the document's Markdown (cap
  `CSAI_EXTRACT_TEST_MAX_CHARS`, default 200 000). Quotes are verified and
  offsets given against the pasted text. No PDF, so no marked-up PDF;
- `{"file_uid": "…"}`: a stored file, READ checked as the caller; its stored
  Markdown is used exactly as a real run would (re-indexed first if stale,
  `skipped` with the reason if it has no text).

Optional: `revision` (default: the editor's current draft, sent inline as
`set` so unsaved edits can be tested) and `model` (an id from §4.4, for this
test only).

It runs the normal extractor as the caller, metered as the caller, and
streams like `/v1/extract`. It writes no cache entry (a draft must not fill
the cache with answers to a question nobody has published), no run record
and no stored marked-up PDF. For a file, the marked-up PDF is returned as a
short-lived download for viewing. Pasted text is not logged; the audit
records the set, revision, model, input kind and length, and counts.

A desktop application sees only what someone published from the builder
(§4.5): a set is developed and tried interactively here, and publishing is
the deliberate act that makes it available. Publishing does not require a
recorded test (owner, 2026-11-02).

### 4.4 Configured models

A new configuration key lists the models extraction may use, so the builder
offers a choice and nothing else can be named:

```
CSAI_EXTRACT_MODELS='[
  {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash (fast, low cost)",
   "model": "deepseek-ai/DeepSeek-V4-Flash"},
  {"id": "qwen3-235b", "label": "Qwen3 235B",
   "model": "Qwen/Qwen3-235B-A22B-Instruct"},
  {"id": "claude-sonnet", "label": "Claude Sonnet", "provider": "anthropic",
   "model": "claude-sonnet-5-5", "api_key_env": "CSAI_ANTHROPIC_API_KEY"}
]'
CSAI_EXTRACT_DEFAULT_MODEL=deepseek-v4-flash
```

- Each entry: `id` (stable, what a prompt set stores), `label`, `model` (the
  provider's model name); optional `provider`, `base_url`, `max_tokens`,
  `json_mode`, and `api_key_env`, the **name** of the environment variable
  holding that provider's key. Without them an entry uses the chat provider's
  provider, base URL and key (`CSAI_CHAT_*`), which covers every model one
  OpenAI-compatible endpoint serves (DeepInfra today). Keys never appear in
  the list, the API or the UI.
- Unset: one entry, the chat model, as now. Invalid JSON, a duplicate id or a
  default not in the list: csai logs it and refuses to start, rather than run
  with a list nobody configured.
- `GET /v1/extract/models` returns `[{id, label, default}]` (no provider
  details), and `capabilities.extraction.models` carries the same for the SPA.
- The provider for an entry is built with the existing
  `make_chat_provider(config)` over a config overlay of that entry, cached per
  id.
- The cache key already includes the model, so the same question asked of a
  different model is a different cache entry.
- A request naming an id not in the list is a 400. A prompt set or
  automation binding whose model has since been removed fails with
  `model_unavailable` (not retryable) and a notice to the binding's folder,
  instead of quietly falling back to another model.
- Ansible: `csai_extract_models` (list, rendered to JSON) and
  `csai_extract_default_model` in the `convert_search_ai` role; per-entry key
  variables come from `.deploy-secrets.env` like `CSAI_CHAT_API_KEY`.

### 4.5 Headless API: list and select prompt sets (desktop applications)

Desktop applications choose from the server-side prompt sets instead of
shipping their own schemas, so a set developed interactively in the builder
(§4.2, §4.3) and published there is what every desktop user runs. Same `api`-scope key session as the rest of the
headless API; per-key limits apply (listing counts as a request and spends no
tokens).

- `GET /v1/prompt-sets`: the sets the caller may use. Only **published**
  revisions are visible to a key session; drafts and retired sets are not
  listed. Each entry:
  ```json
  {"id": "ps_7f3c…", "name": "Door datasheet", "description": "…",
   "revision": 3, "published_at": "…", "model": {"id": "deepseek-v4-flash",
   "label": "DeepSeek V4 Flash"},
   "applies_to": ["door_datasheet"],
   "fields": [{"name": "fire_rating", "label": "Fire rating", "type": "enum",
               "enum": ["EI 30", "EI 60", "EI 90", "EI 120"], "required": true}, …]}
  ```
  Filters: `?q=` (name/description), `?applies_to=<label>`. Paged like the
  run list. An `ETag` lets a desktop app cache the list and revalidate
  cheaply.
- `GET /v1/prompt-sets/{id}`: one set at its current published revision,
  including the compiled JSON Schema, so the desktop app can map fields onto
  its own model properties before running anything.
  `GET /v1/prompt-sets/{id}/revisions/{n}` returns an earlier published
  revision.
- `POST /v1/extract {"prompt_set": "ps_…", "revision": 3?, "files" | "folder_uid", …}`
  (§4.1): a desktop app selects a set by id. Without `revision` the current
  published revision is used. With it, that revision is used if it was ever
  published, so a desktop integration can pin what it has validated. `schema`
  and `instructions` may not be sent together with `prompt_set` (400).
  `model` may not be overridden from the desktop; the set's model is part of
  what was developed and published in the builder.
- The SSE `plan` event and the summary carry `prompt_set {id, name,
  revision, current_revision}`. A desktop app that pinned an old revision can
  therefore tell the user a newer one exists without polling.
- Refusals: unknown id or a draft-only set → 404 (no existence probe); a
  **retired** set → 410 `{"kind": "prompt_set_retired", "replaced_by": id?}`;
  a set whose model was removed from the list → 409
  `{"kind": "model_unavailable"}`.
- Raw schemas stay supported (`schema` + `instructions`) for applications that
  need ad-hoc questions. Every extraction that writes values emits
  `extraction.complete` (§6.0); a raw-schema one has `prompt_set: null`.
- The SPA's Applications view (headless runs) shows the set and revision each
  run used, and links to it in the builder.
- OpenAPI: the new routes and the `prompt_set` request variant are added to
  csai's published schema, alongside `/v1/extract`.

### 4.6 Import from the reviewed repo

The import accepts that repo's YAML/JSON (`type: extractor`, `prompt`,
`fields:[{name, description}]`): `prompt` becomes instructions, every field
becomes `type: string` with its description, and the set is imported as a
**draft** with a note that the types need setting. Our own export is the
prompt-set JSON above.

## 5. Marked-up PDF

### 5.1 What it is

A derived copy of the document with each extracted value's evidence
highlighted. One per (source version, prompt set). Written by csai's
extraction worker beside the cache sidecar.

- **Name**: `<version>-extractpdf_<set short id>.pdf`. It needs a fmt family
  rather than one fmt: `parse_rendition_name` learns that any fmt starting
  `extractpdf_` belongs to the family, so it is pruned with the version like
  `extract`. It must **not** be `markup`: the frontend's user-made
  `<user>_<ts>-markup.pdf` copies are deliberately exempt from pruning.
- Identity, ACL, erasure: same as the cache sidecar (csai service identity,
  readable by whoever can read the document, erased with it).

### 5.2 Which PDF is marked up

1. The source itself, if it is a PDF.
2. Otherwise csai's existing `pdf` rendition of that version (office formats,
   ODT included, via the existing conversion).
3. Otherwise no marked-up PDF. The result says `markup: {"status":
   "no_pdf"}`. Nothing is converted beside the source (the reviewed repo's
   overwrite bug).

### 5.3 Locating the evidence

The quote was taken from the stored Markdown, not the PDF, so an exact search
often misses (table pipes, line breaks, hyphenation, ligatures, Markdown
emphasis). In order:

1. Normalise the quote (strip Markdown markup and table pipes, collapse
   whitespace, unify dashes/quotes/ligatures) and `page.search_for` it.
2. Token-sequence match against the page's words (`get_text("words")`) using
   the same normalisation, allowing line and column breaks; the match's word
   rectangles are merged per line.
3. For a table-row quote: locate the row's cells individually and require
   them on one line band.
4. Narrow to the value: inside the located quote, also highlight the value's
   own text (e.g. "EI 60") in the field colour, so the eye lands on it.
5. Pages are searched from the one nearest the quote's Markdown offset
   proportion outward, to prefer the right occurrence of repeated text.

Unlocated evidence is **reported, never silently skipped**: per field
`markup: {located: true|false, page, method}`. The value itself is still
valid (its quote was verified against the text); only the drawing failed.

### 5.4 Drawing

- One highlight annotation per field, colour from a fixed accessible palette
  (or the set's `markup.colours`), with the annotation's popup text
  `Fire rating: EI 60 (prompt set "Door datasheet" r3)`.
- An appended **legend page**: prompt set and revision, model, extraction
  time, source version, and a table of every field with value, unit, page, and
  located/not located/not found.
- Annotations, not burned-in drawing, so the PDF.js viewer can show and hide
  them and a reader can copy text.
- Text-less scans: no text → no extraction → no marked-up PDF (unchanged
  owner decision: Markdown is the only input until multimodal support).

### 5.5 Dependency

Add `pymupdf` (pinned, not via `pymupdf4llm`) to csai's base dependencies and
fix the image (§2.2 finding). Rendering runs in the extraction worker, not the
API process; PyMuPDF holds the whole PDF in memory, so cap it by size
(`CSAI_EXTRACT_MARKUP_MAX_MB`, default 100) and report `markup:
{"status":"too_large"}` above it.

## 6. Events and folder automation

### 6.0 The `extraction.complete` event (Redis event bus)

Extracted data is announced on the shared bus, `fileengine:events`, so any
subsystem with an interest in extracted values can react, not only folder
automation (owner, 2026-11-02). The event **flags that values exist and names
the JSON payload to read**. It does not carry the values.

**When it is emitted.** Once per extraction that writes values into the
cache sidecar: an automation job, a desktop/headless `/v1/extract` (prompt set
**or** raw schema), and a `cache: refresh`. Not for a cache hit (those values
were already announced when they were written). Not for builder test runs
(they store nothing). A run over many files emits one event per file.
`extraction.failed` is emitted for a file that was attempted and produced no
entry (`reason`: `no_text` | `model_error` | `budget` | `model_unavailable`).

**Envelope.** The same shape as `conversion.complete` (`emit.py`), plus the
extraction block:

```json
{
  "event_id": "9b1c…", "type": "extraction.complete", "schema": 1,
  "tenant": "default", "file_uid": "…", "version": "20261102_141502.117",
  "actor": "fesk_ab12… | <user> | <fa service principal>", "ts": "20261102_141533.402",
  "source": "automation | headless | interactive",
  "values_present": true,
  "counts": {"fields": 5, "found": 4, "not_found": 1, "dropped": 0},
  "prompt_set": {"id": "ps_7f3c…", "name": "Door datasheet", "revision": 3},
  "model": "deepseek-v4-flash",
  "payload": {
    "name": "20261102_141502.117-extract.json",
    "uid": "<sidecar file uid>",
    "entry": "<cache key: the entry inside the sidecar>",
    "schema": "fileengine.extract_cache.v1"
  },
  "markup": {"status": "written | no_pdf | too_large | disabled",
             "name": "20261102_141502.117-extractpdf_7f3c.pdf"},
  "job_id": "…", "run_id": "…"
}
```

- `values_present` is true when at least one field was found. A run where
  nothing was found still emits, with `values_present: false`, because "the
  document does not state these" is itself a result.
- `prompt_set` is `null` for a raw-schema request; the entry in the payload
  then carries its own schema.
- `payload` names the hidden sidecar child of the source file and the entry
  within it. A consumer reads it with its own identity: the sidecar has the
  document's ACL, so a subsystem sees values only for documents it may read.
  No values, quotes or field names travel on the bus, consistent with the
  audit convention (uids, shape and counts) and with erasure: an erased
  document's sidecar goes, and the event names nothing that outlives it.
- The entry format (`fileengine.extract_cache.v1`) becomes a **published
  contract**, documented in `EVENT_CONTRACT.md` next to the event: per entry,
  the schema, instructions hash, model, prompt version, prompt set and
  revision, extracted time, and per field `{found, value, unit, evidence:
  {quote, offset}, markup: {located, page}}`. A change to it bumps the
  `schema` string.
- csai also offers `GET /v1/documents/{uid}/extractions?version=&entry=` (and
  `?prompt_set=`), READ-checked, for consumers that prefer HTTP to reading the
  sidecar through the core.
- Best-effort and fail-open, like `conversion.complete`: a Redis outage never
  fails an extraction. The reconcile sweep that backstops conversion events
  does not need to cover this one, because the sidecar is the durable record.
  A consumer that missed an event can list sidecars.
- Consumers: FA `extraction_rules` (§6.2) uses only events with a
  `prompt_set`, since its rules are written against a set's fields. The
  desktop applications already get the values over SSE.

### 6.1 Plug-in `extract` (trigger)

- `supported_events`: `conversion.complete` (acts only when `"text"` is in
  `renditions`); also `file.moved` into the folder, which asks csai to convert
  first if needed (as `sorter` does).
- Config:
  - `mode`: `fixed` | `by_classification`;
  - `prompt_sets`: multiselect, `options_source: prompt_sets` (new source in
    FieldRenderer and `GET /action-types`);
  - `classifier_set_id` + per-label prompt-set mapping (when `by_classification`;
    reuses FA's classifier exactly as `sorter` does, on the same text);
  - `revision`: `latest_published` (default) | pinned number;
  - `markup`: on/off (default on).
- `execute`: one `POST csai /v1/extract/jobs` per chosen set →
  `{job_id}` (202) and `ActionResult.done(detail={job_ids, sets})`. A 429 or
  503 from csai is a retryable failure.
- csai side: a new job table and the existing worker process run the job; the
  job reads text, checks the cache, runs the extraction, writes the sidecar and
  marked-up PDF, then emits **`extraction.complete`** (§6.0).
- **Metering**: automation spends tokens with no API key. It gets a
  per-tenant automation budget on the same meter, keyed `auto:<tenant>`
  (D3), with the same warning/exceeded states, shown in the folder's Actions
  tab.
- **Identity**: the job runs as FA's service principal, which must hold READ
  on the folder. A binding can only be created by a user with WRITE on it
  (existing rule).
- **Loop guard**: a job is idempotent on `(file_uid, version, prompt set,
  revision)`, so a repeated event for the same file costs nothing. A copy is a
  new uid with no sidecar, so it would be extracted again; that case is
  covered by the filing marker (§6.2, D6).

### 6.2 Plug-in `extraction_rules` (act)

- `supported_events`: `extraction.complete` with a `prompt_set` (raw-schema
  events are skipped); optional filter on prompt set.
- Reads the values through csai `GET /v1/documents/{uid}/extractions?prompt_set=&version=`
  (new, READ-checked; returns the typed values and the compiled schema).
- Config: an ordered list of rules, each `when` → `then`, first match wins
  or all matching rules apply (`match: first|all`).
  - Conditions, typed by the field's schema type:
    `found`, `not found`, `equals`, `in`, `contains`, `matches` (regex, strings
    only), `<, <=, >, >=, between` (number/integer/date, units must match the
    field's unit), `before/after today + n days` (date, e.g. certificate
    expiring within 90 days), `is true/false`; plus `missing required fields`,
    `any value dropped`, `markup not located`.
  - A condition on a field the revision does not have is a configuration
    error, caught when the binding is saved (the editor loads the set's
    fields) and again at run time (`failed`, not retryable).
  - Actions:
    - **write metadata**: map fields to metadata keys (default
      `extract.<set>.<field>`; value as a string with unit, plus a
      `…_source` key holding version and revision, so a stale value is
      recognisable);
    - **file**: move or copy to a folder, with an optional name template
      (`{manufacturer} {model} {fire_rating}.pdf`, values sanitised, the
      extension kept) and optional subfolder by value (`{manufacturer}/`,
      created if missing, depth limit 3);
    - **rename** in place;
    - **raise review** (discussion) with the values in the review text;
    - **notify** (existing templates; values available as placeholders);
    - **flag**: write `extract.status = needs_attention` with the reason.
- `auto_moves=True`. Loop guard extended (D6): FA writes
  `fa.filed_by = <binding_id>@<version>` metadata on whatever it files, and
  `extract` skips a file whose `fa.filed_by` names a binding of the same
  folder tree for the same version.
- `ActionResult.detail` records which rule matched and which actions ran
  (field names and counts, not values).

### 6.3 Webhook: fire on extracted data, with the values merged in

The existing `webhook` plug-in (`plugins/webhook.py`) is extended (owner,
2026-11-02) so a folder can send extracted values to an outside system
without a new plug-in.

- **Fire on data extracted (optional):** `extraction.complete` and
  `extraction.failed` join the webhook's `SUPPORTED` events, so they appear
  in the event pick-list. Choosing them is the opt-in; existing bindings are
  unchanged.
- **Merge in the extracted data:** the payload the webhook already builds
  (`event`, `document_id`, `version`, `tenant`, `metadata`, `mime`,
  `conversion`, `user`, `folder_uid`, `context`) gains an **`extracted`** key
  on those events (owner, 2026-11-02):
  ```json
  "extracted": {
    "status": "complete",                    // complete | failed
    "reason": null,                          // failed only
    "prompt_set": {"id": "ps_7f3c…", "name": "Door datasheet", "revision": 3},
    "model": "deepseek-v4-flash",
    "source": "automation | headless | interactive",
    "extracted_at": "…",
    "counts": {"fields": 5, "found": 4, "not_found": 1, "dropped": 0},
    "values": {
      "fire_rating": {"found": true, "value": "EI 60", "unit": null,
                      "evidence": {"quote": "Fire resistance: EI 60", "offset": 1834}},
      "width":       {"found": true, "value": 926, "unit": "mm", "evidence": {…}},
      "certificate_expiry": {"found": false, "value": null, "unit": null, "evidence": null}
    },
    "fields": {"fire_rating": {"label": "Fire rating", "type": "enum", …}, …},
    "markup": {"status": "written", "name": "…-extractpdf_7f3c.pdf"}
  }
  ```
  The values are read when the webhook runs, through
  `GET /v1/documents/{uid}/extractions?version=&entry=` (the `payload.entry`
  the event names), as FA's service principal (READ-checked like any read).
  `fields` carries the prompt set's field definitions, so the receiver can
  interpret values (types, units, labels) without a second call; it is
  omitted for a raw-schema entry, which carries its JSON Schema instead.
- **Config additions** (`ConfigModel` + `config_fields()`):
  - `extraction_prompt_sets`: optional multiselect (`options_source:
    prompt_sets`); empty means any set. Events from other sets are
    `skipped`. Raw-schema extractions are included only with
    `extraction_include_raw: true` (default false).
  - `extraction_fields`: optional allow-list of field names; only these are
    sent (`counts` still reflects the whole extraction).
  - `extraction_include_evidence`: default **false**. Quotes are verbatim
    document text, so sending them out of FileEngine is an explicit choice.
    `offset` and `markup` are sent either way.
  - `extraction_only_found`: default false; when true, not-found fields are
    left out of `values`.
  These fields are shown only when an extraction event is selected
  (`visible_when` on `on_events`).
- **Other events can also carry the latest values (optional):**
  `extraction_attach: "none" | "latest"` (default none). With `latest`, a
  webhook firing on any other event (for example `file.moved`, or
  `review.approved`) adds the `extracted` key for the file's current
  version and the configured sets when an entry exists, or
  `"extracted": {"status": "none"}` when none does. It never triggers an
  extraction.
- **Acting on the values remotely keeps working:** the webhook's response
  contract (`move_to`, `metadata{}`) is unchanged, so an outside system can
  decide on the values and send the filing and metadata back.
- **Data leaving the system:** a webhook binding already needs WRITE on the
  folder. The editor states plainly that the selected events send extracted
  values (and, if enabled, quotes) to the URL. `action_run.detail` records the
  set, revision, field names sent and counts, never the values. The FileEngine
  audit gains a `webhook.extraction_sent` record per delivery (file uid, set,
  revision, field count, destination host).
- **Delivery:** extraction values can be large (many fields, long quotes), so
  the `extracted` key is capped (`FA_WEBHOOK_EXTRACTED_MAX_BYTES`, default
  256 KB). Over the cap, quotes are dropped first, then its content is replaced
  by `{"status": "too_large", "entry": …}` and the receiver can fetch it.
  Retries reuse the values read on the first attempt, so a retry never sends
  a different extraction.
- **Prerequisites from P0:** the webhook's form-to-model shape mismatches
  (`context`, flat `auth` keys) and its blocking in-process backoff are fixed
  first, since extraction events add more deliveries through the same path.

### 6.4 FA changes required underneath

- `CoreClient`: `copy`, `rename`, `move(new_name=)`, `mkdir` (all exist in
  python_interface).
- `CsaiClient`: `start_extract_job`, `get_extractions`, `list_prompt_sets`.
- `options_source: prompt_sets` in `api.py` manifest and `FieldRenderer.vue`.
- Validate `on_events` against `supported_events` at save.
- Rules editor: a per-action component (like the sorter's routing table),
  because conditions depend on the chosen set's field types.
- Confirm and fix the three form-shape mismatches in §2.3 first; the rules
  editor would inherit the same form plumbing.

## 7. Phases

| Phase | Scope | Repos |
|---|---|---|
| P0 | Fix the PyMuPDF install; confirm the FA form-shape mismatches; `CoreClient` copy/rename/move-with-name | csai, docker_unified, FA, frontend |
| P1 | Prompt-set store, revisions, API, configured model list, builder panel with test (paste text or pick a file), publish, import; **headless list/select** (`GET /v1/prompt-sets`, `/v1/extract {prompt_set}`) | csai, frontend, scripts |
| P2 | Marked-up PDF (locate, draw, legend, sidecar, pruning family), in test runs and in `/v1/extract` (`markup: true`) | csai, frontend |
| P3 | csai extraction jobs; `extraction.complete`/`extraction.failed` for every extraction (jobs and headless) + `EVENT_CONTRACT.md`; `GET /v1/documents/{uid}/extractions`; automation budget; FA `extract` plug-in; **webhook fires on extracted data with values merged in** (§6.3) | csai, FA, frontend |
| P4 | FA `extraction_rules` plug-in + rules editor; loop guard | FA, frontend |

Each phase is shippable on its own; P2 is useful to desktop callers before any
automation exists.

## 8. Tests

- csai unit: compilation of every field type to JSON Schema; revision
  immutability; test runs (pasted text and file) never write the cache, a run
  record or a stored PDF; pasted-text offsets; `CSAI_EXTRACT_MODELS` parsing
  (default, duplicates, missing default refuses start, unknown id 400,
  removed model `model_unavailable`, keys never in `GET /v1/extract/models`); markup locator against a
  fixture corpus (single-line, wrapped, hyphenated, table row, ligature,
  repeated text, absent) with expected page and method; legend contents;
  pruning recognises the `extractpdf_` family and still ignores user `markup`.
- csai events: one `extraction.complete` per file per written entry (job,
  headless prompt set, headless raw schema with `prompt_set: null`, refresh);
  none for a cache hit or a test run; `values_present` false when nothing was
  found; `payload.name`/`uid`/`entry` resolve to the sidecar entry that was
  written; no value, quote or field name anywhere in the event;
  `extraction.failed` reasons; a Redis outage does not fail the extraction.
- FA webhook: fires on `extraction.complete`/`failed` only when selected;
  `extracted` key shape; set filter, raw-schema opt-in, field allow-list,
  evidence off by default, only-found; `extraction_attach: latest` on another
  event (present and absent); size cap order (quotes first, then
  `too_large`); a retry sends the same values; `action_run` and audit hold no
  values; existing bindings' payloads unchanged.
- FA unit: both plug-ins with fakes (event → job request; values → each
  condition type, each action); loop guard; invalid rule on save.
- Desktop simulator: list published sets (drafts and retired hidden, ETag
  revalidation), fetch one and map its fields, run by id with and without a
  pinned revision, `current_revision` reported after a new publish, 404 for a
  draft, 410 for a retired set, 409 for a removed model, 400 for
  `prompt_set` + `schema`, no model override; markup status values.
- New live E2E: upload a datasheet into a folder with both bindings → values
  in metadata, file filed under the templated name, marked-up PDF present with
  the expected highlighted pages, `action_run` rows for both bindings, budget
  decremented, no second extraction after the move.

## 9. Decisions (owner, 2026-11-02: all accepted)

- **D1 Where prompt sets live:** csai. One definition serves automation and
  the desktop API; csai owns the cache key. FA keeps a reference only.
- **D2 Who edits prompt sets:** tenant admins plus a new
  `extraction_editors` role; all members may use published sets.
- **D3 Automation spending:** a per-tenant automation token budget (default
  2M tokens/day, as for an API key), visible in the Actions tab; exceeded →
  jobs refused and retried the next day, with a notice.
- **D4 Extracted data is broadcast:** every extraction that writes values
  (automation, desktop/headless, prompt set or raw schema) emits
  `extraction.complete` on `fileengine:events`, flagging that values exist and
  naming the JSON payload (sidecar and entry) to read (§6.0). Values never go
  on the bus. Folder rules act on prompt-set events only.
- **D5 Model:** a prompt set picks its model from a configured list
  (`CSAI_EXTRACT_MODELS`, §4.4); the default is `CSAI_EXTRACT_DEFAULT_MODEL`.
- **D6 Loop guard for copies:** the `fa.filed_by` metadata marker (§6.2),
  plus "destination inside the bound tree" as a warning in the editor.
- **D7 Marked-up rendition:** a PDF ("ODF" in the request was a typo).
- **Test operation:** as in the classifier editor, paste text or choose a
  stored file (§4.2, §4.3).
- **Desktop selection:** the headless API lists the prompt sets published
  from the builder and runs one by id (§4.5). "Tested" means developed
  interactively in the builder; publishing is not gated on a recorded test.

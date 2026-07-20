# convert_search_ai — "Generate Report to a User‑Chosen Target"

Status: **Design / not yet implemented.** Feature branch `feat/ai-chat-generate-report`
is cut in both `convert_search_ai` and `frontend`. This document is the source of
truth for the feature; it extends [`CHAT_WITH_AI.md`](./CHAT_WITH_AI.md) (§ report
saving) and the wider [`SPECIFICATION.md`](./SPECIFICATION.md). Companion:
[`EVENT_CONTRACT.md`](./EVENT_CONTRACT.md).

---

## 1. Goal

Give the chat UI a **"Generate report"** action that lets the user **pick the exact
destination — a folder and a filename — before** the assistant writes. The UI then
commands the LLM to generate a report of the conversation, and CSAI saves it to
**that exact file**. Destination selection is now **solely the user's**: the model
produces report *content* only — it is not told the destination, cannot choose one,
and the marker no longer carries a path/filename. The report is written **as that
user** and gated by the core's ACLs. After the turn completes, the chat shows an
**"Open report"** link that opens the saved file in the in‑app preview modal.

This replaces the report‑saving that Chat‑with‑AI has today, where the *model*
decides the path — that capability is **removed** (§3a); the *user* owns it now.

```
  ┌──────────┐  "Generate report"  ┌───────────────┐  pick folder+name  ┌───────────┐
  │ ChatView │ ──────────────────► │ ReportTarget  │ ─────────────────► │  confirm  │
  │(composer)│                     │   Dialog      │  (folder tree +    │           │
  └────┬─────┘                     └───────────────┘   filename input)  └─────┬─────┘
       │  WS /chat  { message, history,                                       │
       │            report_target_folder_uid, report_target_filename } ◄──────┘
       ▼
     CSAI  ── inject report directive into system prompt ──► LLM stream
       │    ── [[SAVE_REPORT]] body diverted, target PINNED to the UI's folder_uid+name
       ▼
   FileEngine core  ── touch/put (or put a new version) AS THE USER, ACL‑checked
```

---

## 2. What we build on (current architecture)

- **Chat transport** — WebSocket `/chat` (`api.py`, WS handler → `_stream_answer`
  worker thread → `ChatService.answer()`). Client→server payload today:
  `{ message, system_prompt?, history?, k?, web_search?, conversation_id? }`
  (auth via `?token=` / `Authorization: Bearer`, tenant via `X-Tenant`→Host→default).
- **Report saving is marker‑driven, not a tool.** The system prompt fragment
  `_INSTRUCTIONS_DOCUMENT` (`chat.py`) tells the model to wrap a report in:
  ```
  [[SAVE_REPORT path="/Some/Folder" file="report-name" title="Report Title"]]
  …full report as Markdown or HTML…
  [[/SAVE_REPORT]]
  ```
  After the stream, `ChatService._save_marked_reports()` calls
  `llm_tools.parse_report_markers()` → `llm_tools.save_report_document()`.
- **`save_report_document()`** (`llm_tools.py`): sanitizes the name (`_safe_name`,
  strips separators/traversal, ensures `.html`), Markdown→HTML +
  `wrap_html_document`, resolves the folder (`_resolve_folder`), then writes **as the
  user**: `uid = mf.touch(parent, name)` then `mf.put(uid, document)`. Raises
  `ReportSaveError(kind ∈ empty|too_large|missing_folder|write)`.
- **`_resolve_folder()`** (`llm_tools.py`): walks from `ROOT_UID`, matching each
  path **segment by name** via `mf.dir()` (ListDirectory), `mkdir`‑ing missing
  segments when `create_folders=True`. **It resolves name‑paths, not UIDs.**
- **Identity / ACLs** — `core_client.client_for(identity)` binds a `ManagedFiles`
  gRPC client to the end user; every `touch/put/mkdir/dir` carries the user's
  `AuthenticationContext`, so the core enforces write permission. The read‑only
  `list_folders` tool (`ListFoldersTool`) is the only report‑related *tool*.
- **Frontend** — `views/ChatView.vue` + `services/chatService.ts` (`ChatSession`
  WebSocket, `ChatSendOptions`, `send()`), `services/csaiClient.ts`
  (`chatSocketUrl()`). Filesystem reads/writes go through `services/fileService.ts`
  against the **bridge** (`listDirectory`, `stat`, `touch`, `makeDirectory`,
  `findChildByName`). Modal shell to reuse: `components/ConfirmModal.vue`
  (Teleport + focus‑trap + Esc). **No folder picker or "save as" dialog exists yet.**

**What changes:** today the *model* owns the destination — it browses folders
(`list_folders`), confirms in chat, and emits `path`/`file` in the marker. This
feature makes destination selection **exclusively the user's** (a UI folder +
filename) and **removes the LLM's ability to specify a destination altogether** —
no path/file in the marker, no destination guidance in the system prompt, and no
report save except a UI‑initiated one with a user‑pinned target.

---

## 3. Design principles

1. **The destination is the user's, never the LLM's.** The folder UID + filename
   come **only** from the UI. The model is not told the destination, is not asked to
   choose one, and cannot influence it — it produces report *content*, nothing more.
   This removes the model's prior path‑selection capability entirely (§3a).
2. **Reuse the proven writer.** No parallel "tool" — we keep the existing
   marker → `save_report_document()` write path, but the marker now carries **only
   content** (an optional `title`); the folder + name are supplied out‑of‑band by the
   caller. One code path, lower risk.
3. **UID‑anchored, not name‑walked.** The UI already resolved a real folder UID
   (from `fileService.listDirectory`); the backend writes straight into that UID —
   no name‑matching, no path parsing, no accidental folder creation.
4. **Write as the user; the core is the gate.** Unchanged trust model: no
   privileged identity for user writes. If the user lacks WRITE on the folder, the
   save fails with a clear error surfaced in chat.
5. **Deterministic save.** The report is saved by CSAI after the stream, not left to
   a model tool‑call the model might skip — clicking "Generate report" *commits* to
   producing and saving a report to the chosen file.

### 3a. Removed: LLM‑chosen destination

The following existing behavior is **deleted**, not merely overridden:
- The `path` / `file` (and `filename`) attributes of `[[SAVE_REPORT …]]` — the marker
  keeps only `title` (optional). `parse_report_markers()` no longer reads a path/file.
- The system‑prompt guidance in `_INSTRUCTIONS_DOCUMENT` that tells the model to pick
  a folder / put a path in the marker to choose *where to save*.
- Saving a report to a **model‑decided path in free‑form chat**. A report is saved
  **only** in report mode — i.e. only when the UI supplied a target. Outside report
  mode, no `[[SAVE_REPORT]]` block is solicited or written.

**Kept:** the read‑only **`list_folders` tool stays** — the model may still *navigate
and browse* the user's folders (e.g. to ground answers or reference where things
live). What's removed is only its role in choosing the *save destination*; browsing
folders (read) and picking the write target (the user's job) are now separate
concerns. `list_folders` never writes and cannot influence where a report is saved.

---

## 4. Wire protocol changes (client → server, WS `/chat`)

Add three optional fields; their presence puts the turn in **report mode**:

| Field | Type | Meaning |
|---|---|---|
| `report_target_folder_uid` | string | **Authoritative** destination folder UID (a core/bridge UID; `""`/root‑UUID = filesystem root). Presence ⇒ report mode. |
| `report_target_filename` | string | User‑supplied name; sanitized server‑side (`_safe_name`, `.html` ensured). |
| `report_target_path` | string | Human‑readable path (e.g. `/Projects/Q3`) for logging + the confirmation message only. **Not** used to resolve the write. |

Absent ⇒ ordinary chat with **no** report save (the old "model chooses a path and
saves" behavior is removed — see §3a).

**Server → client**: add a structured `report_saved` event (in addition to the
human‑readable confirmation token) so the SPA can render an **"Open report" link**
that launches the in‑app **preview modal** for the saved file:

```json
{ "type": "report_saved", "uid": "<file-uid>", "name": "q3-summary.html", "path": "/Projects/Q3" }
```

The `uid` is the saved file's core UID — everything the SPA needs to open the
document preview (§6.3). Documented alongside the other events in
[`EVENT_CONTRACT.md`](./EVENT_CONTRACT.md).

---

## 5. Backend changes (`convert_search_ai`)

### 5.1 `api.py` — accept + thread the target
Read `report_target_folder_uid` / `report_target_filename` / `report_target_path`
from the socket payload and pass them into `ChatService.answer(...)` as an optional
`report_target` value object `{ folder_uid, filename, path }`.

### 5.2 `chat.py::ChatService.answer()` — report mode
When `report_target` is present:
- **Inject a content‑only report directive** into the system prompt: *"You MUST
  generate a complete report of this conversation and wrap it in a single
  `[[SAVE_REPORT title="…"]] … [[/SAVE_REPORT]]` block. Provide only a `title` and the
  report body — do not specify or mention any folder, path, or filename; the
  destination is chosen by the user and is not yours to set."*
- The report‑mode directive **replaces** the destination‑oriented guidance in
  `_INSTRUCTIONS_DOCUMENT`. The `list_folders` tool **remains available** (the model
  may browse folders to ground the report), but it plays no part in where the report
  is saved — the destination is the pinned `report_target`.
- **Pass `report_target` to `_save_marked_reports()`** so the save uses the pinned
  folder UID + filename.

### 5.3 `chat.py::_save_marked_reports()` + `llm_tools.save_report_document()`
- `parse_report_markers()` reads only the **body** and optional **title** — the
  `path`/`file`/`filename` marker attributes are removed from the grammar.
- The folder + name come **only** from the pinned `report_target`. Add a
  **UID‑anchored resolution** branch (a `_resolve_folder_by_uid`): given a
  `folder_uid`, use it directly as the parent — no name‑walk, no `mkdir`. Root‑UUID
  / `""` means the filesystem root. The name is `_safe_name(report_target.filename)`.
- **Overwrite semantics:** before `touch`, check the target folder for an existing
  child with the sanitized name (`mf.dir(folder_uid)` match, mirroring the bridge's
  `findChildByName`). If found → `mf.put(existing_uid, document)` (a **new version**
  of that exact file). If not → `mf.touch` + `mf.put` (new file).
- Keep `ReportSaveError` handling; add `kind="denied"` when the core rejects the
  write for lack of permission, surfaced as a clear chat error.
- On success, emit the `report_saved` event (`uid`, `name`, `path`) — this is what
  the SPA turns into the **"Open report" preview‑modal link** after `done` (§6.3).

### 5.4 Non‑goals / removed (backend)
- No change to retrieval or web‑search.
- The report writer stays **marker‑driven** (no new provider/tool schema); the marker
  is now content‑only.
- HTML remains the stored format (Markdown→HTML). The **PDF preview is produced by
  the existing default preview‑generation pipeline** — not by this feature. A
  Markdown/PDF *export* is a possible follow‑up (§9.1).
- **Removed:** destination guidance in `_INSTRUCTIONS_DOCUMENT` and marker
  `path`/`file` parsing (§3a). The `list_folders` **tool is kept** for general folder
  navigation — only its destination‑selection role is gone.

---

## 6. Frontend changes (`frontend`)

### 6.1 `components/ReportTargetDialog.vue` (new)
Fork the `ConfirmModal.vue` shell (Teleport, backdrop, focus‑trap, Esc). Contents:
- **Folder navigator** backed by `fileService.listDirectory(uid)` + `stat`: shows
  the current folder's **sub‑folders**, a breadcrumb trail (root = `/`,
  root‑UUID), enter a sub‑folder on click, "up" via breadcrumb. A **"Use this
  folder"** state marks the current folder as the destination. (Mirror the
  navigation logic in `stores/files.ts` — `openDirectory`/`navigateToCrumb`/
  `revealFile` — but use `fileService` directly so the file browser's own state is
  untouched.)
- **New‑folder operation.** A **"New folder"** affordance in the navigator creates a
  sub‑folder in the current folder via `fileService.makeDirectory(currentUid, name)`,
  then re‑lists and enters it — so the user can make the destination on the spot. The
  create runs **as the user** (the bridge/core ACL‑gates it); a failure (e.g. no
  WRITE) shows an inline error and doesn't close the dialog.
- **Filename input** (plain text; the `.html` extension is ensured server‑side, and
  previewed in the dialog).
- Validation: a folder must be chosen and the filename non‑empty. Emit
  `select { folderUid, folderPath, filename }`; `cancel` closes.

### 6.2 `services/chatService.ts`
Extend `ChatSendOptions` with `reportTarget?: { folderUid: string; folderPath: string; filename: string }`
and map it in `send()` to the wire fields `report_target_folder_uid` /
`report_target_filename` / `report_target_path`. Add `onReportSaved?(evt)` to the
handler interface and parse the `report_saved` event in `parseChatEvent`.

### 6.3 `views/ChatView.vue`
- Add a **"Generate report"** button in the `.composer` (next to Send). Clicking it
  opens `ReportTargetDialog`.
- On `select`, send a report‑generation turn: the conversation `history` + a command
  message (e.g. *"Generate a report of our conversation."*) + `opts.reportTarget`.
  Push a user‑visible chip like `📄 Generate report → /Projects/Q3/q3-summary.html`.
  (The command message no longer contains the destination — that rides in
  `reportTarget`, per §3a.)
- Reuse the existing `onToolCall`/`onToolResult` indicator pattern for a
  **"Writing report…"** state.
- **Open‑report link (post‑`done`).** On the `report_saved` event, stash
  `{ uid, name }` for the current assistant turn. When the turn's **`done`** event
  fires, append an **"📄 Open report"** link/button to that message. Clicking it
  opens the saved file in the in‑app **preview modal** via the existing
  `usePreviewStore().open(uid, name)` — the same store + `PdfPreviewOverlay` this view
  already uses for citation clicks (`preview.open(c.fileUid)`), so no new modal is
  needed. (Rendering on `done` rather than mid‑stream guarantees the file exists —
  the save runs after the stream completes.)
- **No new preview work.** The saved report is a normal file, so the **default
  preview generation already covers it** — CSAI's convert pipeline produces the PDF
  preview rendition (automatically on ingest, or on‑demand via `DocumentPreview`'s
  "Generate preview" → `POST /documents/:uid/convert`). The preview modal renders
  that rendition like any other document; this feature adds no report‑specific
  rendering or PDF handling.

---

## 7. Security & correctness

- **Authorization is the core's.** Writes carry the user's `AuthenticationContext`;
  a user who can't write the chosen folder gets a denied error — CSAI never
  escalates. The `folder_uid` is *not* trusted for authorization (the core re‑checks
  on `touch`/`put`); it is only a destination hint.
- **Path traversal / injection.** `report_target_filename` is sanitized by
  `_safe_name` (strip separators, `..`, control chars; ensure `.html`). The
  `folder_uid` is opaque; the UID‑anchored branch does no name concatenation, so no
  supplied string can escape the chosen folder.
- **The model can't influence the destination at all.** It never receives a folder,
  path, or filename and the marker has no path/file field — so there is nothing to
  spoof or prompt‑inject toward. The destination comes solely from the UI payload.
- **Overwrite is versioning, not destruction.** Re‑saving to an existing file writes
  a new version (immutable history preserved); the user can restore prior versions.
- **Size / empty guards** (`ReportSaveError` `empty|too_large`) are unchanged.

---

## 8. Testing

- **Unit (`pytest -m "not live"`):** `save_report_document` with a pinned
  `folder_uid` writes to that UID (mocked `ManagedFiles`); existing‑name → `put` to
  the existing uid (new version) vs new‑name → `touch`+`put`; `_safe_name` traversal
  cases; denied‑write → `ReportSaveError`.
- **Marker parsing:** the content‑only grammar parses body + optional `title`; a
  legacy‑style `path=`/`file=` in the body is ignored (no destination leaks from the
  model).
- **No‑target regression:** an ordinary chat turn (no `report_target`) writes **no**
  file — confirms the model‑chosen‑path save is gone.
- **Live end‑to‑end:** open chat → "Generate report" → pick a folder + name →
  confirm → assert the file exists at the exact target (REST `GET /v1/nodes` +
  `GET /v1/dirs`); the `report_saved` event carries the right uid; after `done` the
  **"Open report" link opens the preview modal** on that uid; a second save versions
  the same file; a denied folder surfaces an error and writes nothing.

---

## 9. Decisions (resolved)

1. **Output format — HTML.** The report is stored as HTML (Markdown→HTML). PDF is
   **already a free default preview** (CSAI's convert pipeline renders a PDF rendition
   automatically); this feature produces no PDF itself. A downloadable Markdown/PDF
   *export* remains an orthogonal possible follow‑up.
2. **`list_folders` — kept.** The read‑only folder‑navigation tool stays so the model
   can browse the user's folders; it no longer selects the save destination (§3a).
3. **Overwrite — silent versioning, no callout.** Re‑saving to an existing filename
   writes a new version. Versioning is a core platform feature, so the dialog does
   **not** add a special "will create a new version" warning.
4. **New‑folder in the dialog — yes.** The picker includes a "New folder" operation
   (`fileService.makeDirectory`, ACL‑gated as the user) so the destination can be
   created on the spot (§6.1).

*(Also resolved: the LLM no longer specifies the destination — §3a. The writer stays
marker‑driven and content‑only — not promoted to a destination‑bearing tool.)*

---

## 10. Touched files (implementation map)

**convert_search_ai** — `src/convert_search_ai/api.py` (WS payload → answer),
`chat.py` (`answer` report‑mode directive + `_save_marked_reports` pinning; **remove**
the destination guidance from `_INSTRUCTIONS_DOCUMENT` — `list_folders` stays as a
navigation tool), `llm_tools.py` (`save_report_document` UID‑anchored resolve +
overwrite‑as‑version; `parse_report_markers` **content‑only** grammar; emit
`report_saved`), `design_documents/EVENT_CONTRACT.md` (`report_saved`).

**frontend** — `src/components/ReportTargetDialog.vue` (new folder+filename picker
with a **New‑folder** operation), `src/services/chatService.ts`
(`ChatSendOptions.reportTarget` + `send()` + `report_saved`/`onReportSaved`),
`src/views/ChatView.vue` (button + dialog + "Writing report…" indicator + post‑`done`
**"Open report"** link via `usePreviewStore().open`).

No core / proto / bridge changes — the write primitives (`Touch`, `PutFile`,
`ListDirectory`, per‑user ACL) already exist.

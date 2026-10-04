# Media share — publishing audio and video, gated or open

**Status:** Reviewed 2026-10-03 — decisions in §14 (*Resolved 2026-10-03*); implementation begun at MS0–MS2
**Scope (cross-repo):** **`convert_search_ai`** (the transcode side: a new
publish-grade media rendition family and the durable job that produces it),
**`share_service`** (a new link kind, two new access modes, the media door, the
audience roster), `commercial_embedding` (the embeddable Web Component),
`frontend` (publish control in the drawer, audience in the Share tab's
status/history surface), `ldap_manager` (unchanged — the new modes deliberately
do not use it), `audit_service` (new action codes), `docker_unified` (a new
media origin + nginx zone). **No change to `file_engine_core`** — the constraint
`OUTSIDE_SHARE_LINKS.md` §4 imposes on `share_service` is inherited here intact,
and §6.5 below is where it is nearly paid for and how it is paid instead.

> **Read first:** `share_service/design_documents/OUTSIDE_SHARE_LINKS.md`. This
> document is an extension of it, not a standalone design, and it **reopens three
> of its locked decisions** (§3 below). Section references of the form
> *OSL §n* point there; bare `§n` points here.

### Who does what

This is a multi-repo change, so the split is stated once here rather than
assembled from the milestones. Each repo's work is separable and most of it is
independently testable.

| Repo | Owns | Sections | Milestones |
|---|---|---|---|
| **`convert_search_ai`** | the four media renditions, the encoder ladder, the durable transcode job + its worker, the publish API, the capability block | §4 | MS0–MS2 |
| **`share_service`** | link `kind = 3`, the three access modes, the media door + origin, the byte cache and Range serving, metering and load shedding, the audience table, **playback telemetry**, the CSV projection | §5–§8 | MS3–MS5b |
| **`frontend`** | inline playback, the Share tab's media branch (which is where publishing is triggered), the audience roster and egress meter, the landing page, the embed snippets | §10 | MS6–MS7 |
| **`commercial_embedding`** | `<fe-media-share>`, the self-hosted script, the iframe snippet, oEmbed | §9 | MS8 |
| **`docker_unified`** | the `<tenant>-media.<base>` vhost, its rate-limit zone and log format, compose defaults | §6.5, §11 | MS4, MS9 |
| **`audit_service`** | the new action codes and the egress alert rule | §12 | MS9 |
| **`folder_actions`** | *(proposed, §14-Q8)* a **Publish media** plug-in: drop a clip in a folder, get a published rendition and a minted link | §4.8 | MS10 |
| **`ldap_manager`** | **nothing** — the two new modes deliberately send no mail and mint no OTP | §5 | — |
| **`file_engine_core`** | the storage pipeline: per-version transform record (**a defect fix, owed regardless**), selective compression, byte-range reads. **Its own specification** — `design_documents/storage_pipeline.md`, branch `proposal/byte-range-reads`. Not a blocker: MS4 ships on the cache | §3-R16 | S0–S5, parallel |

**The two things that gate everything else** are the ops precondition (a
wildcard certificate for `*-media.<base>`, §6.5) and MS0's streaming fetch,
which is a prerequisite for transcoding anything larger than memory. Both can
start immediately and neither depends on the rest of the design being settled.

---

## 1. The original sketch

> Enhance both the converter and external sharing subservice to publish
> audio and video media. Add an operation to convert a video or audio
> file to WEBM, extending the existing video preview system, to convert
> the full video to WEBM at up tp 720P quality with full quality audio.
> Convert audio with the highest quality streaming MP3 file.
>
> ## Integration to the sharing framework
>
> Support both a direct share and an embeddable Web-Component that can embed in
> any other website. The sharing needs to support three options: open-sharing to
> any visitor, gated to the specified emails as per the existing sharing
> framework, and allow anyone to access if they provide their email address without
> needing the verification step. The email addresses need to be gathered and
> displayed in the history/status interface for the sharing UI on the folder drawer.
> Likewise, the gathered email addresses need to be added to a CSV file in the
> file's sidecar. This way fetching the sidecar file is a simple integration.

Everything below is the mechanism for that, plus the parts the sketch leaves
implicit: what a "use" means when a video player issues fifty ranged GETs to
watch one file, where a two-hour transcode runs and what the user sees while it
does, why an embeddable player cannot be served from the origin the SPA lives on,
and what an email address collected without verification is actually worth.

---

## 2. Goals & non-goals

**What this is for.** Media publishing is **outbound**: sending video clips to
clients, to prospects, and to the public website. That is a different job from
everything the conversion pipeline does today, which is **inbound** — making
files that are already in the system legible inside it. The distinction decides
several arguments below, most directly §4.1's: the `preview` clip is a *teaser in
the file browser*, and a published rendition is an *artifact that leaves the
building*. They share FFmpeg and the rendition writer and nothing else — not
their size, not their trigger, not their lifecycle, and not who is allowed to
cause one.

### 2.1 The primary use case, and what it implies

**A Loom-style introduction video, embedded in the sender's own communications,
served from their own platform.** A short talking-head clip — a few minutes —
recorded once or recorded per recipient, dropped into an email, a proposal, or a
web page, with the sender wanting to know whether it was watched and wanting the
whole thing to be *theirs*: their domain, their branding, no third-party player
and no third-party analytics on their prospects.

That is a specific load shape, and it is nearly the opposite of the one a video
platform is designed for. It should be read into every default in this document:

| | A video platform | **This** |
|---|---|---|
| Files | few, large, long | **many, small, short** (2–5 min) |
| Links per file | one | **many** — often one per recipient |
| Viewers per link | thousands | **one to a few** |
| Concurrency per link | high | **~1** |
| The scarce resource | bandwidth | **distinct-file churn** — every new clip is a fresh cache fill (§6.9) |
| What the creator wants back | aggregate analytics | **"did *this person* watch it"** |

Four consequences that shape the design rather than decorate it:

1. **Watch tracking is a headline feature, not a garnish.** For a one-to-one
   intro video, *"Priya watched 94% of it and stopped at 1:47"* is the entire
   point. §7.4 specifies it properly — coverage rather than playhead position, a
   union-merged bitmap so a lost beacon costs nothing, and a bytes-served floor
   so the claim has a basis the client cannot inflate.
2. **Email is a first-class embedding target, and it cannot run the Web
   Component.** No email client executes a module script or a cross-origin
   iframe. §9.4 specifies what actually works: an animated poster that links out.
3. **Many links on one file** is the normal case, not an edge case — which the
   per-link CSV sidecar does not survive unqualified (§8.1).
4. **The metering in §6.9 is about runaway and abuse, not growth.** In this
   shape, an hourly *byte* budget is almost never the binding control; **cache
   fills and concurrency** are.

### 2.2 The three modes and the three audiences

| Audience | Mode | What the creator gets back |
|---|---|---|
| A named client, one-to-one | `verified` | proof that a specific person watched it |
| A prospect | `claimed` | a self-asserted address — a lead, not a fact (§7) |
| The website | `open` | view counts, referrers, and an embed (§9) |

For the primary use case the common shape is an **`open` link minted per
recipient** — unguessable, short-lived, one person expected — which gives
"did they watch it" with no gate in the recipient's way. `verified` exists for
when the answer must be proof rather than inference.

**Goals**

1. **Publish-grade media renditions, produced on demand.** Full-length,
   web-streamable copies of a video (WebM, VP9 + Opus, at 720p and 480p) and of
   an audio file (MP3 at LAME V0, plus Opus in WebM), stored as hidden children
   of the source exactly like every other rendition — but **created only when a
   media share is configured on the file**, never on ingest and never by a
   deployment-wide switch (§4.3). The existing automatic poster + 10-second
   preview is untouched.
2. **A media share link** that plays rather than downloads: seekable, resumable,
   poster-framed, with a duration the player knows before the first frame (§6).
3. **Three access modes on one link** — open, claimed-email, verified-email —
   chosen at creation and immutable thereafter (§5).
4. **An embeddable player** that a third-party website can drop in as a Web
   Component and that works with no FileEngine account, no session, and no
   integrator backend (§8).
5. **An audience roster.** Whoever identified themselves — by whichever mode
   asked them to — appears in the Share tab's status/history surface (OSL §10.2)
   and in a CSV sidecar on the shared file, so an integration can read the
   audience by fetching one file (§7).
6. **Nothing here weakens the existing gated share.** The `verified` mode is
   byte-for-byte the flow OSL §6.9 specifies; the two new modes are additional
   states, not a relaxation of the default.

**Non-goals (v1)**

- **Not a video platform.** No *adaptive* bitrate switching, no DASH/HLS
  manifest, no segmentation, no per-viewer transcoding, no live streaming. Video
  is published at **two fixed sizes** (720p and 480p) as whole progressive files
  and the viewer picks between them; the player never switches mid-playback
  (§4.5). Audio is published twice for codec compatibility, not for bandwidth
  (§4.5). Real ABR is a different rendition model and its own project.
- **No transcripts or captions in v1** — but the seam is deliberately left open
  and is **specified rather than merely mentioned**, because it is the most
  likely next integration and the decisions that would make it awkward are ones
  this document is taking now. See **§16**, which documents the hooks, the one
  trap in the existing plugin contract, and what is deliberately left undecided.
- **No DRM, no watermarking, no download prevention.** An open-mode link is a
  public URL to a media file; anyone who can play it can save it. Saying
  otherwise in the UI would be a lie the browser disproves in two clicks.
- **No editing** — no trimming, no chapter marks, no thumbnails-on-scrub sprite
  sheet.
- **Not the MCP door's business** (OSL §11 applies unchanged, and more strongly:
  an agent that can publish a video to the open internet is not a default).
- **Not a CDN, and explicitly not for high-volume media.** This is a stated
  product boundary, not a limitation to be engineered away later. A link that
  sustains real audience traffic is a **signal that the content belongs on
  YouTube or Vimeo**, and §6.9 is built to detect exactly that, bound it, and say
  so to the creator with the numbers attached. Raising those ceilings repeatedly
  is a decision to become a video host, and should be made as one.

**Threat model — what changes from OSL §2.** OSL's model assumed *the URL leaks
and the OTP makes it inert*. Two of the three modes here have no OTP, so for
those the model is different and must be stated plainly:

| | `verified` | `claimed` | `open` |
|---|---|---|---|
| A leaked URL conveys | nothing | access, after typing any address | access |
| The audience list is | proven | self-asserted, and may be fictional | absent (or a bare counter) |
| The primary bound is | the recipient allowlist | the budgets in §6.4 | the budgets in §6.4 |
| Suitable for | a document worth gating | marketing collateral, lead capture | a public product video |

The `open` and `claimed` modes therefore treat **egress and abuse budgets as the
control**, where `verified` treats *identity* as the control. That is the whole
reason §6.4 exists as its own section rather than a paragraph.

---

## 3. What this reopens in `OUTSIDE_SHARE_LINKS.md`

Three of OSL's locked decisions are contradicted by the sketch. They are listed
here rather than quietly overridden, because OSL §13 is explicit that its
decisions stand unless deliberately reopened, and two of the three were argued at
length.

### R14 — There is now an open mode, and a claimed-email mode (reopens R4)

OSL §2 says *"Not a way to publish. Every link is addressed to a closed set of
people named at creation; there is no 'anyone with the link' mode"*, and R4 adds
*"there is no open-email mode"*. Both are reversed **for `kind = 3` (media)
links only**. File, folder and drop links are untouched and keep the mandatory
allowlist.

The two rejections had different reasons, and only one of them survives:

- **The open-email rejection was about mail, and there is no mail here.** R4's
  argument was that letting an unauthenticated caller choose a destination
  address for tenant-branded mail is an open relay backed by the deployment's
  sending reputation. The `claimed` mode **sends nothing** — the address is
  recorded, not written to. The capability R4 removed is not created by this
  mode, because no message is ever addressed. What *is* created is a different
  and much smaller problem: a field into which an internet caller types
  arbitrary text that lands in a tenant's file (§7.3).
- **The "not a way to publish" rejection does not survive, and should not.** It
  was a scoping statement about documents, and a product video is not a
  document. It is reopened deliberately and narrowly: only for a link whose
  payload is a published media rendition, never for the source file, never for a
  folder, never for a drop.

**Consequence to hold onto:** the security argument for the whole feature in OSL
was *delegation plus a closed recipient set*. For media links the second half is
gone in two of three modes, so the first half carries the entire load — a link
can still never convey more than its creator holds at redemption (OSL §6.3), and
that re-check becomes the only identity-shaped control left. It must therefore
run on the media door exactly as it runs everywhere else, and §6.7 says how often.

### R15 — Media bytes move to their own origin (invokes OSL §8.3's reserved answer)

OSL §8.3 protects the tenant origin with a header triple —
`Content-Disposition: attachment` + `X-Content-Type-Options: nosniff` +
`Content-Security-Policy: sandbox` — set once for the whole public prefix, and
records that *"a separate `dl.<base>` origin remains the structural answer if the
deployment ever serves untrusted HTML/SVG at volume"*.

**A media player cannot live behind that triple.** `Content-Disposition:
attachment` makes a browser download the response instead of feeding it to a
`<video>` element; `CSP: sandbox` on the landing document blocks the very
scripting the player is; and a third-party embed is by definition cross-origin,
which OSL §7.2's *"No CORS wildcard. Same-origin only"* forbids.

Weakening the triple on the shared prefix is not an option — it is one header
away from an unauthenticated HTML upload running script next to the SPA's
`localStorage` bearer token. So the structural answer is taken now rather than
later: **media is served from `<tenant>-media.<base>`**, mirroring the
`<tenant>-drive.<base>` pattern `webdav_bridge` already uses
(`docker_unified/images/nginx/render-config.sh:66`). See §6.5.

### R16 — the core gains byte-range reads; the media cache stays, as an optimisation *(revised 2026-09-26)*

Media playback needs byte-accurate `Range`. The core's `GetFileRequest`
(`file_engine_core/proto/fileservice.proto:328`) has **no offset field**, and the
bridge's range support (`http_bridge/src/http_server.cpp:751`) is implemented by
streaming the file from byte zero and discarding everything before the window.
For a 1.2 GB published video, one seek to the 90% mark reads 1.08 GB through
gRPC and throws it away — per seek, per viewer.

**An earlier draft of this section declined to change the core** and answered
with a disk cache alone. That has been **reversed deliberately**, which is
precisely the procedure OSL §14 prescribes — *"if a milestone starts wanting a
core change, that is the signal to re-open §4 deliberately rather than to make
the change quietly."* The reasoning for reversing it:

- **The gap is platform-wide, not media-specific.** `http_bridge` and
  `webdav_bridge` both want real `Range` and neither has it; the bridge's
  current implementation is additionally emitting a **malformed**
  `Content-Range: bytes <start>-/*` (no last-byte-pos, unknown total), which is
  why in-browser seeking is unreliable today for authenticated users who have
  nothing to do with share links.
- **A cache in front of a missing capability is not the same as a cache in front
  of a slow one.** The first has to be correct or the feature breaks; the second
  can be dropped at any time.

The design is now a **core specification in its own right** —
**`file_engine_core/design_documents/storage_pipeline.md`**, with
`PROPOSAL_byte_range_reads.md` and `PROPOSAL_selective_compression.md` retained
as its rationale. It is scoped and staged as core work rather than as a
dependency of this feature, because specifying it turned up **two defects that
exist today in every deployment and have nothing to do with media**: the core
does not record which transforms it applied to a stored version (it re-derives
them from current configuration at read time, so turning compression off hands
the raw zlib stream to clients as file content, silently), and it compresses
payloads that cannot be compressed, on write and on every read. Its **S0 is a
prerequisite** — a defect fix owed regardless of this feature. Its shape, in one paragraph: `offset` / `length` on `GetFileRequest`, range
metadata including `total_size` on the first response frame, and a tiered
implementation — every format gets a correct range immediately by windowing at
the emit sink (which stops the discarded bytes crossing gRPC), formats that
allow it get a true seek, and a chunked storage format v2 makes random access and
GCM authentication stop being in tension. The obstacle is not the RPC; it is that
**zlib has no seek points and a GCM tag covers the whole object**, which that
document works through properly.

**The `share_service` media cache (§6.6) stays**, with a different justification:

| | Before | Now |
|---|---|---|
| Why it exists | the core *cannot* serve ranges | not crossing a process boundary for a hot object is faster, and it is where a CDN sits later |
| If it is removed | seeking breaks | seeking gets slower |
| When to populate | always | when the core reports `range_method = "scan"` for that object — a measurable trigger rather than a blanket rule |

§6.6's rule that **the cache never short-circuits the authority re-check** is
unaffected and remains non-negotiable.

**Sequencing:** MS4 does not block on the core work, and the core work does not
wait on MS4. `storage_pipeline.md` §6.1 records what each consumer needs: this
feature needs **S2 + S3b** for an O(1) seek and **nothing at all** to ship, since
the cache carries it exactly as originally designed until then. **S0 is owed
independently** and should be scheduled on its own merits.

---

## 4. The transcode side (`convert_search_ai`)

### 4.1 The new rendition vocabulary

Five new `fmt` values join the family in `renditions.py`:

| `fmt` | ext / mime | Produced from | Purpose |
|---|---|---|---|
| `media` | `webm` / `video/webm` | `video/*` | full-length, ≤720p, VP9 + Opus — the default source |
| `media-sd` | `webm` / `video/webm` | `video/*` | full-length, ≤480p, VP9 + Opus — the low-bandwidth source (§4.5) |
| `audio` | `mp3` / `audio/mpeg` | `audio/*` | full-length MP3, LAME V0 — the compatibility source |
| `audio-opus` | `webm` / `audio/webm` | `audio/*` | full-length Opus in WebM — the preferred source (§4.5) |
| `emailposter` | `gif` / `image/gif` | `video/*` | ~3 s animated GIF with a play-button overlay, for embedding in email (§9.4) |

The existing `poster` (a PNG frame) and `preview` (a **10-second, silent, 640px**
clip) stay exactly as they are. This is deliberate and load-bearing:

**`preview` must not become the full video.** *(Confirmed 2026-09-26 — §14-Q5.)*
`preview` is a **teaser for the file browser**; a published rendition is an
**outbound artifact** (§2). The sketch's phrase *"extending the existing video
preview system"* means reuse the plugin, the FFmpeg plumbing and the rendition
writer — not redefine what `preview` means. The two differ in every dimension
that matters:

| | `preview` | `media` / `media-sd` |
|---|---|---|
| Purpose | recognise the file at a glance | watch it, elsewhere |
| Length / audio | 10 s, silent | full, with sound |
| Trigger | automatic, on ingest | **a share being configured** (§4.3) |
| Fetched by | hover cards and list views, speculatively | one viewer who asked |
| Lifetime | as long as the version | as long as a live link, plus a grace (§4.3.1) |

Making `preview` full-length would turn a directory listing into hundreds of
megabytes of speculative fetches. A new `fmt`
costs one entry in three allowlists and keeps both behaviours.

**Four new fmts, not two.** `media-sd` and `audio-opus` are the second sources
the player picks between (Q3/Q4, resolved 2026-09-26). They are *alternates of
the same content*, which is the one thing `toRenditionSet` in the frontend
(`renditions.ts`) is not built for — it keeps one entry per fmt, latest version
winning, and has no notion of "these two are the same thing at different
bitrates". §6.8 therefore assembles the source list **server-side** from the
link's own record and hands the player an ordered array, rather than asking the
client to infer a relationship the rendition vocabulary does not express.

Three allowlists, and **all three must be updated**:

1. `convert_search_ai/src/convert_search_ai/renditions.py` — `_KNOWN_FMTS`
2. `frontend/src/services/renditions.ts` — `RenditionFmt` and `KNOWN`
3. `docker_unified/images/csai/build-src/...` — the vendored copy, if the build
   still ships one

> **The precedent that proves this is a real trap.** `metamodel` is written as a
> rendition by `plugins/xeokit3d.py:824` and is present in the frontend's `KNOWN`
> list, but it is **absent from `_KNOWN_FMTS`** in `renditions.py:41`. Because
> `parse_rendition_name` returns `None` for an unrecognized `fmt` token and
> `prune_old_versions` skips anything that does not parse, every superseded
> `metamodel` JSON ever written is still there. For a 5 KB JSON that is untidy;
> for a 1 GB `media` rendition it is a storage incident on the first re-upload.
> **A test must assert that every `fmt` any plugin emits round-trips through
> `parse_rendition_name`** — that single test retires the whole class of bug, and
> the existing `metamodel` leak should be fixed in the same change (a one-line
> addition plus a one-off sweep).

### 4.2 Why this cannot run in the existing pipeline

Two blockers, both structural, both in `pipeline.py`:

**(a) The pipeline reads the whole file into memory.**
`ConversionPipeline.convert` does `blob = self.mf.get(...)` then `data =
blob.read()` (`pipeline.py:135–138`) and hands `data: bytes` to
`plugin.render()`. The video plugin then writes those bytes straight back out to
a temp file for FFmpeg (`plugins/video.py:50`). For the 10-second preview this is
merely wasteful; for a 4 GB source it kills the worker, which is exactly the
failure mode `bounds_own_memory` was introduced to reason about — and note that
`bounds_own_memory = True` on `VideoPlugin` *exempts* it from the sweep's size
limit, so a large video reaches `blob.read()` with nothing in the way.

**Fix:** a streaming fetch. `python_interface` already exposes
`ManagedFiles.get_stream` (`fileengine/client.py:542`). Add to the plugin
interface:

```python
class ConversionPlugin:
    #: Can this plugin work from a PATH instead of bytes? When true the pipeline
    #: streams the source to a temp file and calls render_from_path, and the
    #: source is never held in memory at all.
    consumes_path: bool = False

    def render_from_path(self, path: str, mime: str, name: str) -> List[Rendition]:
        ...
```

`VideoPlugin` and the new `AudioPlugin` set `consumes_path = True`. This is a
concrete instalment on the standing platform requirement that content paths move
bounded chunks rather than whole files, and it should be written so other
plugins can adopt it without another interface change.

**(b) Conversion is synchronous inside the event loop of the ingest worker.**
`Ingestor.handle` converts inline while reading `fileengine:events`
(`ingest.py:113`). A VP9 encode of a one-hour 1080p source is **tens of minutes
to hours** of CPU. Doing that inside `handle` stalls the stream for every other
file in the tenant, and any restart loses the work entirely.

**Fix:** publishing is a **durable job** (§4.4) run by a separate worker, and it
is **never triggered by a `file.created` event** (§4.3).

### 4.3 Configuring the share is what triggers the transcode

**The full web-playable rendition is produced on demand, and the demand is a
media share being configured on the file.** There is no automatic conversion
path, no ingest-time trigger, and no deployment switch that creates one.

The event-driven worker keeps doing exactly what it does today for media: poster
+ 10-second `preview`, cheap and immediate, for the file browser (§4.1). Nothing
about that changes, and it never escalates to a full encode.

**The trigger, precisely.** A publish job is created when — and only when — a
media share is configured for a `(file, source version)` that has no current
`media` / `audio` rendition:

| Path | Who | When |
|---|---|---|
| **Minting a `kind = 3` link** (§6.2) | `share_service`, calling CSAI | the canonical trigger; the Share tab's *Create* button |
| A `folder_actions` **Publish media** binding (§4.8, proposed) | the action, on a file arriving in a folder configured for sharing | the folder binding *is* the share configuration |
| **`POST /documents/{uid}/media`** (§4.6) | a person or script, directly | the underlying mechanism the two above call — and the **retry / re-encode** path when a job failed or the encoder changed |

The third is deliberately last. It is the mechanism, not a second product
concept: there is **no standalone "Publish" button** in the drawer's preview area
(§10), because a rendition with no share attached to it is exactly the cost this
rule exists to avoid — and because two ways to reach the same state is how the
two drift.

**Why this is a rule and not a default.** The cost runs in both directions and
both are silent:

- **CPU.** A tenant that syncs a 400-clip footage archive would burn a machine
  for a week producing renditions nobody asked for. In §2.1's shape the archive
  is likely to exist *and* to be mostly unshared — raw recordings, of which a few
  become intro videos.
- **Storage, charged to the tenant.** Renditions are hidden children under the
  source's own ACL, so a published copy lands in the tenant's quota with nothing
  in the UI having announced it. At 720p + 480p + Opus + a GIF, an automatic
  policy roughly doubles what a video folder occupies.

**There is no `CSAI_MEDIA_AUTOPUBLISH`.** An earlier draft of this document had
one, defaulted off. It is removed rather than defaulted, because a knob that
recreates precisely the cost the rule prevents is not a safety valve — it is the
bug, pre-installed, waiting for someone to tidy up a backlog with it. A
deployment that genuinely wants everything published can bind the §4.8 folder
action to the root, which at least leaves a configured, auditable, per-folder
record of the decision.

### 4.3.1 The rendition's lifetime is the share's

The corollary of an on-demand rendition is that it should not outlive the reason
it was made. When the **last live media link** on a `(file, version, profile)`
is revoked or expires, the rendition becomes an orphan: bytes in the tenant's
quota serving nothing.

A reaper in the existing background-worker pattern removes published renditions
with no live link after `CSAI_MEDIA_ORPHAN_DAYS` (default `30`). Three properties
it needs:

- **A grace period, not immediate deletion.** Re-sharing the same intro video a
  week later is the normal case, and re-encoding it would be pure waste. Thirty
  days makes the second share instant.
- **It asks `share_service`, and it fails safe.** Liveness is
  `GET /share/v1/internal/media-refs/{uid}` (§6.2). If that call fails the
  rendition is **kept** — deleting content because a sibling service was briefly
  unreachable is the wrong direction to be wrong in.
- **It never touches `poster` or `preview`.** Those belong to the file browser
  and have nothing to do with sharing.

### 4.4 The job model

A new table in the tenant schema (`schema.py`), following the existing
`CREATE TABLE IF NOT EXISTS` + additive-migration style:

```sql
CREATE TABLE IF NOT EXISTS "<tenant>".media_jobs (
    job_uid         UUID PRIMARY KEY,
    file_uid        UUID        NOT NULL,
    source_version  TEXT        NOT NULL,   -- the version being published; a new
                                            -- source version is a NEW job, never
                                            -- a mutation of this one
    profile         TEXT        NOT NULL,   -- 'video-720p-vp9' | 'video-480p-vp9'
                                            -- | 'audio-mp3' | 'audio-opus'
    status          TEXT        NOT NULL,   -- queued|running|succeeded|failed|cancelled
    requested_by    TEXT        NOT NULL,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    heartbeat_at    TIMESTAMPTZ,            -- a running job with a stale heartbeat
                                            -- is requeued; see below
    attempts        SMALLINT    NOT NULL DEFAULT 0,
    progress_pct    SMALLINT    NOT NULL DEFAULT 0,
    source_bytes    BIGINT,
    output_bytes    BIGINT,
    duration_ms     BIGINT,                 -- media duration, from ffprobe
    encoder         TEXT,                   -- which target of the ladder won
    rendition_name  TEXT,                   -- '<version>-media.webm' on success
    detail          TEXT,                   -- failure reason, user-safe
    UNIQUE (file_uid, source_version, profile)
);
CREATE INDEX IF NOT EXISTS media_jobs_queue ON "<tenant>".media_jobs (status, requested_at)
    WHERE status IN ('queued', 'running');
```

**The `UNIQUE` is the idempotency.** Asking twice for the same
`(file, version, profile)` returns the existing job. A new source version
produces a new row and a new rendition name — the same versioned-idempotency the
rendition writer already has (`rendition_name()` = `<version>-<fmt>.<ext>`), so
re-uploading a video does not silently keep serving the old cut.

**Claiming and crash recovery.** A worker claims with
`UPDATE ... SET status='running', started_at=now(), attempts=attempts+1 WHERE
job_uid = (SELECT job_uid ... ORDER BY requested_at FOR UPDATE SKIP LOCKED LIMIT 1)`.
It writes `heartbeat_at` every 15 s from the FFmpeg progress reader. A `running`
job whose heartbeat is older than `CSAI_MEDIA_STALE_SECONDS` (default 300) is
returned to `queued` — **but only up to `CSAI_MEDIA_MAX_ATTEMPTS` (default 3)**,
after which it is `failed` with `detail = "repeatedly crashed"`. Without the
attempt cap a file that reliably kills the encoder becomes an infinite loop that
looks, from the queue, exactly like a busy worker.

**Progress** comes from `ffmpeg -progress pipe:1 -nostats`, whose `out_time_ms`
against the ffprobe duration gives a real percentage. This is worth the plumbing:
the alternative is a spinner for ninety minutes, which users read as "broken".

**Cancellation** sets `status='cancelled'`; the worker checks between progress
ticks and kills the child. A cancelled job leaves no rendition.

**Concurrency** is `CSAI_MEDIA_WORKERS` (default 1) per worker process, and the
encoder is additionally bounded by `-threads`. Media transcoding will otherwise
starve the ordinary conversion worker of every core on the box; they are separate
processes for this reason (`convert-search-ai-media-worker`, a third entrypoint
alongside the app and the ingest worker).

### 4.5 Encoder ladder and parameters

**Two video profiles.** `video-720p-vp9` (the default) and `video-480p-vp9`
(`media-sd`) — same encoder ladder, same settings, different `-vf scale` cap and
`-crf` (`33` at 480p). Both are produced by **one job per profile**, queued
together when publishing is requested, so a failure of the SD encode never
withholds the HD one. The SD encode is skipped, not failed, when the source is
already ≤480p on its long edge: a second identical file is pure storage.

This is a **manual quality picker, not adaptive streaming** — the player offers
*Auto / 720p / 480p*, `Auto` meaning "start at SD on a connection the browser
reports as slow, otherwise HD", and never switches mid-playback. Real ABR needs
segmentation and a manifest, which is a different rendition model (§2, §14-Q4).

**Video encoder ladder.** Targets, best-first, chosen the way
`_PREVIEW_TARGETS` already is (`plugins/video.py:26`), by probing
`tools.ffmpeg_encoders()`:

| Encoder | Container | Notes |
|---|---|---|
| `libvpx-vp9` | webm | the target the sketch names |
| `libvpx` (VP8) | webm | still fully open; a build without VP9 |
| `libx264` | mp4 | last resort, `+faststart`; the rendition ext/mime change with it |

Video settings:

- **Scale:** `-vf scale='min(1280,iw)':-2` — cap the *long edge* at 1280×720
  while never upscaling and keeping even dimensions. A portrait phone video must
  cap at 720 **wide**, not 1280 wide; writing this as `scale=1280:-2` gets that
  wrong and produces a 1280×2276 file. Use
  `scale='if(gt(iw,ih),min(1280,iw),-2)':'if(gt(iw,ih),-2,min(1280,ih))'`.
- **Quality:** constrained quality — `-crf 31 -b:v 0` for VP9 with
  `-row-mt 1 -tile-columns 2 -deadline good -cpu-used 2`. Not `-deadline
  realtime -cpu-used 8` as the preview uses: that is tuned for a ten-second clip
  produced while a user waits, and it costs roughly 2× the bitrate for the same
  perceived quality. A publish job is allowed to be slow.
- **Audio:** `-c:a libopus -b:a 128k` (or `-c:a aac -b:a 192k` in the mp4
  fallback). *"Full quality audio"* in the sketch is read as **transparent**, not
  **lossless**: Opus at 128 kb/s stereo is transparent for speech and
  near-transparent for music, and lossless audio inside a 720p video is a
  bandwidth choice nobody would make deliberately. `CSAI_MEDIA_AUDIO_BITRATE`
  exists for a deployment that disagrees.
- **Seekability:** `-g 240` (keyframe every ~8 s at 30 fps). WebM is streamable
  by construction, but without periodic keyframes a seek lands seconds away from
  where the user clicked. For the mp4 fallback, `-movflags +faststart` moves the
  index to the front — without it the browser must download the whole file before
  it can play anything, which is the single most common "my video won't play"
  cause.
- **Timeout:** not the plugin's 180 s. `CSAI_MEDIA_JOB_TIMEOUT_SECONDS`, default
  `21600` (6 h), with the heartbeat as the real liveness signal.

**Two audio profiles** (Q3, resolved 2026-09-26 — emit both).

- `audio-mp3` → the `audio` rendition. `-c:a libmp3lame -q:a 0` (LAME V0,
  ~245 kb/s VBR) with `-write_xing 1`. V0 rather than 320 CBR: it is
  transparent, smaller, and LAME writes a Xing/LAME header carrying the frame
  count so a player can show an accurate duration and seek in a VBR file — the
  historical reason people reach for CBR, and one that has not applied for
  fifteen years. `CSAI_MEDIA_MP3_QUALITY` selects otherwise.
- `audio-opus` → the `audio-opus` rendition. `-c:a libopus -b:a 96k` in a WebM
  container. Opus at 96 kb/s is transparent for speech and beats MP3 V0 at
  ~40% of the bytes.

**The player prefers Opus; everything else gets the MP3.** The peek payload
(§6.8) lists both sources in preference order and the player takes the first its
browser reports it can play (`canPlayType`). The MP3 is what a direct link, an
RSS/podcast consumer, or a hardware player receives, and it is what
`allow_download` hands over — which is the whole reason it is still produced.

Both come from **one decode of the source**, and if the deployment's FFmpeg has
no `libopus` the `audio-opus` job is skipped (not failed) and the MP3 stands
alone — the same graceful-degradation posture the video ladder already has.

**A source that is already conformant is copied, not re-encoded.** If ffprobe
says the input is already VP9/Opus in WebM within the size cap, use
`-c copy` (remuxing only if needed for faststart). Re-encoding a file to produce
a worse copy of itself is pure loss.

### 4.6 API and status surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/documents/{file_uid}/media` | Request publication. Body: `{profile?}`. Returns the job (existing or new) — **202**, never blocking. **Requires WRITE** on the file — publishing writes a child and consumes quota; settled as a decision, not a default (§14-Q6). **This is the mechanism §4.3's triggers call, not a second product surface** — `share_service` is its usual caller, and a direct call is the retry / re-encode path. |
| `GET` | `/documents/{file_uid}/media` | Current state: the job (if any) and the `media`/`audio` rendition for the current source version, with `duration_ms`, `output_bytes`, `encoder`. READ-gated. |
| `DELETE` | `/documents/{file_uid}/media` | Cancel a running job, or remove a published rendition. WRITE-gated. Refuses (409) while a **live** share link depends on it (§6.2). |

`GET /v1/capabilities` gains a `media` block — `{publish: bool, profiles: [...],
reason?: "..."}` — following the `_editing()` pattern in
`routers/capabilities.py`, so the frontend hides the media share option on a
deployment whose FFmpeg has no usable encoder instead of offering a button that
always fails. The three conditions to report are: FFmpeg present, a target from
the ladder available, and the media worker seen alive recently (a `media_jobs`
heartbeat within the last 5 minutes). The third is the one that is otherwise
discovered only by waiting an hour.

### 4.7 Pruning, storage, and the interaction with culling

- `prune_old_versions` removes superseded `media`/`audio` children automatically
  once they are in `_KNOWN_FMTS` — which is the point of §4.1's warning.
- **A published rendition can be larger than the source.** A 200 MB h.265 phone
  video transcoded to VP9 at CRF 31 will usually shrink, but an already-efficient
  source re-encoded can grow. The job records `output_bytes`; the UI shows it
  before and after; `CSAI_MEDIA_MAX_OUTPUT_BYTES` (default 0 = unbounded) can
  refuse a job whose output exceeds a ceiling, and a job that exceeds it
  mid-encode is failed and its partial output discarded rather than written.
- **Erasure.** `pipeline.convert` already refuses an erased uid
  (`pipeline.py:85`); the media worker must check `store.is_erased` at claim time
  **and** immediately before the rendition write, since a job's lifetime is long
  enough for an erasure to land in the middle of it. This is the same race the
  existing comment describes, with a much wider window.

---

### 4.8 Publish on arrival — a `folder_actions` plug-in *(proposed, severable)*

*"Publishing automation"* has a more literal reading than a button in a drawer:
**a folder you drop a clip into publishes it.** `folder_actions` already is that
mechanism — it binds actions to a virtual folder, consumes `fileengine:events`,
ships five plug-ins, and extends through the `folder_actions.actions`
entry-point group with a typed `config_fields()` so a generic frontend renders
the form with no bespoke UI. It already has a `csai_client.py` and already
consumes CSAI's `conversion.complete`.

A **Publish media** action would therefore be one plug-in, not a subsystem:

| | |
|---|---|
| `supported_events` | `file.created`, `file.updated`, `conversion.complete` |
| Config | `profiles[]`, `access_mode`, `expires_days`, `max_bytes`, `allowed_embed_origins[]`, `notify` — **no `mint_link` toggle**: minting is what the action does, and an action that publishes without sharing is the automatic conversion §4.3 forbids |
| Does | mints a `kind = 3` link, which triggers the transcode (§6.2); on `media.published`, posts the URL + embed snippet to the existing notification/attention path |
| Guard | declares no `auto_moves`; it writes a child, never relocates the file |

**This does not reintroduce automatic conversion** (§4.3). The action's job is to
**configure a share**; the rendition is produced because a share now exists, by
the same path the Share tab uses. A binding that mints no link publishes nothing.
The distinction is not pedantry — it is what keeps "a folder whose contents get
shared" from quietly becoming "a folder whose contents get transcoded".

`Marketing/To publish` becomes an inbox in exactly the sense the sorter action
already makes one: drop a clip in, get back a link and an embed snippet, with the
folder's binding — not the uploader — deciding the access mode. That also puts
the `open`-mode authority on a **folder** an admin configured once, which is a
better place for it than a checkbox a user ticks per link.

Two new events CSAI must emit for this, both siblings of the existing
`conversion.complete` / `conversion.failed` (`EVENT_CONTRACT.md`):
`media.published` and `media.publish_failed`, carrying `file_uid`,
`source_version`, `profile`, `rendition_name`, `output_bytes`, `duration_ms`.
They are worth emitting whether or not this section is built — the Share tab's
*preparing → ready* transition (§6.2) wants a push rather than a poll.

**This is proposed, not assumed.** It is a sixth repo and nothing else in this
document depends on it; §15's MS10 is deliberately last and deliberately
optional. §14-Q8 asks.

---

## 5. Access modes

A new column on `share_links`, not a boolean and not a pair of flags:

```sql
ALTER TABLE "<tenant>".share_links
    ADD COLUMN IF NOT EXISTS access_mode TEXT NOT NULL DEFAULT 'verified';
    -- 'verified' | 'claimed' | 'open'
```

`verified` is the default so that every existing row, and every link of a kind
other than media, keeps precisely today's behaviour with no migration.

| Mode | What the visitor does | What is recorded | Mail sent |
|---|---|---|---|
| `verified` | email → OTP → code (OSL §6.9, unchanged) | the **verified** address | the OTP |
| `claimed` | types an email, presses play | the **claimed** address, flagged unverified | none |
| `open` | nothing | a viewer counter and, if enabled, coarse analytics | none |

**Rules that bind all three:**

1. **`access_mode` is immutable after creation.** Changing a link from `verified`
   to `open` would silently widen an already-distributed URL, and an audit entry
   after the fact is not a substitute for the change being impossible. Widening
   means minting a new link — the same reasoning OSL applies to
   `add_recipient` on a revoked link.
2. **`claimed` and `open` are only legal for `kind = 3`.** Enforced by a CHECK
   constraint, not only by the route handler: this is the single rule keeping
   R14's reopening narrow, and it should not depend on a validator someone can
   forget to call on a new endpoint.
3. **`recipients[]` remains required for `verified`, is optional for `claimed`
   (a pre-seeded list is advisory only), and is refused for `open`.** For
   `claimed`, an address on the recipient list that shows up is marked as such —
   *"the person we expected"* is useful even unverified.
4. **The creator must explicitly acknowledge an `open` link.** The create call
   carries `confirm_public: true` and the UI makes the visitor see the words
   *"anyone with this link, and anyone they forward it to, can watch this"*.
   OSL's entire design was built to make that sentence untrue; making it true
   again should require typing something.
5. **`open` may be disabled deployment-wide** (`share.allow_open_mode`, default
   **`false`**) and gated on a *separate* LDAP group
   (`share.open_ldap_group`, default `share_public`) from the one that gates
   minting at all. Publishing to the open internet and sharing with a named
   client are different authorities, and a tenant should be able to grant one
   without the other.

---

## 6. The share side (`share_service`)

### 6.1 A new kind, not a variant of kind 0

```
kind 3 = media  — resource_uid is the SOURCE file; the payload is its
                  published `media` or `audio` rendition.
```

A media link is not a file-download link with different headers. It differs in
what it serves (a derived child, not the resource), how it is consumed (played,
not downloaded), how it is metered (§6.4), what origin it is served from (§6.5),
and which headers apply (§6.5). Overloading kind 0 would put four conditionals
into every branch of the redemption path and make "did this link get the
attachment header?" a question with a runtime answer.

The source file uid stays in `resource_uid` because that is what the authority
re-check must be run against, what the drawer shows the link under, and what the
CSV sidecar attaches to. The rendition uid is resolved at session open, never
stored and never accepted from the caller — OSL §7.3's containment rule applies
verbatim.

### 6.2 Creation

`POST /share/v1/nodes/{uid}/links` with `kind: 3` additionally:

1. **Requires READ on the source** (as kind 0) and the `share_external` group;
   plus `share_public` and `confirm_public` when `access_mode = "open"`.
2. **Resolves the published rendition** for the source's current version — and
   **this call is the transcode's trigger** (§4.3). If there is none:
   - with `publish: true` (the default, and what the Share tab always sends) it
     asks CSAI to publish and mints the link in state **`pending_media`** — live,
     but `/content` returns *"still preparing"* and the landing page shows
     progress. This is the right default: the creator wants to send the URL now
     and have it work by the time anyone clicks. For a 2–5 minute clip (§2.1)
     that is usually a minute or two, and it is finished before the email is.
   - with `publish: false` it refuses with a reason, for a scripted caller that
     wants a link only if the bytes already exist and does not want to spend CPU
     discovering otherwise.
3. **Pins the rendition by name**, not by uid resolved later — same reasoning as
   OSL's version pinning, and the same trap the development plan records
   (`back=N` is positional and shifts). A new source version does **not**
   retarget a live link; the link keeps serving the cut that was published when
   it was minted, and the Share tab shows *"a newer version of this file has been
   published — re-share to use it"*.
4. **Stores `duration_ms`, `output_bytes`, `poster_uid`** on the link, so the
   peek endpoint and the embed can answer without touching the core.
5. **Refuses if the published rendition exceeds `share.media_max_bytes`.**

`DELETE /documents/{uid}/media` in CSAI refuses while a live media link points at
that rendition — otherwise unpublishing silently breaks every embed on a customer
site. CSAI asks `share_service` (`GET /share/v1/internal/media-refs/{uid}`, the
`require_internal` seam) rather than growing a share table.

### 6.3 Three redemption flows

```
verified:  peek → identify(email) → verify(code) → session → play
claimed:   peek → claim(email) ─────────────────→ session → play
open:      peek ─────────────────────────────────→ session → play
```

`POST /share/v1/public/{link_uid}/claim` `{email, consent}` is the one new public
route. It:

- **validates the address syntactically and by MX lookup only** (§7.3) — it never
  sends to it;
- normalizes it exactly as the verified path does (lowercase, trim);
- writes a `share_link_audience` row (§7.1) with `verified = false`;
- issues the same shape of **viewer token** the verified path's recipient token
  has, with the same storage (Redis, hashed, TTL) and the same binding — so the
  session code path downstream is *identical* for all three modes and there is
  exactly one place where a session opens.

For `open`, `/session` issues a viewer token bound to nothing but the link and a
random client id. **This uniformity is deliberate**: the alternative — three
session paths — is how a mode ends up skipping the authority re-check.

### 6.4 What a "use" means for media

This is where OSL's accounting model does not survive contact, and the section
exists because getting it wrong is how a link either breaks for a legitimate
viewer or serves a terabyte.

**A single view of a video is:** one `GET` for the first bytes, a `Range` request
for the tail (the browser looking for the index), then a series of ranged GETs as
it buffers, plus one more per seek. **Ten to fifty HTTP requests, over an hour,
possibly from a changing IP on mobile.** OSL's *"a use is a redemption session"*
was written precisely to stop Range requests burning budget, and that part holds.
But OSL's session TTL is one hour, which a long video or a paused tab outlives.

| Counter | Media meaning |
|---|---|
| `max_uses` | **sessions**, as elsewhere. For media the session TTL is `share.media_session_ttl_seconds` (default `86400`) — a session covers *watching this thing*, including pauses and a resume tomorrow, not one hour. |
| `max_bytes` | **the real bound.** Total bytes served across all sessions. This is the one that matters for open links and the one the UI must show: `output_bytes × expected viewers`, with the worst case (`max_bytes`) stated in the create form the way OSL states `archive_bytes × max_uses`. |
| `max_viewers` (new) | distinct audience identities (verified or claimed addresses; for `open`, distinct viewer tokens). `0` = unlimited. This is what a creator actually means by "about thirty people". |
| `max_uses_per_recipient` | unchanged for `verified`/`claimed`, keyed on the address. |

**Bytes are metered on what is actually written to the socket**, counted as
chunks leave, not from `Content-Length` — a viewer who watches ten seconds and
closes the tab must not be charged for the whole file, and a re-buffered range
must be charged twice because it *was* sent twice. When `bytes_consumed` crosses
`max_bytes` mid-stream, the current response is allowed to finish and the next
request is refused: cutting a response mid-body produces a corrupt-looking
failure with no way to explain itself.

**`share.media_max_egress_per_hour`** per link is the burst control that a
monthly budget cannot provide — an embed on a page that unexpectedly goes viral
should throttle, not silently spend the whole allowance in ten minutes and go
dark.

### 6.5 The media origin

Served at **`<tenant>-media.<base>`**, rendered alongside the existing vhosts in
`docker_unified/images/nginx/render-config.sh` (the `<tenant>-drive.<base>` block
at `:66` is the model), with its own `limit_req` zone and `access_log` format
that omits `$query_string`.

What it serves, and how it differs from `/share/v1/public/*`:

| Header | Tenant origin (OSL §7.2) | Media origin |
|---|---|---|
| `Content-Disposition` | `attachment` | **`inline`** |
| `Content-Security-Policy` | `sandbox` | **`default-src 'none'; media-src 'self'; frame-ancestors <allowed>`** |
| `X-Content-Type-Options` | `nosniff` | `nosniff` (unchanged) |
| `Cache-Control` | `no-store` | **`private, max-age=…`** on the bytes; `no-store` on every JSON route |
| `Accept-Ranges` | `none` (zip) / bytes (file) | **`bytes`, always** |
| `Access-Control-Allow-Origin` | absent | **echoed from the link's embed allowlist**, never `*` |
| `X-Robots-Tag` | `noindex, nofollow` | `noindex` unless the link opts in |

**The `Content-Type` served is the rendition's own** (`video/webm`,
`audio/mpeg`) — it must be, for a `<video>` element to play it — which is exactly
why this cannot share an origin with the SPA. The set of MIME types this origin
will *ever* emit is a fixed allowlist of media types; **anything else is a bug
and must 500 rather than being served**. That assertion is cheap and it is the
property that makes the origin split structural instead of another header to
remember.

**The origin serves no authenticated route at all.** No SPA, no bearer token, no
cookie with a domain broad enough to reach it. Set
`SHARE_MEDIA_BASE_URL` explicitly in the same way `SHARE_PUBLIC_BASE_URL` is, so
moving to a CDN hostname later does not invalidate embeds already in the wild.

**Ops precondition:** a wildcard TLS certificate covering `*-media.<base>`, or an
issuance path for it. The deployment's DNS/TLS story for `<tenant>-drive` is the
one to copy.

### 6.6 The media cache (R16)

`share_service` keeps published renditions on local disk:

```
<share.media_cache_dir>/<tenant>/<rendition_uid>/<version>.<ext>
```

> **Why a cache at all, now that the core can serve ranges (§3-R16).** It is an
> optimisation, not a mechanism: a published rendition is immutable and derived,
> so caching it avoids crossing a process boundary for every seek of a hot
> object, and it is where a CDN would sit later. The core's `range_method`
> tells the service when it is worth doing. Everything below stands either way —
> and until the core proposal's B0 lands, this is also what makes seeking work.

- **Filled once**, on the first request that needs bytes, by a single
  `StreamFileDownload` **as the creator** — with a per-(tenant, rendition) lock
  so fifty simultaneous viewers of a newly-published video cause one fetch, not
  fifty. Writes go to a temp name and are `rename`d into place, so a partially
  fetched file is never served.
- **Immutable.** The key includes the rendition's version; a new publish is a new
  key. There is no invalidation problem because there is no mutation.
- **Ranges are served from the file**, with `Content-Length`, a correct
  `Content-Range: bytes X-Y/TOTAL`, and 206. Multi-range requests are refused
  with 200 + the whole body (legal, and no browser needs them for media).
- **Culled** by size (`share.media_cache_max_bytes`, LRU on last access) and by a
  sweep that drops entries whose link is dead. **The cull must be sync-aware in
  the same sense the core's disk cull is**: never evict an entry with an in-flight
  read, and fail closed — if the cache cannot be written, fall back to streaming
  from the core rather than serving nothing.
- **Authority is still re-checked** (§6.7) before any byte is served. The cache is
  a byte store, never an authorization decision. This is the sentence that has to
  stay true: a cache that answers a request without asking the core is how a
  revoked share keeps working for a week.

### 6.7 The authority re-check, on a door that gets fifty requests a view

OSL §6.3's re-check (`CheckPermission(created_by, resource_uid, READ)` with roles
resolved live from LDAP, admin roles stripped) is the *only* identity-shaped
control left in open mode (§3-R14), so it cannot be dropped — but running it per
ranged GET means an LDAP round-trip and a gRPC call per 2 MB of video.

**Decision:** the check runs **at session open**, and again on any request more
than `share.media_recheck_seconds` (default `300`) after the last check for that
session, with the result cached per (link, creator) — reusing CSAI's own
precedent of a TTL-bounded permission cache that is **also invalidated in real
time** by the core's `acl.changed` / `role.*` events. `share_service` already
consumes the core's event stream for nothing; subscribing to those two events and
dropping the affected creator's cache entries makes revocation effectively
immediate without a check per request.

Worst case without the event (events off, or a missed message): five minutes of
continued playback after access is revoked, bounded and stated. With the event:
the next range request fails.

### 6.8 What the player needs, and where it gets it

`GET /media/v1/{link_uid}` (peek, on the media origin) returns, for a live link:

```json
{
  "kind": "video", "mode": "open",
  "title": "Site walkthrough",
  "duration_ms": 4321000,
  "poster": "/media/v1/<link>/poster",
  "state": "ready",            // ready | preparing | dead
  "progress_pct": null,        // set while preparing
  "requires": "none",          // none | email | code
  "allow_download": false,
  "sources": [                 // ORDERED by preference; the player takes the
    {                          // first its browser says it can play
      "label": "720p", "quality": "hd", "default": true,
      "mime": "video/webm; codecs=\"vp9,opus\"",
      "width": 1280, "height": 720, "bytes": 812345678,
      "url": "/media/v1/<link>/content?q=hd"
    },
    {
      "label": "480p", "quality": "sd", "default": false,
      "mime": "video/webm; codecs=\"vp9,opus\"",
      "width": 854, "height": 480, "bytes": 331002110,
      "url": "/media/v1/<link>/content?q=sd"
    }
  ]
}
```

**The source list is assembled server-side** and `q` is validated against it —
an enum, never a rendition name or uid from the caller (§13.4). An audio link's
list is the same shape with `audio-opus` first and `audio` (mp3) second. A link
whose SD or Opus encode was skipped simply has one entry, and the player's
quality control disappears rather than offering something that 404s.

`state: "preparing"` is what makes §6.2's *mint before the encode finishes* work:
the landing page and the embed both poll and then play, rather than showing a
broken player. `requires` tells the embed which gate to render **before** asking
for bytes, so the visitor never sees a failed media element.

The **title is the source file's name by default** and the creator can override
it per link (`display_name`). Defaulting to the filename on an open link leaks
whatever the internal naming convention says —
`ACME-Q3-teardown-CONFIDENTIAL-v4.mp4` is a sentence the creator did not mean to
publish. The create form shows the name that will be public and lets it be
changed; for `open` links it is **required** rather than defaulted.

---

### 6.9 Metering, load shedding, and the signal to publish elsewhere

**This is not a video platform, and the metering is how it says so.** A handful
of clients watching a deliverable, a prospect watching a demo, a product clip on
a marketing page — those are the load this is built for. A video that goes viral,
a page that embeds it above the fold on a high-traffic site, or someone pointing
a script at the URL are all the same shape from here: sustained egress a document
platform should not be absorbing. The system's job is to **notice, bound it, keep
serving everyone else, and tell the creator to put the file on YouTube or Vimeo
and share that link instead**.

That last clause is a feature, not an apology. The failure this section prevents
is not only the denial of service — it is the *silent* success where a link
quietly serves four terabytes and the first anyone knows is the hosting bill.

#### What makes this different from the rate limits already specified

OSL §8.4's controls are built around **discrete attempts**: a wrong code, a
guessed secret, a session opened. They count events and lock an address out. None
of that describes the media door, where a single legitimate viewer generates
dozens of requests over an hour and the resource being consumed is **bandwidth,
time and concurrency** rather than attempts. A per-request rate limit tuned to
allow normal playback is far too loose to stop a flood; one tuned to stop a flood
breaks seeking on a slow connection.

So media metering counts **bytes, concurrency and cache misses**, over rolling
windows, at three scopes — and the per-attempt controls stay exactly as they are
for the `/claim` and `/verify` routes, which *are* attempt-shaped (§7.3).

#### The five shapes worth detecting

These are named because a generic "requests per second" limiter catches only the
first, and the other four are what would actually hurt.

| Shape | What it looks like | What catches it |
|---|---|---|
| **Flood** — many clients, many requests | request rate up, unique clients up, cache hit rate high | nginx `limit_req`/`limit_conn` per IP; link and tenant egress windows |
| **Range thrash** — one client, endless small seeks | bytes served to one session far exceeding the file size; many ranges, low completion | `bytes_served / output_bytes` per session ratio (§6.4 already meters bytes written, not `Content-Length`) |
| **Cache-miss amplification** — one request each to many *different* links | request rate *low*, but every request is a cache fill: a `StreamFileDownload` of a whole rendition from the core | **cache-miss rate**, metered separately and capped hard — this is the expensive path and the one a naive request limiter never sees |
| **Slow-read occupancy** — many connections held at a trickle | concurrency high, aggregate throughput low | concurrency caps + a **minimum throughput** floor, never a "slow client" rule (see below) |
| **Distributed low-and-slow** — every source under every per-IP limit | nothing local looks wrong; the tenant total climbs | the **tenant-scope** window, which is the only scope that sees it |

**Slow clients are not attackers.** A phone on a train and a slowloris look
identical for the first thirty seconds, and refusing slow readers would break
exactly the viewers the 480p rendition exists for. The rule is therefore a
minimum *sustained* throughput (`share.media_min_throughput_bps`, default
`8 KiB/s` averaged over 60 s, evaluated only after a 60 s grace) combined with a
concurrency cap — a connection that has moved almost nothing for a minute is
dropped, and a client that reconnects gets a fresh grace. Under pressure the
answer is to shed *new* sessions, not to punish slow ones.

#### Scopes and windows

Three scopes, each with a short window (burst) and a long one (sustained):

| Scope | Short | Long | Why it exists |
|---|---|---|---|
| **Link** | `media_max_egress_per_hour` (5 GiB) | `max_bytes` on the record (§6.4) | the creator's own budget |
| **Tenant** | `media_tenant_egress_per_hour` (20 GiB) | `media_tenant_egress_per_day` (200 GiB) | 500 links each at 99% of their own budget is still an outage |
| **Service instance** | `media_max_concurrent_streams` (64) | `media_cache_fills_per_minute` | protects the process, the box, and the core behind it |

Plus the counters that are not byte budgets:

- `media_max_concurrent_per_link` (default 16) — simultaneous streams for one
  link. This is the control that actually stops an embed on a busy page, because
  it bites in seconds where an hourly byte budget bites in minutes.
- `media_max_concurrent_per_client` (default 3) — per viewer token / IP pair.
  A browser legitimately opens two (video + a speculative range); three is
  headroom, four is a script.
- `media_cache_fills_per_minute` (default **30** per instance) — the
  cache-miss-amplification bound.

  **This default is set by §2.1, not by the attack.** In the primary use case
  every new intro video is a distinct small file watched by one person, so a
  cache *miss* is the normal path and a low ceiling here would throttle ordinary
  Tuesday-morning use. Thirty a minute accommodates a sender whose whole
  prospect list opens their mail at once, while still bounding the one shape that
  would otherwise walk the tenant's entire rendition set through the core.

  Two things keep the ceiling honest despite being generous:
  - **Fills are bounded by size, not just by count** —
    `media_cache_fill_bytes_per_minute` (default 2 GiB) — because thirty 40 MB
    clips and thirty 2 GB recordings are not the same event.
  - **A fill for a link whose first request has not yet been authorized never
    starts.** The order is authorize, then fill, then serve; a caller who cannot
    open a session cannot cause a read from the core at all. That single ordering
    rule removes most of the amplification surface before any counter is
    consulted.

#### Warm the cache when the link is minted

In the primary use case the creator knows *exactly* when the first view will
happen: they are about to paste the URL into an email. So the cache fill should
not wait for the recipient.

`share.media_warm_on_mint` (default **true**) fetches the published rendition
into the cache in the background as the link is created. It makes the recipient's
first click fast, and — more usefully here — it moves the expensive whole-file
read from an unauthenticated request path to an **authenticated one, at a moment
the service chose**. Warming is queued, bounded by the same fill budget, skipped
when the rendition is already cached, and never blocks the create call.

A link parked at rung 3 or revoked before its first view simply has a cache entry
that the LRU reclaims. That is the cost, and it is small.

#### The response ladder

Escalating, and modelled on OSL §8.4's three-rung structure so the two doors
behave recognisably alike. **No rung ever revokes**, for the same reason OSL gives:
anyone holding the URL could otherwise destroy the link.

| Rung | Trigger | Response |
|---|---|---|
| **0 — observe** | any window above 50% | metrics only; nothing user-visible |
| **1 — advise** | any window above 80% | the creator gets an attention item with the numbers and the *publish elsewhere* guidance (below); serving is unaffected |
| **2 — throttle** | a short window exhausted, or concurrency at cap | new requests get **429 with `Retry-After`**; in-flight responses finish. A per-session bandwidth cap (`media_throttled_bps`) may be applied instead where that keeps more viewers watching |
| **3 — park** | a long window exhausted, or rung 2 sustained for `media_park_after_minutes` | `throttled_until` on the link: `/content` returns a **friendly, explanatory** page — *"this video is temporarily unavailable because it has been very popular"* — not a generic 404; the creator is notified loudly |

**Rung 3 breaks OSL §8.5's uniform-failure rule, deliberately.** OSL requires
identical responses for unknown / expired / revoked / exhausted links so the door
discloses nothing. Here the visitor is someone watching a public marketing video
and the fact being disclosed — *this link is popular* — is not a secret; it is
usually already on the page that embedded it. A generic 404 would send them to
support and the creator to a bug report. **The exception is scoped to
`access_mode = 'open'` links at rung 3 and to nothing else**: gated links keep the
uniform failure, because there the existence of the link *is* the secret.

#### Ceilings are enforced, not just measured

- **Authoritative budgets live in Postgres** (`bytes_consumed`, `uses_consumed`
  on `share_links`). Rolling windows live in **Redis**, shared across replicas —
  the same reasoning OSL §7.4 gives for the recipient token, and the same trap
  (`ReplayGuard`'s header) it names twice: a per-process counter hands an attacker
  one fresh allowance per replica.
- **Byte accounting is flushed, not written per chunk.** A session accumulates in
  memory and flushes to Postgres every `media_meter_flush_bytes` (16 MiB) or
  `media_meter_flush_seconds` (10 s), whichever comes first, and always at session
  end. A crash therefore loses at most one flush interval of accounting — bounded,
  and stated here rather than discovered from a budget that never quite adds up.
- **If Redis is unavailable, media serving degrades rather than stopping.** The
  durable Postgres budgets still apply and are still enforced; the rolling windows
  are unavailable, so the service falls back to a conservative static concurrency
  cap (`media_max_concurrent_streams / 4`) and logs loudly. This is the one place
  this document does **not** follow the platform's fail-closed default, and the
  reason is that the thing Redis protects here is a *comfort* bound, not an
  authorization decision — a Redis blip should not take every customer's embedded
  video dark. Authorization (§6.7) and budgets (Postgres) are unaffected, and both
  still fail closed.

#### The signal to publish elsewhere

At rung 1 and again at rung 3, the creator's attention item (OSL §10.6) carries
the measurement and the recommendation:

> **"Site walkthrough" is getting more traffic than FileEngine is meant to
> serve.** In the last 7 days it served **412 GB** to **9,140 viewers** across
> **38 referring sites**, and reached its hourly limit 14 times. FileEngine
> publishes media for clients and prospects — it is not a CDN. For an audience
> this size, upload the video to YouTube or Vimeo and share that link from your
> site instead. *(Your file and this share link are unaffected.)*

The same thresholds surface in the Share tab's egress meter (§10) and in
`/admin/shares` as a tenant-wide *"links outgrowing this platform"* list. The
numbers are the argument; a bare "throttled" badge would read as a defect rather
than as advice.

**`share.media_soft_ceiling_*`** (the rung-1 thresholds) are therefore
documentation as much as configuration: they are the deployment stating, in
numbers, what it considers normal. They should be set deliberately and low —
a deployment that keeps raising them is a deployment that has decided to be a
video host.

#### Metrics

Exposed through the verbatim `metrics.py` (`fileengine_*` prefix, loopback-only
scrape), labelled by `tenant` and, where cardinality allows, by `link_uid` for
the top-N only:

```
fileengine_media_bytes_served_total{tenant}
fileengine_media_requests_total{tenant,outcome}      # ok|throttled|parked|denied
fileengine_media_sessions_total{tenant,mode}
fileengine_media_active_streams{tenant}
fileengine_media_cache_fills_total{tenant}
fileengine_media_cache_hit_ratio{tenant}
fileengine_media_rung{tenant,link_uid}               # 0..3, top-N links only
fileengine_media_budget_used_ratio{tenant,scope}     # link|tenant, worst offender
fileengine_media_meter_degraded                      # 1 when Redis windows are unavailable
fileengine_media_playback_beacons_total{tenant}
fileengine_media_completions_total{tenant,basis}     # beacon+bytes|beacon|bytes-floor
```

**Alert on `fileengine_media_cache_fills_total` before request rate.** A fill is
a whole-file read from the core; it is the metric that leads every other one when
something is going wrong, and the only one that catches cache-miss amplification
at all.

---

## 7. The audience

### 7.1 `share_link_audience`

`share_link_recipients` (OSL §5.4) is an *allowlist* — a closed set written by an
authenticated user. What the new modes gather is the opposite: an open-ended set
written by outsiders. Putting rows an internet caller created into the table that
defines who is authorized is the kind of conflation that becomes a privilege bug
two refactors later. A separate table:

```sql
CREATE TABLE IF NOT EXISTS "<tenant>".share_link_audience (
    audience_uid   UUID PRIMARY KEY,
    link_uid       UUID        NOT NULL REFERENCES "<tenant>".share_links(link_uid) ON DELETE CASCADE,
    email          TEXT,                    -- NULL for open mode
    email_norm     TEXT,                    -- lowercased/trimmed; the dedupe key
    verified       BOOLEAN     NOT NULL DEFAULT false,
    on_allowlist   BOOLEAN     NOT NULL DEFAULT false,  -- was this address expected?
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    sessions       INTEGER     NOT NULL DEFAULT 0,
    bytes_served   BIGINT      NOT NULL DEFAULT 0,
    -- playback, rolled up from share_media_playback (§7.4). Kept here as well
    -- as there so the roster is one query, the same reasoning OSL §5.4 gives
    -- for the recipient status ladder.
    coverage       BIT VARYING,             -- union of every session's watched buckets
    coverage_pct   SMALLINT    NOT NULL DEFAULT 0,  -- distinct content watched
    furthest_pct   SMALLINT    NOT NULL DEFAULT 0,  -- furthest point reached
    plays          INTEGER     NOT NULL DEFAULT 0,
    completed_at   TIMESTAMPTZ,             -- first time completion was met (§7.4)
    completion_basis TEXT,                  -- 'beacon' | 'beacon+bytes' | 'bytes-floor'
    source_addr    TEXT,
    user_agent     TEXT,
    referer_host   TEXT,                    -- which site the embed was on
    consent_text_id TEXT,                   -- WHICH consent wording they saw (§7.3)
    UNIQUE (link_uid, email_norm)
);
```

`UNIQUE (link_uid, email_norm)` makes a returning viewer an update, not a second
row — so "how many people watched" is a row count and not a distinct-query
somebody will forget to write.

The playback columns are a rollup of §7.4, which is where the measurement and —
more importantly — its limits are specified.

### 7.2 In the Share tab

The roster in OSL §10.2 gains a second table for media links, sitting next to the
recipient roster rather than replacing it:

- **`verified`** — the existing roster, unchanged, plus watch stats per address.
- **`claimed`** — every address typed, with an explicit **"unverified"** marker
  on each row and a one-line explanation at the top of the table: *"These people
  typed an address to watch. We did not check it, so any of them may be
  inaccurate or invented."* A column shows whether the address was on the
  advisory allowlist.
- **`open`** — no addresses. A viewer count, distinct-client count, total bytes,
  a small referrer-host breakdown, and the same watch statistics in aggregate.

**Export CSV** is the same data as the sidecar (§7.3), offered as a download for
the case where the sidecar is not wanted.

### 7.3 Accepting an address from the internet

The `claim` route accepts arbitrary text from an unauthenticated caller and
stores it in a tenant's database and a tenant's file. Everything about that needs
saying out loud:

- **Validation is syntax + optional MX**, nothing more. No probing the mailbox.
- **Length-capped** (320 bytes, the RFC maximum) and **rejected if it contains
  anything but an address** — the field is not a free-text field, and a CSV is
  precisely the format where `=cmd|' /c calc'!A1` in a cell becomes code in
  someone's spreadsheet. **Every CSV field is written with the formula-injection
  guard**: a leading `=`, `+`, `-`, `@`, tab or CR is prefixed with `'`. This
  applies to the sidecar and the UI export alike and is a required test.
- **Rate-limited** per IP and per link (`share.claim_rate`, default
  `10 / hour / IP / link`), because the table is otherwise a free write endpoint
  for anyone holding the URL, and a million junk rows is both a storage problem
  and a denial of the roster's usefulness.
- **The consent line is recorded, not assumed.** The landing page and the embed
  must state what the address is used for — *"the owner of this file will see
  that you watched it"* — and `consent_text_id` records which wording the person
  actually saw. If the wording changes later, the record still says what was
  agreed to. This is the minimum an operator needs to answer a data-protection
  question about rows they did not collect themselves.
- **This is PII in a tenant schema and in a tenant file.** OSL §5.5's retention
  window applies; the erasure path must reach both the table and the sidecar
  (§8.4).

---

### 7.4 Playback telemetry — *did they watch the whole thing?*

For §2.1's primary use case this is the question the feature exists to answer. It
deserves more than a boolean, and it deserves to be honest about what it knows.

#### Reaching the end is not the same as watching it

Three different measurements get conflated by every naive implementation, and
they disagree in exactly the cases that matter:

| Measure | What it says | How it is fooled |
|---|---|---|
| **Furthest point** | the highest timestamp reached | drag the scrubber to the end: 100% in one second |
| **Coverage** | how much *distinct* content was actually played | skipping the middle shows as a gap, correctly |
| **Watch time** | seconds of playback | a paused-but-open tab, a 2× playback rate, a loop |

**Coverage is the headline number**, because "did they watch the whole thing" is a
question about the *content*, not the playhead. A recipient who scrubs to the end
of a three-minute intro has 100% furthest-point and ~2% coverage, and reporting
that as *"watched"* would make the whole surface untrustworthy. Furthest point is
kept alongside it, because "they got to the end, skipping most of it" and "they
watched the first half carefully and stopped" are different sales signals and the
creator should see both.

#### How coverage is measured

The browser already computes this. `HTMLMediaElement.played` is a `TimeRanges` of
every interval that has been played in this element — union-maintained by the
browser, correct across seeks, and free. The player reads it rather than
integrating `timeupdate` events itself, which is both simpler and more accurate.

Those ranges are quantised into a **fixed coverage bitmap of
`min(round(duration_s), 1000)` buckets**, one bit per bucket:

- **Fixed width bounds the storage and the payload** — a 3-minute clip gets
  ~180 one-second buckets (23 bytes); a 90-minute recording gets 1000 buckets of
  5.4 s each (125 bytes). Precision degrades gracefully with length, and the
  short clips this is built for get second-level detail.
- **A bitmap makes merging trivial and idempotent.** Postgres `BIT VARYING`
  supports `|` natively, so merging a session into the audience rollup is one
  `UPDATE ... SET coverage = coverage | $1`. Union is commutative and
  idempotent, which is what makes the beacon safe to lose *and* safe to
  duplicate.
- **`coverage_pct` is popcount / width**, with the trailing bucket discounted so
  a video whose last 200 ms never plays (which is normal — `ended` fires early on
  some encodes) still reaches 100%.

#### The beacon

`POST /media/v1/{link_uid}/playback`, on the media origin, requiring the session's
viewer token.

**It posts the session's full cumulative state, never a delta.** This is the
single most important property of the design: a lost beacon costs nothing,
because the next one carries everything; a duplicated beacon is a no-op, because
the merge is a union. A delta protocol would drift, and the drift would be
invisible and always in the flattering direction.

```json
{ "session": "…", "duration_ms": 184000, "buckets": 184,
  "coverage": "<base64 bitmap>", "furthest_ms": 184000,
  "plays": 1, "rate_max": 1.0, "muted_ms": 0, "fullscreen": false,
  "ended": true }
```

Sent on: first play; every 30 s of playback (not of wall time — a paused tab
sends nothing); on pause; on `ended`; and on `visibilitychange` / `pagehide` via
**`navigator.sendBeacon`**, which is the only mechanism that reliably survives a
closing tab. Payload capped; the server recomputes `coverage_pct` itself and
never trusts a client-supplied percentage.

Storage, one row per session:

```sql
CREATE TABLE IF NOT EXISTS "<tenant>".share_media_playback (
    session_uid    UUID PRIMARY KEY REFERENCES "<tenant>".share_redemptions(redemption_uid),
    link_uid       UUID        NOT NULL,
    audience_uid   UUID,                    -- NULL for an open link with no identity
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_beacon_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    buckets        SMALLINT    NOT NULL,
    coverage       BIT VARYING NOT NULL,
    furthest_ms    BIGINT      NOT NULL DEFAULT 0,
    watch_ms       BIGINT      NOT NULL DEFAULT 0,
    plays          INTEGER     NOT NULL DEFAULT 0,
    rate_max       REAL        NOT NULL DEFAULT 1.0,
    ended          BOOLEAN     NOT NULL DEFAULT false,
    bytes_served   BIGINT      NOT NULL DEFAULT 0,   -- from the meter (§6.9), not the client
    quality        TEXT,                             -- 'hd' | 'sd'
    device_class   TEXT                              -- 'desktop' | 'mobile' | 'tablet'
);
```

`device_class` is coarse on purpose. A user-agent string stored per viewer, on a
link sent to one named person, is fingerprinting; three buckets answer *"they
watched it on their phone"* without collecting anything that identifies a device.

#### Completion, and the server-side floor that keeps it honest

A view is **complete** when `coverage_pct ≥ share.playback_complete_pct`
(default 95). But the beacon is client-reported and therefore forgeable — anyone
who can open the network tab can claim 100%. So completion carries a **basis**,
and the server has one signal the client cannot inflate away:

**Bytes served is a floor the client cannot fake downward.** If the meter (§6.9)
recorded 4 MB delivered for a 40 MB rendition, no beacon claiming full coverage
is true — the bytes were never sent. That gives three bases, and the UI shows
which one applies:

| `completion_basis` | Meaning |
|---|---|
| `beacon+bytes` | the beacon says complete **and** enough bytes were served to be consistent with it — the strong case, and the normal one |
| `beacon` | the beacon says complete but byte accounting is unavailable or ambiguous (a cached replay, a partial-range pattern) |
| `bytes-floor` | **no usable beacon** — blocked, or an old client — but ≥ `playback_bytes_complete_pct` (default 90) of the rendition was delivered in this session. Reported as *"probably watched"*, never as confirmed |

The converse never holds: serving 100% of the bytes does not mean anyone watched,
because a browser buffers ahead. Bytes bound the claim **from below** only, and
the document should not be read as saying otherwise.

#### What this cannot tell you

Stated here so it can be stated in the UI, which is where it matters:

- **A blocked beacon looks like an unwatched video.** Privacy extensions and
  strict tracking-protection modes block exactly this kind of request. The
  `bytes-floor` basis exists to partly cover it, and *"no playback data"* must
  never render as *"0% watched"*.
- **Playing is not watching.** A muted autoplay in a background tab, a tab left
  open over lunch — the beacon's 30 s tick is tied to playback progress rather
  than wall time, and `rate_max` is recorded so a 2× skim is visible, but neither
  makes this attention-measurement.
- **A poster fetch is not a play** (§9.4), and never enters this table.
- **An open link's numbers are per client token, not per person.** Two devices
  are two rows; a cleared browser is a new viewer.

#### Consent and control

Per-person playback measurement is behavioural tracking, and this document should
not pretend otherwise just because the sender considers it ordinary.

- `share.playback_tracking` (default `true`) disables collection entirely,
  deployment-wide. With it off, the player sends no beacon, the table stays
  empty, and the UI says *"playback tracking is disabled on this deployment"*
  rather than showing empty charts.
- The landing page and the embed carry a short, honest line — *"the sender can
  see whether and how much of this you watch"* — and the wording is recorded in
  `consent_text_id` (§7.1) alongside the address, so the record says what the
  person was actually told.
- Playback rows are PII-adjacent and fall under OSL §5.5's retention window and
  §8.4's erasure path, with the rest of the audience data.

#### What the creator sees

- **Per recipient:** a small **coverage bar** — the video's timeline with watched
  segments filled, gaps visible, rewatched segments darker — plus
  *"watched 94% · reached the end · 2 plays · Tue 14:02 · mobile"*. For a
  one-to-one intro video that single row is the product.
- **Drop-off point:** the first bucket after the last watched one, phrased in
  time — *"stopped at 1:47"*. For an intro video this is the most actionable
  number in the feature, and it is free once coverage exists.
- **Across viewers** (open links): the classic **retention curve** — percentage
  of viewers still watching at each bucket — which is a column-wise popcount over
  the session bitmaps and needs no additional collection.
- **An attention item on completion** (`share.attention_events` gains
  `media_completed`): *"Priya finished your introduction video"*. For the primary
  use case this is the most useful notification the platform can send, and it is
  off by default for `open` links, where it would be noise.

---

## 8. The CSV sidecar

*"The gathered email addresses need to be added to a CSV file in the file's
sidecar. This way fetching the sidecar file is a simple integration."*

### 8.1 Where it lives and what it is called

A hidden child of the **source file** (the same place renditions live —
`parent_uid = resource_uid`), named:

```
audience-<link_uid>.csv        mime text/csv
```

One file per link, not one per file: two links on the same video (an open one on
the website, a gated one for the client) are two audiences, and merging them
loses the distinction that matters most.

**But §2.1's primary use case mints a link per recipient**, so one intro video
may carry two hundred links — and a naive reading of the rule above would hang
two hundred hidden CSV children off one file. That is unusable for the
integration this exists to serve (*"fetch the sidecar"* becomes *"list, parse and
merge two hundred files"*), and it is unpleasant in the drawer.

So there are **two sidecars, and the rule is a threshold**:

| File | When | Contents |
|---|---|---|
| `audience-<link_uid>.csv` | a link with its own distinct audience | that link's rows |
| `audience.csv` (the **rollup**) | always, when the file has any media link | every link's rows, with `link_uid`, `link_note` and `mode` as columns |

- The rollup is the one an integration should read, and it is what the Share tab
  offers as *Export*. It is regenerated by the same debounced projection (§8.2),
  from the same query, with the link filter removed.
- Per-link files are written only while a file has **fewer than
  `share.audience_csv_per_link_max` (default 10)** live media links. Past that,
  the rollup alone is written and existing per-link files are removed on the next
  projection — the threshold is not a mode the user chooses, and crossing it must
  not leave a half-populated set of stale files behind.
- The rollup's `link_uid` column is what preserves the distinction the per-link
  split was protecting: an audience can still be separated per link by a consumer
  that cares, and the two-hundred-recipient case gets one file.

**It must never be mistaken for a rendition.** `parse_rendition_name` splits on
the last `-` and requires the trailing token to be in `_KNOWN_FMTS`; here the
trailing token is a UUID, so it returns `None` and `prune_old_versions` leaves it
alone (`renditions.py:53–67`, `:119–142`). That is correct today **by
coincidence** — it holds only as long as no `fmt` is ever named after a UUID.
Make it explicit: a test asserts `parse_rendition_name("audience-<uuid>.csv") is
None`, and the name pattern is documented in `renditions.py` next to
`_KNOWN_FMTS` as a reserved sibling namespace.

Being a hidden child buys the two properties that make this a good idea rather
than a hack: it **inherits the source file's ACL**, so exactly the people who can
see the video can see who watched it and nobody else; and it is **cascade-deleted
with the source**, so the PII does not outlive the thing it is about.

### 8.2 It is a projection, never an append

The obvious implementation — read the CSV, append a line, write it back — is
wrong in three ways: it is a lost-update race between concurrent viewers, it
creates a new **version** of the file per viewer (a thousand-version file for a
thousand-viewer link), and it makes the CSV the system of record for data whose
system of record is Postgres.

**Decision:** the CSV is **regenerated in full from `share_link_audience`** by a
debouncer:

- at most once per `share.audience_csv_interval_seconds` (default `300`) per
  link, coalescing everything that happened in between;
- immediately on revoke, on expiry, and on demand
  (`POST /share/v1/links/{link_uid}/audience/flush`);
- written with `touch` + `put_stream` as **the link's creator**, so the file is
  owned by a real principal and the write is an ordinary delegated call;
- skipped entirely when nothing changed (compare a content hash), so an idle link
  does not accumulate identical versions.

A creator who needs it *now* has the flush button and the UI export; everyone
else gets a file that is at most five minutes stale, which is what an integration
polling a file can actually use.

### 8.3 Format

RFC 4180, UTF-8 with a BOM (Excel opens it correctly; every other tool ignores
it), CRLF, header row, one row per audience member, newest last:

```csv
email,verified,on_allowlist,first_seen_utc,last_seen_utc,sessions,plays,bytes_served,coverage_pct,furthest_pct,completed,completed_utc,completion_basis,dropoff_seconds,device_class,referer_host,link_uid,link_note,mode
```

`coverage_pct` / `completed` / `completion_basis` are the playback answer (§7.4)
in the form an integration can act on — a CRM that wants *"followed up with
everyone who finished the intro"* needs the basis column as much as the flag,
because `bytes-floor` is a weaker claim than `beacon+bytes` and a CSV row should
not flatten the two.

- `open`-mode links still get a sidecar, with `email` empty — the counts and
  referrers are the useful part, and an integration that always finds the file
  is simpler than one that must handle its absence.
- `verified` is the column an integration must branch on. Any consumer treating
  the address list as a verified mailing list is making a claim the data does not
  support, and the column is the only place the file can say so.
- Every field is escaped per RFC 4180 **and** guarded against formula injection
  (§7.3).

### 8.4 Retention and erasure

- The sidecar dies with the source file (cascade) and with the link, when link
  rows are purged at `share.retention_days` — the purge must **delete the
  sidecar too**, or the PII outlives the record that explains it. That deletion
  is part of the fail-closed purge OSL §5.5 describes.
- An erasure request naming an email address must clear the matching
  `share_link_audience` rows **and** trigger a regeneration of every affected
  sidecar. This is a new obligation on `share_service` — it has no erasure
  consumer today, while CSAI does (`ingest.py:147`) — and it should reuse that
  shape rather than invent one.

---

## 9. The embeddable Web Component

### 9.1 Where it ships

In `commercial_embedding` (MIT, zero-dependency, plain ESM) as
`@fileengine/embed-components/media-share` — **but it is the first component in
that kit that does not use `<fe-session>` and must not require one.** It takes a
share URL and nothing else:

```html
<script type="module"
        src="https://acme-media.example.com/embed/v1/fe-media-share.js"></script>

<fe-media-share src="https://acme-media.example.com/s/3f2a…b91c"
                poster="auto" autoplay="false"
                width="720"></fe-media-share>
```

It is also served **directly from the media origin** at a stable path, because a
host site that cannot add an npm dependency is the common case, and the script
and the media then share an origin and a cache.

A plain `<iframe>` snippet is offered alongside it, and an **oEmbed endpoint**
(`/media/v1/oembed?url=…`) so WordPress, Notion and the rest embed by pasting the
URL. That endpoint is a few dozen lines and removes the single largest category
of "can I put this on our site" support requests.

### 9.2 Two renderings, chosen by the link's mode

*(Q2, resolved 2026-09-26.)* The component asks the peek endpoint (§6.8) what the
link requires, **before rendering anything**, and takes one of two shapes:

| `requires` | Rendering |
|---|---|
| `email` or `code` (`claimed` / `verified`) | a cross-origin **`<iframe>`** to the media origin's player page |
| `none` (`open`) | an in-page **`<video>`** inside the component's shadow root, sources straight from §6.8 |

**Why gated links must be framed.** In `claimed` mode the visitor types an
address into a form. If that form lives in the host page's DOM, **the host page
can read it** — and, worse, forge it: any site embedding the player could post
arbitrary addresses to the claim endpoint and produce a roster of people who
never visited. Inside a cross-origin iframe the host page cannot touch the field,
cannot read the viewer token, and cannot see what was typed. The gate is on our
origin, under our branding, subject to our CSP.

**Why open links need not be.** An open link has no gate, no viewer token worth
protecting, and no identity to collect — the only secret in play is the URL
itself, which the host page already has because it wrote it into the markup.
Framing would buy nothing and cost the thing integrators most want: a player they
can style to match their site.

Three consequences that are not optional:

1. **The mode decides, not an attribute.** There is no `mode="inline"` a host can
   set. The component reads `requires` from *our* endpoint and a gated link is
   framed whatever the host page would prefer. Otherwise the security property is
   an integrator's setting, which is to say not a property.
2. **The peek must happen before the first render**, so a link whose mode changes
   between page loads cannot be caught mid-flight in the wrong shape. Since
   `access_mode` is immutable (§5, rule 1) the only real case is a link replaced by
   another at the same slot, but the ordering costs nothing and removes the
   question.
3. **The in-page path still never sees a credential.** Open-mode `/session`
   returns a token bound to a random client id; the component keeps it in a
   closure inside the shadow root, never in `localStorage`, and never in an
   attribute or a `dataset` field the host can read. It is worth nothing to an
   attacker, and it still does not go anywhere the host can reach.

The cost is two code paths in one component. That is real, and it is smaller than
it looks: both render the same controls over the same source list, and only the
gated path needs `postMessage` plumbing at all.

**Attributes:** `src` (required), `poster`, `autoplay`, `muted`, `loop`,
`width`/`height`/`aspect`, `theme` (`light`|`dark`|`auto`), `lang`.
**Events:** `fe:media-ready`, `fe:media-play`, `fe:media-ended`,
`fe:media-gate` (the gate was shown), `fe:media-identified` (an address was
accepted — **the address itself is not in the event detail**), `fe:media-error`.
Host→frame messages are `postMessage` with an explicit target origin, and the
frame validates `event.origin` on the way back; no `*` in either direction.

### 9.3 Embed allowlisting

A media link carries `allowed_embed_origins TEXT[]`:

- **empty** — no embedding: `frame-ancestors 'none'`, direct link only.
- **listed** — `frame-ancestors https://customer.example https://www.customer.example`,
  and `Access-Control-Allow-Origin` echoed for exactly those.
- **`['*']`** — `frame-ancestors *`, only permitted when `access_mode = 'open'`
  and only with the same `confirm_public` acknowledgement.

`frame-ancestors` is the enforcement; the `Referer` header is recorded for the
roster (§7.1) and is never trusted as a control, because it is absent under
several ordinary referrer policies and forgeable outside a browser.

---

### 9.4 Email — the target that cannot run any of the above

**No email client will run the Web Component, and none will render the iframe.**
Gmail strips `<script>` and `<iframe>` outright; Outlook renders through Word;
Apple Mail is the only mainstream client with meaningful `<video>` support and
even there it is conditional. Since §2.1 names email as the primary destination,
"embed" has to mean something different there, and the thing that actually works
is the thing Loom and every competitor settled on:

> **An image that looks like a video, wrapped in a link.**

So publishing a media link also produces an **email embed artifact**:

```html
<a href="https://acme-media.example.com/s/3f2a…b91c">
  <img src="https://acme-media.example.com/media/v1/3f2a…b91c/emailposter.gif"
       width="560" alt="Watch: Introduction from Dana" style="…">
</a>
```

Three pieces, all served from the media origin:

1. **`emailposter`** — a new CSAI rendition (§4.1's vocabulary, one more entry in
   the three allowlists): a **short animated GIF**, ~3 seconds from early in the
   clip, ≤560 px wide, with a **play-button overlay composited in** and a
   deliberately small frame budget (`CSAI_MEDIA_GIF_SECONDS` 3,
   `CSAI_MEDIA_GIF_FPS` 8, `CSAI_MEDIA_GIF_MAX_BYTES` 2 MiB).
   - **GIF, not WebP or APNG**, purely because Outlook: it shows the *first
     frame* of a GIF and nothing at all for the alternatives. Degrading to a
     still poster with a play button on it is exactly the right failure, and it
     only happens if the first frame carries the overlay — so the overlay is
     composited onto **every** frame, starting with the first.
   - The play-button overlay matters more than it sounds: an image of a video
     *without* one reads as a screenshot and is not clicked.
2. **A copyable HTML snippet** in the Share tab, next to the component and
   iframe snippets, with the inline styles mail clients need (no external CSS)
   and a sensible `alt` for image-blocking clients — which is a large minority,
   and for whom the `alt` text plus the link is the entire experience. The
   snippet therefore also includes a visible text fallback line.
3. **A plain-text form** — a labelled URL — for the plain-text alternative part
   of the message, because a multipart mail without one looks like spam to the
   filters that matter here.

**The open-rate trap, stated plainly.** Fetching that image is a signal, and it
is *not* a view: Gmail proxies and pre-caches images, Apple's Mail Privacy
Protection fetches them unconditionally, and corporate scanners fetch everything.
An `emailposter` fetch therefore means "this message reached a mailbox", not
"a human looked at it", and the roster must **never** count it as a view or fold
it into `play_seconds`. It is recorded, if at all, as a separate and clearly
labelled *"poster fetched"* signal. Presenting proxy fetches as viewer engagement
is the single easiest way for this feature to lie to its user about the thing it
exists to report.

A `?t=<token>` on the poster URL lets a **per-recipient** link's poster fetch be
attributed to that recipient — useful, with the same caveat stamped on it.

---

## 10. Frontend

**Drawer — the file preview.** A file that already has a `media` / `audio`
rendition plays it inline in the existing preview area (the `poster` rendition is
the poster). A file that does not shows the `poster` and the existing 10-second
`preview` behaviour, unchanged.

**There is no *Publish* button here** (§4.3). Publishing is not a thing a user
does to a file; it is what happens because they configured a share. The empty
state points at the Share tab — *"Share this video to make it playable
outside"* — which is one click further away and describes what is actually about
to happen, including that it will take a minute and cost storage. Capability-gated
on §4.6's `media` block, so on a deployment whose FFmpeg cannot do it the pointer
is absent rather than leading somewhere that fails.

**Drawer — Share tab.** For a media-capable file the kind selector gains
*"Let someone watch this"*, and that choice reveals:
- the **access mode** as three clearly-worded radio options, not a dropdown —
  the difference between them is the entire security posture of the link and a
  collapsed control hides it;
- for `open`: the confirmation sentence, the required public display name, the
  embed-origins field, and the **worst-case egress figure** stated in the same
  copyable summary block OSL §10.1 specifies for folder links;
- **before creation**, a plain statement of what pressing it will do when no
  rendition exists yet: *"Creating this link will prepare a web-playable copy
  (about 2 minutes, ~40 MB)"* — the trigger is explicit rather than a side
  effect the user discovers from a progress bar;
- **after creation**, encode state and progress with an ETA — the link is
  mintable, copyable and sendable while this runs (§6.2);
- **Embed** next to **Copy link**: the `<fe-media-share>` snippet, the iframe
  snippet, and the oEmbed URL, each one-click copyable.

**Status & history** (OSL §10.2) gains the audience surface from §7.2, the
*"a newer version of this file has been published"* state from §6.2, and an
egress meter — bytes served against `max_bytes`, plus the §6.9 rung with its
*publish elsewhere* guidance when it is above zero.

**Watch tracking leads, because for §2.1 it is the answer to the only question
being asked.** A one-to-one intro link's primary state is the §7.4 coverage bar
and the line beneath it — *"watched 94% · reached the end · 2 plays · Tue 14:02 ·
mobile"* — not a byte count. The roster orders by last seen; a link nobody has
opened says so plainly; and a viewer who stopped shows **where** they stopped.

Four labels have to stay honest, and each is easy to get wrong in the flattering
direction:

- **"Reached the end" ≠ "watched it"** (§7.4). Both are shown; the coverage
  number is the headline and the furthest-point number is secondary, never the
  reverse.
- **Completion shows its basis.** `beacon+bytes` renders as *"finished"*;
  `bytes-floor` renders as *"probably finished"* with a tooltip saying why. A
  single confident badge over both would be the feature's first lie.
- **No playback data is not 0%.** A blocked beacon renders as *"no playback
  data"* — visibly distinct from a viewer who opened it and watched nothing.
- **"Poster fetched" is not a view** (§9.4), and **"Delivered" is not offered at
  all**, because nothing here sends the mail.

For an `open` link the same data becomes the **retention curve** (§7.4) rather
than a per-person roster.

**Embed snippets** (§9.3, §9.4) are three copyable blocks, not one: the Web
Component, the `<iframe>`, and the **email HTML** with its plain-text fallback.
The email block is first for a media link, since §2.1 says that is where most of
them are going.

**The player** — shared by the landing page, the drawer and the embed's in-page
path, so it is written once. It consumes §6.8's ordered `sources` array and takes
the first entry its browser reports it can play. For video it offers
*Auto / 720p / 480p* (present only when both encodes exist); `Auto` picks SD when
`navigator.connection.effectiveType` reports a slow link and HD otherwise,
remembers the viewer's explicit choice for the session, and **never switches
mid-playback** (§4.5). For audio the choice is invisible — Opus where supported,
MP3 where not.

It is also written with an empty **`<track>` slot and a caption menu that hides
itself when there are no tracks** (§16.5). That costs nothing now and is the
difference between a day and a week if transcripts are ever added.

**Recipient landing page** (`ShareLandingView.vue`) gains the media branch:
poster + player, the gate appropriate to the mode, a *preparing* state with
progress, and — deliberately — a **Download** button unless the creator disabled
it (`allow_download`, default true for `verified`, false for `open`). The
download hands over the **MP3** for audio and the **720p WebM** for video: the
most compatible artifact, not whichever source the player happened to pick. Hiding it
on an open link is not protection, it is a default: a public marketing video does
not need to advertise a direct file URL, while a client receiving a deliverable
usually does want the file.

---

## 11. Configuration

**`convert_search_ai` (`CSAI_*`)**

| Key | Default | Meaning |
|---|---|---|
| `CSAI_MEDIA_ENABLED` | `true` | Publish operations exist at all. |
| `CSAI_MEDIA_ORPHAN_DAYS` | `30` | Grace before a published rendition with no live link is reaped (§4.3.1). |
| `CSAI_MEDIA_WORKERS` | `1` | Concurrent transcodes per media-worker process. |
| `CSAI_MEDIA_JOB_TIMEOUT_SECONDS` | `21600` | Hard ceiling on one encode. |
| `CSAI_MEDIA_STALE_SECONDS` | `300` | Heartbeat age before a `running` job is requeued. |
| `CSAI_MEDIA_MAX_ATTEMPTS` | `3` | Requeues before a job is failed for good. |
| `CSAI_MEDIA_MAX_INPUT_BYTES` | `0` | Refuse sources above this (0 = no limit). |
| `CSAI_MEDIA_MAX_OUTPUT_BYTES` | `0` | Abandon an encode whose output exceeds this. |
| `CSAI_MEDIA_VIDEO_HEIGHT` | `720` | Long-edge cap for the `media` profile. |
| `CSAI_MEDIA_VIDEO_CRF` | `31` | VP9 constant quality at 720p. |
| `CSAI_MEDIA_SD_ENABLED` | `true` | Also produce the 480p `media-sd` rendition (§4.5). |
| `CSAI_MEDIA_SD_HEIGHT` | `480` | Long-edge cap for `media-sd`. |
| `CSAI_MEDIA_SD_CRF` | `33` | VP9 constant quality at 480p. |
| `CSAI_MEDIA_AUDIO_BITRATE` | `128k` | Opus bitrate in the video mux. |
| `CSAI_MEDIA_MP3_QUALITY` | `0` | LAME `-q:a` (0 = V0). |
| `CSAI_MEDIA_OPUS_ENABLED` | `true` | Also produce the `audio-opus` rendition (§4.5). |
| `CSAI_MEDIA_OPUS_BITRATE` | `96k` | Opus bitrate for the standalone audio rendition. |
| `CSAI_MEDIA_GIF_ENABLED` | `true` | Produce the `emailposter` GIF (§9.4). |
| `CSAI_MEDIA_GIF_SECONDS` | `3` | Animated poster duration. |
| `CSAI_MEDIA_GIF_FPS` | `8` | Animated poster frame rate. |
| `CSAI_MEDIA_GIF_WIDTH` | `560` | Animated poster width — the usual email body width. |
| `CSAI_MEDIA_GIF_MAX_BYTES` | `2 MiB` | Refuse to attach a poster larger than this. |
| `CSAI_MEDIA_FFMPEG_THREADS` | `0` | `-threads`; 0 = FFmpeg's choice. |

**`share_service` (`share.*` / `SHARE_*`)**

| Key | Default | Meaning |
|---|---|---|
| `share.media_enabled` | `false` | `kind = 3` exists. Off by default, like every new door. |
| `share.allow_open_mode` | `false` | Whether `access_mode = 'open'` may be chosen at all. |
| `share.open_ldap_group` | `share_public` | The group that may mint an open link (§5). |
| `share.media_max_bytes` | `4 GiB` | Largest rendition a link may be minted over. |
| `share.media_session_ttl_seconds` | `86400` | A viewing session (§6.4). |
| `share.media_recheck_seconds` | `300` | Authority re-check interval within a session (§6.7). |
| `share.media_default_max_bytes` | `50 GiB` | Default egress budget on a new media link. |
| `share.media_max_egress_per_hour` | `5 GiB` | Per-link burst ceiling. |
| `share.media_cache_dir` | `/var/cache/share/media` | Where §6.6's cache lives. |
| `share.media_cache_max_bytes` | `50 GiB` | LRU ceiling. |
| `share.claim_rate` | `10 / hour / IP / link` | Rate limit on `/claim` (§7.3). |
| `share.audience_csv_interval_seconds` | `300` | Sidecar regeneration debounce (§8.2). |
| `share.audience_csv_enabled` | `true` | Write the sidecar at all. |
| `share.audience_csv_per_link_max` | `10` | Live media links on one file above which only the rollup is written (§8.1). |
| `share.media_warm_on_mint` | `true` | Pre-fill the byte cache when a link is created (§6.9). |
| `share.media_min_throughput_bps` | `8192` | Sustained floor, after a 60 s grace, before a stream is dropped (§6.9). |
| `share.media_max_concurrent_streams` | `64` | Per service instance (§6.9). |
| `share.media_max_concurrent_per_link` | `16` | Simultaneous streams for one link (§6.9). |
| `share.media_max_concurrent_per_client` | `3` | Per viewer token / IP pair (§6.9). |
| `share.media_cache_fills_per_minute` | `30` | Cache-miss-amplification bound, per instance (§6.9). |
| `share.media_cache_fill_bytes_per_minute` | `2 GiB` | The same bound, by size. |
| `share.media_tenant_egress_per_hour` | `20 GiB` | Tenant-scope burst window (§6.9). |
| `share.media_tenant_egress_per_day` | `200 GiB` | Tenant-scope sustained window (§6.9). |
| `share.media_soft_ceiling_ratio` | `0.8` | Rung-1 *advise* threshold (§6.9). |
| `share.media_park_after_minutes` | `30` | Sustained rung 2 before a link parks (§6.9). |
| `share.media_throttled_bps` | `0` | Per-session cap at rung 2; `0` = 429 instead of throttling. |
| `share.media_meter_flush_bytes` | `16 MiB` | Byte-accounting flush interval (§6.9). |
| `share.media_meter_flush_seconds` | `10` | The same, by time. |
| `share.playback_tracking` | `true` | Collect playback telemetry at all (§7.4). |
| `share.playback_complete_pct` | `95` | Coverage at which a view counts as complete. |
| `share.playback_bytes_complete_pct` | `90` | Bytes-served share qualifying for the `bytes-floor` basis. |
| `share.playback_beacon_seconds` | `30` | Beacon interval, measured in playback progress, not wall time. |
| `share.playback_max_buckets` | `1000` | Coverage bitmap width ceiling. |
| `SHARE_MEDIA_BASE_URL` | `https://{tenant}-media.<base>` | The media origin (§6.5). |

---

## 12. Audit events

New codes in `audit_service`, all following OSL §12's fail-closed rule:

| Code | When | Notes |
|---|---|---|
| `media_published` | a transcode succeeds | actor is the requester; detail carries profile, durations, sizes |
| `media_publish_failed` | a job ends `failed` | detail carries the user-safe reason |
| `media_unpublished` | a rendition is removed | |
| `share_media_session` | a viewing session opens | **one per session, never per range request** — a per-request event would flood the hash chain and cost more than the bytes |
| `share_media_claim` | an address is claimed | actor `share:<link_uid>\|claimed:<email>` |
| `share_media_open_view` | an open-mode session opens | actor `share:<link_uid>\|anon:<client_id>` |
| `share_audience_exported` | the sidecar is written or exported | this is a PII disclosure and belongs in the chain |
| `share_media_throttled` | a link reaches rung 2 (§6.9) | carries the window that tripped and its measured value |
| `share_media_parked` | a link reaches rung 3 | the adjudicated overload signal — **this is the one to alert on**, as OSL §14-M9 alerts on `share_link_locked` |
| `share_media_meter_degraded` | the rolling-window store became unavailable | so the gap in metering is itself in the record |
| `share_media_completed` | a view reaches `playback_complete_pct` (§7.4) | carries `completion_basis`; the event that drives the *"Priya finished your video"* attention item |

### 12.1 The rule this door needs stated out loud

**Audit the decision, not the request.** Every other door in this platform sees
roughly one request per user action. This one sees ten to fifty — ranged GETs,
seeks, re-buffers — and a hash-chained log is the wrong place for all of them.

That is why `share_media_session` is once per session. The rule is written here
rather than left as a pattern because the natural instinct, when something needs
measuring, is to emit an event per occurrence, and on this door that produces a
chain nobody can read and a cost exceeding the bytes served.

**Two events are deliberately NOT emitted**, and the reasoning belongs in the
record so it is not "fixed" later:

- **The poster fetch** (§9.4). Mail-privacy proxies fetch images
  unconditionally, so a campaign to two hundred recipients would write hundreds
  of audit entries describing something that is not even a view. It is metered;
  it is not audited.
- **The playback beacon** (§7.4). Once per 30 s of playback, per viewer. The
  meaningful transition — completion — has its own event above.

### 12.2 Additions the feature needs

Six, each closing a question the current set cannot answer.

| Code | When | Why it is needed |
|---|---|---|
| `share_media_session_end` | a viewing session closes | **The important one.** `share_media_session` records that a session opened; nothing records what it *did*. Carries `bytes`, duration, and how it ended — `completed` \| `abandoned` \| `throttled` \| `expired`. Without it the ledger cannot answer how much egress a link caused, which is the number billing is made of and the number cross-tenant overload detection needs. One per session, so it is bounded by viewers rather than by seeks. |
| `share_media_cache_fill` | the byte cache fetches a rendition from the core | **A read of the file that currently leaves no trace.** The fill is a delegated `StreamFileDownload` as the creator — the audit log's job is "everything that touched this file", and a whole-file read that appears nowhere in its history is a gap. Bounded: once per rendition, not per request. It is also §6.9's leading indicator, so having it in the ledger rather than only in metrics makes it queryable after the fact. |
| `share_media_new_referrer` | a link is played from a referring host not seen before | Bounded by *distinct* sites, not by requests, and it is what makes §4.4a's referrer-concentration detection possible. One embedding site driving many links looks ordinary per link. |
| `media_rendition_reaped` | the orphan reaper removes a published rendition (§4.3.1) | Content deletion. Not optional — the platform audits destruction, and a reaper that quietly removes gigabytes is exactly the kind of background process that should not be silent. |
| `media_publish_abandoned` | a job ends after exhausting `CSAI_MEDIA_MAX_ATTEMPTS` | `media_publish_failed` does not distinguish "failed once" from "failed, requeued three times, gave up". For a feature whose cost is CPU, the retries are the expensive part, so the terminal event carries the attempt count. |
| `share_media_mode_denied` | an `open` link is refused for want of `share_public`, `confirm_public`, or `allow_open_mode` | Denials are audited generally; a specific code makes "who keeps trying to publish to the open internet" a rule rather than a log search. |

`share_media_session_end` deserves one more note: **its `bytes` is the same
field the billing work needs** on the envelope, not a media-specific addition.
Metering egress from the ledger rather than from metrics requires it anyway, and
putting it on session close gives both at once — durable, attributable, and
bounded by viewers.

**The actor string must distinguish the three modes.** OSL §4.3 defines
`share:<link_uid>|<verified_email>` and builds its whole accountability argument
on that address being *proven*. Writing a claimed address in the same slot would
quietly degrade every historical record's meaning. The `claimed:` and `anon:`
prefixes are not cosmetic — a query that asks *"who verifiably accessed this"*
must be able to exclude them.

Per the platform's standing rule on audit PII: an email address a person supplied
to identify themselves is a principal identifier and is retained; the **file
name** is not, and media events reference the file by uid.

---

## 13. Security review points

Collected for the review this feature needs before `share.media_enabled` is
turned on anywhere — this is R14's door and it warrants the same scrutiny OSL
§14-M4 demanded of the first public one.

1. **The media origin never serves a non-media MIME type**, and never serves an
   authenticated route. Assert both.
2. **The header table in §6.5 holds on every response, including errors**, and is
   set once for the prefix — OSL §8.3's guard, applied to a second prefix.
3. **`access_mode` cannot change after creation**, and `claimed`/`open` are
   rejected for kinds 0–2 by a database constraint.
4. **The rendition uid comes only from the link record.** OSL §7.3's containment
   rule; the media path resolves a *child* of the resource, which is one more hop
   for a caller-supplied uid to slip into.
5. **The cache never short-circuits the authority re-check** (§6.6, §6.7), and a
   revoked link stops serving within `media_recheck_seconds` — tested with the
   clock advanced, not by inspection.
6. **`/claim` cannot be used to write arbitrary text into a tenant file** —
   validation, length cap, formula-injection guard, rate limit (§7.3).
7. **Budget accounting cannot be evaded by ranged requests**, and `max_bytes` is
   enforced on bytes actually written (§6.4).
8. **A gated link is always framed, and the host cannot opt out** (§9.2) — the
   component's shape follows `requires` from our own endpoint, and an in-page
   render of a `claimed` or `verified` link is a failing test, not a preference.
   For the framed path, `postMessage` origin is validated in both directions; for
   the in-page path, the viewer token never reaches `localStorage`, an attribute,
   or `dataset`.
9. **An open link is not silently created**: `share.allow_open_mode`, the
   `share_public` group, and `confirm_public` are three independent gates and all
   three are tested.
10. **The sidecar's ACL is the source file's**, and it is destroyed with the link
    and with the file (§8.4).
11. **A cache fill cannot be caused by an unauthorized caller** (§6.9) — the
    order is authorize, fill, serve, and a test drives the unauthorized path
    against a cold cache and asserts the core was never called.
12. **Metering counters are shared, not per-process** (§6.9) — the trap OSL names
    twice. Tested with two service instances against one Redis.
13. **Losing Redis degrades metering but not authorization or durable budgets**
    (§6.9), and raises `fileengine_media_meter_degraded`. This is the document's
    one deliberate departure from fail-closed and it is tested as such.
14. **Rung 3's explanatory response is scoped to `open` links only** (§6.9);
    a gated link still fails uniformly per OSL §8.5.
15. **The playback beacon cannot write another session's row** (§7.4) — it is
    authorized by the viewer token and keyed on that session alone; a
    caller-supplied `session` that does not match the token is refused, not
    merged.
16. **A client-supplied percentage is never stored** — the server recomputes
    `coverage_pct` from the bitmap, and a beacon claiming completion that the
    byte meter contradicts is recorded with the weaker basis, not the flattering
    one (§7.4).
17. **`share.playback_tracking = false` collects nothing** — no beacon route, no
    rows, and the UI says so rather than rendering empty charts.

---

## 14. Open questions

### Resolved (2026-09-26)

**Q1 — The media origin: a separate `<tenant>-media.<base>` per tenant.** The
structural answer OSL §8.3 reserved, taken now rather than later (§3-R15). It
costs a wildcard certificate covering `*-media.<base>` and a DNS change per
deployment — an **ops precondition, not an implementation detail**, and the
thing most likely to block MS4 if it is not started early. A single shared
`media.<base>` was rejected: it would put one tenant's published video on the
same origin as another's, which is exactly the isolation the per-tenant origin
model exists to provide.

**Q2 — The embed renders an iframe when the link is gated and an in-page
`<video>` when it is open.** §9.2 rewritten. The deciding input is `requires`
from our own peek endpoint, never a host-settable attribute — the security
property must not be an integrator's preference.

**Q3 — Both MP3 and Opus.** `audio` (LAME V0) and `audio-opus` (96 kb/s in WebM),
player prefers Opus, MP3 serves direct links, podcast consumers, hardware players
and the download button (§4.5).

**Q4 — Two fixed video sizes with a manual picker, not a ladder.** `media` (720p)
and `media-sd` (480p), both whole progressive files, viewer-selected, never
switched mid-playback (§4.5). Segmented ABR (HLS/DASH) remains out of scope and
would be its own project — it changes the rendition model from one file to many.

**Q5 — `preview` is unchanged.** Confirmed: `preview` is the file browser's
teaser, media publishing is outbound automation for clients, prospects and the
website (§2). Separate renditions, separate triggers, separate lifecycles; they
share only FFmpeg and the rendition writer (§4.1).

**Q6 — Publishing requires WRITE** *(settled 2026-09-26)*. A publish writes a
hidden child and consumes the tenant's quota, and the permission that governs
writing to a file is WRITE. Confirmed rather than relaxed.

The alternative considered and rejected was READ + `share_external`, on the
grounds that publishing is a marketing action on someone else's file and the
person sending a clip to a prospect is frequently not the person who owns the
footage. It was rejected because the quota consequence is real — a publish can
add gigabytes to a folder the requester cannot otherwise write to — and because
`share_external` is a statement about *sending things outside*, not about
spending someone else's storage. The two are separable authorities and
collapsing them would be the easier mistake to make.

**The consequence to design around, rather than work around:** a user who may
share a video but not write to it cannot publish it. That is intended, and the
supported answers are to grant WRITE on the folder the marketing clips live in,
or to keep those clips in a folder that user owns. The **`folder_actions`
binding** (§4.8, Q8) is the third and probably best answer at scale — the action
runs as its own service principal, so the folder's configuration carries the
authority and no individual needs WRITE on production media at all.

This is now a **decision**, not a default: §4.6 and MS2 implement WRITE, and
relaxing it later would need the quota argument answered rather than merely
noted.

**Q7 — Eager SD encode** *(settled by §2.1, 2026-09-26)*. The question was
whether doubling transcode cost per video is worth a 480p option nobody may pick.
For a 2–5 minute talking-head clip both encodes are a minute of CPU, and the
recipient of an intro video is often on a phone — which is precisely who the SD
rendition is for. **Eager**, and the lazy variant is not worth keeping as an
option. It would become one again only if long recordings turned out to be a
common input, which §2.1 says they are not.

### Resolved (2026-10-03) — review against the platform as it now stands

The specification was written before the storage-pipeline and tenant-lifecycle
work landed (core and doors 1.9.37, share/ldap_manager 1.9.42). The review
reconciled it with what now exists; these supersede anything above that
disagrees.

**Q8 — The `folder_actions` trigger is out of scope.** Deferred to a later
project. Publishing happens from the Share tab (and the direct API). CSAI still
emits `media.published` / `media.publish_failed`, so adding the plug-in later is
the plug-in and nothing else. MS10 is removed from this feature.

**Q9 — The media origin is a tenant interface, provisioned exactly like
`-drive`.** *(Supersedes the wildcard-certificate precondition in §6.5 and Q1.)*
Production issues one certificate per host by HTTP-01 through the ingress role,
and the deployment console checks each tenant interface's DNS and TLS. So
`<tenant>-media.<base>` is added as a third entry in the console's
`AMC_TENANT_INTERFACES`, gets one A record per tenant, and a per-host
certificate from the existing play — no wildcard, no DNS-01. The per-tenant
isolation Q1 argued for is unchanged.

**Q10 — No `share_service` media cache in v1.** *(Supersedes §6.6, and the
cache-fill / warm-on-mint parts of §6.9.)* §3-R16 kept the cache because the core
could not serve ranges. It now can: `GetFileRequest` carries `offset` /
`length`, the first frame reports `total_size` and `range_method`, and storage
format v2 — written for every new version since core 1.9.37 — serves a range
by an **authenticated seek**. MS4 therefore streams each `Range` from the core.
The cache returns only if measurement shows a need, with `range_method` as its
trigger exactly as R16 describes. Dropping it removes the cache's cull, its
single-flight fill, warm-on-mint, and the whole cache-miss-amplification
surface; §6.9's byte, concurrency and session controls stay.
**Precondition for MS4:** confirm a v2 read reports `range_method = "seek"` —
the post-deploy verification on 2026-10-01 observed `scan` on an 80 MiB v2
file, which must be explained before MS4 relies on seeks.

**Q11 — The media door honours tenant state.** Not in the original design,
because the mechanism did not exist. Every public share route now refuses a
tenant that is not `live` (`share_service` 1.9.42, `public._resolve`); the media
door must resolve through the same gate, with the same uniform failure. §13
gains it as a review point.

**Q12 — New tenant tables go inside the guarded DDL block.** `media_jobs`
(CSAI) and `share_link_audience` / `share_media_playback` (`share_service`) are
created inside each service's existing `pg_advisory_xact_lock` provisioning
section (`convert_search_ai/schema.py`), never as a free-standing
`CREATE TABLE IF NOT EXISTS`: idempotent DDL is not concurrency-safe DDL, and an
unguarded critical section once produced "intermittent" failures in five
services.

**Confirmed by the review, against the code on 2026-10-03:** the `metamodel`
pruning leak (§4.1) is real — absent from `_KNOWN_FMTS`; the pipeline still reads
the whole source into memory (`pipeline.py:135–138`, §4.2a); and video still
converts inline in the ingest worker (`ingest.py:126`, §4.2b).

**Q13 — Two fmt names change: `media_sd` and `audio_opus`** *(found by MS1's
round-trip test, 2026-10-03)*. A rendition is named `<version>-<fmt>.<ext>` and
`parse_rendition_name` splits on the LAST hyphen — the fmt token may never
contain one. `media-sd` would parse as fmt `sd`, so it would never be pruned:
the exact leak §4.1 warns about. The **profiles** keep their names
(`video-480p-vp9`, `audio-opus`); only the rendition fmts above change. Read
`media-sd` / `audio-opus` elsewhere in this document as those fmts.

**The drawer's default stays the 10-second silent `preview`** *(2026-10-03,
refines §10)*. Where a `media` / `media_sd` rendition exists, the preview player
offers the full video as an explicit choice — *Watch full video*, 720p / 480p —
and never switches to it by itself: the preview is for a quick idea of the
video without distracting sound.

**Transcode trigger, restated (2026-10-03):** the full 720p + 480p conversion
of the whole video happens on the request to publish — a media share being
configured — and a wait after creating the link is acceptable. That is §4.3 and
Q7 as written; the link is still mintable immediately and reports *preparing*
until the encode finishes (§6.2).

---

## 15. Implementation stages

1. **MS0 — CSAI: streaming source fetch.** `consumes_path` / `render_from_path`
   on the plugin interface, `get_stream`-to-temp in the pipeline, `VideoPlugin`
   converted to it. **No new behaviour** — the existing poster+preview must be
   byte-identical afterwards. Separable, independently valuable, and it removes
   the `blob.read()` ceiling for the plugins that already stream to FFmpeg.
   **Tests:** a source larger than the process's comfortable memory converts; the
   temp file is removed on every path including a killed child.
2. **MS1 — CSAI: the media renditions.** `media`, `media-sd`, `audio` and
   `audio-opus` in all three allowlists, `AudioPlugin`, the encoder ladder,
   `-c copy` passthrough for a conformant source, the portrait-aware scale
   filter, and the skip rules (no SD for an already-small source, no Opus
   without `libopus`). **Fix the `metamodel` pruning leak and add the round-trip
   test for every emitted fmt** (§4.1), and the `emailposter` GIF with its
   composited play button (§9.4).
   **Tests:** a portrait source stays portrait and caps at 720 on its long edge;
   the GIF's **first frame carries the overlay** (the Outlook degradation path)
   and the file stays under `CSAI_MEDIA_GIF_MAX_BYTES`;
   a VP9/Opus source is remuxed not re-encoded; the encoder ladder falls back
   when VP9 is absent; the MP3 carries a Xing header and reports a correct
   duration; a 360p source produces `media` and **no** `media-sd`; a build
   without `libopus` produces the MP3 alone and reports success, not failure.
3. **MS2 — CSAI: the job model and worker.** `media_jobs`, the claim/heartbeat/
   requeue cycle with the attempt cap, `-progress` parsing, cancellation, the
   three routes, the capability block, the erasure check at both ends of the job,
   and the §4.3.1 orphan reaper.
   **Tests:** a killed worker's job is requeued exactly `MAX_ATTEMPTS` times and
   then fails; two concurrent requests for the same (file, version, profile)
   yield one job; an erasure landing mid-encode discards the output; **ingest of
   a video produces `poster` + `preview` and starts no job** — the §4.3 rule,
   asserted rather than assumed; the reaper keeps a rendition when the liveness
   call fails, and never touches `poster` or `preview`.
4. **MS3 — `share_service`: kind 3, owner side.** `access_mode` + its CHECK
   constraint, `max_viewers`, `allowed_embed_origins`, `display_name`, creation
   with `pending_media`, the `media-refs` internal route, the audience table.
   No public surface yet. **Tests:** `claimed`/`open` refused for kinds 0–2 at
   the database; `access_mode` immutable; an open link needs all three gates.
5. **MS4 — `share_service`: the media door.** The `<tenant>-media.<base>` origin
   and its nginx block, the header set with the all-responses test, the cache
   with its single-flight fill and LRU cull, byte-accurate Range with a correct
   `Content-Range`, the server-assembled source list and the `?q=` enum, the
   `/claim` route, session/byte accounting, the §6.7 re-check with `acl.changed`
   invalidation, **and the whole of §6.9** — the three scopes and their windows,
   the concurrency and cache-fill caps, warm-on-mint, the four-rung ladder, the
   flushed byte accounting, the Redis-degraded fallback, and the
   `fileengine_media_*` series. **This is the review gate** (§13).
   **Tests:** a seek to 90% of a 1 GB file reads only the requested window from
   the cache; revocation stops playback within the recheck window; budget cannot
   be evaded by ranged requests; an unauthorized request against a cold cache
   never reaches the core; two instances against one Redis share their counters;
   a link crossing each rung produces the specified response and exactly one
   audit event; a slow-but-legitimate reader survives the throughput floor while
   a stalled connection does not; killing Redis degrades metering while
   Postgres budgets still refuse an exhausted link.
6. **MS5 — the audience & the sidecar.** The debounced projection writer, the
   CSV format with the injection guard, flush-on-revoke, the purge and erasure
   paths. **Tests:** a thousand viewers produce one sidecar version per debounce
   window, not a thousand; `parse_rendition_name` refuses the sidecar name; a
   formula-shaped address is neutralized in the file; erasure clears the row and
   rewrites the file.
7. **MS5b — playback telemetry.** The player's `played`-TimeRanges reader and
   the bitmap quantiser, the cumulative (never delta) beacon with `sendBeacon`
   on pagehide, `share_media_playback`, the union rollup onto the audience row,
   the three completion bases, the `media_completed` attention item, and the
   `playback_tracking` off-switch. Small, self-contained, and the piece §2.1
   says the feature is judged on.
   **Tests:** scrubbing to the end yields ~2% coverage and 100% furthest point;
   a lost beacon and a duplicated beacon both produce the same stored coverage
   (the union property, driven directly); a beacon claiming completion with 10%
   of bytes served is recorded as `beacon`, never `beacon+bytes`; a beacon for
   another session's uid is refused; with tracking off there is no route and no
   rows; the retention curve matches a hand-computed column popcount.

8. **MS6 — frontend: owner side.** Inline playback of the published rendition,
   the encode state and progress, the media branch of the Share tab with the
   three access modes and the confirmation, the embed snippets, the audience
   roster and the egress meter.
9. **MS7 — frontend: the landing page and the email embed.** The media branch of
   `ShareLandingView.vue` — poster, player, the three gates, the preparing state,
   the optional download — plus the three copyable embed blocks (§9.4), the
   email HTML first. **Tests:** the email snippet renders with no external CSS
   and degrades to `alt` text plus a visible link when images are blocked; a
   poster fetch is never counted as a view.
10. **MS8 — the embed kit.** `<fe-media-share>` in `commercial_embedding` with
   both renderings (§9.2), the self-hosted script on the media origin, the
   iframe snippet, the oEmbed endpoint, and an entry in the host harness.
   **Tests:** the component works with no `<fe-session>` on the page; a gated
   link renders an iframe **even when the host asks for otherwise**, and an open
   link renders in-page; `postMessage` from a wrong origin is ignored; the
   open-mode viewer token never reaches `localStorage`, an attribute or
   `dataset`; `frame-ancestors` actually refuses a non-allowlisted host.
11. **MS9 — ops & docs.** Audit codes; a rules-engine alert on
    **`share_media_parked`** directly (the adjudicated overload signal) plus a
    burst rule on `share_media_throttled`; a Prometheus alert on
    `fileengine_media_cache_fills_total` **before** request rate, since it leads
    every other symptom (§6.9); the media cache in the backup/exclusion story (it
    is a cache — it must be *excluded*); the wildcard certificate; the help page,
    including the *"when to use YouTube instead"* guidance the rung-1 item links
    to; compose defaults with `share.media_enabled = false`.

12. **MS10 — `folder_actions`: publish on arrival** *(deferred — §14-Q8, 2026-10-03; kept for reference)*. The
    **Publish media** plug-in against the existing `folder_actions.actions`
    entry-point group, plus the `media.published` / `media.publish_failed` events
    CSAI emits for it (§4.8). Severable from everything above and last for that
    reason. **Tests:** a clip dropped in a bound folder is published and linked
    with the folder's access mode, not the uploader's choice; a failed publish
    raises an attention item rather than failing silently; the action declares no
    `auto_moves` and cannot participate in a move loop.

MS0–MS2 land entirely in `convert_search_ai` and are useful on their own — a
published rendition plays in the SPA with no share link anywhere. MS3–MS5 are the
security-bearing work. MS6–MS9 are surface. **`file_engine_core` is not on this
list**, and §3-R16 is the section to re-read if a milestone starts wanting it to
be.


---

## 16. Future development: transcripts, captions and multi-modal understanding

**Not in scope, and documented anyway.** A published video is the one artifact on
this platform whose content is completely opaque to every other feature: it is
not searchable, not citable in RAG chat, not accessible to a deaf viewer, and not
summarisable. A transcript fixes all four at once, and the likely route is a
multi-modal AI integration rather than anything bespoke.

This section exists so that route stays cheap. Nothing here is a commitment; the
point is that **v1 must not make it expensive**, and one thing in the current
plugin contract would (§16.3).

### 16.1 What it unlocks, in order of value

1. **Video becomes searchable and chattable through machinery that already
   exists.** CSAI's whole pipeline is *extract text → Markdown → Postgres FTS +
   `pg_trgm` → heading-aware chunks → pgvector → permission-gated RAG chat*. A
   transcript is just text, so a transcribed video enters that pipeline with **no
   new index, no new retrieval path, and no new permission surface** — it is
   gated by the same `CheckPermission` as every other document, because it *is*
   another document. This is far and away the largest return and it is almost
   entirely free.
2. **Citations that deep-link to the moment.** A chunk carrying a start
   timestamp lets a chat citation resolve to `…/s/<token>#t=107` rather than "in
   this video somewhere". The chunk metadata needed is a start/end pair, and it
   should be carried from the beginning if transcripts are ever built — retrofitting
   timestamps onto already-embedded chunks means re-embedding the corpus.
3. **Captions on the player** — a `captions` WebVTT rendition, `<track>` in the
   player. Accessibility first, but also the reason a muted autoplay works at
   all: most embedded video is watched silently on first contact.
4. **A transcript panel beside the player**, click-to-seek. For a Loom-style
   intro (§2.1) this is the feature recipients actually ask for — *"skim it in
   twenty seconds"* — and it is the same data as (3) rendered differently.
5. **Summary, chapters, and an auto-generated description.** Once the text
   exists, the existing chat providers produce these; none of it needs new
   infrastructure.
6. **Visual understanding** — the genuinely multi-modal part. A screen recording
   is mostly *visual*: slides, a UI being demonstrated, code on screen. Sampled
   frames through a vision model, or OCR over sampled frames, yields text that
   speech alone never contains. For the intro-video use case this matters less;
   for the demo and walkthrough content that will follow it matters a lot.

### 16.2 The hooks that already exist

Nothing below needs inventing — it needs using:

| Need | Existing hook |
|---|---|
| Somewhere for the text to go | `ConversionPlugin.extract() -> Optional[str]`, whose return is already indexed, chunked and embedded |
| A place for the WebVTT | the rendition vocabulary — one more `fmt` (`captions`, ext `vtt`), in the three allowlists of §4.1 |
| A durable, resumable, long-running job | `media_jobs` (§4.4); a `transcript` value in `profile` costs nothing structurally, and ASR has exactly the runtime profile the table was designed for |
| Audio to feed it | the `audio` rendition already exists. ASR wants mono 16 kHz PCM, so an intermediate (`-ac 1 -ar 16000`) is one FFmpeg invocation, not a new pipeline |
| Not holding a 4 GB file in memory | `consumes_path` / `render_from_path` (MS0) — ASR and frame sampling both want a path |
| Choosing a provider | `providers/factory.py`. Embeddings, chat and web search are all pluggable with an **offline, dependency-free default**; a transcription provider should be the fourth family, defaulting to `none` |
| Emitting progress and completion | `media.published` / `media.publish_failed` (§4.8) generalise to `transcript.*` |

**The provider shape to copy** is the one the other three already use:
`CSAI_TRANSCRIBE_PROVIDER` with a `*_BASE_URL` / `*_API_KEY` / `*_MODEL` triple,
so a local `whisper.cpp` / `faster-whisper` and a hosted multi-modal API are the
same code path with a different base URL — exactly as `ollama` and OpenAI already
are. A **local default matters more here than elsewhere** (§16.4).

### 16.3 The one trap v1 must not walk into

`ConversionPlugin.extracts_text()` is **derived, not declared**:

```python
def extracts_text(self) -> bool:
    return type(self).extract is not ConversionPlugin.extract
```

It is `True` for any plugin that overrides `extract`, and the reconcile sweep
uses it to tell *"no text by nature"* (an image, a video) from *"text we failed
to get"* — the comment in `plugins/base.py` says so explicitly, and notes that
without it the sweep would "either re-convert every JPEG forever or keep skipping
the PDF that actually failed".

**So the day `VideoPlugin` gains an `extract` method, every video in every tenant
becomes a file the sweep believes should have text.** Any video not transcribed —
because transcription is off, because no provider is configured, because the
source is silent, because it failed — looks exactly like a failure, and the sweep
retries it forever. On a corpus of raw footage that is an infinite, expensive
loop, and it arrives silently at whatever moment someone adds the method.

Three ways out; the choice is deliberately **not** made here, but the trap is
recorded so it is a decision rather than an incident:

1. **A separate `TranscriptPlugin`** claiming the same MIME types through
   `claims()`, registered behind `VideoPlugin`. Keeps `VideoPlugin.extracts_text()`
   false and the sweep's reasoning intact. Cleanest, and it composes with the
   registry as it stands.
2. **Make `extracts_text()` declarable** — an override that a plugin can answer
   from configuration (*"yes, when a transcription provider is configured"*).
   Truthful, and a small change to a contract the comment in `base.py` argues
   should stay derived.
3. **Record a terminal `no_transcript` status** per `(file, version)` so the
   sweep can distinguish "tried, nothing to get" from "never tried". Needed
   anyway for a silent video, whichever of the above is chosen.

Whichever is picked, **v1 changes nothing here** — `VideoPlugin` has no `extract`
today and must not grow one as a side effect of this document.

### 16.4 Decisions this document deliberately does not take

Recorded as questions so they are not rediscovered as gaps:

- **Where the audio goes.** Sending a customer's video to a third-party
  multi-modal API is a **data egress decision**, and on this platform it is a
  tenant-level one — the same class of decision as the chat provider, and
  arguably sharper, because a recorded meeting carries voices and faces rather
  than a document's text. A local provider should be the default and a remote one
  should be opt-in per tenant, visible in the capability surface, and audited on
  each use. Whether a *per-file* opt-in is also required is open.
- **Whether a transcript is a rendition, a document, or both.** WebVTT is
  presentation (a rendition); the Markdown fed to the index is content. They are
  the same information twice, and keeping both is probably right — but the
  version-pruning and erasure rules then apply to each, and the erasure path
  (§4.7) must reach the transcript text *and* its embeddings, which is the same
  obligation the existing `_honour_erasure` already carries.
- **Who may transcribe, and who pays.** The §14-Q6 argument about WRITE versus
  READ + `share_external` recurs here with more force: transcription costs money
  per minute at a hosted provider, so the gate is also a spending control.
- **Diarisation and speaker labels.** *"Priya said X"* is far more useful than a
  wall of text, and materially more sensitive. Out of scope until someone asks.
- **Whether transcripts are shown to share recipients.** A transcript panel on a
  public landing page is a feature; it is also the full text of the video handed
  to anyone with the link, indexed by search engines if the page is indexable.
  Default should be off for `open` links, and it is a per-link control if it
  exists at all.
- **Languages.** Detection, non-English output, and translated caption tracks are
  each a product decision rather than a technical one.

### 16.5 What v1 does to keep the door open

Concretely, and at no cost to the current scope:

1. **MS0's `consumes_path`** is the prerequisite for any ASR or frame-sampling
   work, and it ships in v1 for its own reasons.
2. **`media_jobs.profile` is a free-text column, not an enum**, so `transcript`
   and `describe-frames` need no migration.
3. **The rendition allowlists are changed in one place per repo** (§4.1's
   three-allowlist rule, with the round-trip test) — so adding `captions` is a
   one-line change with a test that already exists to catch the `metamodel`-style
   omission.
4. **`VideoPlugin` gains no `extract` method** (§16.3).
5. **The player is written with a `<track>` slot from the start** — an empty
   caption menu costs nothing and retrofitting a caption UI into a finished
   player costs a day.
6. **Chunk metadata keeps room for a start/end timestamp.** If transcripts ever
   land, citations should deep-link from the first day rather than after a
   re-embed of the corpus.

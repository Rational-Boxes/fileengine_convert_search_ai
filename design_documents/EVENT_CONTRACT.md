# FileEngine file-activity event contract

Status: **Implemented (publisher)**. The FileEngine **core publisher** is built
and merged (`file_engine_core`, see its
`design_documents/redis_event_queueing_plan.md`); this document is the
authoritative, as-built contract for any **consumer** (`convert_search_ai` is the
first; search, AI extraction, audit, cache invalidation, etc. may follow). The
`convert_search_ai` consumer side is not yet built.

This contract is deliberately **generic and reusable** — it is *not* specific to
`convert_search_ai`. Core publishes file-activity events once; many independent
consumers subscribe.

> **Publisher status:** optional and **off by default** — compiled in with
> `-DFILEENGINE_ENABLE_EVENTS=ON` (needs `hiredis-devel`) and enabled at runtime
> with `FILEENGINE_EVENTS_ENABLED=true`. Verified end-to-end against a dev Redis:
> a full gRPC CRUD run produced file/dir lifecycle, `acl.changed`, and `role.*`
> events; fail-open confirmed.

## 1. Design goals

- **Generic / multi-subscriber.** One published stream of file-activity events;
  N independent consumer groups, each tracking its own position.
- **Transport-agnostic.** The schema and semantics are independent of the broker.
  **Redis Streams** is the development transport (durable, consumer groups,
  at-least-once); Kafka/NATS/SQS are drop-in alternatives behind the same
  `EventSource` consumer interface.
- **At-least-once + idempotent.** Consumers must dedupe; the contract carries the
  keys needed to do so.
- **Authority-aware.** Every event carries `tenant` and the acting `user`, sourced
  from the request's `AuthenticationContext`, so consumers can reason about tenancy
  and provenance.

## 2. Event envelope (JSON)

Base envelope (always present), as emitted by the core publisher:

```json
{
  "event_id":   "uuid-v4",            // unique per emission; primary dedupe key
  "type":       "file.updated",       // see §3
  "tenant":     "default",            // never empty — "default" when unset
  "file_uid":   "uuid",               // the affected entity (empty for role.* events)
  "parent_uid": "uuid",               // current parent (folder, or file uid for a rendition)
  "name":       "report.docx",
  "path":       "/projects/report.docx", // best-effort, advisory only
  "is_folder":  false,
  "is_rendition": false,              // true if the entity is a hidden child rendition
  "version":    "20260623_021500.482", // FileEngine version id (string); "" when N/A
  "size":       482190,               // bytes; 0 for dirs / ACL / role events
  "actor":      "testuser@rationalboxes.com", // user from AuthenticationContext
  "ts":         "20260623_021500.913", // emit time, format YYYYMMDD_HHMMSS.mmm (see note)
  "schema":     1                     // contract version
}
```

Type-specific fields (added only for the noted event types):

```jsonc
// acl.changed
"principal":   "dave",   // the principal whose access changed (user/group/role name)
"permissions": 1024      // permission bits granted/revoked

// role.assigned / role.member_removed / role.deleted
"role":   "editors",     // the role
"member": "carol"        // the affected user ("" for role.deleted — affects all members)
```

Notes on the as-built envelope:
- **`version` is a string** — FileEngine's version timestamp id (e.g.
  `YYYYMMDD_HHMMSS.mmm`), not an integer. It is `""` for events without a content
  version (`file.created`/`moved`/`renamed`/`deleted`, `dir.*`, `acl.*`, `role.*`).
- **`ts`** is the server-clock emit time in `YYYYMMDD_HHMMSS.mmm` form — **not
  RFC3339** (a known deviation from the original design; a later `schema` bump may
  switch to RFC3339). It lexicographically sorts chronologically.
- **`mime` is not emitted** — derive it from a FileEngine `stat`/content sniff
  when needed. (A future additive field may add it.)
- Enrichment (`name`/`parent_uid`/`path`/`size`/`is_folder`/`is_rendition`) is
  **best-effort** via a post-commit DB read; a field may be empty/zero if that
  read fails. The event is still emitted (fail-open).
- Consumers must tolerate unknown additional fields (forward-compatible).

## 3. Event types

| `type` | Emitted when | Notes for consumers |
|--------|--------------|---------------------|
| `file.created`  | a new file row is committed | index/convert the new file |
| `file.updated`  | a new **version** is written (PUT) | re-extract, re-embed, re-render; supersede stale data |
| `file.moved`    | parent changes | no re-index — identity is the stable `file_uid`; update path only |
| `file.renamed`  | name changes | metadata only |
| `file.deleted`  | soft/hard delete | **cascade**: remove the file's derived rows/renditions |
| `file.restored` | undelete | re-instate / re-index |
| `file.erased`   | a file is **erased** — true delete | **destroy, tombstone, acknowledge**. NOT a delete. See §3.1 |
| `dir.created` / `dir.deleted` | folder lifecycle | optional for most consumers |
| `acl.changed` | permission grant/revoke on a resource | **governance** — adds `principal` + `permissions`; invalidate cached permission decisions for `file_uid` |
| `role.assigned` / `role.member_removed` | user added to / removed from a role | **governance** — adds `role` + `member`; invalidate cached decisions for the member |
| `role.deleted` | a role was deleted | **governance** — adds `role` (empty `member`); invalidate for all members of the role |
| `accountability.committed` | the core committed a security record to its accountability chain | **a hint, not data** — adds `accountability_seq`. Only `audit_service` consumes it; every other consumer must ignore it |

### 3.1 `file.erased` — obligations, not suggestions

Erasure (`PROPOSAL_accountability_record.md` §5.4) is a compliance capability. A
consumer that mishandles it leaves the platform certifying a destruction that did
not happen, so this event carries requirements rather than guidance.

**It is not `file.deleted`, and must not share a code path with it.** A soft
delete is recoverable — the core has undelete — so keeping an index entry marked
deleted is a reasonable response to it. Erasure is irreversible, and the derived
data is precisely what has to go. Reusing the delete branch would leave the
extracted text and the embeddings exactly where they were, which is the failure
this event exists to prevent.

A consumer receiving `file.erased` must:

1. **Destroy** everything it derived from that uid — extracted text, chunk
   contents, embeddings, index entries, rendered pages, converted copies,
   comparison manifests, comment bodies quoting the document. Not mark deleted:
   destroy.
2. **Tombstone** the uid and refuse to write derived data for it thereafter. An
   erasure can arrive while a job is mid-flight on the same uid; letting that job
   finish puts the data straight back, *after* the erasure was recorded complete.
   Cancelling in-flight work is best-effort — the refusal is what closes the race.
   A consumer holding no derived data of its own has nothing to tombstone and
   should say so in its acknowledgement.
3. **Acknowledge** to the core, stating what was destroyed. "Acknowledged" with
   no such statement is not evidence, and the completion record is what an
   auditor is shown. A consumer that could not comply acknowledges
   `complied=false` with the reason — never silence, and never an optimistic ack.

**The event triggers; it does not guarantee.** This stream is fail-open,
trimmed and drop-oldest (§4) — right for a notification, unacceptable for a
contractual obligation, because a dropped erasure would leave a service holding
data the platform has certified destroyed and would do it silently. So every
participant must **also poll** `ListPendingErasures` for erasures it has not
acknowledged. That poll is the guarantee path and is what the attestation counts;
the event only makes it fast. It is also how a consumer that was down, or was
restored from a backup taken before the erasure, converges — with no instruction
needing to be redelivered.

The reconcile-sweep obligation in §7 is advisory. **For erasure it is
mandatory.**

**Governance events** (`acl.changed`, `role.*`) are first-class: they change
*effective* access. `acl.changed` is resource-scoped (`file_uid`); the `role.*`
events are not resource-scoped — a role change fans out to every resource the
member(s) could reach, so a permission-cache consumer invalidates by member (or
by role on `role.deleted`). These events add fields beyond the base envelope:
`principal`/`permissions` (ACL) and `role`/`member` (role).

**`accountability.committed` is a freshness hint, and nothing more.** The core
writes a guaranteed, hash-chained accountability record in the same transaction
as the operation it describes, and `audit_service` reads that record forward by
cursor over gRPC. This event carries only `{tenant, accountability_seq}`, and its
consumer does **not** process the payload — it reads the core's table
immediately, out of schedule. That buys latency without making anything depend on
the hint arriving.

Which is exactly why it rides *this* stream rather than the durable audit one.
Every property this contract gives — fail-open, trimmed, drop-oldest (§6.4) — is
correct for a notification and unacceptable for a system of record. A lost hint
costs latency only; the scheduled poll collects the record regardless. **Nothing
is ever only on the queue.**

The seq also gives the consumer a staleness check it could not otherwise have:
the hint asserts "at least seq N exists", so a read that does not show N is
reading state that has not caught up (a replica behind the primary, say) and must
retry rather than advance its cursor. Without that assertion the condition is
invisible, because it looks identical to "no new records".

Consumers other than `audit_service` should ignore this type, as they should
ignore any type they do not recognise. See
`file_engine_core/design_documents/PROPOSAL_accountability_record.md` §4.3.

**Rendition writes (as built):** the publisher **emits** rendition-child writes
**flagged `is_rendition: true`** (it does not suppress them). A conversion
consumer **must ignore events where `is_rendition` is true** so it does not
recurse on its own output. `is_rendition` is set when the affected entity's
parent is a *file* (renditions are hidden children of a file).

**New-file sequence:** creating a file through the bridges is `touch` then `put`,
so a brand-new file surfaces as a **`file.created` followed by a `file.updated`**
(the first content version). Treating `file.updated` as "(re)index this content"
and `file.created` as "an entity now exists" handles both idempotently.

## 4. Delivery semantics

- **At-least-once.** Duplicates and redeliveries are expected.
- **Idempotency keys:** `event_id` (exact dedupe, all events) and the logical key
  `(file_uid, version)` for content events (collapse repeated work for the same
  state). `version` is a string timestamp id; for events without a content
  version it is `""`, so fall back to `event_id` there.
- **Ordering:** best-effort per `file_uid`. Consumers must not assume global order;
  for content state treat the `version` timestamp string as the authority for
  "newest wins" (it sorts lexicographically = chronologically).
- **Acknowledgement:** consumer-group ack after durable processing; unacked entries
  are redelivered. A poison message goes to a dead-letter stream after N attempts.

## 5. Transport mapping — Redis Streams (dev)

- **Stream key:** a **single** stream `fileengine:events` (configurable via
  `FILEENGINE_EVENTS_STREAM`) shared by all tenants; the `tenant` travels in the
  body and consumers are multi-tenant aware (resolved decision). The publisher
  default matches this.
- **Producer:** `XADD <stream> MAXLEN ~ <maxlen> * payload <json>` after the DB
  commit succeeds (never before). The whole event is a single JSON string in the
  `payload` field. `maxlen` defaults to `100000`
  (`FILEENGINE_EVENTS_STREAM_MAXLEN`).
- **Producer-side backpressure:** a bounded in-memory outbox
  (`FILEENGINE_EVENTS_OUTBOX_CAPACITY`, default `10000`) feeds one worker thread;
  if Redis is down/slow the outbox fills and **drops oldest** (the freshest
  activity wins) — so a consumer can miss events during an outage and **must** run
  the reconcile sweep to recover.
- **Consumer:** `XREADGROUP` with a named group per consumer service
  (e.g. `convert_search_ai`), `XACK` on success, `XAUTOCLAIM` for recovery.
- **Retention:** capped (`MAXLEN ~`) — durability is for catch-up, not archival;
  the reconcile sweep (consumer-side) covers gaps beyond retention.

## 6. Publisher (as implemented in `file_engine_core`)

The publisher is built and merged. How it behaves:

1. **Emit points** — after the authoritative DB commit:
   - File/dir lifecycle from `FileSystem`: `mkdir`→`dir.created`, `rmdir`→
     `dir.deleted`, `touch`→`file.created`, `put`→`file.updated`, `remove`→
     `file.deleted`, `undelete`→`file.restored`, `move`→`file.moved`, `rename`→
     `file.renamed`, `copy`→`dir.created`/`file.created` (per new uid),
     `restore_to_version`→`file.updated`.
   - Governance from the gRPC RPCs (which call `AclManager`/`RoleManager`
     directly, via `FileSystem::publish_acl_change` / `publish_role_change`):
     `GrantPermission`/`RevokePermission`→`acl.changed`; `AssignUserToRole`→
     `role.assigned`; `RemoveUserFromRole`→`role.member_removed`; `DeleteRole`→
     `role.deleted`.
2. `tenant` and `actor` come from the request `AuthenticationContext`.
3. Events go through a **broker-agnostic `IEventSink`**; the Redis implementation
   (`RedisEventSink`, hiredis) is one backend. Call sites don't depend on Redis.
4. **Fail-open:** `IEventSink::publish` is `noexcept`; emission runs on a bounded
   async outbox + worker thread (mirroring the object-store backup worker) and can
   never fail, block, or roll back the filesystem operation. Overflow / broker
   outage → drop-oldest + a counter; recover via the consumer reconcile sweep.
5. Rendition-child writes are emitted **flagged `is_rendition: true`** (§3).
6. **Config** (`FILEENGINE_*` convention; events **disabled by default**):
   - Build: `-DFILEENGINE_ENABLE_EVENTS=ON` (requires `hiredis-devel`).
   - Runtime: `FILEENGINE_EVENTS_ENABLED=true`.
   - Connection: `FILEENGINE_REDIS_HOST` (`host` or `host:port`),
     `FILEENGINE_REDIS_PORT` (default 6379), `FILEENGINE_REDIS_PASSWORD`,
     `FILEENGINE_REDIS_DB`. The older `REDDIS_*` names are accepted as a legacy
     alias.
   - Stream: `FILEENGINE_EVENTS_STREAM` (default `fileengine:events`),
     `FILEENGINE_EVENTS_STREAM_MAXLEN` (default 100000),
     `FILEENGINE_EVENTS_OUTBOX_CAPACITY` (default 10000).

Not yet in the publisher (tracked in the core plan): TLS (`hiredis_ssl`), the
Prometheus event metrics, RFC3339 `ts`, a `mime` field, and `metadata.changed`.

## 7. Consumer requirements

1. Talk to an `EventSource` abstraction; never hardcode Redis.
2. Dedupe on `event_id` / `(file_uid, version)`; processing idempotent.
3. Run a periodic **reconcile sweep** against FileEngine to cover startup (initial
   corpus), retention gaps, and missed events.
4. Maintain a consumer-group cursor; ack only after durable side-effects; retry with
   dead-letter.
5. **Honour erasures** (§3.1): destroy, tombstone, acknowledge — and poll
   `ListPendingErasures` on a timer rather than relying on the event. This one is
   not optional for any consumer that holds derived content.

## 8. Versioning

`schema` starts at `1`. Additive fields don't bump it; breaking changes do, with
consumers handling the versions they understand and ignoring unknown ones.

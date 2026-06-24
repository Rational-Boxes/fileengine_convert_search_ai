# FileEngine file-activity event contract

Status: **Design**. Shared contract between the FileEngine **core publisher** (a
**future effort** — core does not emit events today) and any **consumer**
(`convert_search_ai` is the first; search, AI extraction, audit, cache
invalidation, etc. may follow).

This contract is deliberately **generic and reusable** — it is *not* specific to
`convert_search_ai`. Core publishes file-activity events once; many independent
consumers subscribe.

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

```json
{
  "event_id":   "uuid-v4",            // unique per emission; primary dedupe key
  "type":       "file.updated",       // see §3
  "tenant":     "default",
  "file_uid":   "uuid",               // the affected entity
  "parent_uid": "uuid",               // current parent (folder, or file uid for a rendition)
  "name":       "report.docx",
  "path":       "/projects/report.docx", // best-effort, advisory only
  "is_folder":  false,
  "is_rendition": false,              // true if the entity is a hidden child rendition
  "version":    7,                    // source version after the change (files)
  "size":       482190,
  "mime":       "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "actor":      "testuser@rationalboxes.com", // user from AuthenticationContext
  "ts":         "2026-06-23T02:00:00Z",       // RFC3339 emit time
  "schema":     1                     // contract version
}
```

Fields that don't apply to an event type may be omitted (e.g. `mime`/`size` on a
delete). Consumers must tolerate unknown additional fields (forward-compatible).

## 3. Event types

| `type` | Emitted when | Notes for consumers |
|--------|--------------|---------------------|
| `file.created`  | a new file row is committed | index/convert the new file |
| `file.updated`  | a new **version** is written (PUT) | re-extract, re-embed, re-render; supersede stale data |
| `file.moved`    | parent changes | no re-index — identity is the stable `file_uid`; update path only |
| `file.renamed`  | name changes | metadata only |
| `file.deleted`  | soft/hard delete | **cascade**: remove the file's derived rows/renditions |
| `file.restored` | undelete | re-instate / re-index |
| `dir.created` / `dir.deleted` | folder lifecycle | optional for most consumers |
| `acl.changed` | permission grant/revoke on a resource | **governance** — adds `principal` + `permissions`; invalidate cached permission decisions for `file_uid` |
| `role.assigned` / `role.member_removed` | user added to / removed from a role | **governance** — adds `role` + `member`; invalidate cached decisions for the member |
| `role.deleted` | a role was deleted | **governance** — adds `role` (empty `member`); invalidate for all members of the role |

**Governance events** (`acl.changed`, `role.*`) are first-class: they change
*effective* access. `acl.changed` is resource-scoped (`file_uid`); the `role.*`
events are not resource-scoped — a role change fans out to every resource the
member(s) could reach, so a permission-cache consumer invalidates by member (or
by role on `role.deleted`). These events add fields beyond the base envelope:
`principal`/`permissions` (ACL) and `role`/`member` (role).

Rendition writes (hidden children) **should be suppressed or clearly flagged**
(`is_rendition: true`) so the conversion consumer does not recurse on its own
output. Recommended: do **not** emit `file.created` for rendition children; if
emitted, consumers must ignore `is_rendition` events for conversion.

## 4. Delivery semantics

- **At-least-once.** Duplicates and redeliveries are expected.
- **Idempotency keys:** `event_id` (exact dedupe) and the logical key
  `(file_uid, version)` (collapse repeated work for the same state).
- **Ordering:** best-effort per `file_uid`. Consumers must not assume global order;
  treat `version` as the authority for "newest wins".
- **Acknowledgement:** consumer-group ack after durable processing; unacked entries
  are redelivered. A poison message goes to a dead-letter stream after N attempts.

## 5. Transport mapping — Redis Streams (dev)

- **Stream key:** a **single** stream `fileengine:events` (configurable via
  `FILEENGINE_EVENTS_STREAM`) shared by all tenants; the `tenant` travels in the
  body and consumers are multi-tenant aware (resolved decision). The publisher
  default matches this.
- **Producer:** `XADD … MAXLEN ~ <maxlen>` after the DB commit succeeds (never
  before).
- **Consumer:** `XREADGROUP` with a named group per consumer service
  (e.g. `convert_search_ai`), `XACK` on success, `XAUTOCLAIM` for recovery.
- **Retention:** capped (`MAXLEN ~`) — durability is for catch-up, not archival;
  the reconcile sweep (consumer-side) covers gaps beyond retention.

## 6. Publisher requirements (for the core effort)

These are the requirements the **next effort** — *add Redis event emission to
FileEngine core* — must satisfy:

1. Emit **after** the authoritative DB commit for: put/new-version, move, rename,
   delete (soft + hard), undelete, mkdir/rmdir.
2. Populate `tenant` and `actor` from the request `AuthenticationContext`.
3. Publish through a **broker-agnostic sink interface** in core (e.g.
   `IEventSink::publish(event)`), with a Redis implementation first; the call site
   in `FileSystem`/`grpc_service` does not depend on Redis.
4. **Fail-open for the filesystem op:** event publication must never fail or block
   the user's file operation. Use a bounded async outbox/queue; drop-with-metric
   (and rely on consumer reconcile) rather than rolling back a committed op.
5. Suppress or flag rendition-child writes (`is_rendition`) to avoid feedback loops.
6. Config via the existing `FILEENGINE_*` convention (e.g.
   `FILEENGINE_EVENTS_ENABLED`, `FILEENGINE_EVENTS_BROKER`,
   `FILEENGINE_EVENTS_REDIS_URL`); **disabled by default** so existing deployments
   are unaffected until opted in.

## 7. Consumer requirements

1. Talk to an `EventSource` abstraction; never hardcode Redis.
2. Dedupe on `event_id` / `(file_uid, version)`; processing idempotent.
3. Run a periodic **reconcile sweep** against FileEngine to cover startup (initial
   corpus), retention gaps, and missed events.
4. Maintain a consumer-group cursor; ack only after durable side-effects; retry with
   dead-letter.

## 8. Versioning

`schema` starts at `1`. Additive fields don't bump it; breaking changes do, with
consumers handling the versions they understand and ignoring unknown ones.

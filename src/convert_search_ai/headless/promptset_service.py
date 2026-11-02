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

"""The rules around prompt sets (EXTRACTION_PROMPT_SETS.md §4).

- **Editors** (tenant admins, ``extraction_editors``) build sets in the web
  application: drafts, publish, retire, take over, lock, YAML import/export.
- **Everyone** in the tenant may use published sets. A desktop key session sees
  only published, active sets (§4.5).
- **Applications publish their own definitions** with no person and no prior
  setup (§4.5): ``PUT /v1/prompt-sets/slug/{slug}`` from any member's ``api``
  key. A set first published that way is *application-managed*; any key may
  publish to it. A set made (or taken over) in the builder needs the editor
  role; a locked slug refuses changes; a retired set is gone for applications.
  An identical definition is always a no-op, so an application whose set an
  editor adopted keeps working.
- A requested model is a preference: one the deployment does not list falls
  back to the default, with a ``model_fallback`` warning, never an error (§4.4).
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import List, Optional, Tuple

from .. import audit
from . import promptsets as ps
from .promptset_store import SlugTaken, new_id

log = logging.getLogger("convert_search_ai.headless.promptsets")

EDITOR_ROLES = {"administrators", "tenant_admin", "system_admin", "extraction_editors"}


class ServiceError(Exception):
    def __init__(self, status: int, kind: str, message: str, **extra):
        super().__init__(message)
        self.status, self.kind, self.message, self.extra = status, kind, message, extra

    def body(self) -> dict:
        return {"kind": self.kind, "error": self.message, **self.extra}


def is_key_session(identity) -> bool:
    return bool(getattr(identity, "client_key_id", ""))


def is_editor(identity) -> bool:
    return bool(set(getattr(identity, "roles", None) or []) & EDITOR_ROLES)


def _iso(v) -> str:
    return v.isoformat() if hasattr(v, "isoformat") else (v or "")


class PromptSetService:
    def __init__(self, store, models, config):
        self.store, self.models, self.config = store, models, config

    # ------------------------------------------------------------------ helpers
    def normalise(self, raw: dict) -> dict:
        try:
            return ps.normalise(raw, max_fields=int(getattr(self.config,
                                                            "headless_max_schema_fields", 60)),
                                max_chars=int(getattr(self.config, "headless_max_input_chars",
                                                      20000)))
        except ps.DefinitionError as e:
            raise ServiceError(400, "invalid_definition", str(e))

    def _model_warnings(self, body: dict) -> List[dict]:
        if body.get("model") and not self.models.is_known(body["model"]):
            _entry, report = self.models.resolve(body["model"])
            return [{"kind": "model_fallback", "requested": body["model"],
                     "used": report["used"],
                     "message": f"model {body['model']!r} is not offered here; runs use "
                                f"{report['used']!r}"}]
        return []

    def _model_view(self, body: dict) -> dict:
        _entry, report = self.models.resolve(body.get("model"))
        label = self.models.by_id[report["used"]]["label"]
        return {**report, "label": label}

    def _require_editor(self, identity) -> None:
        if not is_editor(identity):
            raise ServiceError(403, "forbidden", "editing prompt sets needs the tenant "
                                                 "administrator or extraction editor role")

    def _builder_only(self, identity) -> None:
        """Builder operations are for people in the web application."""
        self._require_editor(identity)
        if is_key_session(identity):
            raise ServiceError(403, "forbidden", "use PUT /v1/prompt-sets/slug/{slug} to "
                                                 "publish from an application")

    def _latest(self, tenant: str, set_id: str) -> Optional[dict]:
        revs = self.store.list_revisions(tenant, set_id)
        return revs[-1] if revs else None

    def _draft(self, tenant: str, set_id: str) -> Optional[dict]:
        last = self._latest(tenant, set_id)
        return last if last and last["state"] == "draft" else None

    def _view(self, identity, s: dict, rev: Optional[dict], *, full: bool = False) -> dict:
        body = rev["body"] if rev else None
        out = {"id": s["id"], "slug": s["slug"], "name": s["name"],
               "description": s["description"], "status": s["status"],
               "managed": s["managed"], "revision": s["current_revision"]}
        if rev and rev["state"] == "published":
            out["published_at"] = _iso(rev["published_at"])
        if rev and rev.get("managed_by"):
            out["managed_by"] = {"label": rev["managed_by"].get("label", ""),
                                 "client_key_id": rev["managed_by"].get("client_key_id", "")}
        if body is not None:
            out["model"] = self._model_view(body)
            out["applies_to"] = body["applies_to"]["labels"]
            out["fields"] = ps.public_fields(body)
            if full:
                out["instructions"] = body["instructions"]
                out["schema"] = ps.compile_schema(body)
                out["markup"] = body["markup"]
                out["applies_to_detail"] = body["applies_to"]
        if is_editor(identity) and not is_key_session(identity):
            draft = self._draft(identity.tenant, s["id"])
            out.update(locked=s["locked"], replaced_by=s["replaced_by"],
                       draft_revision=draft["revision"] if draft else None)
        if s["status"] == "retired":
            out["replaced_by"] = s["replaced_by"]
        return out

    # ------------------------------------------------------------------ reads
    def list(self, identity, *, q: str = "", applies_to: str = "",
             limit: int = 100, offset: int = 0) -> dict:
        builder = is_editor(identity) and not is_key_session(identity)
        out = []
        for s in self.store.list_sets(identity.tenant):
            if not builder and (s["status"] != "active" or s["current_revision"] is None):
                continue
            rev = (self.store.get_revision(identity.tenant, s["id"], s["current_revision"])
                   if s["current_revision"] else (self._latest(identity.tenant, s["id"])
                                                  if builder else None))
            v = self._view(identity, s, rev)
            if q and q.lower() not in (v["name"] + " " + v["description"]).lower():
                continue
            if applies_to and applies_to not in (v.get("applies_to") or []):
                continue
            out.append(v)
        total = len(out)
        return {"prompt_sets": out[offset:offset + max(1, min(int(limit), 500))],
                "total": total}

    def get(self, identity, set_id: str, revision: Optional[int] = None) -> dict:
        s = self.store.get_set(identity.tenant, set_id)
        builder = is_editor(identity) and not is_key_session(identity)
        if s is None:
            raise ServiceError(404, "not_found", "no such prompt set")
        if revision is not None:
            rev = self.store.get_revision(identity.tenant, set_id, int(revision))
            if rev is None or (rev["state"] != "published" and not builder):
                raise ServiceError(404, "not_found", "no such revision")
        elif s["current_revision"]:
            rev = self.store.get_revision(identity.tenant, set_id, s["current_revision"])
        elif builder:
            rev = self._latest(identity.tenant, set_id)
        else:
            raise ServiceError(404, "not_found", "no such prompt set")
        if not builder and s["status"] == "retired" and revision is None:
            raise ServiceError(410, "prompt_set_retired", "this prompt set was retired",
                               replaced_by=s["replaced_by"] or None)
        v = self._view(identity, s, rev, full=True)
        v["revision_shown"] = rev["revision"]
        v["state"] = rev["state"]
        if builder:
            v["definition"] = rev["body"]
        return v

    def revisions(self, identity, set_id: str) -> List[dict]:
        s = self.store.get_set(identity.tenant, set_id)
        builder = is_editor(identity) and not is_key_session(identity)
        if s is None or (not builder and s["current_revision"] is None):
            raise ServiceError(404, "not_found", "no such prompt set")
        out = []
        for r in self.store.list_revisions(identity.tenant, set_id):
            if r["state"] != "published" and not builder:
                continue
            out.append({"revision": r["revision"], "state": r["state"], "hash": r["hash"],
                        "created_by": r["created_by"], "created_at": _iso(r["created_at"]),
                        "published_at": _iso(r["published_at"]),
                        "managed_by": r.get("managed_by")})
        return out

    def resolve_for_run(self, identity, set_id: str, revision: Optional[int]) -> Tuple[dict, dict]:
        """(set view, body) for an extraction. Published revisions only; a
        retired set is 410 for everyone (a run is not the builder)."""
        s = self.store.get_set(identity.tenant, set_id)
        if s is None or s["current_revision"] is None:
            raise ServiceError(404, "not_found", "no such prompt set")
        if s["status"] == "retired":
            raise ServiceError(410, "prompt_set_retired", "this prompt set was retired",
                               replaced_by=s["replaced_by"] or None)
        n = int(revision) if revision is not None else s["current_revision"]
        rev = self.store.get_revision(identity.tenant, set_id, n)
        if rev is None or rev["state"] != "published":
            raise ServiceError(404, "not_found", "no such published revision")
        info = {"id": s["id"], "name": rev["body"]["name"], "slug": s["slug"],
                "revision": n, "current_revision": s["current_revision"]}
        return info, rev["body"]

    # ------------------------------------------------------------------ builder writes
    def create(self, identity, raw: dict) -> dict:
        self._builder_only(identity)
        body = self.normalise(raw)
        set_id = new_id()
        try:
            self.store.create_set(identity.tenant, set_id=set_id, slug=body["slug"] or None,
                                  name=body["name"], description=body["description"],
                                  managed="builder", created_by=identity.user)
        except SlugTaken:
            raise ServiceError(409, "slug_taken", f"slug {body['slug']!r} is already used")
        n = self.store.add_revision(identity.tenant, set_id, body=body,
                                    hash=ps.definition_hash(body), state="draft",
                                    user=identity.user)
        self._audit(identity, "prompt_set.created", set_id, n)
        return {"id": set_id, "draft_revision": n, "warnings": self._model_warnings(body)}

    def save_draft(self, identity, set_id: str, raw: dict) -> dict:
        self._builder_only(identity)
        s = self.store.get_set(identity.tenant, set_id)
        if s is None:
            raise ServiceError(404, "not_found", "no such prompt set")
        body = self.normalise(raw)
        if body["slug"] and body["slug"] != (s["slug"] or ""):
            other = self.store.by_slug(identity.tenant, body["slug"])
            if other and other["id"] != set_id:
                raise ServiceError(409, "slug_taken", f"slug {body['slug']!r} is already used")
        h = ps.definition_hash(body)
        draft = self._draft(identity.tenant, set_id)
        if draft:
            self.store.update_draft(identity.tenant, set_id, draft["revision"], body=body,
                                    hash=h, user=identity.user)
            n = draft["revision"]
        else:
            n = self.store.add_revision(identity.tenant, set_id, body=body, hash=h,
                                        state="draft", user=identity.user)
        return {"id": set_id, "draft_revision": n, "warnings": self._model_warnings(body)}

    def publish(self, identity, set_id: str) -> dict:
        self._builder_only(identity)
        draft = self._draft(identity.tenant, set_id)
        if draft is None:
            raise ServiceError(409, "no_draft", "there is no draft to publish")
        if draft["body"].get("slug"):
            other = self.store.by_slug(identity.tenant, draft["body"]["slug"])
            if other and other["id"] != set_id:
                raise ServiceError(409, "slug_taken", "the draft's slug is already used")
        self.store.publish_revision(identity.tenant, set_id, draft["revision"])
        self._audit(identity, "prompt_set.published", set_id, draft["revision"], source="builder")
        return {"id": set_id, "revision": draft["revision"],
                "warnings": self._model_warnings(draft["body"])}

    def retire(self, identity, set_id: str, replaced_by: str = "") -> dict:
        self._builder_only(identity)
        return self._retire(identity, set_id, replaced_by, source="builder")

    def _retire(self, identity, set_id, replaced_by, source):
        if not self.store.get_set(identity.tenant, set_id):
            raise ServiceError(404, "not_found", "no such prompt set")
        if replaced_by and not self.store.get_set(identity.tenant, replaced_by):
            raise ServiceError(400, "bad_request", "replaced_by is not a prompt set")
        self.store.update_set(identity.tenant, set_id, status="retired",
                              replaced_by=replaced_by or "")
        self._audit(identity, "prompt_set.retired", set_id, None, source=source)
        return {"id": set_id, "status": "retired", "replaced_by": replaced_by or None}

    def set_control(self, identity, set_id: str, *, take_over: Optional[bool] = None,
                    locked: Optional[bool] = None) -> dict:
        """Editor control over application-managed sets (§4.5)."""
        self._builder_only(identity)
        if not self.store.get_set(identity.tenant, set_id):
            raise ServiceError(404, "not_found", "no such prompt set")
        fields = {}
        if take_over is not None:
            fields["managed"] = "builder" if take_over else "application"
        if locked is not None:
            fields["locked"] = bool(locked)
        self.store.update_set(identity.tenant, set_id, **fields)
        self._audit(identity, "prompt_set.control", set_id, None, **fields)
        return self.get(identity, set_id)

    # ------------------------------------------------------------------ YAML
    def export(self, identity, set_ids: List[str], revision: Optional[int] = None) -> str:
        builder = is_editor(identity) and not is_key_session(identity)
        docs = []
        for sid in set_ids:
            s = self.store.get_set(identity.tenant, sid)
            if s is None:
                raise ServiceError(404, "not_found", f"no such prompt set {sid}")
            if revision is not None and len(set_ids) == 1:
                rev = self.store.get_revision(identity.tenant, sid, int(revision))
            elif s["current_revision"]:
                rev = self.store.get_revision(identity.tenant, sid, s["current_revision"])
            else:
                rev = self._latest(identity.tenant, sid) if builder else None
            if rev is None or (rev["state"] != "published" and not builder):
                raise ServiceError(404, "not_found", f"nothing published in {sid}")
            docs.append(ps.to_export(rev["body"], revision=rev["revision"], status=rev["state"]))
        self._audit(identity, "prompt_set.exported", ",".join(set_ids), revision,
                    count=len(docs))
        return ps.dump_yaml(docs)

    def import_yaml(self, identity, text: str, on_conflict: str = "new") -> dict:
        self._builder_only(identity)
        if on_conflict not in ("new", "revision"):
            raise ServiceError(400, "bad_request", "on_conflict must be 'new' or 'revision'")
        try:
            parsed = ps.parse_import(text)
        except ps.DefinitionError as e:
            raise ServiceError(400, "invalid_file", str(e))
        # All or nothing: every definition must be valid before any is stored.
        prepared = []
        for raw, warnings in parsed:
            body = self.normalise(raw)
            prepared.append((body, list(warnings) + self._model_warnings(body)))
        results = []
        by_name = {s["name"]: s for s in self.store.list_sets(identity.tenant)}
        for body, warnings in prepared:
            existing = None
            if body["slug"]:
                existing = self.store.by_slug(identity.tenant, body["slug"])
            if existing is None:
                existing = by_name.get(body["name"])
            if existing is not None and on_conflict == "revision":
                draft = self._draft(identity.tenant, existing["id"])
                h = ps.definition_hash(body)
                if draft:
                    self.store.update_draft(identity.tenant, existing["id"], draft["revision"],
                                            body=body, hash=h, user=identity.user)
                    n = draft["revision"]
                else:
                    n = self.store.add_revision(identity.tenant, existing["id"], body=body,
                                                hash=h, state="draft", user=identity.user)
                results.append({"id": existing["id"], "name": body["name"], "draft_revision": n,
                                "created": False, "warnings": warnings})
                continue
            if existing is not None:
                body = {**body, "name": f"{body['name']} (imported)"[:ps.MAX_NAME]}
                if body["slug"] and self.store.by_slug(identity.tenant, body["slug"]):
                    warnings.append({"kind": "slug_dropped",
                                     "message": f"slug {body['slug']!r} is already used here"})
                    body = {**body, "slug": ""}
            set_id = new_id()
            self.store.create_set(identity.tenant, set_id=set_id, slug=body["slug"] or None,
                                  name=body["name"], description=body["description"],
                                  managed="builder", created_by=identity.user)
            n = self.store.add_revision(identity.tenant, set_id, body=body,
                                        hash=ps.definition_hash(body), state="draft",
                                        user=identity.user)
            results.append({"id": set_id, "name": body["name"], "draft_revision": n,
                            "created": True, "warnings": warnings})
        self._audit(identity, "prompt_set.imported", ",".join(r["id"] for r in results), None,
                    count=len(results))
        return {"imported": results}

    # ------------------------------------------------------------------ applications
    def app_publish(self, identity, slug: str, raw: dict, *, base_revision: Optional[int] = None,
                    force: bool = False, publish: bool = True) -> Tuple[int, dict]:
        """(HTTP status, body). Idempotent by definition hash (§4.5)."""
        if not (is_key_session(identity) or is_editor(identity)):
            raise ServiceError(403, "forbidden", "publish from an application's API key")
        if not isinstance(raw, dict):
            raise ServiceError(400, "invalid_definition", "the definition must be an object")
        raw = {k: v for k, v in raw.items() if k not in ("format", "type", "exported", "status")}
        raw["slug"] = slug
        body = self.normalise(raw)
        h = ps.definition_hash(body)
        warnings = self._model_warnings(body)
        managed_by = {"client_key_id": identity.client_key_id or "",
                      "label": identity.client_label or "", "user": identity.user}
        tenant = identity.tenant
        s = self.store.by_slug(tenant, slug)
        created = False
        if s is None:
            cap = int(getattr(self.config, "prompt_set_app_max_sets", 50))
            if self.store.count_sets(tenant, "application") >= cap:
                raise ServiceError(429, "app_set_limit",
                                   f"this tenant already has {cap} application-managed sets")
            try:
                s = self.store.create_set(tenant, set_id=new_id(), slug=slug, name=body["name"],
                                          description=body["description"], managed="application",
                                          created_by=identity.user)
                created = True
            except SlugTaken:            # a concurrent publish created it first
                s = self.store.by_slug(tenant, slug)
        if not created:
            current = (self.store.get_revision(tenant, s["id"], s["current_revision"])
                       if s["current_revision"] else None)
            if s["status"] == "retired":
                raise ServiceError(410, "prompt_set_retired", "this prompt set was retired",
                                   replaced_by=s["replaced_by"] or None)
            if publish and current is not None and current["hash"] == h:
                return 200, {"id": s["id"], "slug": slug, "revision": current["revision"],
                             "changed": False, "model": self._model_view(body),
                             "warnings": warnings}
            if s["managed"] == "builder" and not is_editor(identity):
                raise ServiceError(409, "managed_in_builder",
                                   "this prompt set is managed in the web application",
                                   current_revision=s["current_revision"])
            if s["locked"]:
                raise ServiceError(409, "locked", "this prompt set is locked by an editor",
                                   current_revision=s["current_revision"])
            if (base_revision is not None and not force
                    and int(base_revision) != (s["current_revision"] or 0)):
                raise ServiceError(409, "revision_conflict",
                                   "another revision was published since base_revision",
                                   current_revision=s["current_revision"])
            per_day = int(getattr(self.config, "prompt_set_app_revisions_per_day", 20))
            since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
            if self.store.revisions_since(tenant, s["id"], since) >= per_day:
                raise ServiceError(429, "revision_limit",
                                   f"at most {per_day} revisions of one set per day")
        if publish:
            n = self.store.add_revision(tenant, s["id"], body=body, hash=h, state="published",
                                        user=identity.user, managed_by=managed_by)
        else:
            draft = self._draft(tenant, s["id"])
            if draft:
                self.store.update_draft(tenant, s["id"], draft["revision"], body=body, hash=h,
                                        user=identity.user)
                n = draft["revision"]
            else:
                n = self.store.add_revision(tenant, s["id"], body=body, hash=h, state="draft",
                                            user=identity.user, managed_by=managed_by)
        self._audit(identity, "prompt_set.published" if publish else "prompt_set.drafted",
                    s["id"], n, source="headless", slug=slug, hash=h)
        return (201 if created else 200), {
            "id": s["id"], "slug": slug, "revision": n if publish else s["current_revision"],
            "draft_revision": None if publish else n, "changed": True,
            "model": self._model_view(body), "warnings": warnings}

    def app_retire(self, identity, slug: str, replaced_by: str = "") -> dict:
        s = self.store.by_slug(identity.tenant, slug)
        if s is None:
            raise ServiceError(404, "not_found", "no such prompt set")
        if not (is_key_session(identity) or is_editor(identity)):
            raise ServiceError(403, "forbidden", "retire from an application's API key")
        if s["managed"] == "builder" and not is_editor(identity):
            raise ServiceError(409, "managed_in_builder",
                               "this prompt set is managed in the web application")
        if s["locked"]:
            raise ServiceError(409, "locked", "this prompt set is locked by an editor")
        return self._retire(identity, s["id"], replaced_by, source="headless")

    # ------------------------------------------------------------------ audit
    @staticmethod
    def _audit(identity, action, set_id, revision, **extra):
        try:
            audit.record(action=action, user=identity.user, tenant=identity.tenant,
                         result="ok", prompt_set=set_id, revision=revision,
                         client_key_id=getattr(identity, "client_key_id", "") or "",
                         **{k: v for k, v in extra.items() if v is not None})
        except Exception:
            log.debug("audit failed for %s", action, exc_info=True)

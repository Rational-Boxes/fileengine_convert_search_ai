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

"""Prompt-set routes (EXTRACTION_PROMPT_SETS.md §4).

For the builder (editors in the web application): create, save a draft,
publish, retire, take over / lock, YAML export and import, test runs on pasted
text or a stored file.

For desktop applications (``api`` key sessions, §4.5): list the published sets,
fetch one with its compiled schema, export it, and publish the definitions the
application relies on (``PUT /v1/prompt-sets/slug/{slug}``) with no person and
no prior setup. Runs by id go through ``POST /v1/extract {prompt_set}``.

``GET /v1/extract/models`` lists the models extraction may use (§4.4)."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from ..api import _identity
from ..ldap_auth import Identity
from . import promptsets as ps
from . import usage as usage_mod
from .extract import ExtractRequest, ExtractRequestError, Extractor
from .models import registry as model_registry
from .promptset_service import PromptSetService, ServiceError, is_editor, is_key_session
from .promptset_store import PromptSetStore

log = logging.getLogger("convert_search_ai.headless.promptsets")

router = APIRouter(prefix="/v1", tags=["prompt-sets"])


def service(app) -> PromptSetService:
    s = getattr(app.state, "promptsets", None)
    if s is None:
        store = getattr(app.state, "promptset_store", None) or PromptSetStore(app.state.config)
        s = PromptSetService(store, model_registry(app), app.state.config)
        app.state.promptsets = s
    return s


def _err(e: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=e.status, content=e.body())


def _admit_key(request: Request, identity: Identity):
    """Key sessions count a request against the key's limits (no tokens)."""
    if not is_key_session(identity):
        return None, None
    from .routes import _admit
    return _admit(request.app, identity, spends_tokens=False)


def _yaml(text: str, filename: str) -> Response:
    return PlainTextResponse(text, media_type="application/yaml", headers={
        "Content-Disposition": f'attachment; filename="{filename}"'})


def _filename(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in name).strip() or "prompt-set"
    return f"{safe[:80]}.promptset.yaml"


# ------------------------------------------------------------------ models

@router.get("/extract/models")
def extract_models(request: Request, identity: Identity = Depends(_identity)):
    """The models extraction may use: ``[{id, label, default}]``. A requested
    model that is not listed falls back to the default."""
    return {"models": model_registry(request.app).public()}


# ------------------------------------------------------------------ list / read

@router.get("/prompt-sets")
def list_prompt_sets(request: Request, q: str = "", applies_to: str = "", limit: int = 100,
                     offset: int = 0, identity: Identity = Depends(_identity)):
    """Published prompt sets (a key session), or every set (an editor in the web
    application). ``ETag`` / ``If-None-Match`` let an application revalidate."""
    _st, err = _admit_key(request, identity)
    if err is not None:
        return err
    out = service(request.app).list(identity, q=q, applies_to=applies_to, limit=limit,
                                    offset=offset)
    etag = '"' + hashlib.sha256(json.dumps(out, sort_keys=True, default=str)
                                .encode()).hexdigest()[:24] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=out, headers={"ETag": etag})


@router.get("/prompt-sets/export")
def export_many(request: Request, ids: str, identity: Identity = Depends(_identity)):
    set_ids = [i for i in ids.split(",") if i.strip()]
    if not set_ids:
        raise HTTPException(status_code=400, detail="ids is required")
    try:
        text = service(request.app).export(identity, set_ids)
    except ServiceError as e:
        return _err(e)
    return _yaml(text, "prompt-sets.promptset.yaml")


@router.get("/prompt-sets/{set_id}")
def get_prompt_set(request: Request, set_id: str, revision: Optional[int] = None,
                   identity: Identity = Depends(_identity)):
    _st, err = _admit_key(request, identity)
    if err is not None:
        return err
    try:
        return service(request.app).get(identity, set_id, revision)
    except ServiceError as e:
        return _err(e)


@router.get("/prompt-sets/{set_id}/revisions")
def list_revisions(request: Request, set_id: str, identity: Identity = Depends(_identity)):
    try:
        return {"revisions": service(request.app).revisions(identity, set_id)}
    except ServiceError as e:
        return _err(e)


@router.get("/prompt-sets/{set_id}/revisions/{revision}")
def get_revision(request: Request, set_id: str, revision: int,
                 identity: Identity = Depends(_identity)):
    try:
        return service(request.app).get(identity, set_id, revision)
    except ServiceError as e:
        return _err(e)


@router.get("/prompt-sets/{set_id}/export")
def export_one(request: Request, set_id: str, revision: Optional[int] = None,
               identity: Identity = Depends(_identity)):
    svc = service(request.app)
    try:
        text = svc.export(identity, [set_id], revision)
        name = svc.get(identity, set_id, revision)["name"]
    except ServiceError as e:
        return _err(e)
    return _yaml(text, _filename(name))


# ------------------------------------------------------------------ builder

class ControlBody(BaseModel):
    take_over: Optional[bool] = None
    locked: Optional[bool] = None


class RetireBody(BaseModel):
    replaced_by: str = ""


@router.post("/prompt-sets", status_code=201)
def create_prompt_set(request: Request, body: dict, identity: Identity = Depends(_identity)):
    try:
        return service(request.app).create(identity, body)
    except ServiceError as e:
        return _err(e)


@router.put("/prompt-sets/{set_id}")
def save_draft(request: Request, set_id: str, body: dict,
               identity: Identity = Depends(_identity)):
    try:
        return service(request.app).save_draft(identity, set_id, body)
    except ServiceError as e:
        return _err(e)


@router.post("/prompt-sets/{set_id}/publish")
def publish(request: Request, set_id: str, identity: Identity = Depends(_identity)):
    try:
        return service(request.app).publish(identity, set_id)
    except ServiceError as e:
        return _err(e)


@router.post("/prompt-sets/{set_id}/retire")
def retire(request: Request, set_id: str, body: RetireBody = RetireBody(),
           identity: Identity = Depends(_identity)):
    try:
        return service(request.app).retire(identity, set_id, body.replaced_by)
    except ServiceError as e:
        return _err(e)


@router.post("/prompt-sets/{set_id}/control")
def control(request: Request, set_id: str, body: ControlBody,
            identity: Identity = Depends(_identity)):
    """Take over an application-managed set (``take_over``) or lock its slug."""
    try:
        return service(request.app).set_control(identity, set_id, take_over=body.take_over,
                                                locked=body.locked)
    except ServiceError as e:
        return _err(e)


@router.post("/prompt-sets/import")
async def import_prompt_sets(request: Request, on_conflict: str = "new",
                             identity: Identity = Depends(_identity)):
    """Raw YAML (or JSON) body, or a multipart upload in ``file``. Imports are
    drafts; nothing is imported unless every set in the file is valid."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise HTTPException(status_code=400, detail="multipart import needs a 'file' part")
        data = await f.read()
    else:
        data = await request.body()
    if len(data) > ps.MAX_FILE_BYTES:
        return _err(ServiceError(400, "invalid_file",
                                 f"the file is larger than {ps.MAX_FILE_BYTES // 1024} KB"))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _err(ServiceError(400, "invalid_file", "the file is not UTF-8 text"))
    if not text.strip():
        raise HTTPException(status_code=400, detail="empty body: provide the YAML")
    try:
        return service(request.app).import_yaml(identity, text, on_conflict)
    except ServiceError as e:
        return _err(e)


# ------------------------------------------------------------------ applications

def _parse_definition(text: str, ctype: str) -> dict:
    if "json" in ctype:
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ServiceError(400, "invalid_definition", f"not valid JSON: {e}")
        if not isinstance(data, dict):
            raise ServiceError(400, "invalid_definition", "the definition must be an object")
        return data
    try:
        items = ps.parse_import(text)
    except ps.DefinitionError as e:
        raise ServiceError(400, "invalid_definition", str(e))
    if len(items) != 1:
        raise ServiceError(400, "invalid_definition", "publish one prompt set per request")
    return items[0][0]


@router.put("/prompt-sets/slug/{slug}")
async def app_publish(request: Request, slug: str, base_revision: Optional[int] = None,
                      force: bool = False, publish: bool = True,
                      identity: Identity = Depends(_identity)):
    """Publish the definition an application relies on (§4.5), as YAML
    (``application/yaml``) or JSON. Idempotent: an unchanged definition is a
    no-op (200, ``changed: false``); a new one is created (201); a changed one
    becomes the next published revision (200). ``base_revision`` detects a
    revision published since (409 ``revision_conflict``) unless ``force``."""
    _st, err = _admit_key(request, identity)
    if err is not None:
        return err
    data = await request.body()
    if len(data) > ps.MAX_FILE_BYTES:
        return _err(ServiceError(400, "invalid_definition", "the definition is too large"))
    try:
        raw = _parse_definition(data.decode("utf-8", errors="replace"),
                                request.headers.get("content-type", ""))
        status, out = service(request.app).app_publish(
            identity, slug, raw, base_revision=base_revision, force=force, publish=publish)
    except ServiceError as e:
        return _err(e)
    return JSONResponse(status_code=status, content=out)


@router.post("/prompt-sets/slug/{slug}/retire")
def app_retire(request: Request, slug: str, body: RetireBody = RetireBody(),
               identity: Identity = Depends(_identity)):
    _st, err = _admit_key(request, identity)
    if err is not None:
        return err
    try:
        return service(request.app).app_retire(identity, slug, body.replaced_by)
    except ServiceError as e:
        return _err(e)


# ------------------------------------------------------------------ test runs

class TestBody(BaseModel):
    """One of ``set`` (an unsaved definition, so edits can be tried before
    saving) or ``prompt_set`` (+ ``revision``); one of ``text`` (pasted) or
    ``file_uid`` (a stored document); optional ``model`` for this test only."""
    model_config = {"populate_by_name": True}
    set_: Optional[dict] = Field(default=None, alias="set")
    prompt_set: Optional[str] = None
    revision: Optional[int] = None
    text: Optional[str] = None
    file_uid: Optional[str] = None
    name: str = "Pasted text"
    model: Optional[str] = None


@router.post("/prompt-sets/test")
async def test_prompt_set(request: Request, body: TestBody,
                          identity: Identity = Depends(_identity)):
    """Try a definition (§4.3) on pasted text or a stored file. Runs the normal
    extractor as the caller and streams like ``/v1/extract``. Stores nothing:
    no cache entry, no run record."""
    return await _test(request, body, identity)


@router.post("/prompt-sets/{set_id}/test")
async def test_saved_prompt_set(request: Request, set_id: str, body: TestBody,
                                identity: Identity = Depends(_identity)):
    body.prompt_set = set_id
    return await _test(request, body, identity)


async def _test(request: Request, body: TestBody, identity: Identity):
    from .routes import (_Slot, _admit, _client_for, _convert_fn, _final_usage, _maybe_await,
                         _respond, _svc, _usage_error)
    from .sse import EventRun
    from .. import audit

    app = request.app
    config = app.state.config
    svc = service(app)
    if not is_editor(identity) or is_key_session(identity):
        return _err(ServiceError(403, "forbidden", "testing prompt sets is for editors in the "
                                                   "web application"))
    if (body.set_ is None) == (body.prompt_set is None):
        raise HTTPException(status_code=400, detail="give set or prompt_set")
    if (body.text is None) == (body.file_uid is None):
        raise HTTPException(status_code=400, detail="give text or file_uid")
    try:
        if body.set_ is not None:
            set_body = svc.normalise(body.set_)
            set_info = {"id": None, "name": set_body["name"], "revision": None}
        else:
            view = svc.get(identity, body.prompt_set, body.revision)
            set_body = view["definition"]
            set_info = {"id": view["id"], "name": view["name"],
                        "revision": view["revision_shown"]}
    except ServiceError as e:
        return _err(e)
    cap = int(getattr(config, "extract_test_max_chars", 200000))
    if body.text is not None and len(body.text) > cap:
        raise HTTPException(status_code=400, detail=f"pasted text exceeds {cap} characters")
    if body.text is not None and not body.text.strip():
        raise HTTPException(status_code=400, detail="the pasted text is empty")

    models = model_registry(app)
    entry, choice = models.resolve(body.model or set_body.get("model"))
    chat = models.provider(entry, app.state.chat.chat)
    req = ExtractRequest(schema=ps.compile_schema(set_body),
                         files=[body.file_uid] if body.file_uid else [],
                         instructions=set_body["instructions"], prompt_set=set_info,
                         model_choice=choice, write_cache=False)
    status, err = _admit(app, identity, spends_tokens=True)
    if err is not None:
        return err
    s = _svc(app)
    slot = _Slot(s.meter, identity)
    try:
        slot.take()
    except (usage_mod.RateLimited, usage_mod.UsageUnavailable) as e:
        return _usage_error(e, identity)
    extractor = Extractor(config, client_for=_client_for(app), gate=app.state.permission_gate,
                          store=s.store, chat=chat, cache=None, convert=_convert_fn(app),
                          meter=s.meter)

    def producer(emit, cancelled):
        try:
            emit("usage", status)
            if body.text is not None:
                emit("plan", {"files": [{"file_uid": "", "name": body.name, "version": ""}],
                              "skipped": [], "truncated": False, "prompt_set": set_info,
                              "model": choice, "test": True})
                item = extractor.extract_text(identity, req, body.name, body.text)
                emit("item", item)
                summary = {"files": 1, "extracted": 1, **(item.get("counts") or {}),
                           "tokens": item.get("tokens", 0), "prompt_set": set_info,
                           "model": choice, "test": True}
            else:
                summary = {**extractor.run(identity, req, emit, cancelled), "test": True}
            audit.record(action="prompt_set.test", user=identity.user, tenant=identity.tenant,
                         result="ok", prompt_set=set_info.get("id") or "",
                         revision=set_info.get("revision"), model=choice["used"],
                         input="text" if body.text is not None else "file",
                         input_chars=len(body.text) if body.text is not None else None,
                         file_uid=body.file_uid or "",
                         **{k: v for k, v in summary.items() if isinstance(v, int)
                            and not isinstance(v, bool)})
            final = _final_usage(s.meter, identity, status)
            emit("usage", final)
            emit("done", summary)
            return {"summary": summary, "usage": final}
        finally:
            slot.release()

    def shape(events, result):
        return {"plan": next((d for e, d in events if e == "plan"), None),
                "items": [d for e, d in events if e in ("item", "error")],
                "summary": result.get("summary"), "usage": result.get("usage")}

    return await _maybe_await(_respond(request, EventRun(producer), status, shape))

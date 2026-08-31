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

"""What this deployment actually offers.

Every feature here is a-la-carte: a deployment may run without in-browser
editing, without a chat provider, without web search, without an embedding
model. The SPA had no way to ask, so it offered everything and let whichever
endpoint was missing produce the error — an "Edit in browser" button that
answered 404 on a deployment with no Document Server, and the user with no way
to tell a missing feature from a broken one.

Reporting a feature as OFF is a promise about the deployment, not about the
caller. Whether a particular user may edit a particular file needs WRITE on that
file, cannot be cached, and stays with the endpoint that does the work. A client
needs both: this to decide whether a control exists at all, and the per-object
check to decide whether to offer it here and now.

WHAT MUST NEVER APPEAR HERE. This reports configuration, so the line is worth
stating rather than assuming: no API keys, no signing secrets, no bind
passwords, and no internal URLs. A base URL carrying a credential, or the
address of a service that is not meant to be reachable from a browser, is not
made safe by being described as configuration. Booleans, limits the user runs
into, and the names of providers and models are the intended shape.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import onlyoffice as oo
from ..config import Config
from ..http_auth import extract_tenant, resolve_identity

router = APIRouter(prefix="/v1", tags=["capabilities"])


def _cfg(request: Request) -> Config:
    return request.app.state.config


def _require_identity(request: Request) -> None:
    """Authenticated, like the rest of the API.

    A client asking what it may offer has a session already, and there is no
    reason to describe a deployment to anyone who does not.
    """
    config = _cfg(request)
    headers = {k.lower(): v for k, v in request.headers.items()}
    tenant = extract_tenant(headers, headers.get("host", ""), config.tenant)
    ident = resolve_identity(headers.get("authorization", ""), tenant, config,
                             request.app.state.token_store,
                             getattr(request.app.state, "bridge_verifier", None))
    if ident is None:
        raise HTTPException(status_code=401, detail="authentication required")


def _editing(config: Config) -> dict:
    """In-browser editing, and the three conditions that decide it.

    These are exactly what makes /v1/onlyoffice/config answer 404 or 503 no
    matter who asks. The third used to be reachable only by trying: the first
    two are checked up front, but the signing secret is not consulted until a
    config is being signed, so a deployment missing it looked healthy right up
    until someone clicked Edit.
    """
    if not config.onlyoffice_enabled:
        reason = "CSAI_ONLYOFFICE_ENABLED is off"
    elif not config.onlyoffice_docserver_url:
        reason = "CSAI_ONLYOFFICE_DOCSERVER_URL is not set"
    elif not config.onlyoffice_signing_secret:
        reason = "no signing secret (CSAI_ONLYOFFICE_SIGNING_SECRET or FILEENGINE_JWT_SECRET)"
    else:
        reason = ""
    return {
        "available": not reason,
        "reason": reason,
        # Handed out so a client need not keep its own copy of this list in step
        # with ours — the SPA's carries a comment saying it "mirrors the
        # backend's editable set", which is a duplication waiting to drift.
        "extensions": sorted(oo.editable_extensions()),
        "max_bytes": config.onlyoffice_max_bytes,
    }


def _chat(config: Config) -> dict:
    """Whether the chat surface will answer.

    A provider name alone is not enough: the default provider is set whether or
    not anyone configured a key, so `available` asks whether it can actually
    reach a model. Ollama is the exception — it authenticates by being reachable
    rather than by a key.
    """
    needs_key = config.chat_provider not in ("ollama", "echo")
    available = bool(config.chat_provider) and (bool(config.chat_api_key) or not needs_key)
    return {
        "available": available,
        "provider": config.chat_provider,
        "model": config.chat_model,
        "max_tokens": config.chat_max_tokens,
        "document_tool": bool(config.chat_document_tool_enabled),
        "max_k": config.max_chat_k,
    }


def _web_search(config: Config) -> dict:
    """The chat's web-search tool. `default_on` is what the toggle starts as."""
    return {
        "available": bool(config.web_search_enabled),
        "default_on": bool(config.web_search_default),
        "provider": config.web_search_provider,
        "results": config.web_search_results,
    }


def _search(config: Config) -> dict:
    """Semantic search, which needs an embedding model to have been configured.

    Without one nothing was ever indexed, so the search box would return an
    empty result for every query and look like an empty library rather than a
    feature that is not installed.
    """
    needs_key = config.embedding_provider not in ("ollama", "")
    available = bool(config.embedding_provider) and (
        bool(config.embedding_api_key) or not needs_key)
    return {
        "available": available,
        "provider": config.embedding_provider,
        "model": config.embedding_model,
        "dimension": config.embedding_dimension,
    }


@router.get("/capabilities")
def capabilities(request: Request) -> dict:
    """The deployment's feature configuration, as one document.

    One request rather than one per feature, and one place to add the next
    a-la-carte switch. Sections are namespaced so a client reads the ones it
    understands and ignores the rest — an older SPA against a newer deployment
    sees keys it does not know, which is not an error.
    """
    _require_identity(request)
    config = _cfg(request)
    return {
        "editing": _editing(config),
        "chat": _chat(config),
        "web_search": _web_search(config),
        "search": _search(config),
    }

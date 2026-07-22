"""CSAI as an MCP *client* (MCP_INTEGRATIONS §6).

Connects to a tenant's enabled MCP servers over Streamable-HTTP / SSE, discovers
their tools, and wraps each as a CSAI :class:`~.llm_tools.Tool` so the chat model
can call it in the existing agentic loop. Three invariants shape this module:

  * **Fail open on the chat.** A down/misconfigured/erroring server contributes
    ZERO tools and logs — it never raises into the answer stream.
  * **User consent is mandatory.** A wrapped tool's ``run`` obtains explicit
    per-call consent (via ``ctx.consent``) BEFORE it touches the network; a missing
    consent channel, a denial, or a timeout means the tool does not run.
  * **Namespacing.** Every tool is exposed as ``mcp__<slug>__<tool>`` so it can
    neither shadow a built-in tool nor collide across integrations.

The MCP SDK client is async; each discovery/call opens a short-lived session driven
by ``asyncio.run`` from the (sync) tool thread — simple and robust for P1. Session
reuse/pooling is a P2 optimization.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

from . import audit
from .config import Config
from .llm_tools import Tool, ToolContext, ToolOutput
from .mcp_store import McpIntegration, McpIntegrationStore

log = logging.getLogger("convert_search_ai.mcp_client")


class McpConnectionError(Exception):
    """A connect/discovery/call failure against an external MCP server."""


@dataclass
class ToolSpec:
    """A discovered MCP tool (server-native name + its JSON input schema)."""
    name: str
    description: str
    input_schema: dict


@dataclass
class ConsentRequest:
    """Handed to ``ctx.consent`` so the user can approve/deny one MCP tool call."""
    integration: str    # admin-facing name (display)
    slug: str
    tool: str           # server-native tool name
    tool_full: str      # namespaced (mcp__slug__tool) — the consent/remember key
    args_summary: str   # short, human-readable preview of the arguments


# --------------------------------------------------------------------------- #
# Endpoint validation (SSRF) — reuse the webfetch guard (https + public host).
# --------------------------------------------------------------------------- #
def validate_endpoint(url: str) -> str:
    """Raise ``ValueError`` unless ``url`` is https with a public, routable host —
    blocking loopback/private/link-local/metadata SSRF targets (webfetch guard)."""
    from .webfetch import FetchBlocked, validate_url
    try:
        return validate_url(url)
    except FetchBlocked as e:
        raise ValueError(str(e)) from e


# --------------------------------------------------------------------------- #
# Async MCP session helpers (driven synchronously via asyncio.run).
# --------------------------------------------------------------------------- #
async def _with_session(url: str, transport: str, headers: dict, coro_fn):
    from mcp import ClientSession
    if transport == "sse":
        from mcp.client.sse import sse_client
        async with sse_client(url, headers=headers or None) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await coro_fn(session)
    else:  # streamable-http (default)
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client(url, headers=headers or None) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await coro_fn(session)


def _run(coro, timeout_s: float):
    """Drive an async MCP operation to completion with a hard wall-clock bound."""
    async def _bounded():
        return await asyncio.wait_for(coro, timeout_s)
    return asyncio.run(_bounded())


def _content_to_text(result) -> str:
    """Flatten a CallToolResult's content blocks to plain text for the model."""
    parts: List[str] = []
    for block in (getattr(result, "content", None) or []):
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        elif getattr(block, "type", None) == "resource":
            res = getattr(block, "resource", None)
            parts.append(getattr(res, "text", None) or getattr(res, "uri", "") or "")
        else:
            parts.append(str(block))
    return "\n".join(p for p in parts if p)


def discover_tools(*, endpoint_url: str, transport: str, headers: dict,
                   timeout_s: float) -> List[ToolSpec]:
    """Connect + ``list_tools()``. Raises :class:`McpConnectionError` on any failure
    (the caller decides whether that's fatal — discovery for chat swallows it)."""
    async def _list(session):
        res = await session.list_tools()
        out = []
        for t in (getattr(res, "tools", None) or []):
            schema = getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
            out.append(ToolSpec(name=t.name, description=(getattr(t, "description", "") or ""),
                                input_schema=schema))
        return out
    try:
        return _run(_with_session(endpoint_url, transport, headers, _list), timeout_s)
    except Exception as e:  # network, protocol, timeout, cancellation — all fatal here
        raise McpConnectionError(str(e)) from e


def call_tool(*, endpoint_url: str, transport: str, headers: dict, timeout_s: float,
              tool_name: str, args: dict) -> str:
    """Connect + ``call_tool``; return the flattened text content."""
    async def _call(session):
        res = await session.call_tool(tool_name, args or {})
        text = _content_to_text(res)
        if getattr(res, "isError", False):
            return f"(the tool reported an error) {text}".strip()
        return text
    try:
        return _run(_with_session(endpoint_url, transport, headers, _call), timeout_s)
    except Exception as e:
        raise McpConnectionError(str(e)) from e


# --------------------------------------------------------------------------- #
# Header assembly (auth credential + opt-in identity forwarding).
# --------------------------------------------------------------------------- #
def _build_headers(config: Config, store: McpIntegrationStore, integ: McpIntegration,
                   identity) -> dict:
    headers = dict(integ.headers or {})
    if integ.auth_type in ("bearer", "header") and identity is not None:
        secret = store.decrypted_secret(getattr(identity, "tenant", ""), integ.id)
        if secret:
            if integ.auth_type == "bearer":
                headers["Authorization"] = f"Bearer {secret}"
            else:
                headers[integ.auth_header or "Authorization"] = secret
    # Opt-in, minimal identity forwarding (§7): never roles/tokens/ACLs.
    if integ.forward_identity and identity is not None:
        from .crypto import sign_identity_assertion
        user = getattr(identity, "user", "") or ""
        tenant = getattr(identity, "tenant", "") or ""
        headers["X-Fileengine-User"] = user
        headers["X-Fileengine-Tenant"] = tenant
        assertion = sign_identity_assertion(config.mcp_identity_secret, user=user,
                                            tenant=tenant, ttl=config.mcp_identity_ttl)
        if assertion:
            headers["X-Fileengine-User-Assertion"] = assertion
    return headers


def _args_summary(args: dict, *, max_chars: int = 300) -> str:
    if not args:
        return "(no arguments)"
    parts = []
    for k, v in args.items():
        sv = v if isinstance(v, str) else str(v)
        if len(sv) > 80:
            sv = sv[:77] + "…"
        parts.append(f"{k}={sv}")
    s = ", ".join(parts)
    return s if len(s) <= max_chars else s[: max_chars - 1] + "…"


# --------------------------------------------------------------------------- #
# The wrapped tool the model sees.
# --------------------------------------------------------------------------- #
class McpTool(Tool):
    """One discovered MCP tool, wrapped as a CSAI ``Tool``. ``run`` is consent-gated
    and executes against the integration's credentials (never the user's core ACLs)."""

    def __init__(self, config: Config, store: McpIntegrationStore,
                 integ: McpIntegration, spec: ToolSpec):
        self._config = config
        self._store = store
        self._integ = integ
        self._tool_name = spec.name
        self.name = f"mcp__{integ.slug}__{spec.name}"
        self.description = (spec.description
                            or f"External tool '{spec.name}' from the '{integ.name}' integration.")
        self.schema = spec.input_schema or {"type": "object", "properties": {}}

    def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        args = args or {}
        integ = self._integ
        user = getattr(ctx.identity, "user", "")
        tenant = getattr(ctx.identity, "tenant", "")

        # (a) Consent is mandatory. No consent channel ⇒ deny (fail-closed).
        consent = getattr(ctx, "consent", None)
        if not callable(consent):
            log.warning("MCP tool %s called with no consent channel; denying", self.name)
            audit.record(action="mcp_consent", user=user, tenant=tenant, result="denied",
                         integration=integ.slug, tool=self._tool_name, reason="no_channel")
            return ToolOutput(text="(this action requires your approval, which was not available)")
        req = ConsentRequest(integration=integ.name, slug=integ.slug, tool=self._tool_name,
                             tool_full=self.name, args_summary=_args_summary(args))
        try:
            approved = bool(consent(req))
        except Exception:
            log.warning("MCP consent channel failed for %s", self.name, exc_info=True)
            approved = False
        audit.record(action="mcp_consent", user=user, tenant=tenant,
                     result="ok" if approved else "denied",
                     integration=integ.slug, tool=self._tool_name)
        if not approved:
            return ToolOutput(text="(the user declined to run this tool, so it was not executed)")

        # (b) Approved — call the external server with the integration's credentials.
        headers = _build_headers(self._config, self._store, integ, ctx.identity)
        timeout_s = max(1.0, self._config.mcp_tool_timeout_ms / 1000.0)
        try:
            text = call_tool(endpoint_url=integ.endpoint_url, transport=integ.transport,
                             headers=headers, timeout_s=timeout_s,
                             tool_name=self._tool_name, args=args)
        except McpConnectionError as e:
            audit.record(action="mcp_tool", user=user, tenant=tenant, result="error",
                         integration=integ.slug, tool=self._tool_name)
            log.info("MCP tool %s failed: %s", self.name, e)
            return ToolOutput(text=f"(the '{integ.name}' tool could not be reached: {e})")
        cap = self._config.mcp_max_tool_output_chars
        truncated = len(text) > cap
        if truncated:
            text = text[:cap] + "\n…(truncated)"
        audit.record(action="mcp_tool", user=user, tenant=tenant, result="ok",
                     integration=integ.slug, tool=self._tool_name, truncated=truncated)
        # Leave a bibliographic note: a successful MCP call is a cited source in the
        # turn, alongside document and web citations (chat.py assigns its [n] marker).
        src = {"kind": "mcp", "integration": integ.name, "tool": self._tool_name,
               "label": f"{integ.name} · {self._tool_name}"}
        ctx.sources.append(src)
        return ToolOutput(text=text or "(the tool returned no content)", sources=[src])


# --------------------------------------------------------------------------- #
# Discovery + caching across a tenant's enabled integrations.
# --------------------------------------------------------------------------- #
class McpToolProvider:
    """Resolves the MCP tools available to a tenant's chat, with per-integration
    discovery caching (keyed by config-version so an edit busts the cache)."""

    def __init__(self, config: Config, store: Optional[McpIntegrationStore] = None):
        self.config = config
        self.store = store or McpIntegrationStore(config)
        # (tenant, integ.id, integ.updated_at) -> (expiry_epoch, [ToolSpec])
        self._cache: dict = {}

    def invalidate(self, tenant: str, integ_id: str = "") -> None:
        for key in [k for k in self._cache
                    if k[0] == tenant and (not integ_id or k[1] == integ_id)]:
            self._cache.pop(key, None)

    def _discover_cached(self, tenant: str, integ: McpIntegration) -> List[ToolSpec]:
        key = (tenant, integ.id, integ.updated_at)
        now = time.time()
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        headers = _build_headers(self.config, self.store, integ, _Ident(tenant))
        specs = discover_tools(endpoint_url=integ.endpoint_url, transport=integ.transport,
                               headers=headers,
                               timeout_s=max(1.0, self.config.mcp_tool_timeout_ms / 1000.0))
        self._cache[key] = (now + max(0, self.config.mcp_connect_cache_ttl), specs)
        return specs

    def tools_for(self, identity) -> List[Tool]:
        """Every enabled integration's discovered, allowlist-permitted tools for
        ``identity``'s tenant. Fail-open: a bad server contributes nothing."""
        if not getattr(self.config, "mcp_enabled", False) or identity is None:
            return []
        tenant = getattr(identity, "tenant", "") or ""
        try:
            integrations = self.store.list(tenant, enabled_only=True)
        except Exception:
            log.warning("MCP: could not load integrations for tenant %s", tenant, exc_info=True)
            return []
        tools: List[Tool] = []
        for integ in integrations:
            try:
                specs = self._discover_cached(tenant, integ)
            except McpConnectionError as e:
                log.info("MCP integration '%s' unreachable — omitting its tools: %s",
                         integ.slug, e)
                audit.record(action="mcp_discover", user=getattr(identity, "user", ""),
                             tenant=tenant, result="error", integration=integ.slug)
                continue
            allow = set(integ.allowed_tools) if integ.allowed_tools is not None else None
            cap = self.config.mcp_max_tools_per_integration
            for spec in specs:
                if allow is not None and spec.name not in allow:
                    continue
                tools.append(McpTool(self.config, self.store, integ, spec))
                if len([t for t in tools if t.name.startswith(f"mcp__{integ.slug}__")]) >= cap:
                    log.info("MCP integration '%s' hit the per-integration tool cap (%d)",
                             integ.slug, cap)
                    break
        return tools


@dataclass
class _Ident:
    """Minimal identity for discovery (tenant only — no per-user credential)."""
    tenant: str
    user: str = ""

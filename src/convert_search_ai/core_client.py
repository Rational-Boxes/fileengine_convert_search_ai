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

"""Build FileEngine gRPC clients (``ManagedFiles``) bound to a specific identity.

The trusted-upstream model: whatever user/roles/tenant we pass is sent verbatim
in every request's ``AuthenticationContext``, and the core enforces ACLs against
it. Two callers exist:

- ``client_for(identity)`` — a client acting **as the end user**. All retrieval
  (search results, RAG chunks) MUST go through one of these so the core's
  permission checks gate what the user can see.
- ``agent_client(config)`` — a client acting as this service's own agent
  identity, used only for indexing / writing renditions. Never reuse it for
  retrieval on behalf of a user. By default it carries the core's trusted
  ``system_admin`` role so indexing sees *all* content (a complete index);
  per-user ACLs are then enforced at retrieval time via ``client_for``.
"""
import contextvars
from dataclasses import replace

from .ldap_auth import Identity, authenticate
from ._client import ManagedFiles

# Request-scoped client IP (set by the HTTP middleware), forwarded to the core so
# per-user file-access audit rows carry the real caller's address. Empty for
# background work (e.g. the ingest worker) which has no client connection.
request_source_addr: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "request_source_addr", default="")

# The core grants this role an ACL bypass (acl_manager.h / server.cpp): a trusted
# upstream attaches it to legitimately privileged, system-level requests. Indexing
# is exactly that — it must read everything so denied/restricted content is still
# indexed, with visibility decided per user at query time.
SYSTEM_ADMIN_ROLE = "system_admin"


def client_for(identity: Identity, config) -> ManagedFiles:
    """A gRPC client that acts as ``identity`` (the end user).

    Mirrors the bridges' tenant-admin aliasing (http_bridge, security review H2): a
    member of the tenant ``administrators`` group acts with the core's
    ``tenant_admin`` role, so CSAI resolves the **same** effective permissions the
    REST/WebDAV doors do. Without this, CSAI under-privileges tenant admins — e.g.
    denying WRITE (and blocking ONLYOFFICE editing) on files they can edit via the
    SPA. The global ``system_admin`` bypass is deliberately NOT added (it is granted
    only to actual ``system_admin`` group members, carried verbatim)."""
    roles = list(identity.roles or [])
    if "administrators" in roles and "tenant_admin" not in roles:
        roles.append("tenant_admin")
    return ManagedFiles(
        server_address=config.grpc_address,
        user_name=identity.user,
        user_roles=roles,
        tenant=identity.tenant or config.tenant,
        source_addr=request_source_addr.get(),  # forwarded to the core for audit
    )


def agent_identity(config) -> Identity:
    """Authenticate the service's own agent account against LDAP."""
    return authenticate(config, config.agent_user, config.agent_password)


def agent_client(config) -> ManagedFiles:
    """A gRPC client acting as this service's agent identity (indexing/renditions
    only — never retrieval).

    When ``config.index_bypass_acl`` (default on), the client carries the
    ``system_admin`` role so the ingest sweep can read every file regardless of
    ACLs, giving a complete vector index. This is safe because retrieval re-checks
    permissions as the end user (see retrieval.py / PermissionGate); a deny rule
    therefore hides content at query time without ever removing it from the index
    for users who *are* allowed to see it."""
    ident = agent_identity(config)
    if getattr(config, "index_bypass_acl", True) and SYSTEM_ADMIN_ROLE not in ident.roles:
        ident = replace(ident, roles=list(ident.roles) + [SYSTEM_ADMIN_ROLE])
    return client_for(ident, config)

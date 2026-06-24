"""LDAP authentication and role resolution — the auth/permission authority.

Ported from the FileEngine MCP server (which mirrors the WebDAV/HTTP bridges):
a real bind authenticates the user, roles come from group membership, and a
tenant's ``administrators`` group maps to the core's ``system_admin`` role. The
resolved identity is forwarded to the gRPC core, which enforces ACLs."""
from dataclasses import dataclass, field
from typing import List

from ldap3 import Server, Connection, ALL, SUBTREE
from ldap3.core.exceptions import LDAPException


@dataclass
class Identity:
    user: str
    roles: List[str] = field(default_factory=list)
    tenant: str = "default"
    authenticated: bool = False


def authenticate(cfg, username: str, password: str) -> Identity:
    """Bind as ``username`` to authenticate, then resolve roles from LDAP groups.

    Returns an Identity with ``authenticated=False`` if the bind fails or the
    user is not found. The tenant is taken from config."""
    ident = Identity(user=username, tenant=cfg.tenant)
    if not username or not password:
        return ident

    server = Server(cfg.ldap_uri, get_info=ALL)
    try:
        # Service bind to look up the user's DN.
        svc = Connection(server, cfg.ldap_bind_dn, cfg.ldap_bind_password, auto_bind=True)
    except LDAPException:
        return ident

    try:
        svc.search(cfg.ldap_user_base, f"(uid={username})", search_scope=SUBTREE, attributes=["cn"])
        if not svc.entries:
            return ident
        user_dn = svc.entries[0].entry_dn

        # Authentication: bind as the user with their password.
        try:
            user_conn = Connection(server, user_dn, password, auto_bind=True)
            user_conn.unbind()
        except LDAPException:
            return ident

        # Roles from group membership (groupOfNames with member=user_dn).
        roles: List[str] = []
        svc.search(cfg.ldap_tenant_base,
                   f"(&(objectClass=groupOfNames)(member={user_dn}))",
                   search_scope=SUBTREE, attributes=["cn"])
        for entry in svc.entries:
            cn = str(entry.cn)
            if cn and cn not in roles:
                roles.append(cn)

        # A tenant administrator gets the core's privileged role.
        if "administrators" in roles and "system_admin" not in roles:
            roles.append("system_admin")

        ident.roles = roles
        ident.authenticated = True
        return ident
    finally:
        svc.unbind()

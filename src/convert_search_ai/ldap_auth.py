"""LDAP authentication and role resolution — the auth/permission authority.

Ported from the FileEngine MCP server (which mirrors the WebDAV/HTTP bridges):
a real bind authenticates the user, roles come from group membership, and a
tenant's ``administrators`` group maps to the core's ``system_admin`` role. The
resolved identity is forwarded to the gRPC core, which enforces ACLs."""
from dataclasses import dataclass, field
from typing import List, Optional

from ldap3 import Server, Connection, ALL, SUBTREE
from ldap3.core.exceptions import LDAPException

from .failover import CircuitBreaker


@dataclass
class Identity:
    user: str
    roles: List[str] = field(default_factory=list)
    tenant: str = "default"
    authenticated: bool = False


class _ServerUnreachable(Exception):
    """The directory server couldn't be reached (vs. a credential rejection) —
    signals the caller to fail over to the replica."""


# Process-wide breaker for the master directory (REPLICATION_FAILOVER.md).
_ldap_breaker: Optional[CircuitBreaker] = None


def _breaker(cfg) -> CircuitBreaker:
    global _ldap_breaker
    if _ldap_breaker is None:
        _ldap_breaker = CircuitBreaker(cooldown_s=getattr(cfg, "failover_cooldown_s", 30))
    return _ldap_breaker


def _ldap_targets(cfg):
    """Ordered (uri, is_primary) directories to try. Master first while healthy;
    replica-only while the breaker says the master is down (re-probed after the
    cooldown). No replica configured -> just the master (unchanged behavior)."""
    if not getattr(cfg, "ldap_replica_enabled", False):
        return [(cfg.ldap_uri, True)]
    if _breaker(cfg).should_try_primary():
        return [(cfg.ldap_uri, True), (cfg.ldap_uri_replica, False)]
    return [(cfg.ldap_uri_replica, False)]


def authenticate(cfg, username: str, password: str) -> Identity:
    """Bind as ``username`` to authenticate, then resolve roles from LDAP groups.

    Returns an Identity with ``authenticated=False`` if the bind fails or the
    user is not found. When a replica directory is configured, an unreachable
    master fails over to the replica (auth is read-only). The tenant is from config."""
    ident = Identity(user=username, tenant=cfg.tenant)
    if not username or not password:
        return ident

    for uri, is_primary in _ldap_targets(cfg):
        try:
            result = _authenticate_against(uri, cfg, username, password)
            if is_primary:
                _breaker(cfg).reset()
            return result
        except _ServerUnreachable:
            if is_primary:
                _breaker(cfg).trip()
            continue  # fall over to the replica
    return ident


def _authenticate_against(uri: str, cfg, username: str, password: str) -> Identity:
    """Run the bind + role resolution against one directory. Raises
    :class:`_ServerUnreachable` if the directory can't be reached."""
    ident = Identity(user=username, tenant=cfg.tenant)
    server = Server(uri, get_info=ALL)
    try:
        # Service bind to look up the user's DN. A failure here is treated as the
        # server being unreachable (so the caller can fail over).
        svc = Connection(server, cfg.ldap_bind_dn, cfg.ldap_bind_password, auto_bind=True)
    except LDAPException as e:
        raise _ServerUnreachable(uri) from e

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

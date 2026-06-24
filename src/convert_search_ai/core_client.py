"""Build FileEngine gRPC clients (``ManagedFiles``) bound to a specific identity.

The trusted-upstream model: whatever user/roles/tenant we pass is sent verbatim
in every request's ``AuthenticationContext``, and the core enforces ACLs against
it. Two callers exist:

- ``client_for(identity)`` — a client acting **as the end user**. All retrieval
  (search results, RAG chunks) MUST go through one of these so the core's
  permission checks gate what the user can see.
- ``agent_client(config)`` — a client acting as this service's own agent
  identity, used only for indexing / writing renditions. Never reuse it for
  retrieval on behalf of a user.
"""
from .ldap_auth import Identity, authenticate
from ._client import ManagedFiles


def client_for(identity: Identity, config) -> ManagedFiles:
    """A gRPC client that acts as ``identity`` (the end user)."""
    return ManagedFiles(
        server_address=config.grpc_address,
        user_name=identity.user,
        user_roles=identity.roles,
        tenant=identity.tenant or config.tenant,
    )


def agent_identity(config) -> Identity:
    """Authenticate the service's own agent account against LDAP."""
    return authenticate(config, config.agent_user, config.agent_password)


def agent_client(config) -> ManagedFiles:
    """A gRPC client acting as this service's agent identity (indexing/renditions only)."""
    ident = agent_identity(config)
    return client_for(ident, config)

"""Unit tests for the bearer-token store."""
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.token_store import TokenStore


def test_issue_resolve_revoke():
    s = TokenStore(3600)
    ident = Identity(user="alice", roles=["users"], tenant="default", authenticated=True)
    tok = s.issue(ident)
    assert s.resolve(tok) is ident
    assert s.resolve("nope") is None
    s.revoke(tok)
    assert s.resolve(tok) is None


def test_expired_token_is_evicted():
    s = TokenStore(-1)  # already expired
    ident = Identity(user="x", authenticated=True)
    assert s.resolve(s.issue(ident)) is None

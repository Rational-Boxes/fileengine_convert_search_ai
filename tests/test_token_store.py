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

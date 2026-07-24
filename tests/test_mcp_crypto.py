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

"""MCP secret encryption + minimal identity-assertion signing (crypto.py)."""
import pytest

from convert_search_ai import crypto
from convert_search_ai.jwt_verify import verify_hs256


def test_secret_round_trip():
    key = crypto.generate_key()
    blob = crypto.encrypt_secret(key, "s3cr3t-token")
    assert isinstance(blob, (bytes, bytearray)) and b"s3cr3t" not in bytes(blob)
    assert crypto.decrypt_secret(key, blob) == "s3cr3t-token"


def test_decrypt_wrong_key_raises_without_leaking():
    blob = crypto.encrypt_secret(crypto.generate_key(), "abc")
    with pytest.raises(crypto.SecretError):
        crypto.decrypt_secret(crypto.generate_key(), blob)


def test_missing_key_is_an_error():
    with pytest.raises(crypto.SecretError):
        crypto.encrypt_secret("", "abc")


def test_identity_assertion_verifies_and_is_minimal():
    tok = crypto.sign_identity_assertion("shared-secret", user="alice", tenant="acme", ttl=60)
    claims = verify_hs256(tok, "shared-secret")
    assert claims is not None
    assert claims["sub"] == "alice" and claims["tenant"] == "acme"
    # Minimal: no roles / core token / ACLs are forwarded.
    assert "roles" not in claims and "token" not in claims


def test_identity_assertion_none_without_secret():
    assert crypto.sign_identity_assertion("", user="a", tenant="b") is None

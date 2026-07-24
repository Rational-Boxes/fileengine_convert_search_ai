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

"""Unit tests for the structured, secret-free audit log."""
import json

from convert_search_ai import audit


def _read(path):
    return [json.loads(ln[len("audit "):]) for ln in path.read_text().splitlines()
            if ln.startswith("audit ")]


def test_records_are_structured_json_lines(tmp_path):
    p = tmp_path / "audit.log"
    audit.configure(str(p))
    audit.record(action="search", user="alice", tenant="default", result="ok",
                 candidates=10, hits=3)
    audit.record(action="document_text", user="bob", tenant="t1", result="denied",
                 file_uid="f1")
    entries = _read(p)
    assert len(entries) == 2
    assert entries[0] == {"ts": entries[0]["ts"], "action": "search", "user": "alice",
                          "tenant": "default", "result": "ok", "candidates": 10, "hits": 3}
    assert entries[1]["action"] == "document_text" and entries[1]["result"] == "denied"
    assert entries[1]["file_uid"] == "f1"


def test_record_is_content_and_secret_free(tmp_path):
    p = tmp_path / "audit.log"
    audit.configure(str(p))
    audit.record(action="chat", user="u", tenant="t", result="ok",
                 retrieved=4, citations=2, context_trimmed=True)
    line = p.read_text().lower()
    assert "password" not in line and "token" not in line
    entry = _read(p)[0]
    # only shape/outcome — no message/content/query fields
    assert set(entry) == {"ts", "action", "user", "tenant", "result",
                          "retrieved", "citations", "context_trimmed"}

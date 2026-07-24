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

"""Unit tests for the pluggable web-search provider (offline; no network)."""
import pytest

from convert_search_ai.config import Config
from convert_search_ai.providers import make_web_search_provider
from convert_search_ai.providers.websearch import (
    DuckDuckGoSearchProvider, FakeWebSearchProvider, NullWebSearchProvider)


def _cfg(**over):
    c = Config()
    for k, v in over.items():
        setattr(c, k, v)
    return c


def test_factory_defaults_to_duckduckgo():
    # Config default is duckduckgo; the factory builds the DDG backend without
    # touching the network (no search() call here).
    assert isinstance(make_web_search_provider(Config()), DuckDuckGoSearchProvider)
    assert isinstance(make_web_search_provider(_cfg(web_search_provider="ddg")),
                      DuckDuckGoSearchProvider)


def test_factory_fake_null_and_unknown():
    assert isinstance(make_web_search_provider(_cfg(web_search_provider="fake")),
                      FakeWebSearchProvider)
    assert isinstance(make_web_search_provider(_cfg(web_search_provider="none")),
                      NullWebSearchProvider)
    with pytest.raises(ValueError):
        make_web_search_provider(_cfg(web_search_provider="bing"))


def test_fake_provider_is_deterministic_and_sized():
    p = FakeWebSearchProvider()
    a = p.search("climate models", k=3)
    b = p.search("climate models", k=3)
    assert len(a) == 3
    assert [r.url for r in a] == [r.url for r in b]      # deterministic
    assert all(r.url.startswith("https://") and r.title and r.snippet for r in a)


def test_null_provider_returns_nothing():
    assert NullWebSearchProvider().search("anything", k=5) == []


def test_duckduckgo_passes_config_through():
    p = make_web_search_provider(_cfg(web_region="us-en", web_safesearch="strict",
                                      web_timelimit="w", web_timeout_ms=2000))
    assert p.region == "us-en" and p.safesearch == "strict"
    assert p.timelimit == "w" and p.timeout == 2.0

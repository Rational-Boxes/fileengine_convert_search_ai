

import pytest


# --- an over-long chunk is split, not fatal ---------------------------------
#
# The chunker bounds chunks by CHARACTERS, which is only a proxy for tokens:
# prose runs ~4 chars/token, IFC GUIDs and identifiers closer to 2.3. A
# 1200-char chunk of IFC arrived as 513 tokens against a 512-token model, and
# one rejected chunk failed the request for the whole document — four IFC files
# stayed unindexed even after the chunker was capped.

class _LimitedEmbedder:
    """Rejects any chunk over `limit` chars, the way the API rejects tokens."""

    def __init__(self, limit=600, dim=4):
        self.limit = limit
        self.dim = dim
        self.rejections = 0

    def embed(self, texts):
        for t in texts:
            if len(t) > self.limit:
                self.rejections += 1
                raise RuntimeError(
                    "Error code: 400 - You passed 513 input tokens ... "
                    "the model's context length is only 512 tokens")
        return [[0.1] * self.dim for _ in texts]


def _indexer_with(embedder):
    from convert_search_ai.indexing import Indexer
    ix = Indexer.__new__(Indexer)
    ix._embedder = embedder          # `embedder` is a lazy property with no setter
    return ix


def test_an_over_long_chunk_is_halved_until_it_fits():
    emb = _LimitedEmbedder(limit=600)
    ix = _indexer_with(emb)

    out = ix._fit_to_model(["x" * 2000])

    assert len(out) > 1
    assert all(len(t) <= 600 for t in out)
    assert "".join(out) == "x" * 2000          # nothing dropped
    assert emb.rejections > 0                  # it really did reject first


def test_only_the_offending_chunk_is_split():
    """A short chunk next to a long one must not be split as collateral."""
    emb = _LimitedEmbedder(limit=600)
    out = _indexer_with(emb)._fit_to_model(["short", "y" * 2000, "also short"])

    assert "short" in out and "also short" in out
    assert all(len(t) <= 600 for t in out)


def test_a_non_length_error_still_propagates():
    """Splitting must not swallow a real failure — a bad API key is not a
    chunk that is too long."""
    class Broken:
        def embed(self, texts):
            raise RuntimeError("401 Unauthorized: invalid api key")

    with pytest.raises(RuntimeError, match="401"):
        _indexer_with(Broken())._fit_to_model(["anything"])


def test_a_pathological_chunk_is_truncated_rather_than_split_forever():
    class AlwaysRejects:
        def embed(self, texts):
            raise RuntimeError("input tokens exceed the model's context length")

    out = _indexer_with(AlwaysRejects())._fit_to_model(["z" * 5000])
    assert out and all(len(t) <= 400 for t in out)

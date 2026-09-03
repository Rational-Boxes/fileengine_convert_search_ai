

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
    """Truncation is the last resort, and the truncated text is what gets used."""
    emb = _LimitedEmbedder(limit=400)
    out = _indexer_with(emb)._fit_to_model(["z" * 5000])
    assert out and all(len(t) <= 400 for t in out)
    assert emb.rejections > 0


def test_a_chunk_rejected_even_when_truncated_is_dropped_not_fatal():
    """A fragment no amount of splitting will fit must not take the document with
    it. It used to: _fit_to_model returned the truncated text unembedded, the
    caller re-embedded everything, the same rejection came back, and the whole
    document was left index_failed."""
    class AlwaysRejects:
        def embed(self, texts):
            raise RuntimeError("input tokens exceed the model's context length")

    texts, vectors = _indexer_with(AlwaysRejects())._fit(["z" * 5000], 0)
    assert texts == [] and vectors == []


# --- a document with more chunks than the provider accepts -------------------
#
# The other half of "one rejected chunk failed the request for the whole
# document", and the half that reached production: providers cap the input ARRAY
# as well as each item. DeepInfra allows 1024. A 6 MB reference PDF chunked to
# 1445 was refused whole — `422 ... at most 1024 items after validation, not
# 1445` — and because that is not a per-chunk length error the splitting above
# never engaged. Four versions of that one file sat `index_failed`, retried on
# every sweep, failing identically each time.

class _CountingEmbedder:
    """Accepts at most `cap` items per request, like a real provider."""

    def __init__(self, cap=1024, dim=4):
        self.cap = cap
        self.dim = dim
        self.calls = []          # items per call
        self.refusals = 0

    def embed(self, texts):
        self.calls.append(len(texts))
        if len(texts) > self.cap:
            self.refusals += 1
            raise RuntimeError(
                f"Error code: 422 - {{'error': {{'message': 'Value should have at "
                f"most {self.cap} items after validation, not {len(texts)}', "
                f"'code': 'array_above_max_length'}}}}")
        return [[0.1] * self.dim for _ in texts]


def _config(batch_size):
    from convert_search_ai.config import Config
    cfg = Config()
    cfg.embedding_batch_size = batch_size
    return cfg


def _n_chunks(n):
    """Markdown that chunks to exactly `n` chunks.

    The chunker packs blocks up to ~1200 chars, so short paragraphs merge; each
    block here is over the budget on its own. Small numbers on purpose — the bug
    is a ratio (chunks per request vs the provider's cap), not a scale, and the
    production case was 1445 chunks against a cap of 1024."""
    from convert_search_ai.chunking import chunk_markdown
    md = "\n\n".join(f"para{i} " + ("word " * 200) for i in range(n))
    assert len(chunk_markdown(md)) == n, "fixture no longer yields one chunk per block"
    return md


def test_a_document_larger_than_the_provider_cap_is_indexed_in_batches():
    from convert_search_ai.indexing import Indexer

    class Store:
        def __init__(self):
            self.items = None

        def replace(self, tenant, uid, items):
            self.items = items

        def delete(self, tenant, uid):
            pass

    emb, store = _CountingEmbedder(cap=8), Store()
    ix = Indexer(_config(4), embedder=emb, chunk_store=store)

    n = ix.index("default", "f1", _n_chunks(20))

    assert n == 20                         # more chunks than the provider takes at once
    assert emb.refusals == 0               # never asked for more than it allows
    assert max(emb.calls) <= 4             # and never more than our own batch size
    # Every stored chunk carries its own vector, and the ordinals are contiguous.
    assert [i for i, _, _ in store.items] == list(range(n))
    assert all(v for _, _, v in store.items)


def test_a_batch_the_provider_still_refuses_is_halved():
    """Belt and braces: if a provider's cap is lower than CSAI_EMBEDDING_BATCH_SIZE,
    the refusal is recognised as a count problem and the batch is split, rather
    than the document failing the way it did in production."""
    from convert_search_ai.indexing import Indexer

    class Store:
        def replace(self, tenant, uid, items):
            self.items = items

        def delete(self, tenant, uid):
            pass

    emb = _CountingEmbedder(cap=3)         # much lower than the batch size below
    ix = Indexer(_config(16), embedder=emb, chunk_store=Store())

    n = ix.index("default", "f1", _n_chunks(12))

    assert n == 12
    assert emb.refusals > 0                # it really did refuse first
    assert max(emb.calls) <= 16


def test_chunks_are_embedded_once_not_twice():
    """The fitting pass used to embed the whole document and throw the vectors
    away, so every document was embedded twice — twice the cost, latency and
    rate-limit pressure for an answer already given."""
    from convert_search_ai.indexing import Indexer

    class Store:
        def replace(self, tenant, uid, items):
            self.items = items

        def delete(self, tenant, uid):
            pass

    emb = _CountingEmbedder(cap=64)
    ix = Indexer(_config(64), embedder=emb, chunk_store=Store())
    n = ix.index("default", "f1", _n_chunks(10))

    assert n == 10
    assert sum(emb.calls) == n             # one item embedded once, no second pass
    assert len(emb.calls) == 1

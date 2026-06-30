"""pgvector chunk store: write chunk embeddings and ANN-search them (per tenant).

Vectors are sent as ``[f,f,…]`` text and cast with ``::vector`` so no extra
psycopg adapter is needed; ``psycopg`` is imported lazily via ``db``. ANN search
uses cosine distance (``<=>``) against the HNSW index from the baseline schema."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

from .config import Config


@dataclass
class RetrievedChunk:
    file_uid: str
    ordinal: int
    text: str
    distance: float


def _vec_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class ChunkStore:
    def __init__(self, config: Config):
        self.config = config

    def _conn(self, tenant: str, readonly: bool = False):
        from .db import connect_for_tenant
        return connect_for_tenant(self.config, tenant, readonly=readonly)

    def replace(self, tenant: str, file_uid: str,
                items: List[Tuple[int, str, Sequence[float]]]) -> None:
        """Replace all chunks for a file (idempotent re-index)."""
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM chunks WHERE file_uid = %s", (file_uid,))
            for ordinal, text, emb in items:
                cur.execute(
                    "INSERT INTO chunks (file_uid, ordinal, text, embedding) "
                    "VALUES (%s, %s, %s, %s::vector)",
                    (file_uid, ordinal, text, _vec_literal(emb)),
                )
            conn.commit()

    def delete(self, tenant: str, file_uid: str) -> None:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM chunks WHERE file_uid = %s", (file_uid,))
            conn.commit()

    def ann_search(self, tenant: str, query_embedding: Sequence[float], k: int) -> List[RetrievedChunk]:
        ql = _vec_literal(query_embedding)
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT file_uid, ordinal, text, embedding <=> %s::vector AS distance "
                "FROM chunks WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (ql, ql, k),
            )
            return [RetrievedChunk(r[0], r[1], r[2], float(r[3])) for r in cur.fetchall()]

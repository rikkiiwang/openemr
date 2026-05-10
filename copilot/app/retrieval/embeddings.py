"""OpenAI text embeddings for the dense-retrieval path.

Produces 1536-dimensional vectors via ``text-embedding-3-small`` for the
~12-chunk seed corpus (~$0.000002 per cold cache) and the per-query
embedding (one call per turn that uses ``evidence_retriever``).

Lazy initialization mirrors ``rerank.py``'s pattern: the OpenAI client
is constructed on first use, not at import time, so test environments
without ``OPENAI_API_KEY`` don't fail to import. ``embed_text`` and
``embed_batch`` raise on any error — callers (corpus.search, build) wrap
in try/except and fall back to BM25-only on failure, matching the
existing critic / rerank fail-soft pattern.

In-memory LRU cache for query embeddings (max 256 entries) so a chat
session asking the same question twice doesn't pay the API roundtrip
twice. The corpus's chunk embeddings are persisted directly on the
``GuidelineCorpus`` instance after ``build()``, so they don't go through
this cache.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import OpenAI

logger = logging.getLogger("copilot.retrieval.embeddings")

# Pinned model. text-embedding-3-small: 1536 dims, $0.02/1M tokens, fast.
# text-embedding-3-large would also work (3072 dims) but cost 6.5x with
# negligible recall lift on a 12-chunk corpus.
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536


@lru_cache(maxsize=1)
def _get_client() -> "OpenAI":
    """Lazy-init the OpenAI client. Raises ImportError or auth errors only
    on first call, not at module import."""
    from openai import OpenAI  # local import — keep test envs without the
                               # SDK importable for unrelated tests

    return OpenAI()


def embed_text(text: str) -> list[float]:
    """Embed a single string. Returns a 1536-dim list of floats."""
    return _embed_query_cached(text)


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed N strings in one API call. Returns a list of 1536-dim vectors
    in input order."""
    if not texts:
        return []
    client = _get_client()
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    # OpenAI returns embeddings in input order; preserve.
    return [d.embedding for d in resp.data]


@lru_cache(maxsize=256)
def _embed_query_cached(text: str) -> tuple[float, ...]:
    """LRU-cached single-query embedder. Tuple return so it's hashable in
    case any caller wraps further; ``embed_text`` converts back to list for
    the standard API."""
    client = _get_client()
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=[text])
    return tuple(resp.data[0].embedding)


def cosine(a: list[float] | tuple[float, ...], b: list[float] | tuple[float, ...]) -> float:
    """Cosine similarity in pure Python — fast enough on a 12-chunk corpus
    that pulling NumPy in for this would be premature. For larger corpora
    (>100 chunks) swap to ``numpy.dot(a, b) / (linalg.norm(a) * linalg.norm(b))``."""
    if len(a) != len(b):
        raise ValueError(f"vector dim mismatch: {len(a)} vs {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    # math.sqrt would be slightly faster but pow(_, 0.5) avoids the import.
    return dot / ((na ** 0.5) * (nb ** 0.5))


def reset_query_cache() -> None:
    """Clear the LRU cache. Test-only; not called in production."""
    _embed_query_cached.cache_clear()


__all__ = [
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "embed_text",
    "embed_batch",
    "cosine",
    "reset_query_cache",
]

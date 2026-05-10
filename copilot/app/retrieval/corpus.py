"""Guideline corpus reader — BM25 (always) + optional dense via OpenAI.

When ``settings.copilot_dense_retrieval_enabled`` is True, ``search``
runs BM25 + dense in parallel and fuses via reciprocal rank fusion
(RRF). Default-OFF preserves the byte-identical BM25-only path that
has been live since W2 MVP.

Embeddings are computed once at ``build()`` time and held in memory on
this instance — no SQLite BLOB column, no separate vector store. The
12-chunk seed corpus × 1536 dims × 4 bytes = ~72KB, trivial. For larger
corpora the SQLite + ``apsw_blob`` or pgvector swap is in
``W2_ARCHITECTURE.md``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import aiosqlite

from app.config import get_settings
from app.retrieval.embeddings import cosine, embed_batch, embed_text

logger = logging.getLogger("copilot.retrieval.corpus")


@dataclass(frozen=True)
class GuidelineHit:
    chunk_id: str
    source: str
    section: str
    text: str
    url: str
    last_updated: date | None
    score: float


_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS guidelines USING fts5(
  chunk_id UNINDEXED,
  source UNINDEXED,
  section,
  text,
  url UNINDEXED,
  last_updated UNINDEXED,
  tokenize = 'porter unicode61'
);
"""

# Reciprocal Rank Fusion constant. k=60 is the canonical default from
# Cormack et al. 2009; it works well across retrieval-quality regimes
# and doesn't need tuning at this corpus size.
_RRF_K = 60


class GuidelineCorpus:
    def __init__(self, *, jsonl_path: str | Path, sqlite_path: str) -> None:
        self._jsonl = Path(jsonl_path)
        self._db = sqlite_path
        # Materialized at build() time so the W2 critic node's
        # check_evidence_chunk_in_corpus rule can resolve known chunk_ids
        # synchronously without a per-request SQL hit.
        self._chunk_ids: frozenset[str] = frozenset()
        # Dense path: chunk_id → embedding vector. Populated at build()
        # if dense retrieval is enabled. Empty dict means dense is off
        # (or build skipped embedding due to API failure / disabled flag).
        self._embeddings: dict[str, list[float]] = {}
        # Mirror of the per-chunk row data so dense search doesn't need
        # a SQL lookup per hit. Keyed by chunk_id.
        self._rows: dict[str, dict[str, object]] = {}

    async def build(self) -> None:
        loaded_ids: list[str] = []
        rows: list[dict[str, object]] = []
        async with aiosqlite.connect(self._db) as db:
            await db.executescript(_SCHEMA)
            await db.execute("DELETE FROM guidelines")  # idempotent rebuilds
            for line in self._jsonl.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                loaded_ids.append(row["chunk_id"])
                rows.append(row)
                await db.execute(
                    """
                    INSERT INTO guidelines
                      (chunk_id, source, section, text, url, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["chunk_id"],
                        row["source"],
                        row["section"],
                        row["text"],
                        row["url"],
                        row.get("last_updated", ""),
                    ),
                )
            await db.commit()
        self._chunk_ids = frozenset(loaded_ids)
        self._rows = {r["chunk_id"]: r for r in rows}

        # Dense embedding step. Skipped (a) when the kill-switch flag is
        # off OR (b) when the embedding API call fails — either way the
        # BM25 path keeps working unchanged. We only catch the embedding
        # failure here; configuration validity errors propagate.
        settings = get_settings()
        if settings.copilot_dense_retrieval_enabled and rows:
            try:
                vectors = embed_batch([str(r["text"]) for r in rows])
                self._embeddings = dict(zip(loaded_ids, vectors))
                logger.info(
                    "corpus dense embeddings built: %d chunks", len(vectors)
                )
            except Exception as e:  # noqa: BLE001 — fail-soft to BM25
                logger.warning(
                    "dense embedding build failed (BM25-only fallback): %s", e
                )
                self._embeddings = {}

    def known_chunk_ids(self) -> frozenset[str]:
        """Return the set of chunk_ids loaded into this corpus.

        Used by the W2 critic node to validate ``Guideline/{chunk_id}``
        citations. Returns an empty frozenset before ``build()`` runs.
        """
        return self._chunk_ids

    async def search(self, query: str, *, top_k: int = 5) -> list[GuidelineHit]:
        """Return the top_k best-matching chunks for ``query``.

        Behavior depends on ``settings.copilot_dense_retrieval_enabled``:
        - **Off (default):** BM25 only. Identical to the W2 MVP path.
        - **On:** BM25 + dense in parallel, fused via reciprocal rank
          fusion (RRF, k=60), truncated to top_k. Dense failure
          (embedding API down) silently degrades to BM25-only for that
          one query.
        """
        settings = get_settings()
        # Per-path candidate count. Each path returns top-N before fusion.
        # We need MORE candidates than top_k from each path so the fusion
        # has room to combine. dense_top_k bounds both.
        per_path_k = max(top_k, settings.copilot_dense_retrieval_top_k)

        bm25_hits = await self._bm25_search(query, top_k=per_path_k)

        if not settings.copilot_dense_retrieval_enabled or not self._embeddings:
            return bm25_hits[:top_k]

        try:
            dense_hits = self._dense_search(query, top_k=per_path_k)
        except Exception as e:  # noqa: BLE001 — fail-soft to BM25 only
            logger.warning(
                "dense search failed (BM25-only for this query): %s", e
            )
            return bm25_hits[:top_k]

        return self._rrf_fuse(bm25_hits, dense_hits, top_k=top_k)

    async def _bm25_search(self, query: str, *, top_k: int) -> list[GuidelineHit]:
        sql = """
            SELECT chunk_id, source, section, text, url, last_updated,
                   bm25(guidelines) AS score
              FROM guidelines
             WHERE guidelines MATCH ?
          ORDER BY score
             LIMIT ?
        """
        async with aiosqlite.connect(self._db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, (_fts_query(query), top_k))
            rows = await cur.fetchall()

        hits: list[GuidelineHit] = []
        for r in rows:
            try:
                lu = date.fromisoformat(r["last_updated"]) if r["last_updated"] else None
            except ValueError:
                lu = None
            hits.append(
                GuidelineHit(
                    chunk_id=r["chunk_id"],
                    source=r["source"],
                    section=r["section"],
                    text=r["text"],
                    url=r["url"],
                    last_updated=lu,
                    score=-r["score"],
                )
            )
        return hits

    def _dense_search(self, query: str, *, top_k: int) -> list[GuidelineHit]:
        """Cosine over the in-memory chunk embeddings. ``embed_text`` is
        LRU-cached so repeated queries in the same session pay only one
        API roundtrip."""
        q_vec = embed_text(query)
        scored: list[tuple[float, str]] = []
        for chunk_id, vec in self._embeddings.items():
            scored.append((cosine(q_vec, vec), chunk_id))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]

        hits: list[GuidelineHit] = []
        for score, chunk_id in top:
            row = self._rows.get(chunk_id)
            if not row:
                continue
            lu_raw = row.get("last_updated", "")
            try:
                lu = date.fromisoformat(str(lu_raw)) if lu_raw else None
            except ValueError:
                lu = None
            hits.append(
                GuidelineHit(
                    chunk_id=chunk_id,
                    source=str(row.get("source", "")),
                    section=str(row.get("section", "")),
                    text=str(row.get("text", "")),
                    url=str(row.get("url", "")),
                    last_updated=lu,
                    score=score,
                )
            )
        return hits

    @staticmethod
    def _rrf_fuse(
        bm25: list[GuidelineHit],
        dense: list[GuidelineHit],
        *,
        top_k: int,
    ) -> list[GuidelineHit]:
        """Reciprocal Rank Fusion. Score(d) = sum over each list of
        1 / (k + rank). Higher = better. Ties broken by BM25 order."""
        # rank is 1-based per RRF convention.
        rrf: dict[str, float] = {}
        first_seen: dict[str, GuidelineHit] = {}
        for rank, hit in enumerate(bm25, start=1):
            rrf[hit.chunk_id] = rrf.get(hit.chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
            first_seen.setdefault(hit.chunk_id, hit)
        for rank, hit in enumerate(dense, start=1):
            rrf[hit.chunk_id] = rrf.get(hit.chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
            first_seen.setdefault(hit.chunk_id, hit)

        ordered = sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)
        fused: list[GuidelineHit] = []
        for chunk_id, score in ordered[:top_k]:
            base = first_seen[chunk_id]
            # Replace score with the fused RRF score so callers can see
            # the post-fusion ranking. Per-source scores are recoverable
            # from re-running each path if needed.
            fused.append(
                GuidelineHit(
                    chunk_id=base.chunk_id,
                    source=base.source,
                    section=base.section,
                    text=base.text,
                    url=base.url,
                    last_updated=base.last_updated,
                    score=score,
                )
            )
        return fused


def _fts_query(query: str) -> str:
    """Sanitize free-text into an FTS5 MATCH expression — strip punctuation,
    OR-join the surviving tokens. Keeps recall up on short clinical queries."""
    keep = []
    for tok in query.split():
        cleaned = "".join(ch for ch in tok if ch.isalnum() or ch == "-")
        if cleaned and len(cleaned) > 1:
            keep.append(cleaned)
    if not keep:
        return query.strip() or '""'
    return " OR ".join(keep)

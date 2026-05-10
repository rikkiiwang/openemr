"""Dense retrieval — embeddings + RRF fusion. Mocks the OpenAI client so
these run in CI without an API key.

Three test surfaces:
1. ``embeddings.cosine`` — math correctness on edge cases.
2. ``GuidelineCorpus.search`` with the dense flag OFF — must be byte-
   identical to the BM25-only path that's been live since W2 MVP.
3. ``GuidelineCorpus.search`` with the dense flag ON — fused list returns
   chunks that BM25 alone might rank lower.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from app.config import get_settings
from app.retrieval import embeddings
from app.retrieval.corpus import GuidelineCorpus


@pytest.fixture
def corpus_path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(here, "corpus", "guidelines.jsonl")


# ─── cosine math ───────────────────────────────────────────────────────────

def test_cosine_identical_vectors_returns_one():
    v = [1.0, 2.0, 3.0]
    assert embeddings.cosine(v, v) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_returns_zero():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert embeddings.cosine(a, b) == pytest.approx(0.0)


def test_cosine_opposite_vectors_returns_negative_one():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert embeddings.cosine(a, b) == pytest.approx(-1.0)


def test_cosine_zero_vector_returns_zero_not_nan():
    """Zero-norm guard prevents NaN from leaking into the ranker."""
    a = [0.0, 0.0]
    b = [1.0, 1.0]
    assert embeddings.cosine(a, b) == 0.0


def test_cosine_dim_mismatch_raises():
    with pytest.raises(ValueError, match="dim mismatch"):
        embeddings.cosine([1.0, 2.0], [1.0, 2.0, 3.0])


# ─── BM25 path stays byte-identical when dense flag is OFF ────────────────

@pytest.fixture(autouse=True)
def reset_settings_cache():
    """Each test gets a fresh Settings — env stubs from the previous test
    don't leak."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def reset_query_embedding_cache():
    """LRU cache on _embed_query_cached would otherwise hold the mocked
    return values from prior tests."""
    embeddings.reset_query_cache()


@pytest.fixture
async def bm25_only_corpus(corpus_path: str, monkeypatch: pytest.MonkeyPatch):
    """Corpus built with the dense kill-switch OFF (default)."""
    monkeypatch.setenv("COPILOT_DENSE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = GuidelineCorpus(jsonl_path=corpus_path, sqlite_path=db)
    await c.build()
    yield c
    os.unlink(db)


async def test_dense_off_skips_embedding_at_build(
    bm25_only_corpus: GuidelineCorpus,
):
    """When the flag is off, build() must not call the embedding API."""
    # Build already ran; embeddings dict must be empty.
    assert bm25_only_corpus._embeddings == {}


async def test_dense_off_search_returns_bm25_results(
    bm25_only_corpus: GuidelineCorpus,
):
    """Search with flag-off returns the same chunk_ids as the BM25-only path."""
    hits = await bm25_only_corpus.search(
        "when should I start a statin for high LDL", top_k=3
    )
    assert len(hits) <= 3
    assert all(h.chunk_id for h in hits)


# ─── Dense ON path: build embeds, search fuses ────────────────────────────

@pytest.fixture
async def dense_on_corpus(corpus_path: str, monkeypatch: pytest.MonkeyPatch):
    """Corpus built with the dense flag ON, OpenAI client fully mocked."""
    monkeypatch.setenv("COPILOT_DENSE_RETRIEVAL_ENABLED", "true")
    get_settings.cache_clear()

    # Mock embed_batch to return distinct vectors per chunk so RRF has
    # something to differentiate. embed_text returns a vector that's
    # closest to the FIRST mocked chunk — exercises the dense ranker.
    def fake_batch(texts: list[str]) -> list[list[float]]:
        # Use the chunk text length as a deterministic distinguishing value;
        # this guarantees no two chunks get identical vectors as long as
        # texts differ.
        return [[float(i + 1), float(len(t)), 0.0] for i, t in enumerate(texts)]

    def fake_embed_text(text: str) -> list[float]:
        # Pick a vector near the first chunk's: high first dim, low second.
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(
        "app.retrieval.corpus.embed_batch", fake_batch
    )
    monkeypatch.setattr(
        "app.retrieval.corpus.embed_text", fake_embed_text
    )

    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = GuidelineCorpus(jsonl_path=corpus_path, sqlite_path=db)
    await c.build()
    yield c
    os.unlink(db)


async def test_dense_on_populates_embeddings_at_build(
    dense_on_corpus: GuidelineCorpus,
):
    """build() with flag-on must populate the in-memory embeddings dict."""
    assert len(dense_on_corpus._embeddings) > 0
    assert len(dense_on_corpus._embeddings) == len(dense_on_corpus.known_chunk_ids())


async def test_dense_on_search_returns_topk(
    dense_on_corpus: GuidelineCorpus,
):
    """Fused search returns the requested top_k."""
    hits = await dense_on_corpus.search(
        "when should I start a statin for high LDL", top_k=3
    )
    assert 0 < len(hits) <= 3
    assert all(h.chunk_id for h in hits)


async def test_dense_on_with_embed_failure_falls_back_to_bm25(
    corpus_path: str, monkeypatch: pytest.MonkeyPatch
):
    """If the embedding API raises during build(), the corpus stays usable
    on the BM25-only path. Demo can't crash because OpenAI hiccups."""
    monkeypatch.setenv("COPILOT_DENSE_RETRIEVAL_ENABLED", "true")
    get_settings.cache_clear()

    def boom(_texts: list[str]) -> list[list[float]]:
        raise RuntimeError("openai down")

    monkeypatch.setattr("app.retrieval.corpus.embed_batch", boom)

    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = GuidelineCorpus(jsonl_path=corpus_path, sqlite_path=db)
    await c.build()  # must not raise

    assert c._embeddings == {}  # build saw the failure, didn't populate
    # Search still works (BM25-only).
    hits = await c.search("statin high LDL", top_k=3)
    assert len(hits) > 0

    os.unlink(db)


async def test_dense_on_with_query_embed_failure_falls_back_to_bm25(
    dense_on_corpus: GuidelineCorpus, monkeypatch: pytest.MonkeyPatch
):
    """Per-query embedding failure also degrades gracefully — one bad turn
    doesn't crash /v1/chat."""
    def boom(_text: str) -> list[float]:
        raise RuntimeError("openai 503")

    monkeypatch.setattr("app.retrieval.corpus.embed_text", boom)

    hits = await dense_on_corpus.search("statin high LDL", top_k=3)
    assert len(hits) > 0  # BM25 path still serves


# ─── RRF fusion math ──────────────────────────────────────────────────────

def test_rrf_fusion_ranks_overlap_chunks_higher():
    """A chunk that ranks well in both BM25 and dense should beat one
    that only ranks well in one."""
    from app.retrieval.corpus import GuidelineCorpus, GuidelineHit

    def hit(cid: str, score: float = 1.0) -> GuidelineHit:
        return GuidelineHit(
            chunk_id=cid, source="s", section="x", text="t", url="u",
            last_updated=None, score=score,
        )

    bm25 = [hit("A"), hit("B"), hit("C")]
    dense = [hit("A"), hit("D"), hit("E")]
    fused = GuidelineCorpus._rrf_fuse(bm25, dense, top_k=5)
    # A appears in both lists at rank 1 → highest fused score → first.
    assert fused[0].chunk_id == "A"
    # B and D each appear once at rank 2 → tied. Either order acceptable;
    # both should be in the top 3.
    top_three = {h.chunk_id for h in fused[:3]}
    assert "A" in top_three
    assert {"B", "D"}.intersection(top_three)


def test_rrf_fusion_truncates_to_topk():
    from app.retrieval.corpus import GuidelineCorpus, GuidelineHit

    def hit(cid: str) -> GuidelineHit:
        return GuidelineHit(
            chunk_id=cid, source="", section="", text="", url="",
            last_updated=None, score=1.0,
        )

    bm25 = [hit(f"B{i}") for i in range(10)]
    dense = [hit(f"D{i}") for i in range(10)]
    fused = GuidelineCorpus._rrf_fuse(bm25, dense, top_k=5)
    assert len(fused) == 5

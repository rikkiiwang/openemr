"""Unit tests for TtlSingleFlightCache.

Tests the cache module in isolation — no FHIR / httpx involvement.
The cache takes any fetcher coroutine, so we pass plain async functions
that count their invocations.
"""
from __future__ import annotations

import pytest

from app.fhir.cache import TtlSingleFlightCache


@pytest.mark.asyncio
async def test_ttl_hit_returns_cached_without_invoking_fetcher_twice():
    cache = TtlSingleFlightCache(ttl_seconds=60, max_entries=10)
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        return {"data": "value"}

    first = await cache.get_or_fetch(("k",), fetcher)
    second = await cache.get_or_fetch(("k",), fetcher)

    assert first == {"data": "value"}
    assert second == {"data": "value"}
    assert calls == 1


@pytest.mark.asyncio
async def test_ttl_miss_after_expiry_re_invokes_fetcher(monkeypatch):
    cache = TtlSingleFlightCache(ttl_seconds=1, max_entries=10)
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        return {"call": calls}

    # Use a controllable monotonic clock so we don't sleep in tests.
    fake_now = [1000.0]

    def fake_monotonic():
        return fake_now[0]

    monkeypatch.setattr("app.fhir.cache.time.monotonic", fake_monotonic)

    first = await cache.get_or_fetch(("k",), fetcher)
    fake_now[0] += 0.5  # Inside TTL.
    second = await cache.get_or_fetch(("k",), fetcher)
    fake_now[0] += 1.0  # Now past 1s TTL.
    third = await cache.get_or_fetch(("k",), fetcher)

    assert first == {"call": 1}
    assert second == {"call": 1}  # cache hit
    assert third == {"call": 2}   # cache miss
    assert calls == 2

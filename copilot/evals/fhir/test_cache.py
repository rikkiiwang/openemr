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

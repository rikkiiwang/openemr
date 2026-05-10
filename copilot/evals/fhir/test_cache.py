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


@pytest.mark.asyncio
async def test_lru_evicts_oldest_when_bound_exceeded():
    cache = TtlSingleFlightCache(ttl_seconds=60, max_entries=2)
    calls: dict[str, int] = {"a": 0, "b": 0, "c": 0}

    def make_fetcher(name: str):
        async def fetcher():
            calls[name] += 1
            return name
        return fetcher

    # Fill cache to bound.
    await cache.get_or_fetch(("a",), make_fetcher("a"))
    await cache.get_or_fetch(("b",), make_fetcher("b"))
    # Adding ("c",) should evict ("a",) (LRU).
    await cache.get_or_fetch(("c",), make_fetcher("c"))

    # Re-fetching "a" should miss and re-invoke its fetcher.
    await cache.get_or_fetch(("a",), make_fetcher("a"))

    assert calls["a"] == 2  # Fetched again after eviction.
    assert calls["b"] == 1
    assert calls["c"] == 1


@pytest.mark.asyncio
async def test_single_flight_concurrent_misses_share_one_fetch():
    import asyncio

    cache = TtlSingleFlightCache(ttl_seconds=60, max_entries=10)
    calls = 0

    async def slow_fetcher():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"shared": True}

    # Two concurrent gets for the same key should share a single fetch.
    a, b = await asyncio.gather(
        cache.get_or_fetch(("k",), slow_fetcher),
        cache.get_or_fetch(("k",), slow_fetcher),
    )

    assert a == {"shared": True}
    assert b == {"shared": True}
    assert calls == 1


@pytest.mark.asyncio
async def test_fetcher_error_propagates_and_does_not_cache():
    cache = TtlSingleFlightCache(ttl_seconds=60, max_entries=10)
    calls = 0

    async def boom():
        nonlocal calls
        calls += 1
        raise RuntimeError("upstream blew up")

    with pytest.raises(RuntimeError, match="upstream blew up"):
        await cache.get_or_fetch(("k",), boom)

    # Subsequent calls should re-invoke the fetcher (key was NOT cached).
    with pytest.raises(RuntimeError, match="upstream blew up"):
        await cache.get_or_fetch(("k",), boom)

    assert calls == 2


@pytest.mark.asyncio
async def test_fetcher_error_propagates_to_concurrent_awaiters():
    import asyncio

    cache = TtlSingleFlightCache(ttl_seconds=60, max_entries=10)
    calls = 0

    async def slow_boom():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        raise RuntimeError("slow boom")

    # Two concurrent awaiters: both must see the exception. The single-flight
    # primary call propagates via raise; the awaiter receives via the Future.
    with pytest.raises(RuntimeError, match="slow boom"):
        await asyncio.gather(
            cache.get_or_fetch(("k",), slow_boom),
            cache.get_or_fetch(("k",), slow_boom),
        )

    # Fetcher invoked exactly once even though both raised.
    assert calls == 1


@pytest.mark.asyncio
async def test_two_physicians_get_separate_cache_entries():
    cache = TtlSingleFlightCache(ttl_seconds=60, max_entries=10)
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        return {"call": calls}

    # Same patient, different physicians → keys must differ → fetcher
    # invoked twice. This is the panel-scope safety contract.
    await cache.get_or_fetch(("get", "Patient", "abc-123", "dr_alvarez"), fetcher)
    await cache.get_or_fetch(("get", "Patient", "abc-123", "dr_chen"), fetcher)

    assert calls == 2


@pytest.mark.asyncio
async def test_ttl_zero_bypasses_cache_entirely():
    cache = TtlSingleFlightCache(ttl_seconds=0, max_entries=10)
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        return {"v": calls}

    a = await cache.get_or_fetch(("k",), fetcher)
    b = await cache.get_or_fetch(("k",), fetcher)

    # Both calls invoke the fetcher; nothing is cached.
    assert a == {"v": 1}
    assert b == {"v": 2}
    assert calls == 2


@pytest.mark.asyncio
async def test_fhir_client_get_resource_caches_when_ttl_positive():
    """End-to-end: same get_resource call twice in a row hits the cache."""
    from app.config import Settings
    from app.fhir.client import FhirClient

    settings = Settings(
        copilot_fhir_cache_ttl_seconds=60,
        openemr_fhir_base="https://example.invalid/fhir",
    )
    client = FhirClient(settings)

    calls = 0

    async def fake_do_get_resource(resource_type, resource_id, physician_user_id):
        nonlocal calls
        calls += 1
        return {"resourceType": resource_type, "id": resource_id, "call": calls}

    # Replace the inner fetcher with a counter so we don't need an HTTP server.
    client._do_get_resource = fake_do_get_resource  # type: ignore[assignment]

    a = await client.get_resource("Patient", "abc", physician_user_id="dr_alvarez")
    b = await client.get_resource("Patient", "abc", physician_user_id="dr_alvarez")

    assert a["call"] == 1
    assert b["call"] == 1  # cache hit — fetcher invoked once
    assert calls == 1

    await client.aclose()


@pytest.mark.asyncio
async def test_fhir_client_get_resource_skips_cache_when_ttl_zero():
    from app.config import Settings
    from app.fhir.client import FhirClient

    settings = Settings(
        copilot_fhir_cache_ttl_seconds=0,
        openemr_fhir_base="https://example.invalid/fhir",
    )
    client = FhirClient(settings)
    calls = 0

    async def fake_do_get_resource(resource_type, resource_id, physician_user_id):
        nonlocal calls
        calls += 1
        return {"call": calls}

    client._do_get_resource = fake_do_get_resource  # type: ignore[assignment]

    await client.get_resource("Patient", "abc", physician_user_id="dr_alvarez")
    await client.get_resource("Patient", "abc", physician_user_id="dr_alvarez")

    assert calls == 2  # No cache; both calls hit the fetcher.

    await client.aclose()

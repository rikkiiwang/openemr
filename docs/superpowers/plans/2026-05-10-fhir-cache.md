# FHIR Per-Tenant Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut UC1 second-turn latency from ~18s to ~3-5s on Railway prod by caching FHIR reads for 60 seconds, scoped per (resource, query, physician), with single-flight deduping of concurrent in-flight requests.

**Architecture:** One new module `copilot/app/fhir/cache.py` defines `TtlSingleFlightCache` (TTL response cache + in-flight Promise cache in one class). `FhirClient.get_resource` and `FhirClient.search` route reads through the cache when `COPILOT_FHIR_CACHE_TTL_SECONDS > 0`; writes bypass entirely. Cache key includes `physician_user_id` so panel-scope safety is preserved. Kill-switch: set TTL env to `0`.

**Tech Stack:** Python 3.11, asyncio, httpx, pydantic-settings, pytest, pytest-asyncio. Project conventions: `from __future__ import annotations`, type hints everywhere, ruff-clean.

**Spec:** `docs/superpowers/specs/2026-05-10-fhir-cache-design.md`

---

## File Structure

| File | Purpose | New / Modified |
|---|---|---|
| `copilot/app/fhir/cache.py` | `TtlSingleFlightCache` class | New (~120 LOC) |
| `copilot/app/fhir/client.py` | Wrap `get_resource` + `search` with cache | Modified (~+15 / -2 LOC at lines 28-83) |
| `copilot/app/config.py` | Two new settings fields | Modified (~+3 LOC) |
| `copilot/evals/fhir/test_cache.py` | Cache module unit tests | New (~150 LOC) |
| `copilot/COST.md` | Append §10 documenting cache + kill-switch | Modified (~+20 LOC) |
| `copilot/W2_IMPLEMENTATION.md` | TL;DR row noting cache shipped | Modified (~+5 LOC) |

Each file has one responsibility. Cache lives in its own module so tests don't need an `httpx` mock — they pass a fake fetcher coroutine directly.

---

## Pre-flight checks

Before starting:

- [ ] Verify the FHIR client signatures haven't drifted from this plan: `grep -n "async def get_resource\|async def search" copilot/app/fhir/client.py`. Expected output includes `async def get_resource(` at line 45 and `async def search(` at line 64. If line numbers differ but the signatures are intact, the plan still applies.
- [ ] Verify `copilot/evals/fhir/` directory exists. If not, create it: `mkdir -p copilot/evals/fhir && touch copilot/evals/fhir/__init__.py`.
- [ ] Confirm tests run locally in the project's virtualenv: `cd copilot && python -m pytest evals/ -q --co | tail -5` should list collected tests without error.

---

## Task 1: Add config settings

**Files:**
- Modify: `copilot/app/config.py` — add two fields after the existing `copilot_front_desk_users` field

- [ ] **Step 1: Find the insertion point**

```bash
grep -n "copilot_front_desk_users" copilot/app/config.py
```
Expected: one line near `copilot_front_desk_users: str = ""`. Insert immediately after that line.

- [ ] **Step 2: Add the two fields**

Add these lines immediately after the `copilot_front_desk_users` line:

```python
    # FHIR per-tenant cache (2026-05-10). When ttl_seconds > 0, FhirClient
    # routes get_resource and search through TtlSingleFlightCache. Set to 0
    # to disable (kill-switch). max_entries bounds memory; LRU evicts at
    # the bound.
    copilot_fhir_cache_ttl_seconds: int = 60
    copilot_fhir_cache_max_entries: int = 1000
```

- [ ] **Step 3: Verify import + parse**

```bash
cd copilot && python -c "from app.config import Settings; s = Settings(); print(s.copilot_fhir_cache_ttl_seconds, s.copilot_fhir_cache_max_entries)"
```
Expected: `60 1000`.

- [ ] **Step 4: Commit**

```bash
cd copilot && git add app/config.py && cd ..
git commit -m "feat(copilot): add COPILOT_FHIR_CACHE_TTL_SECONDS + max_entries settings

Two pydantic-settings fields for the upcoming FHIR per-tenant cache.
Defaults: 60s TTL, 1000-entry LRU bound. TTL=0 disables the cache.

Assisted-By: Claude Code"
```

---

## Task 2: Create cache module skeleton with first failing test (TTL hit)

**Files:**
- Create: `copilot/app/fhir/cache.py`
- Create: `copilot/evals/fhir/test_cache.py`

- [ ] **Step 1: Write the failing test**

Create `copilot/evals/fhir/test_cache.py`:

```python
"""Unit tests for TtlSingleFlightCache.

Tests the cache module in isolation — no FHIR / httpx involvement.
The cache takes any fetcher coroutine, so we pass plain async functions
that count their invocations.
"""
from __future__ import annotations

import asyncio

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
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd copilot && python -m pytest evals/fhir/test_cache.py -v
```
Expected: `ImportError` or `ModuleNotFoundError` for `app.fhir.cache`.

- [ ] **Step 3: Create the cache module with minimal implementation**

Create `copilot/app/fhir/cache.py`:

```python
"""In-process TTL + single-flight cache for FHIR reads.

Two roles bundled in one class:

1. **TTL response cache** — `(key → (value, expires_at))`. Lazy eviction
   on read. LRU bound at ``max_entries``.
2. **In-flight Promise cache** — `(key → asyncio.Future)`, evicted on
   settle. Two concurrent calls for the same key share a single upstream
   fetch. Mirrors the dashboard's tokenStore.refresh single-flight
   pattern (see frontend/lib/auth/token-store.ts).

Set ``ttl_seconds=0`` to bypass both caches entirely — every call invokes
the fetcher directly. This is the kill-switch path
(COPILOT_FHIR_CACHE_TTL_SECONDS=0).
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable


class TtlSingleFlightCache:
    def __init__(self, ttl_seconds: int, max_entries: int = 1000) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._ttl: OrderedDict[tuple, tuple[Any, float]] = OrderedDict()
        self._inflight: dict[tuple, asyncio.Future] = {}

    async def get_or_fetch(
        self,
        key: tuple,
        fetcher: Callable[[], Awaitable[Any]],
    ) -> Any:
        if self._ttl_seconds <= 0:
            return await fetcher()

        now = time.monotonic()
        cached = self._ttl.get(key)
        if cached is not None:
            value, expires_at = cached
            if expires_at > now:
                self._ttl.move_to_end(key)
                return value
            del self._ttl[key]

        return await fetcher()
```

- [ ] **Step 4: Run the test to verify the first behavior (TTL hit) — but it will still fail**

```bash
cd copilot && python -m pytest evals/fhir/test_cache.py -v
```
Expected: FAIL — `assert calls == 1` will fail because the minimal impl above doesn't yet store anything in `self._ttl`. The test exposes the missing put-after-fetch step.

- [ ] **Step 5: Add the put-after-fetch step**

Replace the body of `get_or_fetch` in `copilot/app/fhir/cache.py` with:

```python
    async def get_or_fetch(
        self,
        key: tuple,
        fetcher: Callable[[], Awaitable[Any]],
    ) -> Any:
        if self._ttl_seconds <= 0:
            return await fetcher()

        now = time.monotonic()
        cached = self._ttl.get(key)
        if cached is not None:
            value, expires_at = cached
            if expires_at > now:
                self._ttl.move_to_end(key)
                return value
            del self._ttl[key]

        result = await fetcher()
        self._ttl[key] = (result, time.monotonic() + self._ttl_seconds)
        self._ttl.move_to_end(key)
        return result
```

- [ ] **Step 6: Run the test to verify it passes**

```bash
cd copilot && python -m pytest evals/fhir/test_cache.py -v
```
Expected: PASS — 1/1 test passes.

- [ ] **Step 7: Commit**

```bash
git add copilot/app/fhir/cache.py copilot/evals/fhir/test_cache.py
git commit -m "feat(copilot): TtlSingleFlightCache skeleton with TTL hit path

Cache stores (key -> (value, expires_at)) in an OrderedDict for LRU.
get_or_fetch checks the TTL cache first; on miss it invokes the fetcher
and populates. ttl_seconds=0 bypasses the cache entirely (kill-switch).

Single-flight, LRU eviction, error propagation, and TTL expiry land in
follow-up commits to keep each step bisectable.

Assisted-By: Claude Code"
```

---

## Task 3: TTL expiry test

**Files:**
- Modify: `copilot/evals/fhir/test_cache.py` — add second test

- [ ] **Step 1: Write the failing test**

Append to `copilot/evals/fhir/test_cache.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it passes**

The minimal implementation from Task 2 already supports TTL expiry (`if expires_at > now`). The test should pass with no implementation change.

```bash
cd copilot && python -m pytest evals/fhir/test_cache.py -v
```
Expected: PASS — 2/2 tests pass.

- [ ] **Step 3: Commit**

```bash
git add copilot/evals/fhir/test_cache.py
git commit -m "test(copilot): add TTL expiry regression test for TtlSingleFlightCache

Pin the lazy-eviction-on-read behavior so future cache changes can't
silently keep returning stale values past TTL.

Assisted-By: Claude Code"
```

---

## Task 4: LRU eviction

**Files:**
- Modify: `copilot/app/fhir/cache.py` — add LRU bound enforcement
- Modify: `copilot/evals/fhir/test_cache.py` — add LRU test

- [ ] **Step 1: Write the failing test**

Append to `copilot/evals/fhir/test_cache.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd copilot && python -m pytest evals/fhir/test_cache.py -v
```
Expected: FAIL on `assert calls["a"] == 2` (currently 1 because the cache has no LRU bound).

- [ ] **Step 3: Add LRU bound enforcement**

In `copilot/app/fhir/cache.py`, replace the `get_or_fetch` method body so the put-after-fetch step trims to the bound. Replace the existing method with:

```python
    async def get_or_fetch(
        self,
        key: tuple,
        fetcher: Callable[[], Awaitable[Any]],
    ) -> Any:
        if self._ttl_seconds <= 0:
            return await fetcher()

        now = time.monotonic()
        cached = self._ttl.get(key)
        if cached is not None:
            value, expires_at = cached
            if expires_at > now:
                self._ttl.move_to_end(key)
                return value
            del self._ttl[key]

        result = await fetcher()
        self._ttl[key] = (result, time.monotonic() + self._ttl_seconds)
        self._ttl.move_to_end(key)
        while len(self._ttl) > self._max_entries:
            self._ttl.popitem(last=False)  # evict LRU (oldest)
        return result
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd copilot && python -m pytest evals/fhir/test_cache.py -v
```
Expected: PASS — 3/3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add copilot/app/fhir/cache.py copilot/evals/fhir/test_cache.py
git commit -m "feat(copilot): LRU-bound eviction on TtlSingleFlightCache writes

When the TTL cache exceeds max_entries, evict oldest entries via
OrderedDict.popitem(last=False). Bound is 1000 by default
(~2MB worst case at 2KB/entry).

Assisted-By: Claude Code"
```

---

## Task 5: Single-flight in-flight cache

**Files:**
- Modify: `copilot/app/fhir/cache.py` — add in-flight Promise cache
- Modify: `copilot/evals/fhir/test_cache.py` — add single-flight test

- [ ] **Step 1: Write the failing test**

Append to `copilot/evals/fhir/test_cache.py`:

```python
@pytest.mark.asyncio
async def test_single_flight_concurrent_misses_share_one_fetch():
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
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd copilot && python -m pytest evals/fhir/test_cache.py -v
```
Expected: FAIL on `assert calls == 1` (currently 2 — both concurrent calls miss the cache and dispatch).

- [ ] **Step 3: Add in-flight Promise cache**

Replace `get_or_fetch` in `copilot/app/fhir/cache.py` with:

```python
    async def get_or_fetch(
        self,
        key: tuple,
        fetcher: Callable[[], Awaitable[Any]],
    ) -> Any:
        if self._ttl_seconds <= 0:
            return await fetcher()

        now = time.monotonic()
        cached = self._ttl.get(key)
        if cached is not None:
            value, expires_at = cached
            if expires_at > now:
                self._ttl.move_to_end(key)
                return value
            del self._ttl[key]

        # Single-flight: piggyback on any in-flight fetch for the same key.
        inflight = self._inflight.get(key)
        if inflight is not None:
            return await inflight

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._inflight[key] = future
        try:
            result = await fetcher()
            self._ttl[key] = (result, time.monotonic() + self._ttl_seconds)
            self._ttl.move_to_end(key)
            while len(self._ttl) > self._max_entries:
                self._ttl.popitem(last=False)
            future.set_result(result)
            return result
        except Exception as e:
            if not future.done():
                future.set_exception(e)
            raise
        finally:
            self._inflight.pop(key, None)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd copilot && python -m pytest evals/fhir/test_cache.py -v
```
Expected: PASS — 4/4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add copilot/app/fhir/cache.py copilot/evals/fhir/test_cache.py
git commit -m "feat(copilot): single-flight in-flight Promise cache

Concurrent calls for the same key share a single upstream fetch.
The first caller installs an asyncio.Future in self._inflight; later
callers await it. On settle, the entry is evicted regardless of
success or failure.

Closes the within-turn duplicate-dispatch race that caused two
get_recent_labs calls (prewarm + agent) to both pay the FHIR
round-trip on a cold cache.

Assisted-By: Claude Code"
```

---

## Task 6: Error propagation

**Files:**
- Modify: `copilot/evals/fhir/test_cache.py` — add error-path test

- [ ] **Step 1: Write the failing test**

Append to `copilot/evals/fhir/test_cache.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they pass**

The `try/except/finally` in Task 5 already implements error propagation correctly. The tests should pass without further code changes.

```bash
cd copilot && python -m pytest evals/fhir/test_cache.py -v
```
Expected: PASS — 6/6 tests pass.

- [ ] **Step 3: Commit**

```bash
git add copilot/evals/fhir/test_cache.py
git commit -m "test(copilot): pin error-propagation behavior of TtlSingleFlightCache

Two regression tests:
- Error from a single fetcher propagates and does not cache the key.
- Error from a concurrent fetch propagates to all awaiters via the
  in-flight Future.

Both pin invariants future cache changes might silently break.

Assisted-By: Claude Code"
```

---

## Task 7: Two-physicians + TTL=0 bypass regression tests

**Files:**
- Modify: `copilot/evals/fhir/test_cache.py` — add panel-scope safety + kill-switch tests

- [ ] **Step 1: Write the failing test**

Append to `copilot/evals/fhir/test_cache.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they pass**

The two-physicians test relies on the cache treating different keys as separate entries (already true). The TTL=0 test relies on the early-return at the top of `get_or_fetch` (also already true). Both should pass with no implementation change.

```bash
cd copilot && python -m pytest evals/fhir/test_cache.py -v
```
Expected: PASS — 8/8 tests pass.

- [ ] **Step 3: Commit**

```bash
git add copilot/evals/fhir/test_cache.py
git commit -m "test(copilot): pin panel-scope safety + TTL=0 kill-switch behavior

Two regression tests:
- Same patient + different physicians produce different cache keys
  (panel-scope leak prevention).
- ttl_seconds=0 bypasses the cache entirely so the kill-switch path
  is identity to the unwrapped fetcher.

Both pin contracts callers depend on. Without these, future cache
refactors could silently leak between physicians or partially honor
the kill-switch.

Assisted-By: Claude Code"
```

---

## Task 8: Wire cache into FhirClient

**Files:**
- Modify: `copilot/app/fhir/client.py:28-83` — wrap `get_resource` and `search`
- Modify: `copilot/evals/fhir/test_cache.py` — integration test against a stub FhirClient (optional but recommended)

- [ ] **Step 1: Write a failing integration test**

Append to `copilot/evals/fhir/test_cache.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd copilot && python -m pytest evals/fhir/test_cache.py -v
```
Expected: FAIL — `AttributeError: 'FhirClient' object has no attribute '_do_get_resource'` (the rename hasn't happened yet).

- [ ] **Step 3: Modify FhirClient to wrap get_resource and search**

Open `copilot/app/fhir/client.py`. The current `get_resource` is at lines 45-62 and `search` is at lines 64-83. Make the following changes:

**3a.** Add the cache import at the top of the file, after the existing imports:

```python
from app.fhir.cache import TtlSingleFlightCache
```

**3b.** Modify `__init__` to construct the cache. Replace the body of `__init__` with:

```python
    def __init__(self, settings: Settings):
        self._settings = settings
        self._oauth = FhirOAuthClient(settings)
        self._http = httpx.AsyncClient(
            timeout=settings.fhir_timeout_seconds,
            headers={"Accept": "application/fhir+json"},
            verify=settings.openemr_verify_tls,
        )
        ttl = settings.copilot_fhir_cache_ttl_seconds
        self._cache: TtlSingleFlightCache | None = (
            TtlSingleFlightCache(
                ttl_seconds=ttl,
                max_entries=settings.copilot_fhir_cache_max_entries,
            )
            if ttl > 0
            else None
        )
```

**3c.** Rename the existing `get_resource` body to `_do_get_resource` and add a new wrapper. Replace the existing `get_resource` method (lines 45-62) with these two methods:

```python
    async def get_resource(
        self,
        resource_type: str,
        resource_id: str,
        *,
        physician_user_id: str,
    ) -> dict[str, Any]:
        if self._cache is None:
            return await self._do_get_resource(resource_type, resource_id, physician_user_id)
        key = ("get", resource_type, resource_id, physician_user_id)
        return await self._cache.get_or_fetch(
            key,
            lambda: self._do_get_resource(resource_type, resource_id, physician_user_id),
        )

    async def _do_get_resource(
        self,
        resource_type: str,
        resource_id: str,
        physician_user_id: str,
    ) -> dict[str, Any]:
        url = f"{self._settings.openemr_fhir_base}/{resource_type}/{resource_id}"
        try:
            r = await self._http.get(url, headers=await self._headers(physician_user_id))
        except httpx.TimeoutException as e:
            raise FhirError(f"FHIR timeout on {resource_type}/{resource_id}") from e
        if r.status_code in (401, 403):
            raise FhirError(f"FHIR access denied on {resource_type}", status=r.status_code)
        if r.status_code == 404:
            raise FhirError(f"{resource_type}/{resource_id} not found", status=404)
        r.raise_for_status()
        return r.json()
```

**3d.** Same pattern for `search`. Replace the existing `search` method (lines 64-83) with:

```python
    async def search(
        self,
        resource_type: str,
        params: dict[str, Any],
        *,
        physician_user_id: str,
    ) -> dict[str, Any]:
        if self._cache is None:
            return await self._do_search(resource_type, params, physician_user_id)
        key = ("search", resource_type, tuple(sorted(params.items())), physician_user_id)
        return await self._cache.get_or_fetch(
            key,
            lambda: self._do_search(resource_type, params, physician_user_id),
        )

    async def _do_search(
        self,
        resource_type: str,
        params: dict[str, Any],
        physician_user_id: str,
    ) -> dict[str, Any]:
        url = f"{self._settings.openemr_fhir_base}/{resource_type}"
        try:
            r = await self._http.get(
                url,
                headers=await self._headers(physician_user_id),
                params=params,
            )
        except httpx.TimeoutException as e:
            raise FhirError(f"FHIR timeout searching {resource_type}") from e
        if r.status_code in (401, 403):
            raise FhirError(f"FHIR access denied on {resource_type}", status=r.status_code)
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 4: Run the integration tests to verify they pass**

```bash
cd copilot && python -m pytest evals/fhir/test_cache.py -v
```
Expected: PASS — 10/10 tests pass.

- [ ] **Step 5: Run the full FHIR test suite to confirm no regressions**

```bash
cd copilot && python -m pytest evals/fhir/ -v
```
Expected: All existing FHIR tests still pass (cache is transparent).

- [ ] **Step 6: Run ruff to confirm lint clean**

```bash
cd copilot && ruff check app/fhir/ evals/fhir/
```
Expected: No errors.

- [ ] **Step 7: Commit**

```bash
git add copilot/app/fhir/client.py copilot/evals/fhir/test_cache.py
git commit -m "feat(copilot): wire TtlSingleFlightCache into FhirClient

FhirClient.get_resource and search now route through the cache when
COPILOT_FHIR_CACHE_TTL_SECONDS > 0. Existing httpx logic moved to
_do_get_resource and _do_search; the cache wraps them via lambdas.

Cache key includes physician_user_id so different physicians get
separate entries (panel-scope safety). Write methods (post_*, create_*)
bypass entirely.

Two integration tests pin the wrapper behavior:
- TTL > 0: second call hits cache, fetcher invoked once.
- TTL = 0: cache disabled, both calls hit fetcher.

Assisted-By: Claude Code"
```

---

## Task 9: Run the full test suite + W2 eval gate

**Files:** none

- [ ] **Step 1: Run the full Co-Pilot test suite**

```bash
cd copilot && python -m pytest evals/ -q
```
Expected: All tests pass (192 prior + 8 new cache tests = 200, ±). Skipped: `live_llm` tests (3, pre-existing).

If anything red that wasn't red before: STOP and triage. Cache must be transparent to existing behavior.

- [ ] **Step 2: Run the W2 hard-gate eval**

```bash
cd copilot && make eval-fast
```
Expected: `15/15 100.0% across all 6 categories`, exit 0.

If the gate fails: the cache is perturbing answer correctness. Triage by setting `COPILOT_FHIR_CACHE_TTL_SECONDS=0` in the test env and re-running — if that fixes it, the bug is in the cache; if not, the bug is unrelated.

- [ ] **Step 3: Run ruff across the whole copilot tree**

```bash
cd copilot && ruff check .
```
Expected: No errors.

- [ ] **Step 4: No commit** (these are verification steps, not code changes)

---

## Task 10: Document in COST.md

**Files:**
- Modify: `copilot/COST.md` — append §10

- [ ] **Step 1: Find the end of the file**

```bash
tail -30 copilot/COST.md
```
Locate the last section heading; the new section appends after it.

- [ ] **Step 2: Append §10**

Append to the end of `copilot/COST.md`:

```markdown

---

## §10 — FHIR per-tenant cache (added 2026-05-10)

Implements §9's "highest-leverage performance lever." In-process
`TtlSingleFlightCache` (60s TTL, LRU bound 1000) wraps
`FhirClient.get_resource` and `search`. Writes bypass.

**Expected impact** (against the §8 baseline):

| Use case | Before (p50) | After (p50, second turn) |
|---|---|---|
| UC1 brief | 18.2s | ~3-5s |
| UC2 meds | 10.0s | ~2-3s |
| UC3 applied guideline | 11.8s | ~3-5s |

First turn after iframe open still pays full FHIR cost (cache cold).
Every subsequent turn within 60s hits cache for resources already
fetched. Within-turn concurrent dispatches share a single upstream
fetch via the in-flight Promise cache.

**Configuration** (env on the `copilot` Railway service):

- `COPILOT_FHIR_CACHE_TTL_SECONDS` — default `60`. Set to `0` to disable
  the cache entirely (kill-switch, no redeploy needed).
- `COPILOT_FHIR_CACHE_MAX_ENTRIES` — default `1000`. Bumpable if memory
  is not the constraint.

**Panel-scope safety:** cache key includes `physician_user_id`, so two
physicians fetching the same patient produce different cache entries.
Verified by `test_two_physicians_get_separate_cache_entries`.

**Out of scope:** cache invalidation on write (60s TTL absorbs);
shared/Redis cache (single-replica only); per-resource TTL tuning
(uniform 60s); streaming / TTFT (verification contract precludes).

See design spec: `docs/superpowers/specs/2026-05-10-fhir-cache-design.md`.
```

- [ ] **Step 3: Commit**

```bash
git add copilot/COST.md
git commit -m "docs(copilot): COST.md §10 — FHIR per-tenant cache

Documents the cache's expected impact on §8 baseline latency,
configuration env vars, panel-scope safety contract, and out-of-scope
deferrals.

Assisted-By: Claude Code"
```

---

## Task 11: Note in W2_IMPLEMENTATION.md TL;DR

**Files:**
- Modify: `copilot/W2_IMPLEMENTATION.md` — TL;DR row

- [ ] **Step 1: Find the TL;DR section**

```bash
grep -n "## TL;DR" copilot/W2_IMPLEMENTATION.md
```

- [ ] **Step 2: Add a bullet about the cache**

In the TL;DR section's bullet list, after the existing `- **Quality at last full-suite verification...` line, add:

```markdown
- **Latency optimization (2026-05-10):** FHIR per-tenant cache shipped (`copilot/app/fhir/cache.py`). 60s TTL keyed by `(resource, query, physician)`. Set `COPILOT_FHIR_CACHE_TTL_SECONDS=0` on the Railway `copilot` service to disable. Spec: `docs/superpowers/specs/2026-05-10-fhir-cache-design.md`. Plan: `docs/superpowers/plans/2026-05-10-fhir-cache.md`.
```

- [ ] **Step 3: Commit**

```bash
git add copilot/W2_IMPLEMENTATION.md
git commit -m "docs(copilot): W2_IMPLEMENTATION TL;DR notes FHIR cache shipped

Single bullet pointing at the cache module + spec + plan + env
kill-switch. Surfaces the change for next-session memory bank pass
without burying it in a phase log.

Assisted-By: Claude Code"
```

---

## Task 12: Manual verification on Railway

**Files:** none — Railway dashboard work

- [ ] **Step 1: Push the branch**

```bash
git push origin HEAD
```

If the branch doesn't have an upstream yet, git will prompt. Use:
```bash
git push -u origin HEAD
```

- [ ] **Step 2: Set the env var on Railway**

1. Go to railway.app → project → `copilot` service → Variables.
2. Add: `COPILOT_FHIR_CACHE_TTL_SECONDS` = `60`
3. (Optional) Add: `COPILOT_FHIR_CACHE_MAX_ENTRIES` = `1000`
4. Railway redeploys automatically. Wait for `/healthz` to return 200.

- [ ] **Step 3: Verify cold first turn still works**

Open a Synthea patient chart. Submit any clinical question. Confirm:
- Answer renders normally.
- Citations work.
- Bbox modal still opens on chip click.

- [ ] **Step 4: Verify cache speedup on second turn**

Without closing the chart, submit the same question again. Compare wall time (DevTools → Network → `/v1/chat`):
- Expected: second turn drops from ~18s to ~3-5s for UC1, ~2-3s for UC2.
- If second turn is NOT faster: check Railway logs for `panel deny` or `cache miss` patterns. The cache may be miskeyed (shouldn't happen after Task 7 tests pass, but live envs differ).

- [ ] **Step 5: Verify Langfuse trace integrity**

Go to cloud.langfuse.com → AgentForge Co-Pilot project → trace for the second turn. Confirm:
- `tool_results[*]` populated normally.
- Citations in answer match `tool_results[*].record_ids`.
- `routing_path` shows `supervisor → answer_composer → critic`.
- No new error logs or PHI substring matches.

- [ ] **Step 6: Verify kill-switch**

In Railway Variables, change `COPILOT_FHIR_CACHE_TTL_SECONDS` from `60` to `0`. Redeploy. Wait for `/healthz`.

Re-run step 4 — second turn should now be back to ~18s (cache disabled).

If kill-switch works as expected, restore `COPILOT_FHIR_CACHE_TTL_SECONDS=60`.

- [ ] **Step 7: Manual log of results**

In a new terminal, write a short note to scratch:

```bash
cat > /tmp/cache-verify-results.txt <<EOF
Date: 2026-05-10
Branch: $(git rev-parse --abbrev-ref HEAD)
Commit: $(git rev-parse HEAD)

UC1 first turn (cold):  ___ s
UC1 second turn (warm): ___ s
UC2 first turn (cold):  ___ s
UC2 second turn (warm): ___ s

Kill-switch verified: yes / no
Notes:
EOF
```

Fill in the actual times. Decide whether to copy the highlights into `copilot/COST.md` §10 as "measured impact" (separate commit if so).

---

## Self-review

Walking the spec section-by-section against the plan:

- **Goals → Tasks:** Goal 1 (cut UC1 second-turn) verified by Tasks 8 + 12. Goal 2 (eliminate within-turn duplicates) verified by Task 5's single-flight test. Goal 3 (zero behavioral change) verified by Tasks 9 + 12. Goal 4 (kill-switchable) verified by Tasks 7 + 8 + 12.
- **Architecture → Tasks:** `cache.py` built incrementally in Tasks 2-7. `client.py` wrapped in Task 8. Config in Task 1. All covered.
- **Components → Tasks:** `TtlSingleFlightCache.__init__` in Task 2. `get_or_fetch` grown across Tasks 2-5. Public `clear()` and `invalidate()` from spec NOT in any task — they're "reserved" per spec, no callers. Skipping is correct (YAGNI).
- **Cache key contract → Tasks:** Key shapes locked in Tasks 7 (panel-scope test) + 8 (FhirClient wiring). Key invariants (physician_user_id last; sorted params tuple) appear in Task 8 step 3c/3d code.
- **Failure modes → Tasks:** Wrong patient: covered by Task 7. Wrong physician: covered by Task 7. Memory leak: covered by Task 4 (LRU). Cache returns errors: covered by Task 6. Stale write-back: documented as out-of-scope in Task 10 §10. TTL=0 path: covered by Task 7.
- **Tests → Tasks:** All 7 spec tests + 2 integration tests = 9 (spec said 7 unit + integration). Tasks 2/3/4/5/6/7 cover them. Match.
- **Verification on Railway → Tasks:** Task 12 mirrors spec section verbatim.
- **Files modified → Tasks:** All 6 files in spec's table appear in the plan tasks (1, 2, 4, 5, 8, 10, 11).

Placeholder scan: no TBDs, TODOs, or "implement later." Every code step has actual code. Every shell step has the actual command and expected output.

Type consistency: `TtlSingleFlightCache` constructor signature `(ttl_seconds, max_entries=1000)` is consistent across Tasks 2 → 8. `get_or_fetch(key, fetcher)` signature stable. Method names `_do_get_resource` and `_do_search` consistent across Task 8 step 3.

Plan looks correct.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-10-fhir-cache.md`.

**Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Each task lands as one or two commits behind a code review.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch with checkpoints for your review.

Which approach?

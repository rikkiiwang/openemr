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
                self._ttl.popitem(last=False)  # evict LRU (oldest)
            future.set_result(result)
            return result
        except Exception as e:
            if not future.done():
                future.set_exception(e)
            raise
        finally:
            self._inflight.pop(key, None)

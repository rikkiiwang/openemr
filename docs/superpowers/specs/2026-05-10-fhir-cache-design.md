# FHIR per-tenant cache — design spec

**Date:** 2026-05-10
**Status:** Approved for implementation
**Owner:** Rikki Wang
**Scope tier:** W2 Final, ships today (subset; latency Approach B)

## Context

The Co-Pilot's measured latency on Railway prod (`copilot/COST.md` §§8-9, 2026-05-09) is dominated by FHIR round-trips, not LLM time:

- UC1 brief: p50 18.2s, p95 21.5s
- UC2 meds: p50 10.0s, p95 10.2s
- UC3 applied guideline: p50 11.8s, p95 13.8s
- LLM is ~20% of UC2 wall time
- Per-tool table shows 5,000ms / 10,000ms quantization on every FHIR-backed tool — a clear timeout-then-retry pattern on the OpenEMR REST proxy

§9 promotes per-tenant FHIR caching from "Network-tier scaling item" to "highest-leverage performance lever today." This spec implements that lever in the smallest, lowest-risk shape that ships within the W2 Final window.

The plan is deliberately **subset for today** — narrower wins (Approach A+B from the brainstorming session) over higher-ambition options (model routing, dense rerank, streaming) that would risk regressions on a deadline day.

## Goals

1. Cut UC1 second-turn latency from ~18s to ~3-5s on Railway prod.
2. Eliminate within-turn duplicate FHIR round-trips (e.g., prewarm + agent both calling `get_recent_labs` before either has populated a cache).
3. Zero behavioral change to the verification gate, citation contract, RAG pipeline, or LLM provider — cache is transparent.
4. Kill-switchable via env in <30 seconds without redeploy.

Non-goals (deferred):
- Cache invalidation on write (60s TTL absorbs)
- Streaming / TTFT (verification contract precludes streaming the answer)
- Dense retrieval and `LocalCrossEncoderReranker` (Approach C)
- Per-resource TTL tuning (uniform 60s for v1)
- Redis / shared cache (in-process only by user constraint)

## Architecture

One new module — `copilot/app/fhir/cache.py` — defines `TtlSingleFlightCache`, a generic in-process cache that bundles two roles:

1. **TTL response cache** — `(key → (json_response, expires_at))`. Lazy eviction on read. LRU bound at 1000 entries.
2. **In-flight Promise cache** — `(key → asyncio.Future)`, evicted on settle. Two concurrent calls for the same key share a single upstream fetch.

`copilot/app/fhir/client.py` is modified at exactly two read methods (`get_resource`, `search`) to route through the cache. Write methods bypass entirely.

Why one module instead of two: the TTL cache addresses cross-turn warmth; the Promise cache addresses within-turn duplicate dispatches. Without single-flight, two concurrent misses both pay the full FHIR round-trip and double-populate. Together they cover both cases with one code path and one set of tests.

## Components

### `copilot/app/fhir/cache.py` (new, ~120 LOC)

```python
class TtlSingleFlightCache:
    def __init__(self, ttl_seconds: int, max_entries: int = 1000) -> None: ...
    async def get_or_fetch(
        self,
        key: tuple,
        fetcher: Callable[[], Awaitable[dict]],
    ) -> dict: ...
    def invalidate(self, key: tuple) -> None: ...   # not wired in v1; reserved
    def clear(self) -> None: ...                     # tests only
```

Behavior:
- `get_or_fetch` → check TTL cache (return if fresh) → check in-flight cache (await if present) → create Future, store in in-flight, call fetcher, on success populate TTL cache, on settle evict in-flight.
- TTL of 0 means cache is bypassed entirely (fetcher always called fresh, no in-flight dedup either) — kill-switch path.
- LRU eviction triggered on TTL-cache write, sweeping any expired entries first.
- Errors propagate to all awaiters; key is NOT populated in TTL cache.
- Non-2xx responses are detected by the fetcher (raises `FhirError`); cache never sees them.

### `copilot/app/fhir/client.py` (modified, ~15 LOC delta)

Module-scope singleton `_cache: TtlSingleFlightCache | None`, initialized lazily from `COPILOT_FHIR_CACHE_TTL_SECONDS` env (default 60; 0 → cache disabled, methods call upstream directly).

```python
async def get_resource(self, resource_type, resource_id, *, physician_user_id):
    if _cache is None:
        return await self._do_get_resource(resource_type, resource_id, physician_user_id)
    key = ("get", resource_type, resource_id, physician_user_id)
    return await _cache.get_or_fetch(
        key,
        lambda: self._do_get_resource(resource_type, resource_id, physician_user_id),
    )

async def search(self, resource_type, params, *, physician_user_id):
    if _cache is None:
        return await self._do_search(resource_type, params, physician_user_id)
    key = ("search", resource_type, tuple(sorted(params.items())), physician_user_id)
    return await _cache.get_or_fetch(
        key,
        lambda: self._do_search(resource_type, params, physician_user_id),
    )
```

The current `get_resource` / `search` bodies are renamed to `_do_get_resource` / `_do_search` (the actual httpx call). Cache layer wraps them.

Write methods (`create_document_reference`, `create_observation`, `post_document_via_rest_api`, etc.) are unchanged — they don't go through the cache.

### `copilot/app/config.py` (modified, ~3 LOC delta)

```python
class Settings(BaseSettings):
    ...
    copilot_fhir_cache_ttl_seconds: int = 60
    copilot_fhir_cache_max_entries: int = 1000
```

The max-entries setting isn't exposed in the spec UX but is available for ops if memory becomes a concern.

## Cache key contract

The key tuple format is the safety contract — getting it wrong is a panel-scope leak.

| Shape | Example |
|---|---|
| Path fetch | `("get", "Patient", "abc-123", "dr_alvarez")` |
| Search | `("search", "MedicationRequest", (("_count", "50"), ("patient", "abc-123")), "dr_alvarez")` |

Required invariants:
- `physician_user_id` is the LAST positional element. Tests verify two physicians fetching the same patient produce different keys.
- Search params are sorted into a tuple of `(str, str)` pairs. `tuple(sorted(d.items()))` is the canonical form.
- All values are strings. No `None`, no `bytes`, no nested structures.

Any FHIR client method that gains read semantics in the future must add to this list explicitly. PR review must catch a new read path that doesn't go through the cache.

## Failure modes

| Failure | Mitigation |
|---|---|
| Cache returns wrong patient | Cache key includes `resource_id` / `patient` query param — different patients → different keys. |
| Cache returns wrong physician's data | Cache key includes `physician_user_id` — physicians get separate cache entries. |
| Memory leak (unbounded growth) | LRU bound at 1000 entries (~2MB worst case at 2KB/entry). Sweep-on-write evicts expired entries before LRU triggers. |
| Cache returns errors | Fetcher raises `FhirError` on non-2xx → exception propagates, key NEVER populated. |
| Cache returns stale write-back | Out-of-scope (no invalidation on write). 60s TTL is the staleness ceiling. Documented in COST.md. |
| Concurrent fetcher errors | In-flight cache: error from the single in-flight fetcher propagates to all awaiters; key is not poisoned. |
| TTL=0 path regression | Code branch when `_cache is None` calls upstream directly — same code path as today. Test asserts identity. |
| Cache disabled by accident | Env flag `COPILOT_FHIR_CACHE_TTL_SECONDS=0` is the documented kill-switch. Operationally reversible without redeploy. |

## Testing

New file `copilot/evals/fhir/test_cache.py` (~150 LOC). Tests cover the cache module in isolation (no FHIR fakes needed):

1. `test_ttl_hit_returns_cached` — first call fetches, second call within TTL returns cached value, fetcher invoked once.
2. `test_ttl_miss_after_expiry` — sleep past TTL, fetcher invoked again.
3. `test_lru_evicts_oldest` — fill cache to bound + 1, oldest entry evicted, fetcher re-invoked for it.
4. `test_single_flight_concurrent_misses` — two `asyncio.gather` calls for the same key with a fetcher that takes 100ms; both receive the same result, fetcher invoked once.
5. `test_error_propagates_no_cache` — fetcher raises; both awaiters see the exception; subsequent call invokes fetcher again.
6. `test_two_physicians_separate_entries` — same patient + different physician → different cache keys → fetcher invoked twice.
7. `test_ttl_zero_bypasses_cache` — `TtlSingleFlightCache(0)` always invokes fetcher.

Existing tests in `copilot/evals/fhir/` continue to pass — the wrapper is transparent.

`make eval-fast` 15/15 across 6 PRD categories must still pass. Cache cannot perturb answer correctness; if it does, `citation_present` or `factually_consistent` trips and the build blocks. This is the W2 hard gate — the cache implementation is gated by it.

## Verification on Railway after deploy

1. Set `COPILOT_FHIR_CACHE_TTL_SECONDS=60` on the `copilot` service. Deploy.
2. Open a Synthea patient chart. Submit "Give me a brief on this patient." Capture wall time (DevTools → Network → `/v1/chat`).
3. Without closing the chart, submit the same question again. Second-turn wall time should drop from ~18s to ~3-5s.
4. Ask a different question that touches different tools (e.g., "What was the LDL?"). First touch of `get_recent_labs` may pay full cost; second touch of any tool used in step 2 is cached.
5. Confirm Langfuse trace shows `tool_results[*]` populated normally on both turns. The cache is invisible at the trace level — citations work the same.
6. If anything looks off: set `COPILOT_FHIR_CACHE_TTL_SECONDS=0` on Railway. Cache is bypassed within ~30 seconds (next request misses the wrapper). No redeploy needed.

## Files modified

| File | Change | LOC delta |
|---|---|---|
| `copilot/app/fhir/cache.py` | new file — `TtlSingleFlightCache` | +120 |
| `copilot/app/fhir/client.py` | wrap `get_resource` + `search`; rename body to `_do_*` | +15 / -2 |
| `copilot/app/config.py` | add `copilot_fhir_cache_ttl_seconds` + max-entries | +3 |
| `copilot/evals/fhir/test_cache.py` | new file — 7 tests | +150 |
| `copilot/COST.md` | append §10 — what changed + how to disable | +20 |
| `copilot/W2_IMPLEMENTATION.md` | TL;DR row noting cache shipped | +5 |

Total: ~310 LOC (~290 new, ~20 modified). Single-PR scope.

## Out of scope (explicit deferrals)

To keep the plan honest: these are NOT covered here, by design.

- Cache invalidation on write (60s TTL absorbs)
- Redis / shared cache (in-process only)
- Streaming / TTFT (verification contract requires full output before reveal)
- Dense retrieval and `LocalCrossEncoderReranker` (Approach C — needs Docker bloat + corpus embedding pass + answer-quality regression check)
- Per-resource TTL tuning (uniform 60s; can split later if a specific resource type is provably lower-write-rate)
- Cache hit-rate metrics in Langfuse (nice-to-have; not required for the win)
- Tool-level (post-PHI-minimizer) dedup (out of scope; FHIR-level single-flight covers the dispatch race)

## Open questions

None remaining. Implementation can proceed.

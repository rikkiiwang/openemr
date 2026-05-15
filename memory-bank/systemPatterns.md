# System Patterns

Architectural decisions that are **load-bearing** for the project. Each pattern includes the *why* — the constraint or audit finding that drove it. Removing a pattern requires understanding what it was protecting against.

This file has two halves: **OpenEMR's existing architecture** (which we did not invent), and **what Week 1 added or changed** (which we own).

---

## Part A — OpenEMR's existing architecture

### A1. Three-tier system

```
Browser / API Client
        ▼
Presentation Layer
  interface/   (legacy PHP pages)
  apis/        (REST + FHIR dispatch)
        ▼
Service Layer
  src/Services/
  src/RestControllers/
  src/FHIR/
        ▼
Data Layer
  MariaDB / MySQL  (285 tables, structured data)
  CouchDB 3.5      (unstructured documents)
```

Key clinical tables: `patient_data`, `form_encounter`, `prescriptions`, `procedure_result`, `lists` (problems/diagnoses), `immunizations`. (`AUDIT.md §3.1`.)

### A2. Modular directory layout

| Path | Purpose | Era |
|---|---|---|
| `/src/` | Modern PSR-4 code, namespace `OpenEMR\` | New code goes here |
| `/library/` | Legacy procedural PHP helpers | Add new helpers only when `/src/` does not fit |
| `/interface/` | Web UI controllers + templates | Legacy; do not extend with new features |
| `/apis/` | REST + FHIR dispatch (`apis/dispatch.php`, `apis/routes/_rest_routes_standard.inc.php`) | The clean integration boundary for the agent |
| `/templates/` | Smarty + Twig templates | Smarty 4.5 (legacy) and Twig 3.x (modern) coexist |
| `/sql/` | Schema + migrations + sample data (`sql/example_patient_data.sql`) | Doctrine Migrations for new schema |
| `/tests/` | PHPUnit (unit, e2e, api, services) + custom PHPStan rules | Both Docker-run and isolated suites |
| `/public/` | Static assets | — |
| `/docker/` | Docker compositions (`development-easy`, `development-insane`, `inferno`, …) | Local dev environments |
| `/modules/` | Custom and third-party modules | Plug-in surface |
| `/gacl/` | phpGACL library | Authorization (Section / Action ACO model) |
| `/oauth2/` | OAuth2 + SMART on FHIR endpoints | The integration point we use |

### A3. The dual-layer write problem (`AUDIT.md §3.2`)

OpenEMR is mid-migration from procedural to service-layer architecture. **Both layers write to the same clinical tables simultaneously.** Three distinct write paths to `patient_data`:

| Path | Validation | ACL | Audit |
|---|---|---|---|
| `src/Services/PatientService` | `PatientValidator` | Caller-enforced | `sqlStatement` → logged |
| `library/patient.inc.php::updatePatientData()` | Delegates to PatientService | Caller-enforced | Logged via service |
| `interface/forms/*/save.php` (34 files) | None | Ad-hoc per page | `sqlQuery` — some `sqlStatementNoLog` |

**Audit-bypass scale:** 43 uses of `sqlStatementNoLog` / `sqlQueryNoLog` in `interface/`, 47 in `library/`. **90 PHI write/read paths skip the audit engine.** This is the single biggest reason the agent never reads from anything except the FHIR API (see B2 below).

### A4. Authorization — phpGACL at the call-site

`/gacl/` (Generic Access Control List). Section + Action model:

```php
AclMain::aclCheckCore('patients', 'rx', $authUser)        // before fetching meds
AclMain::aclCheckCore('encounters', 'notes', $authUser)   // before fetching notes
```

Six roles: Administrators, Physicians, Clinicians, Front Office, Accounting, Emergency Login. **Checks happen at the call-site, not in the service layer** — `BaseService` and `PatientService` contain zero `AclMain` calls. Any new endpoint that forgets to call `aclCheckCore` gets full database access (`AUDIT.md §1.2`). This drove pattern B3 below.

### A5. Coding standards (the canonical reference is the OpenEMR Development Guide section of this `CLAUDE.md`)

- 4-space indent, LF line endings
- PSR-4 with `OpenEMR\` prefix for `/src/`
- `declare(strict_types=1)` on every new file
- PSR-1 / 3 / 4 / 11 + PER-CS 3.0 (supersedes PSR-12)
- Database via `OpenEMR\Common\Database\QueryUtils` + `DatabaseConnectionFactory`
- Globals via `OEGlobalsBag` (typed getters), not `$GLOBALS`
- DateTimeImmutable, never DateTime
- Custom PHPStan rules in `tests/PHPStan/Rules/` enforce: no forbidden globals, no forbidden direct instantiations, namespace rules
- Conventional Commits (`feat`, `fix`, `chore`, `docs`, `refactor`, …) — validated in CI
- `Generated-By: Claude Code` or `Assisted-By: Claude Code` trailer for AI-authored commits
- Multiple template engines coexist — check the file extension before editing (`.twig`, `.html`, `.php`)

---

## Part B — What Week 1 added or changed

### B1. The Co-Pilot is a separate Python service, not PHP code in OpenEMR

**Why:** A1's three-tier system is mid-migration with audit-bypassing legacy paths (A3). Building inside it would inherit those problems and force every change through Smarty/Twig/Angular review. A separate service iterates independently and treats OpenEMR as a typed FHIR boundary.

**Where:** `copilot/` (FastAPI), Railway service `copilot`, talks to OpenEMR over HTTPS / OAuth2.

**Key files:**

| File | Responsibility |
|---|---|
| `copilot/app/main.py` | FastAPI entry, `/healthz`, `/v1/sessions`, `/v1/chat`, `/v1/patient/{id}/raw`, `/v1/patients` (panel-aware UUID list — added 2026-05-15 for external auto-bootstrap, PR #6 → `34ae2d95f`), plus W2 documents endpoints |
| `copilot/app/config.py` | Settings (env-driven via `pydantic-settings`) |
| `copilot/app/fhir/oauth.py` | OAuth2 password-grant token acquisition |
| `copilot/app/fhir/client.py` | httpx-based FHIR HTTP client with TLS toggle |

### B2. FHIR-only data path

**Why:** Audit-bypass (A3). FHIR R4 funnels reads through the modern service layer — the only path that respects audit logging. Also: portable.

**Implication:** No `mysql.connector` import in `copilot/`. Ever. The contract is `OPENEMR_FHIR_BASE` + OAuth2 token.

### B3. Five-step tool pattern (`copilot/app/tools/_base.py:run_tool`)

Every tool — 8 in W1, 9 in W2 — follows the same five steps:

1. Resolve session pseudonym → real FHIR UUID (server-side only)
2. ACL check (mirror of OpenEMR's `aclCheckCore`) — fail fast before hitting FHIR
3. Fetch via FHIR with the OAuth2 token
4. PHI minimization — strip identifiers, keep clinical content + `record_id`
5. Return `ToolResult(data, record_ids, record_type)`

`record_ids` are the verification anchors. Removing step 4 leaks PHI to the LLM. Removing step 5 breaks Layer-1 verification. Removing step 2 makes us trust FHIR's scope alone (no defense-in-depth).

**Tool inventory** (`copilot/app/tools/`):

| File | Tool | UC |
|---|---|---|
| `get_patient_summary.py` | `get_patient_summary` | UC1 / UC2 / UC3 |
| `get_active_medications.py` | `get_active_medications` | UC1 / UC2 / UC3 |
| `get_recent_labs.py` | `get_recent_labs` | UC1 / UC2 / UC3 |
| `get_recent_vitals.py` | `get_recent_vitals` | UC2 |
| `get_encounter_history.py` | `get_encounter_history` | UC1 / UC3 |
| `get_encounter_note.py` | `get_encounter_note` | UC3 |
| `get_allergies.py` | `get_allergies` | UC2 / UC3 |
| `check_drug_interactions.py` | `check_drug_interactions` | UC3 |
| `document_tools.py` (W2) | `attach_and_extract` | W2 ingestion |
| `guideline_tools.py` (W2) | `search_guidelines` | W2 RAG |
| `_base.py` | `run_tool` orchestrator (5-step) | — |
| `registry.py` | `TOOL_REGISTRY` — single source of truth | — |

### B4. Two-layer verification gate

**Where:** `copilot/app/verification/{attribution.py, rules.py}`.

- **Layer 1 — Source attribution.** LLM is forced to emit `(claim_text, record_id)` pairs via the `submit_response` tool (structured output). Before any response leaves the agent, every claim's `record_id` must be in the union of tool-result IDs from this turn. Unanchored claims are stripped; on rejection the agent **retries at most once** with the failure as feedback, then refuses with a "cannot verify" message.
- **Layer 2 — Domain rules** (Python code, not prompt instructions): cross-patient leakage (hard block), allergy contraindication (hard block). Renal-dose and QTc rules deferred (tracked in `progress.md`).

**Why:** The LLM is the thing being verified — we do not let it judge itself. Layer 1 is a deterministic string-set check. Layer 2 catches what attribution can't.

**Schema:** `copilot/app/agent/schemas.py` — `Claim`, `AgentResponse`, `SUBMIT_RESPONSE_TOOL`. `Claim.record_id` accepts FHIR URIs (e.g. `MedicationRequest/142`).

### B5. PHI minimization with session-scoped pseudonyms

**Where:** `copilot/app/phi/{minimizer.py, session.py, log_filter.py}`.

- Strip: name, address, telecom, identifier values (SSN/MRN). Provider names → `Provider-A`, `Provider-B`.
- Transform: `birthDate` → age (e.g. `67yo`).
- Preserve: clinical content (RxNorm/LOINC, values, ranges, encounter dates and reasons).
- Mapping table in-memory, session-scoped, expires at session end.

**Why:** `AUDIT.md §1.4` — no de-identification existed anywhere. Highest-risk audit finding.

### B6. Three-layer per-physician scope

**Why:** `AUDIT.md §1.3` — OpenEMR's GACL has six roles, none scoped to a provider's own patients. `patient_data.providerID` is nullable with no constraint. This entire enforcement layer is project code, not OpenEMR code.

**Where:**

| Layer | File | What it does |
|---|---|---|
| Demographics gate | `copilot-demographics-gate.php` (repo root) | When iframe loads, OpenEMR page checks the requesting clinician is on the patient's panel |
| Finder gate | `copilot-finder-scope.php` (repo root) | Patient finder dropdown is filtered by panel |
| Session gate | `copilot/app/main.py` (`/v1/sessions`) + `_verify_patient_in_panel` helper | Rejects session open if SMART launch context patient is not in the requesting clinician's panel |

Env: `PHYSICIAN_PATIENT_PANEL` (env-driven primary scope path; admin bypass list resolved at `f04657d65`).

### B7. Anthropic primary, OpenAI fallback (per-turn swap)

**Where:** `copilot/app/agent/llm.py:FallbackAdapter`.

- Tries Anthropic at the start of every turn.
- On retryable SDK error (`APIStatusError` / `APIConnectionError` / `APITimeoutError`) **before any tool call has been built**, transparently swaps to OpenAI for that turn.
- Resets to Anthropic at the next turn.

**Why:** Demo-night billing failure on Anthropic — needed a fallback path that did not require pre-validating every key. Verification surface is uniform across providers because both go through the same `submit_response` structured output.

### B8. iframe rail injected at Docker build time

**Where:** repo-root `Dockerfile` runs `awk` to splice `copilot-rail-fragment.php` into stock `interface/patient_file/summary/demographics.php` immediately before `</body>` inside the upstream OpenEMR image. A `grep -q copilot-rail` post-check fails the build if injection didn't land.

**Why:** First attempt (full-file COPY) crashed at runtime with `Call to undefined method` because our fork's PHP was version-mismatched against the upstream image. `awk` injection patches *only the line we care about* and inherits everything else from the stock image.

### B9. Static system prompt + Anthropic prompt caching

**Where:** `copilot/app/agent/{prompt.py, loop.py}`. ~30% cache hit rate on repeat sessions in local testing. Versioned: changes go through eval suite first.

**Why:** Cost projection (`copilot/COST.md`) is prompt-cache-dependent. If the cache invalidates frequently, per-session cost ~doubles.

### B10. Split logging — Langfuse trace ≠ clinical audit

**Where:** `copilot/app/observability/trace.py`.

- **Langfuse trace** — technical observability (tool sequence, latencies, tokens, verification verdict). Question text PHI-screened on ingest.
- **Clinical audit log** — metadata only (user, patient, time, query type), never response body.

**Why:** `AUDIT.md §1.5` — `api_log` stores full request/response body including raw PHI. Logging is the leak surface; we don't generate it.

### B11. Resume-previous-chat persistence

SQLite on Railway volume (`copilot/copilot_docs.db`); endpoints `/v1/sessions/{recent,resume,end}`; "Resume previous chat?" banner in iframe.

### B12. Pre-warm on session open

`copilot/app/agent/prewarm.py` (commit `d682c4da8`) — pre-fetches FHIR tools when `/v1/sessions` is called. Cold first-turn latency was ~15s; warm is ~3s.

### B13. CI is a test gate, not a deploy path

`.github/workflows/copilot-ci.yml` runs ruff + pytest on `copilot/**`. Deploy job dropped (`f88ed610a`) — Railway native GitHub auto-deploy is the deploy mechanism. CI never makes real LLM calls (`evals/conftest.py` skips `@pytest.mark.live_llm` unless `ANTHROPIC_LIVE=1`).

### B14. Modern Patient Dashboard ↔ OpenEMR — same-origin co-resident container (v2, LIVE on master `7124c20dd`)

**Pattern shipped 2026-05-09 (v1 cross-origin), pivoted 2026-05-10 to v2 (same-origin co-resident).** The W2 surprise-challenge dashboard (Next.js 15 / React 19 at `frontend/`) is reachable from inside OpenEMR via a patient-finder click. v2 collapses the deployment from 3 Railway services to 2 by **co-hosting the Next.js process inside OpenEMR's Apache container** behind `mod_proxy_http`. Same origin = same cookie jar, no SameSite=None gymnastics, no CSP frame-ancestors allowlist.

**Why v2 (vs v1):** v1's cross-origin embed required SameSite=None + Secure cookies on both sides, a CSP frame-ancestors allowlist, and a hardcoded `PROD_OPENEMR_ORIGIN_FALLBACK` constant in `csp.ts` because `next.config.ts headers()` runs at build time and Railway runtime env vars don't reach it. Two days of fragility for a single demo. v2 deletes all of that surface — the dashboard now serves under `/modern/*` on OpenEMR's own origin.

**Pattern (still mirrors B8 — `awk`/`sed` into upstream + multi-stage Dockerfile):**

| Layer | File | Behavior |
|---|---|---|
| Build | `Dockerfile` stage `dashboard-build` (`81baf8186`) | `node:24-alpine` → `npm ci` + `npm run build` → produces `frontend/.next/standalone`. |
| Image runtime | `Dockerfile` main stage (`81baf8186`) | `apk add nodejs` (Node 22); `COPY --from=dashboard-build /build/.next/standalone /opt/dashboard`; `COPY dashboard-proxy.conf /etc/apache2/conf.d/dashboard-proxy.conf`. |
| Reverse proxy | `dashboard-proxy.conf` (`2cb818c43`) | `ProxyPass /modern http://127.0.0.1:3000/modern` (no trailing slash on either side, so `/modern`, `/modern/`, and `/modern/anything` all match). `mod_proxy_http` is preloaded by upstream `proxy.conf`. |
| Process lifecycle | `railway-entrypoint.sh` (`81baf8186`) | Forks `node /opt/dashboard/server.js` on 127.0.0.1:3000 before `exec ./openemr.sh`; `trap` reaps Node on container shutdown. |
| Next.js basePath | `frontend/next.config.ts` (`81baf8186`) | `basePath: "/modern"` + `assetPrefix: "/modern"`. Routes, assets, and API paths all serve under `/modern/*`. |
| Launcher | `interface/patient_file/summary/dashboard.php` (`81baf8186`) | View chooser. Modern URL is **relative same-origin** `/modern/api/auth/login?next=/patient/<uuid>&launch=<token>`. No `DASHBOARD_URL` env read. Fallback to legacy fires only when patient lacks a UUID. |
| Finder re-point | `interface/main/finder/{dynamic_finder,patient_select}.php` (`0a49d038d`, unchanged from v1) | Click handlers route to `dashboard.php?set_pid=`. |
| Image-time injection | `Dockerfile` `cp`+`sed` (`4b9f181a2`, unchanged from v1; SameSite=None sed reverted in `81baf8186`) | Build-time grep guards still fail loudly if any injection drops out. |
| OAuth callback Location | `frontend/app/api/auth/callback/route.ts` (`2cb818c43`) | Prepends `/modern` to the `next` path before setting `Location`. Without this, post-login redirect to `/patient/<uuid>` 404s at Apache (no proxy match). |
| CSP at runtime | `frontend/middleware.ts` (`4ec0f07b0`) | Per-request middleware overrides `Content-Security-Policy` so runtime `COPILOT_URL` reaches `frame-src`. The static CSP from `next.config.ts headers()` is build-time-baked; middleware is the right tool for runtime-config-driven response headers. |
| Iframe embed | same-origin (no allowlist needed) | CSP says `frame-ancestors 'self'`; X-Frame-Options `SAMEORIGIN`. OpenEMR (which iframes the chooser → modern dashboard) is the same origin as `/modern/*`. |
| Co-Pilot rail src | `frontend/components/CopilotRail.tsx` (`7124c20dd`) | Builds `${COPILOT_URL}/?patient_id=...`. The deployed Co-Pilot serves the iframe shell at `/`, NOT `/iframe` (`copilot/app/main.py:117`). |

**What this preserves (unchanged from v1):**

- B8's `awk`/`sed`-into-upstream pattern is still the answer for cross-cutting OpenEMR PHP edits.
- B6's three-layer scope is preserved in the dashboard — its FHIR proxy enforces panel scope server-side.
- B9's prompt cache is unchanged; the dashboard embeds the same Co-Pilot service via iframe.
- SMART EHR-launch silent SSO via `SMARTLaunchToken` (`ad40380f3`) still works through the OAuth flow.

**What v2 obviates (vs v1):**

- CSP `frame-ancestors` allowlist — gone. `'self'` is enough.
- `SameSite=None; Secure` cookies in prod — gone. `SameSite=Lax + Secure` works for the same-origin embed.
- Upstream `SessionConfigurationBuilder.php` samesite-Lax flip (sed) — gone, reverted in `81baf8186`.
- Separate `dashboard` Railway service — paused; deletable post-demo.
- `DASHBOARD_URL` env on the OpenEMR service — gone, no longer read.
- `PROD_OPENEMR_ORIGIN_FALLBACK` constant in `csp.ts` — gone (was the build-time-vs-runtime workaround for OPENEMR_OAUTH_BASE).

**What's new and load-bearing in v2:**

- Multi-process container — Apache (PID 1) + Node (background child). ~150MB extra memory, ~5–8s extra startup. Trap-on-exit reaps Node on container shutdown.
- Single Railway service `refreshing-empathy/openemr` carries env vars previously split across two services: `OPENEMR_DASHBOARD_CLIENT_ID/SECRET`, `DASHBOARD_PUBLIC_URL=https://…0c8c…/modern`, `OPENEMR_OAUTH_BASE`, `OPENEMR_FHIR_BASE`, `SESSION_COOKIE_SECRET`, `COPILOT_URL`, `COPILOT_ADMIN_USERS`, `OPENEMR_VERIFY_TLS`, `STRICT_PANEL_SCOPE` (optional).
- OAuth client `Dashboard (Next.js)` redirect_uri = `https://openemr-production-0c8c.up.railway.app/modern/api/auth/callback`. Client scopes must include all six `user/<Resource>.read` for the cards (Allergies, Condition, MedicationRequest, CareTeam, Encounter, Patient). The OpenEMR API-Clients form is brittle — the JWKS field validator rejects empty strings; workaround = type `[]` literally, or update via SQL on `oauth_clients`.
- `COPILOT_ADMIN_USERS` env is shared between the Next.js panel-scope gate AND the legacy `copilot-finder-scope.php`/`copilot-demographics-gate.php` hand-rolled SQL filters. Front-desk users need their login username in this list for both layers to bypass.

---

## Key files added in Week 1 — quick index

| File | Responsibility |
|---|---|
| `AUDIT.md` | Five-section codebase audit; drives every architectural mitigation |
| `USERS.md` | Target user + 3 use cases; source of truth for what we build |
| `ARCHITECTURE.md` | Full Week 1 design with §12 trace-back matrix |
| `copilot-rail-fragment.php` | iframe rail UI fragment, awk-injected at build |
| `copilot-demographics-gate.php` | Per-patient demographics-page scope check |
| `copilot-finder-scope.php` | Per-clinician finder filter |
| `Dockerfile` (repo root) | Railway image; awk-injects iframe fragment + post-check |
| `railway-entrypoint.sh` | Wraps upstream `openemr.sh`; idempotent TLS cert generation |
| `copilot/` (entire tree) | Python FastAPI agent service |

---

## Deferred / explicitly out of scope (do not silently add)

- Renal-dose and QTc Layer-2 rules (only allergy + cross-patient in v1)
- Self-hosted Langfuse (Langfuse Cloud is sufficient for now)
- Haiku routing for cheap lookups (uniform Sonnet for now — keeps verification surface single)
- Multi-language clinical conversation (English only)
- Anything in `USERS.md`'s out-of-scope table
- PHI plaintext-at-rest encryption (audit §5.5 — out of scope for v1; agent does not exacerbate)
- Slow-query log enablement (audit §2.2)
- `idx_provider` on `patient_data.providerID` (audit §2.1) — flagged for production hardening

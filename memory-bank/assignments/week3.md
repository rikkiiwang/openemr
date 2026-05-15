# Week 3 — Adversarial AI Security Platform

**Window:** 2026-05-11 → ongoing
**Spec:** `~/Desktop/Gauntlet/Week3/Week 3 - AgentForge - Adversarial AI Security Platform.pdf`

## TL;DR

Week 3's work does **not live in this repo**. It lives in the sibling repo at:

```
~/Desktop/Gauntlet/agentforge-adversarial/
```

The openemr-repo's role in W3 is to **be the attack target**: the deployed Co-Pilot at `https://copilot-production-b532.up.railway.app` is what the adversarial harness probes. Day-to-day W3 development happens in the sibling repo — its `README.md`, `ARCHITECTURE.md`, `THREAT_MODEL.md`, and `IMPLEMENTATION.md` are the design-of-record.

## Why work doesn't land in this repo

The adversarial harness is a separate Python service with its own Postgres database (Railway service `daring-vitality/agentforge-adversarial`) and its own deploy artifact (Streamlit dashboard). It interacts with the openemr-repo's services only over HTTP. Mixing the two codebases would conflate "attacker" and "defender" surfaces — a security-architecture smell.

## When openemr-repo changes ARE needed

Occasionally the adversarial harness needs a new affordance on the target side. Those changes land here as standalone PRs. As of 2026-05-15:

- `34ae2d95f` (PR #6) — `feat(copilot): add GET /v1/patients`. Enables external tooling (the harness) to auto-bootstrap a Synthea patient UUID without operator copy-paste.
- `304bc832d` (PR #7) — `fix(railway): clear stale docker-leader marker on openemr boot`. Incident hotfix (PR #6 merge triggered an unintended openemr-service redeploy that crash-looped on a stale volume marker).

Future cross-cutting PRs should be small + reversible like these two, and should cite the adversarial-harness use case in the PR description so reviewers can trace the dependency.

## W3 architecture at a glance (as of 2026-05-15)

For full details see `~/Desktop/Gauntlet/agentforge-adversarial/ARCHITECTURE.md`. The 7 architectural pillars are:

1. **Orchestrator** — advisory 5-signal cell scoring (coverage_gap · partial_rate · severity_baseline · staleness · diversity_score; weights 0.30/0.30/0.20/0.10/0.10). Emits a `CampaignBrief` per campaign on `campaigns.brief_json` for audit.
2. **`synthesize_fn` pipeline** — normalize → embed (text-embedding-3-small) → within-batch dedup (cosine ≥ 0.92) → novelty filter vs the last 200 attack_runs (cosine ≥ 0.85) → weighted score → budget-capped greedy pick (K=10 default). Operates on mutator output; seed YAMLs always pass through.
3. **Judge ≠ Red Team model family** — `JUDGE_MODEL=gpt-4o-mini` (OpenAI), `MUTATOR_MODEL=claude-haiku-4-5-20251001` (Anthropic). Different providers to avoid attacker/judge conflict of interest.
4. **Queue-backed regression** — regression replays now go through `attack_queue → dispatch → judge` like normal campaigns, leaving a first-class `attack_runs` audit trail (was synchronous + non-persisted before).
5. **`cross_regressions` table + detection** — runs at every campaign end; matches current FAILs against the most recent prior PASS on a different `target_version`. Detects "a fix elsewhere broke an adjacent behavior."
6. **Langfuse observability** — one trace per campaign on `cloud.langfuse.com`; per-agent spans (orchestrator / red_team_mutator / synthesize / dispatch / judge / class_probe / partial_reentry); per-LLM generations carrying token counts + cost. `campaigns.langfuse_trace_id` stores the deep-link.
7. **`near_misses` lifecycle** — every PARTIAL verdict spawns a row in state `exploring`; state machine `exploring → escalated / exhausted / budget_capped` driven by the orchestrator's post-verdict routing (helpers ship; wiring follow-up).

## Authoritative pointers (in sibling repo)

| Doc | Path |
|---|---|
| README + Quick Start | `~/Desktop/Gauntlet/agentforge-adversarial/README.md` |
| Architecture | `~/Desktop/Gauntlet/agentforge-adversarial/ARCHITECTURE.md` |
| Threat model | `~/Desktop/Gauntlet/agentforge-adversarial/THREAT_MODEL.md` |
| Users + use cases | `~/Desktop/Gauntlet/agentforge-adversarial/USERS.md` |
| Implementation matrix | `~/Desktop/Gauntlet/agentforge-adversarial/IMPLEMENTATION.md` |

# Clinical Co-Pilot in OpenEMR

A production-grade Clinical Co-Pilot AI agent embedded inside OpenEMR (forked from [openemr/openemr](https://github.com/openemr/openemr)), plus a modern Next.js 15 patient dashboard co-hosted same-origin inside OpenEMR's container.

## Live demo

| Surface | URL |
|---|---|
| OpenEMR (fork, with Co-Pilot rail) | <https://openemr-production-0c8c.up.railway.app/> |
| Modern patient dashboard (Next.js, same-origin) | <https://openemr-production-0c8c.up.railway.app/modern/> |
| Clinical Co-Pilot agent (standalone) | <https://copilot-production-b532.up.railway.app/> |

Log in to OpenEMR, pick a patient → a chooser offers **Modern** (Next.js dashboard with the Co-Pilot rail) or **Legacy** (stock `demographics.php` with the Co-Pilot rail injected via build-time `awk`). Both surfaces talk to the same Co-Pilot service over OAuth2 + PKCE. The dashboard runs inside the same Railway container as OpenEMR — Apache `mod_proxy_http` forwards `/modern/*` to a Node process on `127.0.0.1:3000`.

## What this delivers

An AI agent designed for the 60–90 second window a primary care physician has between rooms. Three immovable promises shape every decision:

1. **Every clinical claim is traceable** — no claim leaves the agent without a `record_id` from a tool call this turn (Layer-1 source attribution gate in `copilot/app/verification/attribution.py`).
2. **No raw PHI crosses the LLM boundary** — server-side pseudonymization with session-scoped mapping, scanned for in trace observability (`copilot/app/phi/`).
3. **Refuse over guess** — explicit `data_gaps` when source data is missing; the eval suite has a dedicated `refusal` category enforcing this.

### Baseline — single-agent trust contract

A single-agent FHIR-backed Co-Pilot with 8 read-only tools, two-layer verification (Layer 1 source attribution + Layer 2 domain rules for allergy and cross-patient leakage), three-layer per-physician panel scope, an Anthropic→OpenAI fallback adapter, and Langfuse observability — delivered as an iframe injected into stock `demographics.php`. Three use cases covered: pre-visit brief, multi-condition reasoning, medication safety.

### Optimized — extended along three axes

- **Richer agent.** LangGraph supervisor + workers (`intake_extractor`, `evidence_retriever`, `answer_composer`, `critic`) with a plain-Python router. 11 tools (8 baseline readers + `attach_and_extract`, `search_guidelines`, `get_recent_uploads`).
- **Richer inputs.** Claude vision over lab PDFs and intake forms producing typed Pydantic extractions with `bbox + raw_text` co-anchored citations the physician can click to verify on the original document. Hybrid retrieval — BM25 over SQLite FTS5 fused with OpenAI `text-embedding-3-small` dense vectors via reciprocal rank fusion (k=60), behind a default-OFF kill switch (`COPILOT_DENSE_RETRIEVAL_ENABLED`).
- **Richer surface.** Modern Next.js 15 / React 19 dashboard at `/modern/*`, same-origin co-hosted inside the OpenEMR container. A Front Office role uploads documents via the iframe drop-zone with deferred extraction; the physician sees a confirm/reject UX that writes back to OpenEMR.

### Regression-blocking eval gate

52 fixture cases across 6 categories (citation / extraction / retrieval / refusal / phi / cross), 5 boolean scorers + 1 meta canary (`rules_block_regression`), floor 95% / drop 5pp thresholds vs `evals/baseline.json`. Wired into both a local pre-push hook (`make eval-fast`, ~2 s) and GitHub Actions (full 50-case suite). Three meta-tests in `copilot/evals/regression_demo/test_gate_fires.py` prove the gate itself fires when a Layer-2 rule is disabled, when PHI is injected, or when a category synthetically drops. The recipe: comment out `check_extracted_fact_has_source_doc` and run `make eval-fast` — `cross` category drops and the runner exits non-zero.

## Test surface

- **214** Co-Pilot tests passing (`copilot/`)
- **186** dashboard tests passing (`frontend/`)
- **52/52** eval fixture cases at 100% across all 6 categories
- Static analysis: PHPStan level 10, ruff, ESLint, TypeScript strict

## Documentation

| | File |
|---|---|
| Codebase audit | [`AUDIT.md`](AUDIT.md) |
| Target user + use cases | [`USERS.md`](USERS.md) |
| AI integration design | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| Baseline implementation log | [`copilot/W1_IMPLEMENTATION.md`](copilot/W1_IMPLEMENTATION.md) |
| Extension architecture (LangGraph + RAG + ingestion) | [`copilot/W2_ARCHITECTURE.md`](copilot/W2_ARCHITECTURE.md) |
| Extension implementation log | [`copilot/W2_IMPLEMENTATION.md`](copilot/W2_IMPLEMENTATION.md) |
| Cost & latency analysis | [`copilot/COST.md`](copilot/COST.md) |
| Co-Pilot service setup, eval suite, deploy | [`copilot/README.md`](copilot/README.md) |
| Patient-dashboard migration notes | [`PATIENT_DASHBOARD_MIGRATION.md`](PATIENT_DASHBOARD_MIGRATION.md) |

### Documented gap (carried)

Real `POST /fhir/DocumentReference` — OpenEMR R4 has no such route. The Plan B REST path (`POST /apis/default/api/patient/{puuid}/document`) is shipped but blocked on the `api:oemr` OAuth scope; the fail-soft path is documented in `W2_IMPLEMENTATION.md`.

---

[![Syntax Status](https://github.com/openemr/openemr/actions/workflows/syntax.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/syntax.yml)
[![Styling Status](https://github.com/openemr/openemr/actions/workflows/styling.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/styling.yml)
[![Testing Status](https://github.com/openemr/openemr/actions/workflows/test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/test.yml)
[![JS Unit Testing Status](https://github.com/openemr/openemr/actions/workflows/js-test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/js-test.yml)
[![PHPStan](https://github.com/openemr/openemr/actions/workflows/phpstan.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/phpstan.yml)
[![Rector](https://github.com/openemr/openemr/actions/workflows/rector.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/rector.yml)
[![ShellCheck](https://github.com/openemr/openemr/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/shellcheck.yml)
[![Docker Compose Linting](https://github.com/openemr/openemr/actions/workflows/docker-compose-lint.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/docker-compose-lint.yml)
[![Dockerfile Linting](https://github.com/openemr/openemr/actions/workflows/hadolint.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/hadolint.yml)
[![Isolated Tests](https://github.com/openemr/openemr/actions/workflows/isolated-tests.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/isolated-tests.yml)
[![Inferno Certification Test](https://github.com/openemr/openemr/actions/workflows/inferno-test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/inferno-test.yml)
[![Composer Checks](https://github.com/openemr/openemr/actions/workflows/composer.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/composer.yml)
[![Composer Require Checker](https://github.com/openemr/openemr/actions/workflows/composer-require-checker.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/composer-require-checker.yml)
[![API Docs Freshness Checks](https://github.com/openemr/openemr/actions/workflows/api-docs.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/api-docs.yml)
[![codecov](https://codecov.io/gh/openemr/openemr/graph/badge.svg?token=7Eu3U1Ozdq)](https://codecov.io/gh/openemr/openemr)

[![Backers on Open Collective](https://opencollective.com/openemr/backers/badge.svg)](#backers) [![Sponsors on Open Collective](https://opencollective.com/openemr/sponsors/badge.svg)](#sponsors)

# OpenEMR

[OpenEMR](https://open-emr.org) is a Free and Open Source electronic health records and medical practice management application. It features fully integrated electronic health records, practice management, scheduling, electronic billing, internationalization, free support, a vibrant community, and a whole lot more. It runs on Windows, Linux, Mac OS X, and many other platforms.

### Contributing

OpenEMR is a leader in healthcare open source software and comprises a large and diverse community of software developers, medical providers and educators with a very healthy mix of both volunteers and professionals. [Join us and learn how to start contributing today!](https://open-emr.org/wiki/index.php/FAQ#How_do_I_begin_to_volunteer_for_the_OpenEMR_project.3F)

> Already comfortable with git? Check out [CONTRIBUTING.md](CONTRIBUTING.md) for quick setup instructions and requirements for contributing to OpenEMR by resolving a bug or adding an awesome feature 😊.

### Support

Community and Professional support can be found [here](https://open-emr.org/wiki/index.php/OpenEMR_Support_Guide).

Extensive documentation and forums can be found on the [OpenEMR website](https://open-emr.org) that can help you to become more familiar about the project 📖.

### Reporting Issues and Bugs

Report these on the [Issue Tracker](https://github.com/openemr/openemr/issues). If you are unsure if it is an issue/bug, then always feel free to use the [Forum](https://community.open-emr.org/) and [Chat](https://www.open-emr.org/chat/) to discuss about the issue 🪲.

### Reporting Security Vulnerabilities

Check out [SECURITY.md](.github/SECURITY.md)

### API

Check out [API_README.md](API_README.md)

### Docker

Check out [DOCKER_README.md](DOCKER_README.md)

### FHIR

Check out [FHIR_README.md](FHIR_README.md)

### For Developers

If using OpenEMR directly from the code repository, then the following commands will build OpenEMR (Node.js version 24.* is required) :

```shell
composer install --no-dev
npm install
npm run build
composer dump-autoload -o
```

### Contributors

This project exists thanks to all the people who have contributed. [[Contribute]](CONTRIBUTING.md).
<a href="https://github.com/openemr/openemr/graphs/contributors"><img src="https://opencollective.com/openemr/contributors.svg?width=890" /></a>


### Sponsors

Thanks to our [ONC Certification Major Sponsors](https://www.open-emr.org/wiki/index.php/OpenEMR_Certification_Stage_III_Meaningful_Use#Major_sponsors)!


### License

[GNU GPL](LICENSE)

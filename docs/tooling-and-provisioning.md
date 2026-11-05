# Tooling and Provisioning

> Status: draft rev 3 · Date: 2026-11-05 (WIB) · **Phase 3** (Phase 0 uses `base` only)

## 1. Problem

Agents run tests, start services, drive browsers, and install packages. Ad-hoc
installs are slow, non-reproducible, and a supply-chain risk.

## 2. Three-tier model

### Tier 1 — Baked toolbox images (multi-arch: amd64 + arm64)

| Image | Contents | Phase |
|---|---|---|
| `sireum-runner:base` | git, jq, node, python, pinned Claude Code + OpenCode | 0 |
| `sireum-runner:browser` | Playwright base + the above | 3 |
| `sireum-runner:python` / `:node` | language extras | 5 |

- Built with `docker buildx --platform linux/amd64,linux/arm64`; pinned by digest
  in the hub registry; workers pre-pull.
- Bridge selects from a fixed allowlist.

### Tier 2 — Repo manifest (read at `base_commit`)

```yaml
# .agent/tools.yaml
version: 1
apt: [libpq-dev]                 # image-build time only
npm:
  install: npm ci
pip:
  install: pip install -r requirements.txt --require-hashes
playwright:
  version: "1.5x"
  browsers: [chromium]
test_targets:                    # cards reference these by name only
  auth: pytest -q tests/auth
  unit: pytest -q
  e2e:  npx playwright test
# Phase 6 — requests (the operator approves per repo; a job gets the intersection)
secrets: [STRIPE_TEST_KEY]       # names only; values live on the hub
registries: [npm-company]        # defined on the hub with `sireum-hub registry add`
egress: [api.stripe.com]         # extra hosts (fleet-wide once approved)
services: [postgres, redis]      # throwaway, inside the job; no approval needed
protected_paths:                 # changes here set tests_modified=true
  - tests/
  - .agent/
  - .github/
  - .gitlab-ci.yml
  - pytest.ini
```

- Prepare runs before the agent with `prepare_timeout_sec`; cache key
  `hash(image_digest + tools.yaml + lockfiles)`.

### Tier 3 — Agent-initiated installs (bounded)

Non-root, through `sireum-egress` only, no `sudo`/`apt`; missing system package ⇒ `blocked`.

## 3. Playwright

- `:browser` from `mcr.microsoft.com/playwright:v<version>-noble` (multi-arch), pinned.
- `PLAYWRIGHT_BROWSERS_PATH=/cache/ms-playwright/<version>`, `shm_size >= 1024`,
  4–8 GB RAM — size the worker Linux env accordingly.
- Allowlist the Playwright CDN only when browser jobs are enabled.

## 4. Job class and capacity

| Class | CPU | Memory | SHM | Use |
|---|---|---|---|---|
| `standard` | 2 | 4 GB | 64 MB | code, tests |
| `browser` | 4 | 8 GB | 1 GB | Playwright/E2E |
| `heavy` | 4–8 | 8–16 GB | 1 GB | builds |

A worker env sized 4 CPU / 8 GB fits two `standard` or one `browser` job.

## 5. What the agent is told

`AGENT_ENV.md`: tools and versions, network only via the proxy, `apt`
unavailable, do not edit `protected_paths` unless the task says so, **do not push
— the runner hands commits to the hub**.

## 6. Open questions

- Which repos need Playwright first?
- Separate browser pool or mixed?
- Private npm/PyPI mirror?

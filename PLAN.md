# Exec Plan — Sireum (Adaptiv agent workers on Nomad)

> **Status:** Draft rev 3 (operator answers applied, pending approval)
> **Created:** 2026-11-05 (WIB) · **Revised:** 2026-11-05 (WIB)
> **Scope owner:** Adaptiv
> **Engine:** HashiCorp Nomad (Community Edition, internal use)
> **Control plane:** `sireum-hub` (always-on home PC, Tailscale) — **not** `hermes-host`
> **Task source:** Hermes kanban on `hermes-host` (public SSH, forced command)
> **Related:** `GROOMING.md`, `docs/architecture.md`, `docs/job-contract.md`

## 1. Goal

Give Hermes an **optional booster**: kanban cards a human tags `remote` run on
**idle PCs (Windows, macOS, Linux)** with **Claude Code and/or OpenCode**,
sandboxed, capacity-aware, with workspace reuse, and results written back to the card.

Done means: a `remote`-tagged card produces an `agent/<job_id>` branch / MR on its
git host, a kanban comment, and a Discord notification; and with the whole Sireum
fleet switched off, Hermes behaves exactly as it does today.

## 2. Non-goals

- Replacing or becoming a dependency of Hermes gateway, kanban, Discord, Telegram.
- Running anything long-lived on `hermes-host` (no Tailscale there either).
- Multi-tenant SaaS, billing, or per-customer isolation.
- Training or serving models.
- Integration with systems outside Adaptiv.

## 3. Requirements

| # | Requirement | Covered by |
|---|---|---|
| R1 | Kanban first; Notion/Jira/GitHub later | `sireum-bridge` source adapters |
| R2 | Claude Code and OpenCode | `sireum-runner` executor adapters |
| R3 | Subscription auth (Claude Max, OpenCode Go) | seat mode; API mode opt-in |
| R4 | Both agents on one PC | same image, payload selects executor |
| R5 | Any idle PC: **Windows, macOS, Linux** | Linux VM per PC (WSL2 / Lima / native) |
| R6 | Capacity/backlog aware | Nomad scheduler + **configurable per-executor limits (default unlimited)** |
| R7 | Workspace/cache reuse | hub mirror + worker mirror + worktree |
| R8 | Source-agnostic: any git host by **URL + API key** | git relay on the hub |
| R9 | Zero licence cost (Tailscale: see note) | Nomad CE, Docker Engine, Lima/WSL2, squid, OSS. **Tailscale's free Personal plan is "only suitable for non-commercial use"; business use is Standard at $8/user/month (tagged devices don't add seats).** |
| R10 | Do not reinvent | Nomad owns placement/queue/isolation |
| R11 | Reproducible runtime tools | toolbox images + `.agent/tools.yaml` |
| R12 | Booster, not dependency | opt-in tag, claim grace, fallback to Hermes |
| R13 | Nothing running on `hermes-host` | hub on home PC; public SSH forced command only |

## 4. Architecture

```text
Hermes gateway + kanban (hermes-host)          ← unchanged; source of truth
        ▲  ssh sireum@<hermes-host public host>  (forced command: sireum-kanban …)
        │
sireum-hub (always-on home PC, Tailscale)
  - sireum-bridge: poll/claim, lease, dispatch, results, fallback, limits
  - git relay: hub mirrors, read-only clone for workers, bundle intake,
               push + MR to the git host with its API key
  - Nomad server · registry · SQLite · Discord webhook
        │  nomad job dispatch sireum-runner
        ▼
Idle PCs: Windows (WSL2) · macOS (Lima VM) · Linux (native)
  Linux env: Docker Engine + Nomad client + Tailscale
  docker network "sireum-jobs" (internal, no route out)
     sireum-runner ──► sireum-egress (squid) ──► hub (bridge + git) / model endpoints / package registries
```

See `docs/architecture.md`.

## 5. Stack: buy vs build

| # | Layer | Buy/Build | What | Cost |
|---|---|---|---|---|
| L1 | Task intake | **Build** (thin) | `sireum-bridge` + `sireum-kanban` SSH wrapper | $0 |
| L2 | Queue + state | **Buy** | Nomad dispatch + SQLite in the bridge | $0 |
| L3 | Placement + capacity | **Buy** | Nomad scheduler, constraints, drain, `disconnect` | $0 |
| L4 | Sandbox | **Buy** | Docker Engine in a Linux env; internal network; non-root | $0 |
| L4b | Egress control | **Buy** | squid allowlist + Tailscale ACLs | $0 |
| L5 | Worker runtime | **Buy** | WSL2 (Windows), Lima (macOS), native (Linux) + Nomad client | $0 |
| L5b | Tool provisioning | **Build + buy** | pinned multi-arch toolbox images + `.agent/tools.yaml` | $0 |
| L6 | Agent executor | **Build** (thin) | Claude Code (Max), OpenCode (Go) | existing subscriptions |
| L7 | Workspace cache | **Build** (thin) | hub mirror → worker mirror → worktree | $0 |
| L7b | Git relay | **Build** (thin) | `git http-backend` (read-only) + bundle intake + host API adapters | $0 |
| L8 | Secrets | **Buy + policy** | Nomad Variables (seat tokens); git API keys only on the hub | $0 |
| L9 | Result + Hermes | **Build** (thin) | kanban via SSH wrapper; Discord webhook | $0 |

## 6. Components and deliverables

| Component | Runs on | Responsibility | Deliverable |
|---|---|---|---|
| `sireum-bridge` | hub | Poll/claim, lease, dispatch, results, fallback, limits, `/status` | Service + API |
| git relay | hub (in the bridge) | Mirrors, read-only clone, bundle verify, push, MR | Part of the bridge |
| Nomad server | hub | Scheduling, state, drain, reschedule | Config + snapshot cron |
| Registry | hub | Multi-arch runner images (tailnet only) | `distribution` container |
| `sireum-kanban` | `hermes-host` | SSH forced-command wrapper over `hermes kanban` | One script + `authorized_keys` line |
| Worker Linux env | each idle PC | WSL2 / Lima VM / native Linux | Per-OS setup in `docs/operations.md` |
| Nomad client | worker Linux env | Register capacity, run allocations | `client-bootstrap.sh` |
| `sireum-egress` | worker Linux env | squid allowlist for job containers | squid container + `squid.conf` |
| `sireum-runner` | Nomad allocation | Lease, workspace, executor, gate, bundle, report | Multi-arch Docker image |

## 7. Job contract

Canonical `Job` and `Result`: `docs/job-contract.md`. Non-negotiable invariants:

- `idempotency_key` = `job_id`. **The hub accepts a bundle only from the live lease.**
- One worktree per job; branch `agent/<job_id>`; the hub never pushes to the
  default branch and never force-pushes.
- **Workers hold no git credentials.** They clone read-only from the hub and
  upload a bundle; the hub pushes with the host's API key.
- Hard `timeout_sec` and `max_turns`; `max_cost_usd` only in API mode.
- Test commands come from `.agent/tools.yaml` **at `base_ref`**, never from card text.
- Result always posted (or spooled), success or failure.
- The only model credential on a worker is a per-node, revocable seat token,
  present only in the allocation's `secrets/` dir during a job.

## 8. Booster model (R12)

- **Opt-in:** only cards tagged `remote` are considered.
- **Claim:** atomic `sireum-kanban claim <card>`; dispatch only on success.
- **Claim grace:** a `remote` card not dispatched within `CLAIM_GRACE` (default
  10 min) — no capacity, executor paused, fleet offline — is released: `remote`
  removed, comment added, Hermes handles it.
- **Results:** `success` → complete · `needs_review` → request review ·
  `blocked` → block · `failed` / `cancelled` → release to Hermes.
- **Hub down:** untagged cards unaffected; `remote` cards need a Hermes-side
  staleness rule or manual `sireum-kanban release` (open question).

## 8b. Repo per card

The repo is chosen by each task, not fixed per deployment.

- **Card → repo:** the card names its repo with a label `repo:<git_host>/<path>`
  (e.g. `repo:github-adaptiv/adaptiv/example`), or a short alias defined on the hub
  (`repo:example`). Other sources (Notion/Jira/GitHub) map their own field to the
  same pair in their adapter.
- **Onboarded repos only:** the hub keeps `repos.yaml` — alias, `git_host`, path,
  default branch, enabled. A repo is enabled only after its default branch is
  protected on the host and the host's API key can push branches and open MRs.
- **Unknown, disabled, or missing repo** → no dispatch; comment
  ("Sireum: repo not onboarded") and release to Hermes.
- **Lazy mirror:** first job for a repo creates the hub mirror; later jobs fetch.
- Onboarding a repo = one `repos.yaml` entry + branch protection; no code change.

## 9. Capacity, limits, and scheduling

### Per-executor limits — configurable, default unlimited

```text
EXECUTOR_LIMITS="claude=0,opencode=0"     # 0 or unset = unlimited (default)
EXECUTOR_LIMITS="claude=4,opencode=2"     # example cap
```

- **Default: unlimited.** Concurrency is bounded only by fleet capacity (Nomad).
- A non-zero value caps concurrent jobs per executor (lease-based semaphore).
- **Auto-pause on usage limits:** when a runner reports `error.code=usage_limit`
  (5-hour/weekly cap, 429), the bridge pauses that executor until the reported
  reset time, or backs off 15 → 30 → 60 min if none is given. While paused:
  - cards with `executor: auto` go to the other executor;
  - cards pinned to the paused executor wait up to `CLAIM_GRACE`, then return to Hermes;
  - the failed job's card returns to Hermes.
- `/status` shows running jobs, limit hits, and pause windows per executor so the
  limits can be tuned from real data.
- Subscriptions are shared with the operator's own interactive use.

### Placement

- Node meta: `pool=idle`, `os=linux` (the Linux env), `host_os=windows|macos|linux`,
  `agents=claude,opencode`, `region=...`. Architecture via `${attr.cpu.arch}`;
  runner images are multi-arch (amd64 + arm64) so no arch constraint is needed.
- The hub runs no jobs.
- Each job reserves `cpu=2000`, `memory=4096`; the Linux env's fixed VM size
  bounds slots per PC.
- Sleep/wake: `disconnect { lost_after = "10m", replace = true, reconcile = "keep_replacement" }`
  plus lease fencing at bundle intake.

## 10. Security model (summary)

Full detail: `docs/security-and-credentials.md`.

- Job containers on an **internal Docker network**; all egress via squid
  (hub, model endpoints, package registries). No direct git-host access.
- Tailscale ACL: workers → `sireum-hub:443,4647,5000` only.
- Non-root, `cap_drop ALL`, `no-new-privileges`, read-only root, no `docker.sock`.
- **Git API keys live only on the hub.** The hub verifies each bundle (single ref
  `agent/<job_id>`, descends from `base_ref`, size cap) before pushing.
- Card text is untrusted; only allowlisted users can tag `remote`.
- `hermes-host`: public SSH, key restricted to the forced command; no daemon.
- No auto-merge.

## 11. Phases

Estimates: one engineer + Claude Code. The critical path is the non-compressible items.

### Phase 0 entry gate — status

- [x] Workers: Windows, macOS, Linux → Linux env per PC (WSL2 / Lima / native).
- [x] Git: any host by URL + API key → git relay on the hub.
- [x] Subscriptions: Claude Max + OpenCode Go; limits configurable, default unlimited;
      terms ambiguity **accepted by the operator** (see risks).
- [x] Tailscale: hosted Tailscale; on the hub now; add to each worker Linux env;
      not on `hermes-host`.
- [~] `sireum-hub`: **tentative** — not needed for Phase 0 (stub runs on the MBP);
      must be decided before Phase 1.
- [x] First worker: **MacBook Pro M2** → Lima `vz` Ubuntu VM, **arm64**.
- [x] Repo: **per card** (from the Hermes kanban / source), not one fixed repo.
      Repos must be onboarded on the hub (see §8b). Phase 0 uses a throwaway
      sandbox repo on one host.

### Phase 0 — prove the runner on the MBP M2 (≈1–2 days)

**Progress (2026-11-05):** code is complete for Phase 0, Phase 1, Phase 3 and the
GitHub Issues part of Phase 4. Everything runs with a fake executor against a local sandbox
git host; nothing has run on real hardware yet.

| Area | Built | Verified locally | Needs the operator / real hardware |
|---|---|---|---|
| Bridge: lease/fencing, git relay, bundle intake, push, results, limits, auto-pause, reconciler | ✓ | 14 e2e tests | real git host push + MR |
| Runner: clone, worktree, executor, tests, bundle, spool, heartbeat | ✓ | e2e + Docker smoke (12 checks) | one real Claude / OpenCode seat job; `max_turns` |
| Runner image `:base` (arm64 + amd64) | ✓ | arm64 built and run; amd64 built and run under emulation | push to the hub registry |
| Egress: internal network + squid | ✓ (squid.conf log fix) | hub/model OK; git host, LAN, tailnet, direct all blocked | inside Lima / WSL2 |
| Booster loop: poll, allowlist, claim, grace release, write-back outbox | ✓ | file-source tests | real `hermes kanban` |
| `sireum-kanban` forced command | ✓ | against a fake `hermes` CLI | **match the command templates to the real CLI** |
| Enrollment: `sireum-hub invite`, enroll / seats / leave | ✓ | tests (no Tailscale/Nomad) | Tailscale OAuth key minting, Nomad Variables |
| Hub installer (`hub/install-hub.sh`), Nomad server + ACL policies, crons | ✓ | syntax only | run on the hub PC |
| Native macOS hub (`hub/install-hub-macos.sh`) + GHCR pull auth | ✓ | bash 3.2 syntax, launchd plists lint, bridge + enrol on macOS Python 3.9, 27 tests on 3.9 and 3.13 | run on the hub Mac; GHCR package + tokens |
| Worker installers (`install/`) | updated | syntax only | MBP (Lima), one Windows PC |
| Prepare step + `:browser` image (Phase 3) | ✓ | 4 prepare tests | build `:browser` (skipped here: low disk) |
| GitHub Issues source + GitHub/GitLab/Gitea MR adapters (Phase 4) | ✓ | fake-API tests | real APIs |
| Notion / Jira sources, gVisor, OSV, mirrors (Phases 4–5) | — | — | not started |

- Everything on one machine: the bridge **stub** runs on macOS (or a second Lima
  VM) as a stand-in hub; the worker runs in the `sireum` Lima VM.
- arm64 `sireum-runner:base` first (pinned Claude Code + OpenCode); amd64 added
  to the multi-arch build before the first amd64 worker joins.
- Lima env: size to at most half the Mac's RAM (e.g. 4 CPU / 8 GB on a 16 GB
  MBP; on 8 GB use 2 CPU / 4 GB and one `standard` job). Laptop sleep / lid close
  = node loss; keep it awake (plugged in, `caffeinate`) during tests.
- Stub: lease, read-only git mirror, bundle intake → push to the sandbox repo's
  host with its API key, result endpoint.
- One manual `docker run` (no Nomad, no kanban).
- Moving to the real hub in Phase 1 is a config change (`BRIDGE_URL`, Tailscale name).
- Test the macOS installer (`install/install.sh`) end to end on the MBP; fix
  until a fresh Mac goes from join code to `sireum check` all ✓ in one command.

### Phase 1 addition — simple onboarding
- `sireum-hub invite` (Tailscale API auth key + one-time enroll token → join code).
- Bridge endpoints: `/v1/nodes/enroll`, `/v1/nodes/{name}/seats`, `DELETE /v1/nodes/{name}`.
- Windows installer (`install/install.ps1`) tested on one Windows PC.
- Verify: branch appears on the git host via the hub; worker has no git
  credential; non-allowlisted, LAN, and mesh targets blocked; seat token absent
  after the job.

### Phase 1 — hub + one card end-to-end (2–3 days)
- Hub: Nomad server, registry, bridge (SQLite), snapshot cron, UPS if available.
- `sireum-kanban` on `hermes-host` via public SSH.
- Bridge: poll, claim, dispatch, results, Discord, claim grace + release,
  per-executor limits + auto-pause.
- MR creation adapter for the first host type; default-branch protection on that repo.
- Verify: a real card completes; fleet-off leaves Hermes unchanged; timeout kills;
  redispatch idempotent; simulated `usage_limit` pauses the executor and releases cards.

### Phase 2 — mixed fleet, sleep/wake (2–3 days)
- Add a second PC on a different OS (e.g. Windows WSL2 + macOS Lima).
- Tests: suspend mid-job; WSL2 shutdown; hub reboot mid-job (spool); drain.
- Verify: one branch per job; stale allocation's bundle rejected; no leaked slots.

### Phase 3 — tooling
- `:browser` image, `prepare` phase, Playwright cache, test targets.

### Phase 4 — more sources and hosts
- Notion/Jira/GitHub Issues adapters; more git host adapters (Gitea, generic push-only).

### Phase 1.5 — simplified operator setup (in progress)
Goal: after the hub install, the operator's only manual work is the Claude Code /
OpenCode seat tokens on each worker.
- No registry: the hub serves the runner source; each worker builds the image whose tag
  matches the hub (`sireum-runner:local-<hash>`); installers copy the local repo instead
  of downloading a tarball; `sireum rebuild` after hub updates.
- Join codes without Tailscale OAuth: the worker installer runs `tailscale up` and the user
  clicks the login link.
- `sireum-hub add-repo owner/repo`: snapshot of `gh auth token` into a 0600 file, detects
  the default branch, checks push permission and branch protection (offers to protect),
  writes `repos.yaml`; applied by hot-reload (no restart).
- `sireum-hub connect-hermes user@hermes-host --taggers …`: installs `sireum-kanban` over
  SSH, accepts the host key, sets `SOURCE` / `REMOTE_TAGGERS`, restarts the bridge.
- Deferred (optional): Tailscale ACL, `OFFSITE` backups.
- Built so far (uncommitted): credential resolver (`bridge/sireum_bridge/creds.py`) and
  config hot-reload.

### Phase 6 — credentials, packages, secrets, access requests
Principle (industry practice): short-lived, least-privilege credentials held by the hub;
workers and the agent see as little as possible; a repo **requests** access in its
manifest and the operator **approves** it in `repos.yaml`; a job only receives the
intersection. Every credential uses one pluggable source: `api_key_file`, `api_key_env`,
`api_key_cmd` (gh, 1Password `op`, Vault, Keychain, pass), or `github_app`.

6a. **Git credentials** — `gh` snapshot now (Phase 1.5); GitHub App (1-hour per-repo
    tokens, auto installation lookup) as the target; per-repo overrides.
6b. **Private packages** — `registries.yaml` with `mode: relay` (hub auth relay; no token
    on workers; npm, PyPI, Go adapters) or `mode: inject` (token only during prepare,
    removed before the agent runs); **scoped names** (e.g. `@company/*` never resolves to
    the public registry — dependency-confusion guard); private git submodules via hub
    mirror (`url.<hub>.insteadOf`).
6c. **Test secrets and services** — throwaway service containers per job (Postgres,
    Redis, MinIO, Mailpit; no real secrets); per-repo secrets with exposure
    `prepare` (default) / `test` / `agent`; values redacted from logs and results; extra
    egress hosts per repo only when approved; test-mode/staging credentials only.
6d. **Access requests** — runner detects missing access (install 401/403/404, squid
    denials, agent `SIREUM-NEED: <kind> <name> "<reason>"`, missing-env test errors) →
    `blocked` with `error.code=needs_access` and a structured `needs` list; partial work
    kept on its branch; card + Discord message with one command each:
    `sireum-hub needs` / `grant <id>` / `deny <id> "<note>"`; grant writes the per-repo
    approval and re-runs the job. Pre-flight: the hub compares the manifest's requests
    with approvals before dispatch and asks first.
6e. **Maintenance** — `sireum-hub secrets` (inventory: source, repos, last used, exposure;
    never values), `sireum-hub doctor` (test every credential), expiry warnings 7 days
    ahead (PAT expiry header; App tokens self-renew), unused-for-60-days warnings,
    `revoke`, per-job audit of what was granted.
6f. **Signing** — default: signed at merge (GitHub squash/rebase); per repo: hub SSH
    signing or GitHub App API commits when every branch must be signed.

Verify: private npm package installs via relay with no token in the job; `@company/*`
never hits the public registry; a secret with `prepare` exposure is absent during the
agent phase; a job missing a secret ends `needs_access`, `grant` re-runs it to success;
`doctor` flags an expired token; audit shows granted names only.

### Phase 7 — hub MCP for Hermes (via Tailscale Funnel)
Goal: Hermes (or any MCP client) helps operate the hub. Nothing is installed on
`hermes-host`; the MCP server runs in the bridge on the hub.

- **Transport:** a separate bridge listener (`127.0.0.1:8790`) that serves **only**
  `/mcp` (MCP streamable HTTP). Published with
  `tailscale funnel --bg --https=8443 http://127.0.0.1:8790` → `https://<hub>.ts.net:8443/mcp`.
  The bridge API stays tailnet-only on 443 (Serve); Funnel and Serve can't share a port.
- **Auth:** per-client bearer token (`sireum-hub mcp-client add hermes --tier operate`),
  stored hashed; rate limit + lockout after repeated failures; optional source-IP allowlist
  if Funnel forwards the caller address (verify); every call logged with client id and
  posted to Discord for operate-tier actions.
- **Tool tiers:**
  - *read* — `status`, `runs`, `run_detail`, `nodes`, `needs`, `doctor`, `secrets_inventory`
    (names/sources/expiry only), `job_log_tail` (redacted).
  - *operate* — `pause_executor` / `resume_executor`, `drain_node` / `undrain_node`,
    `cancel_job`, `rerun_job`, `release_card`, `refresh_mirror`.
  - *sensitive* — `grant` / `deny`, `add_repo`, set/rotate secret, `invite`, `revoke`,
    remove node: **propose only**. The hub creates a pending action; the operator confirms
    out of band (`sireum-hub confirm <code>` or a Discord button). Secret values and join
    codes never pass through MCP.
- Hermes gets *read* + *operate*. A leaked token can at most read status and pause/resume.
- Same tools back the CLI, the HTTP API and MCP (one implementation).
- Depends on: Phase 6d (access requests / pending actions).
- Verify: Hermes connects over Funnel and lists tools; a wrong token is rejected and
  locked out; `/v1/*` is unreachable via the Funnel port; a sensitive call only creates a
  pending action; confirm executes it; no secret value appears in any MCP response or log.
- To check first: Hermes supports remote MCP servers over HTTP; Funnel caller-IP header;
  Funnel is beta with unpublished bandwidth limits (MCP traffic is small).

### Phase 5 — hardening / optional
- gVisor, OSV scan, private npm/PyPI mirror, `:python`/`:node`, GPU node class.

## 12. Verification checklist

- [ ] With Sireum stopped entirely, Hermes handles every card as before.
- [ ] Untagged cards never claimed; unclaimable `remote` cards released after `CLAIM_GRACE`.
- [ ] Workers on Windows (WSL2), macOS (Lima) and Linux show in `nomad node status`.
- [ ] arm64 and amd64 nodes both run the same image tag.
- [ ] Job container: non-allowlisted domain, git host, LAN, mesh, `hermes-host` all fail.
- [ ] No git credential exists anywhere on a worker.
- [ ] Hub rejects a bundle with another ref, a non-descendant history, or a stale lease.
- [ ] Hub never pushes the default branch and never force-pushes.
- [ ] `EXECUTOR_LIMITS` unset ⇒ unlimited; set ⇒ enforced.
- [ ] `usage_limit` from a runner pauses that executor; `auto` cards switch executor.
- [ ] Node suspend mid-job → exactly one branch, no leaked slot.
- [ ] `timeout_sec` and `max_turns` kill a runaway agent.
- [ ] Diff touching tests ⇒ `needs_review`; card text cannot inject commands.
- [ ] Results and bundles spooled when the hub is down, uploaded later.
- [ ] Disk alarm at 80% on workers and hub; Nomad snapshot exists off the hub.

## 13. Rollback

1. Stop `sireum-bridge` (no new claims/dispatches/pushes).
2. `nomad job stop -purge sireum-runner`.
3. Release claimed cards (`sireum-kanban release <card>` or remove `remote` by hand).
4. Remove the `sireum` key line from `hermes-host`'s `authorized_keys`.
5. Drain and remove clients; `wsl --unregister` / `limactl delete` the worker envs if retiring.
6. Bad runner image: redeploy the previous pinned digest.

## 14. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Tailscale licensing: free Personal plan is non-commercial only; Sireum is internal business tooling | Medium | Low | Move the tailnet to Standard ($8/user/month, ~$8–16/month for 1–2 operators); features used are on all plans |
| Public MCP endpoint via Funnel (beta) | Medium | Medium | MCP-only listener on 8443; per-client tokens + lockout; read/operate tiers only; sensitive actions propose-then-confirm; no secrets over MCP |
| Subscription terms: one consumer account used concurrently from several machines | Unknown | High | **Operator accepted** (2026-11-05). Limits configurable; switch to Team/API by config if needed |
| Usage caps hit with unlimited concurrency | Medium | Medium | Auto-pause + `auto` executor failover + release to Hermes; tune `EXECUTOR_LIMITS` |
| Sireum consumes the operator's personal quota | Medium | Medium | Visible in `/status`; cap via `EXECUTOR_LIMITS` |
| Hub (home PC) offline | Medium | Low | Booster model; spool; UPS; snapshot off-host |
| Git API keys concentrated on the hub | Medium | High | Hub trusted + Tailscale-only; keys 0600; scoped per host; rotate |
| Prompt injection → exfiltration | Medium | High | Internal network + squid; no git or prod creds on workers; MR-only |
| WSL2 / Lima env stops when PC idles or user logs out | High | Medium | Keep-alive + autostart; Nomad treats it as node loss |
| Idle PC sleeps mid-job | High | Medium | `disconnect` + lease fencing + one reschedule |
| arm64/amd64 drift | Low | Medium | Multi-arch build + smoke test per arch |
| Sandbox escape | Medium | High | Non-root, cap drop, VM boundary on Win/mac; gVisor later |
| Disk growth (hub mirrors, worker caches) | Medium | Medium | Caps, prune, alerts |
| Runtime tool install / supply chain | High | High | Pinned images, lockfiles, allowlist, no sudo/apt |

## 15. Open questions

| Question | Owner | Needed by |
|---|---|---|
| Which PC is `sireum-hub`? (tentative; may be a Mac → `hub/install-hub-macos.sh`) | operator | Phase 1 |
| ~~MBP M2 RAM~~ — **8 GB** ⇒ Lima 2 CPU / 4 GB, one `standard` job | operator | Phase 0 ✓ |
| Sandbox repo + its host for Phase 0 | operator | Phase 0 |
| Initial `repos.yaml` list (which repos may be boosted) | operator | Phase 1 |
| Which private registries (npm / PyPI / Go / other) and package scopes? | operator | Phase 6b |
| Which repos need test secrets, staging access, or signed commits on every branch? | operator | Phase 6c/6f |
| GitHub App now or later (vs `gh` snapshot)? | operator | Phase 6a |
| `hermes kanban` atomic claim / tag add-remove? | operator | Phase 1 |
| Hermes staleness rule for `remote` cards? | operator | Phase 1 |
| Who may tag `remote`? | operator | Phase 1 |
| Who reviews agent MRs? | operator | Phase 1 |
| Does Hermes support remote MCP servers over HTTP (and auth headers)? | operator | Phase 7 |
| Confirm sensitive MCP actions via CLI only, or also a Discord button (needs a bot)? | operator | Phase 7 |
| Tailscale plan: stay Personal or move to Standard for business use? | operator | Phase 1 |

## 16. Decision log

| Date | Decision | Rationale |
|---|---|---|
| 2026-11-05 | Nomad over Windmill | Capacity-aware placement + per-job containers on unreliable PCs |
| 2026-11-05 | Hermes/kanban stays the UI | No dashboard to build |
| 2026-11-05 | Build only bridge + runner | Queue, placement, isolation, retries are bought |
| 2026-11-05 | Contracts engine-agnostic | Engine swap touches only the bridge |
| 2026-11-05 | Name: **Sireum** | Sundanese "ant": many small workers |
| 2026-11-05 | Booster, not dependency | Hermes must work identically with Sireum off |
| 2026-11-05 | Control plane on an always-on home PC | Keep `hermes-host` free of new load |
| 2026-11-05 | Internal Docker network + squid egress | Host networking defeated the allowlist |
| 2026-11-05 | Lease + fencing | Nomad reschedules bypass dispatch-level dedup |
| 2026-11-05 | Test commands from repo at `base_ref` | Card text and agent-edited tests are untrusted |
| 2026-11-05 | **Windows/macOS/Linux workers via a Linux env** (WSL2 / Lima / native) | Keeps one sandbox design for every OS |
| 2026-11-05 | **Git relay on the hub** (bundle in, hub pushes with API key) | Any host by URL + API key; static keys never reach workers; branch scope enforced in code |
| 2026-11-05 | **Per-executor limits configurable, default unlimited** | Operator's usage never hits caps; auto-pause handles limits when they occur |
| 2026-11-05 | Subscription terms risk accepted by operator | Claude Max + OpenCode Go; Team/API is a config switch later |
| 2026-11-05 | **Public SSH to `hermes-host`, no Tailscale there** | Avoid a new daemon on `hermes-host` |
| 2026-11-05 | Browser/prepare in Phase 3 | Not needed to prove the runner |
| 2026-11-05 | **Phase 0 entirely on the MBP M2** (stub hub + Lima worker) | Hub host still tentative; arm64 first |
| 2026-11-05 | **Repo per card** via `repo:` label, onboarded repos only | Tasks span repos; unknown repos return to Hermes |
| 2026-11-05 | **One-command worker install with a join code** + `sireum` helper | Manual setup was too technical for non-ops users; manual path kept in operations.md |
| 2026-11-05 | Bridge in Python stdlib + PyYAML, SQLite, no web framework | Small surface; runs anywhere the hub has python3 |
| 2026-11-05 | Workers share one Nomad `node:write` token by default | Nomad can't scope node:write to one node; per-PC minting optional (`NOMAD_MINT_NODE_TOKENS=1`, needs a management token) |
| 2026-11-05 | Hub registry is plain HTTP on the tailnet (`insecure-registries`) | Tailscale serve can't expose :5000 with TLS; traffic is WireGuard-encrypted |
| 2026-11-05 | Runner fetches hub refs into `refs/remotes/hub/*` | A shared mirror's `fetch --prune` deleted concurrent jobs' local branches (found in tests) |
| 2026-11-05 | Hermes kanban calls isolated in `/etc/sireum-kanban.conf` templates | Hermes CLI not available to verify; one file to adjust on hermes-host |
| 2026-11-05 | **Hub MCP over Tailscale Funnel** (MCP-only listener, tiered tools, propose-then-confirm) | Hermes can help operate the hub with nothing installed on `hermes-host`; public exposure limited to a low-privilege MCP surface |
| 2026-11-05 | **Credentials: one pluggable source model** (file / env / cmd / GitHub App), per host with per-repo override, hot-reloaded | Flexible and rotatable without restarts; secret managers plug in via commands |
| 2026-11-05 | **Repo requests, operator approves**; job gets the intersection | A card or a branch can't grant itself secrets, registries or egress |
| 2026-11-05 | **Missing access ⇒ fail fast as `needs_access`, grant, re-run** (no mid-run waiting) | Doesn't hold slots or seats; keeps an explicit approval point (industry norm for agents/CI) |
| 2026-11-05 | Secrets default to **prepare-only** exposure; private packages via hub relay + scoped names | Keeps secrets out of the agent's reach; blocks dependency confusion |
| 2026-11-05 | **Native macOS hub** (`hub/install-hub-macos.sh`: launchd, pinned Nomad binary, venv); runner image on **GHCR** with a read-only pull token for workers | Hub may be a Mac; a VM + Docker would cost ~3 GB RAM for a ~200 MB hub. Nomad kept (dropping it was costed and declined) |

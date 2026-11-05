# Grooming Results — Sireum (Adaptiv agent workers)

> **Date:** 2026-11-05 (WIB) · **Revised:** 2026-11-05 (rev 3, operator answers)
> **Status:** grooming complete, decisions made
> **Decision:** HashiCorp **Nomad** as the execution substrate
> **Scope:** Adaptiv only

## 1. Context

Adaptiv runs Hermes (`hermes-gateway`) on `hermes-host` with 19
profiles, Discord/Telegram routing, cron, and a SQLite kanban. `hermes-host` is
resource-constrained (disk pressure incident at 96–98%).

Goal: **boost** Hermes by offloading opt-in coding tasks to idle PCs (Windows,
macOS, Linux) running Claude Code / OpenCode — without loading `hermes-host` and
without Hermes depending on Sireum.

Key realisations:

- The bottleneck is tool execution and isolation, not model inference.
- Sireum is a **booster**: Hermes behaves identically with Sireum off.
- Nothing new runs on `hermes-host`; the control plane is an always-on home PC.
- Throughput is bounded by fleet capacity and subscription usage caps, not by a
  fixed seat count; limits are configurable and default to unlimited.

## 2. Requirements

### Must

- M1: Accept opt-in tasks (`remote` tag) from Hermes kanban.
- M2: Run Claude Code and/or OpenCode from one image.
- M3: Subscription auth: Claude Max + OpenCode Go (API mode opt-in).
- M4: Workers on **Windows, macOS, Linux** with a documented setup per OS.
- M5: Schedule against capacity; survive a PC sleeping or dying.
- M6: Reuse repo workspaces/caches.
- M7: Any git host by **URL + API key**.
- M8: Zero licence cost.
- M9: Per-job isolation and enforced egress allowlist.
- M10: Results to kanban + Discord.
- M11: Reproducible runtime tools.
- M12: Booster, not dependency.
- M13: No long-lived process or disk growth on `hermes-host`.
- M14: Per-executor concurrency limits, **configurable, default unlimited**, with
  auto-pause on usage limits.

### Should

- S1: Notion/Jira/GitHub Issues adapters.
- S2: API / Team mode by config.
- S3: Run history and failure alerts.

### Could

- C1: Web UI. C2: Wake-on-LAN. C3: GPU/local-model node class.

### Won't (for now)

- W1: Repo hosting, CI, model serving. W2: Auto-merge.

## 3. Options evaluated

| Option | Verdict |
|---|---|
| **Nomad CE** | **Chosen** — bin-packing, drain, reschedule, Docker isolation |
| Windmill CE | Runner-up — capacity features paid; per-job isolation DIY |
| Coder | Too heavy |
| K8s + KEDA | Overkill |
| Temporal | Needs a substrate |
| Jenkins / custom broker / hosted agents | Rejected |

### Control-plane host

| Option | Verdict |
|---|---|
| `hermes-host` | Rejected — keep it free of new load |
| Cloud VPS | Viable, small monthly cost |
| **Always-on home PC** | **Chosen** — acceptable because Sireum is a booster |

### Worker OS strategy

| Option | Verdict |
|---|---|
| Native Nomad client on Windows/macOS + Docker Desktop | Rejected — different sandbox per OS; Docker driver on macOS/Windows is dev-grade |
| **Linux env per PC** (WSL2 on Windows, Lima on macOS, native on Linux) | **Chosen** — identical sandbox, egress, cache, bootstrap everywhere |

### Git access

| Option | Verdict |
|---|---|
| Per-job tokens on workers (GitHub App / GitLab token) | Rejected — needs host-specific minting; static API keys can't be per-job |
| **Git relay on the hub** | **Chosen** — workers clone read-only from the hub and upload a bundle; the hub pushes with the host's API key and opens the MR |

## 4. Why Nomad

Capacity on unreliable nodes, per-job containers, one binary per client, zero
licence cost, kanban + Discord as UI.

## 5. Cost and licensing

| Item | Licence | Cost |
|---|---|---|
| Nomad CE | BUSL-1.1 | $0 internal use |
| Docker Engine | Apache-2.0 | $0 |
| WSL2 | Microsoft, included in Windows | $0 |
| Lima | Apache-2.0 | $0 |
| squid | GPL-2.0 | $0 |
| CNCF distribution, SQLite | Apache-2.0 / public domain | $0 |
| Tailscale (hosted) | Personal plan is non-commercial only; Standard for business use | $0 (Personal) or $8/user/month (Standard); tagged devices free up to 50 |
| Docker Desktop | free for small businesses (< 250 staff and < $10M revenue) | not used — Linux env instead |
| Claude Max | consumer subscription | existing |
| OpenCode Go | subscription (~$10/month, dollar-metered caps) | existing |
| Electricity (hub + PCs) | — | real |

## 6. Assumptions

- Each git host is reachable from the hub with a URL + API key that can push
  branches and open MRs/PRs.
- Each worker PC can run a Linux env (WSL2 / Lima / native) with fixed CPU/RAM.
- Tailscale (hosted) on the hub and each worker Linux env.
- `hermes-host` has public SSH; a dedicated user with a forced command is allowed.
- The operator accepts the subscription-terms ambiguity.
- Human review of agent MRs.

## 7. Acceptance criteria

- [ ] With Sireum off, Hermes behaves identically.
- [ ] A `remote` card produces a branch/MR + comment + Discord message.
- [ ] Workers on Windows, macOS and Linux run the same image.
- [ ] No git credential on any worker.
- [ ] Unclaimable `remote` cards return to Hermes after `CLAIM_GRACE`.
- [ ] Limits default to unlimited; a configured limit is enforced; usage-limit auto-pause works.
- [ ] Redispatch / reschedule never creates a second branch or force-push.
- [ ] Adding a PC needs only the per-OS setup + `client-bootstrap.sh`.
- [ ] Adding a git host needs only hub config (URL, API key, type).

## 8. Out of scope

Deploying repos, CI replacement, auto-merge/deploy, model serving.

## 9. Risks

See `PLAN.md` §14.

## 10. Backlog

> Build status per item: `PLAN.md` §11 Phase 0 progress table.

| Priority | Epic | Story | Size |
|---|---|---|---|
| P0 | Contracts | `Job`/`Result` v1: lease, bundle, test targets, `error.code` | S |
| P0 | Runner | Multi-arch `sireum-runner:base`, pinned CLIs | S |
| P0 | Runner | Lease + heartbeat; clone from hub; worktree; bundle upload; spool | S |
| P0 | Runner | Executor adapters with timeout/turns; usage-limit detection | S |
| P0 | Worker | Per-OS Linux env setup (WSL2 / Lima / native) + `client-bootstrap.sh` | S |
| P0 | Hub | Git relay: mirrors, read-only `http-backend`, bundle verify, push | M |
| P0 | Hub | Host adapter for the first git host (push + MR via API) | S |
| P1 | Hub | Nomad server, registry, snapshot cron, disk alert | S |
| P1 | Kanban | `sireum-kanban` forced command on `hermes-host` | S |
| P1 | Bridge | Poll, claim, dispatch, results, claim grace, release | M |
| P1 | Bridge | Per-executor limits (default unlimited) + auto-pause + `auto` failover | S |
| P1 | Bridge | Discord webhook, `/status` | S |
| P0 | Onboarding | macOS/Linux installer + `sireum` helper (test on MBP M2) | S |
| P1 | Onboarding | `sireum-hub invite`, enroll/seats/leave endpoints, Windows installer | S |
| P2 | Ops | Mixed-OS fleet, sleep/WSL shutdown/hub-reboot tests | S |
| P2 | Ops | Cache caps + prune + alerts (hub and workers) | S |
| P3 | Tooling | `:browser`, prepare phase, Playwright cache | M |
| P4 | Sources/hosts | Notion/Jira/GitHub Issues adapters; Gitea/generic host adapters | M each |
| P5 | Hardening | gVisor, OSV, private mirrors, GPU class | M–L |

## 11. Open questions

See `PLAN.md` §15.

## 12. Glossary

- **Sireum** — this project (Sundanese: ant).
- **sireum-hub** — always-on home PC: Nomad server, bridge, git relay, registry.
- **sireum-bridge** — control-plane service.
- **git relay** — hub component: mirrors, read-only clone, bundle intake, push, MR.
- **sireum-runner** — job container and Nomad job.
- **sireum-egress** — per-worker squid allowlist proxy.
- **sireum-kanban** — SSH forced-command wrapper on `hermes-host`.
- **Worker Linux env** — WSL2 distro, Lima VM, or native Linux on an idle PC.
- **Lease** — bridge-issued right for one allocation to run a job and submit its bundle.
- **Executor limit** — optional per-executor concurrency cap; default unlimited.
- **Auto-pause** — executor paused after a usage-limit error until reset.

## 13. Decision log

See `PLAN.md` §16.

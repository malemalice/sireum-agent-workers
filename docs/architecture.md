# Architecture — Sireum

> Status: draft rev 3 · Date: 2026-11-05 (WIB) · Engine: Nomad

## 1. Component map

| Component | Host | Responsibility |
|---|---|---|
| Hermes gateway + kanban | `hermes-host` | Unchanged. Source of truth for cards |
| `sireum-kanban` | `hermes-host` | SSH forced-command wrapper (public SSH); no daemon |
| `sireum-bridge` | `sireum-hub` | Poll/claim, lease, dispatch, results, fallback, limits, `/status` |
| git relay | `sireum-hub` | Hub mirrors, read-only clone, bundle intake, push + MR via host API |
| Nomad server | `sireum-hub` | Scheduling, state, drain, reschedule |
| Registry | `sireum-hub` | Multi-arch runner images, tailnet only |
| Worker Linux env | idle PCs | WSL2 (Windows), Lima VM (macOS), native (Linux) |
| Nomad client | worker Linux env | Register capacity, run allocations |
| `sireum-egress` | worker Linux env | squid allowlist for job containers |
| `sireum-runner` | Nomad allocation | Lease, workspace, executor, gate, bundle, report |
| Mesh | hub + workers | Hosted Tailscale; ACLs. `hermes-host` is not on the mesh |

## 2. Booster model

```text
card created in Hermes
  ├─ no `remote` tag ───────────────► Hermes as today
  └─ `remote` tag (allowlisted user)
        ├─ dispatched within CLAIM_GRACE ──► Sireum runs it
        │      ├─ success        → comment + complete
        │      ├─ needs_review   → comment + request review
        │      ├─ blocked        → comment + block
        │      └─ failed/cancel  → comment + release to Hermes
        └─ not dispatchable (no capacity / executor paused / fleet off)
               → release: remove `remote`, comment, Hermes handles it
```

Hub offline: untagged cards unaffected; running jobs continue and spool their
bundle + result; `remote` cards need a Hermes staleness rule or manual release.

## 3. Happy-path sequence

```text
sireum-bridge (hub)
  ├─ ssh sireum@<hermes-host> list-remote / claim <card>
  ├─ build Job; pick executor (card value, or `auto` → first non-paused)
  ├─ check executor limit (default unlimited) and pause state
  ├─ git relay: fetch hub mirror from the host (API key); resolve base_ref → base_commit
  └─ nomad job dispatch -meta job_id=<id> sireum-runner
        ▼
Nomad → worker with pool=idle, os=linux, free CPU/RAM (any host_os, any arch)
        ▼
sireum-runner (internal network → sireum-egress)
  ├─ upload spooled bundles/results from earlier runs
  ├─ GET /v1/jobs/{id} → Job + lease + clone_url + clone_token   (409 if leased)
  ├─ heartbeat lease every 60 s
  ├─ fetch worker mirror from the hub (read-only, per-job clone token)
  ├─ worktree add -b agent/<id> <base_commit>
  ├─ test target from .agent/tools.yaml at base_commit
  ├─ executor with timeout/turn caps (usage-limit errors detected)
  ├─ test target; tests_modified check
  ├─ commit; git bundle create <base_commit>..agent/<id>
  ├─ PUT /v1/jobs/{id}/bundle   (hub rejects if lease not live)
  └─ POST /v1/results           (spool both on failure)
        ▼
git relay (hub)
  ├─ git bundle verify; fetch into refs/sireum/incoming/<id>
  ├─ checks: exactly one ref, descends from base_commit, size cap, no default branch
  ├─ push agent/<id> to the host with its API key (no force)
  └─ open MR/PR via host API (generic hosts: push only, link the branch)
        ▼
sireum-bridge
  ├─ fill commit + mr_url into the Result
  ├─ usage_limit? → pause executor, release card
  ├─ ssh sireum@<hermes-host> comment / complete / block / release
  └─ Discord webhook
```

## 4. Worker Linux env per OS

| Host OS | Linux env | Notes |
|---|---|---|
| Windows 10/11 | WSL2 Ubuntu, `systemd=true` | Fixed CPU/RAM in `.wslconfig`; keep-alive and autostart at logon; WSL shuts down when idle without it |
| macOS (Intel / Apple Silicon) | Lima Ubuntu VM (`vz`) | Fixed CPU/RAM/disk; start at login (launchd); arm64 on Apple Silicon |
| Linux | native | Nothing extra |

Inside every env: Docker Engine, Nomad client, Tailscale, `client-bootstrap.sh`.
The VM boundary adds isolation on Windows and macOS.

## 5. Capacity model

```text
per-node slots = min(floor(env_memory / job_memory),
                     floor(cache_free / cache_per_job),
                     configured_max_jobs)
fleet slots    = sum(node slots)
executor slots = EXECUTOR_LIMITS[executor]   # 0/unset = unlimited (default)
running(executor) <= min(fleet free slots, executor slots)
```

- Default: no executor cap; the fleet is the limit.
- Auto-pause: a `usage_limit` result pauses that executor until reset (or backoff
  15 → 30 → 60 min). `auto` cards fail over to the other executor.
- Reconciler (60 s): frees leases whose allocation is terminal/lost or whose TTL expired.

Node metadata: `pool=idle`, `os=linux`, `host_os=windows|macos|linux`,
`agents=claude,opencode`, `region=home-jkt`. Arch from `${attr.cpu.arch}`.

## 6. Workspace model

Hub:

```text
/srv/sireum/git/<repo-hash>.git     # mirror of the host repo (fetched with the API key)
```

Worker:

```text
/cache/mirrors/<repo-hash>.git      # mirror of the hub mirror (read-only fetch)
/cache/work/<job_id>/               # worktree
/cache/toolchains/{npm,pip,uv,go}/
/cache/results/<job_id>.{json,bundle}   # spool
/cache/locks/<repo-hash>.lock
```

Caps + prune on both; alert at 80%.

## 7. Failure handling

| Failure | Behaviour |
|---|---|
| PC sleeps / WSL2 or Lima stops | After `lost_after=10m` a replacement starts; original's bundle is rejected (stale lease) |
| Partial branch on host | Hub refuses non-fast-forward push; `needs_review` with the bundle kept on the hub |
| Executor hits usage limit | `failed` + `error.code=usage_limit`; executor paused; card released |
| Executor hangs | `timeout_sec` kills it; `failed`; card released |
| Test target fails / tests modified | Bundle still pushed; `needs_review` |
| Bundle fails verification | Not pushed; `failed`; alert (possible tampering) |
| Host API down / key invalid | Bundle kept on hub; retry; then `failed` and release |
| Hub down | Running jobs finish and spool; no new dispatch; Hermes unaffected for untagged cards |
| Duplicate dispatch | Lease `409`; second allocation exits |

## 8. Observability

- Nomad job/alloc/node status and logs.
- `/status`: claimed, running, released; per executor: running, limit, paused-until, limit hits.
- Runner JSON logs with `job_id`.
- Discord per result; releases and pauses stated explicitly.
- Alerts: hub down, failure rate, disk, bundle rejections, executor paused.

## 9. Engine abstraction

`dispatch(Job)`, `cancel(ref)`, `status(ref)`; Nomad is v1.

## 10. Deployment topology

```text
hermes-host (unchanged; not on Tailscale)
  hermes-gateway.service
  user `sireum` + authorized_keys forced command → /usr/local/bin/sireum-kanban

sireum-hub (always-on home PC, Tailscale)
  nomad server · sireum-bridge (SQLite + git relay) · registry
  /srv/sireum/{nomad,bridge,git,registry}
  cron: nomad snapshot → off-host copy

idle PCs
  Windows → WSL2 Ubuntu │ macOS → Lima Ubuntu │ Linux → native
    docker engine · nomad client · tailscale
    docker network sireum-jobs (internal) · sireum-egress (squid)
    /srv/sireum/cache (host volume)

Tailscale ACL: workers → hub:443 (bridge + git), hub:4647 (Nomad RPC), hub:5000 (registry)
Hub → hermes-host: public SSH, forced command only
```

## 11. What is deliberately not built

No scheduler, queue server, dashboard, CI, agent framework, or resident process
on `hermes-host`.

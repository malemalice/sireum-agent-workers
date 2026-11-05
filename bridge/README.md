# sireum-bridge

> Draft rev 3. Control-plane service on `sireum-hub`, including the git relay.

## Implementation

`sireum_bridge/` is a stdlib + PyYAML service:

| Module | What |
|---|---|
| `server.py` | HTTP API, dispatch, lease/fencing, results, limits + auto-pause, reconciler, Discord |
| `relay.py` | Hub mirrors, bundle verify, push, MR adapters (github / gitlab / gitea; `local` + `generic` push only) |
| `booster.py` | Poll → allowlist → claim → dispatch; claim grace; result write-back via an outbox (retried) |
| `sources.py` | `kanban-ssh` (Hermes via `sireum-kanban`), `github` (Issues), `file` (tests); card parser |
| `nodes.py` | Join codes, enroll, seats (Nomad Variables, or 0600 file without Nomad), leave |
| `apis.py` | Nomad + Tailscale HTTP clients |
| `cli.py` | `sireum-hub serve | invite [--name] [--ts-auth-key] [--revoke] | nodes` |

Extra env beyond the table below: `SOURCE` (`kanban-ssh:user@host#keyfile`,
`github:owner/repo#tokenfile`, `file:path`), `NOMAD_SERVERS`, `NOMAD_VERSION`,
`NOMAD_NODE_TOKEN`, `NOMAD_MINT_NODE_TOKENS`, `RUNNER_IMAGE`, `SQUID_IMAGE`, `REGISTRY`,
`REGISTRY_SERVER` / `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` (read-only pull token for workers),
`TS_OAUTH_CLIENT_ID` / `TS_OAUTH_CLIENT_SECRET` / `TS_TAILNET`, `LEASE_TTL_SEC`,
`RECONCILE_INTERVAL`. Template: `hub/bridge.env.example`.

Never-leased jobs are cancelled after `CLAIM_GRACE` (`error.code=no_capacity`, Nomad job
stopped, card released) — this is how "no free worker" hands cards back to Hermes.

```bash
pip install -r bridge/requirements.txt
cp -r bridge/config.example /srv/sireum/bridge   # edit git_hosts.yaml / repos.yaml
WORKER_TOKEN=… OPERATOR_TOKEN=… BRIDGE_PUBLIC_URL=http://<hub>:8787 \
  PYTHONPATH=bridge python3 -m sireum_bridge --config /srv/sireum/bridge --state /srv/sireum/state \
  --listen 0.0.0.0:8787
```

Fencing: a lease is *current* while it is the job's latest lease and not released. An
expired-but-not-superseded lease can still deliver a spooled result; once a reschedule takes
a new lease, the old one's bundle and result get `409`.

## Responsibility

Boost Hermes: claim opt-in (`remote`) cards, lease and dispatch them, relay git
to the real host, write results back — and give cards back to Hermes whenever
Sireum can't run them.

## API (tailnet only, HTTPS via `tailscale cert`)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/v1/jobs/{job_id}` | worker | Job + lease + `clone_url`/`clone_token`; `409` if leased |
| `POST` | `/v1/leases/{lease_id}/heartbeat` | worker | Extend lease |
| `GET` | `/v1/leases/{lease_id}` | worker | Lease check |
| `PUT` | `/v1/jobs/{job_id}/bundle` | worker + live lease | Upload git bundle; `409/410` if lease not live |
| `POST` | `/v1/results` | worker | Result; ignored unless lease is current |
| `GET` | `/git/<repo-hash>.git/*` | clone token | Read-only smart HTTP (`git http-backend`, upload-pack only) |
| `POST` | `/v1/executors/{name}/pause` / `resume` | operator | Manual pause/resume |
| `POST` | `/v1/dispatch` | operator | Dispatch without a card (body/response in the top-level `README.md` §B) |
| `GET` | `/v1/runs/{job_id}` | operator | Run state + Result once finished |
| `GET` | `/status` | operator | Cards, jobs, leases, executors |
| `POST` | `/v1/nodes/enroll` | one-time enroll token | Installer registers a PC; returns node name, Nomad settings, image digests, node secret |
| `PUT` | `/v1/nodes/{name}/seats` | node secret | Store Claude/OpenCode tokens as Nomad Variables (never on the PC) |
| `DELETE` | `/v1/nodes/{name}` | node secret or operator | `sireum leave`: delete seats, revoke node secret |
| `GET` | `/healthz` | none | Liveness |

## Worker enrollment (`sireum-hub invite`)

```text
operator:  sireum-hub invite [--name <pc>]
  → mints a single-use, pre-authorised Tailscale auth key tagged tag:sireum-worker
    (Tailscale API, OAuth client with auth_keys scope)
  → mints a one-time enroll token (24 h)
  → prints  sireum1.<base64url({hub, ts_auth_key, enroll_token, expires})>

installer: POST /v1/nodes/enroll {host_os, arch, hostname, cpus, mem_mb}
  ← {node_name, nomad_servers, nomad_version, nomad_client_token,
     runner_image, squid_image, node_secret}
  (enroll token is burned on first use)

user:      sireum seats → PUT /v1/nodes/{name}/seats {claude_token, opencode_token}
  → nomad var put nomad/jobs/sireum-runner/seat-<name> …
```

- Join codes are secrets: single use, 24 h expiry, revocable with `sireum-hub invite --revoke`.
- `nomad_client_token` is scoped to `node:write` for this node only (used by `sireum pause/resume`).
- `node_secret` lets the PC manage only its own seats and removal.

## Kanban access (public SSH to `hermes-host`)

```bash
ssh sireum@<hermes-host-public> list-remote
ssh sireum@<hermes-host-public> claim <card>          # atomic; exit 1 if taken
ssh sireum@<hermes-host-public> comment <card> < text
ssh sireum@<hermes-host-public> complete|block|request-review|release <card>
```

## Executor limits (configurable, default unlimited)

```text
EXECUTOR_LIMITS="claude=0,opencode=0"    # 0 or unset = unlimited (default)
EXECUTOR_LIMITS="claude=4,opencode=2"    # optional caps
```

State per executor: `running`, `limit`, `paused_until`, `limit_hits_24h`.

- Limit 0/unset ⇒ never blocks; Nomad capacity is the only bound.
- Limit N ⇒ lease-based semaphore of N.
- `usage_limit` result ⇒ `paused_until = reset_at` or backoff 15 → 30 → 60 min
  (reset after a clean job). Manual `pause`/`resume` endpoints override.

## Booster loop

```text
every POLL_INTERVAL (30 s):
  for card in list-remote:
    if tagger not in REMOTE_TAGGERS: skip
    executor = card.executor if not "auto" else first_available(["claude","opencode"])
    if executor is None (paused/at limit) or no fleet capacity:
        if card.age > CLAIM_GRACE: release(card, reason); continue
        continue
    repo = resolve_repo(card)          # `repo:` label → repos.yaml entry
    if repo is None or not repo.enabled:
        release(card, "repo not onboarded"); continue
    if not claim(card): continue
    job = normalise(card, repo); relay.fetch(repo.git_host, repo.path)   # lazy mirror
    job.base_commit = resolve(job.base_ref or repo.default_branch)
    store(job, executor, state=claimed); nomad dispatch -meta job_id=job.id
```

## Card format

| Card part | Maps to Job field |
|---|---|
| Title + body (before `Acceptance:`) | `task` |
| `Acceptance:` line | `acceptance` |
| `remote` | opt-in (tagger must be in `REMOTE_TAGGERS`) |
| `repo:<alias>` / `repo:<git_host>/<path>` | `git_host` + `repo` |
| `executor:<claude\|opencode\|auto>` | `executor` (default `auto`) |
| `test:<target>` | `test_target` (name only) |
| `base:<ref>` | `base_ref` (default: repo default branch) |
| `timeout:<minutes>` | `timeout_sec` (default 30 min, capped by operator) |
| `class:<standard\|browser\|heavy>` | `job_class` |

## Repo per card

Cards name their repo with a label: `repo:<git_host>/<path>` or a hub alias
`repo:<alias>`. Only repos in `repos.yaml` with `enabled: true` are dispatched.

```yaml
# /srv/sireum/bridge/repos.yaml
repos:
  - alias: example
    git_host: github-adaptiv
    path: adaptiv/example
    default_branch: main
    enabled: true            # only after default-branch protection is confirmed
  - alias: sandbox
    git_host: github-adaptiv
    path: adaptiv/sireum-sandbox
    default_branch: main
    enabled: true
```

Unknown/disabled/missing repo ⇒ comment "repo not onboarded" + release to Hermes.

## Git relay

- `git_hosts.yaml` (see `docs/security-and-credentials.md` §4): `id`, `type`,
  `api_url`, `git_url`, `api_key_file`.
- `repos.yaml` (above): which repos on those hosts may be boosted.
- Mirrors: `/srv/sireum/git/<repo-hash>.git`, fetched with the host API key before
  dispatch; `git gc` weekly.
- Bundle intake: verify → fetch into `refs/sireum/incoming/<job_id>` → checks
  (one ref, descends from `base_commit`, size cap, live lease) → push to
  `agent/<job_id>` without force.
- Host adapters (`open_mr(repo, branch, base, title, body)`):

| `type` | Push | MR/PR |
|---|---|---|
| `github` | HTTPS + key | `POST /repos/{owner}/{repo}/pulls` |
| `gitlab` | HTTPS + key | `POST /projects/:id/merge_requests` |
| `gitea` | HTTPS + key | `POST /repos/{owner}/{repo}/pulls` |
| `generic` | HTTPS + key | none — branch URL in the comment |

## Result handling

| Status | Card action |
|---|---|
| `success` | comment (+ MR link) + complete |
| `needs_review` | comment (+ MR link) + request-review |
| `blocked` | comment + block |
| `failed`, `cancelled` | comment + release to Hermes; `usage_limit` also pauses the executor |

Then release the lease and post the Discord webhook.

## Reconciler (60 s and on startup)

- Free leases whose allocation is terminal/lost or TTL-expired.
- Jobs with no result after `timeout_sec + 10m` ⇒ `failed`, release card.
- Push pending accepted bundles whose host push failed earlier.
- On startup rebuild from SQLite + Nomad; accept spooled uploads.

## Configuration

| Env | Meaning |
|---|---|
| `KANBAN_SSH` | `sireum@<hermes-host-public>` + key path |
| `REMOTE_TAGGERS` | Hermes users allowed to tag `remote` |
| `CLAIM_GRACE` / `POLL_INTERVAL` | `10m` / `30s` |
| `EXECUTOR_LIMITS` | Per-executor caps; default unlimited |
| `GIT_HOSTS_FILE` | `/srv/sireum/bridge/git_hosts.yaml` |
| `BUNDLE_MAX_MB` | Default `50` |
| `NOMAD_ADDR` / `NOMAD_TOKEN` | Dispatch target |
| `WORKER_TOKEN` / `OPERATOR_TOKEN` | API auth |
| `DISCORD_WEBHOOK_URL` | Notifications |
| `DATABASE_PATH` | `/srv/sireum/bridge/state.db` |

## Non-goals

Scheduling, sandboxing, a dashboard, auto-merge, being required for Hermes.

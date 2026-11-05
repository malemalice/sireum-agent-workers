# Sireum Setup Status

> **Internal identifiers redacted.** Actual hostnames, account names and paths
> live only on the hub (`/opt/sireum/secrets/bridge.env`). No tokens or join-code
> contents are documented here.

> **Date:** 2026-11-05 (WIB)
> **Teardown (2026-11-05):** the hub, Nomad server, runner job, seats, bridge,
> and Hermes-side `sireum-kanban` were removed after this snapshot. The source
> repo remains; this document is kept as a record.
> **Scope:** Adaptiv only (not CNT/cinte)
> **Hub:** `<hub>` (this Mac), Tailscale `<hub-tailnet-ip>`
> **Hermes:** `hermes-host` (`metallica`), Hermes `v0.22.7`
> **State:** hub + control plane configured and verified; **no worker enrolled yet**

## 1. Summary

| Area | State |
|---|---|
| Hermes kanban source | Connected and verified (`list-remote` works) |
| Hub bridge | Running under launchd, ports `8787` / `8790` |
| Nomad server | Running under launchd, server-only |
| Nomad client | **None** (`No nodes registered`) |
| `sireum-runner` job | Registered (parameterized, local image tag) |
| Git credentials | GitHub token for `<github-account>`, `0600` |
| Repos enabled | `tka`, `tax-app`, plus local `sandbox` |
| Claude seat | 1-year OAuth token, verified |
| OpenCode seat | OpenCode Go token, verified |
| Worker join code | Generated, single-use, 24 h (contents not documented) |

The only missing piece for actual execution is a **Nomad client** on a suitable
idle PC. This Mac is the hub and is already under memory pressure; see §9.

## 2. Topology

```text
Hermes kanban (hermes-host, v0.22.7)
        │  SSH forced command (sireum-kanban), only list/claim/comment/release
        ▼
sireum-bridge (<hub>) ──► Nomad server (<hub>)
        │                          │
        │                          └── no client yet
        └── git relay ──► GitHub (<org>/tka, <org>/tax-app)
```

## 3. Hermes integration

Hermes v0.22.7 has **no tags and no `move`**, so the adapter uses:

- **Opt-in:** a body line `Sireum: remote` (not a label).
- **Directives:** body lines `Repo:`, `Executor:`, `Test:`, `Base:`, `Timeout:`, `Class:`.
- **Claim:** `hermes kanban claim <id> --ttl 7200` (atomic `ready → running`).
- **Result:** `comment`, then `complete --force` / `request-review --force` /
  `reclaim` + `block` / `reclaim` + strip marker.
- Marker and directive lines are stripped before the task text reaches the agent.

Installed on `hermes-host` as user `<hermes-user>` (no service, no daemon):

| Path | Purpose |
|---|---|
| `~/.local/bin/sireum-kanban` | SSH forced command |
| `~/.config/sireum-kanban.conf` | CLI templates, `HERMES`/`HERMES_HOME`, marker |
| `~/.ssh/authorized_keys` | One `command=...,restrict` line for the hub key |

Hub side (`/opt/sireum/secrets/bridge.env`):

```text
SOURCE=kanban-ssh:<hermes-user>@hermes-host#/opt/sireum/secrets/kanban_ed25519
REMOTE_TAGGERS=<allowlisted Hermes profiles>
```

`REMOTE_TAGGERS` matches Hermes `created_by` (the v0.22.7 equivalent of a tagger).

## 4. Git credentials and repos

| Item | Value |
|---|---|
| Token file | `/opt/sireum/secrets/github-adaptiv.key` (`0600`) |
| Token source | Mac `gh auth token` for `<github-account>` (`repo` scope) |
| TLS | `SSL_CERT_FILE` + `REQUESTS_CA_BUNDLE` = `/etc/ssl/cert.pem` in `bridge.env` |
| Host id | `github-adaptiv` (`https://github.com`, `https://api.github.com`) |

Repos (`/opt/sireum/bridge/repos.yaml`):

| Alias | Path | Default | State |
|---|---|---|---|
| `tka` | `<org>/tka` | `main` | enabled |
| `tax-app` | `<org>/tax-app` | `main` | enabled |
| `sandbox` | local `/opt/sireum/sandbox-host/sireum-sandbox.git` | `main` | enabled |
| `example` | `adaptiv/example` | `main` | disabled |

Verified with Sireum's own `git_auth_env`:

- `git ls-remote https://github.com/<org>/tka.git` → `<sha>`
- `git ls-remote https://github.com/<org>/tax-app.git` → `<sha>`

The local sandbox bare repo was created with one `main` commit so `Repo: sandbox`
smoke tests can fetch.

## 5. Seats (shared from this Mac)

| Seat | File | Verification |
|---|---|---|
| Claude | `/opt/sireum/seats/claude.token` (`0600`, 1-year OAuth) | `claude -p` → `OK` |
| OpenCode | `/opt/sireum/seats/opencode-go.token` (`0600`) | `opencode run` → `OK` |

Stored on the hub as Nomad Variable:

```text
nomad/jobs/sireum-runner/seat-<node>
  claude_token
  opencode_token
```

The runner job templates this variable using `node.unique.name`, which the worker
installer sets to the enrollment name (`<node>`). The tokens are only in
the allocation's `secrets/seat.env`; nothing is stored on the worker host.

**`agy` / Antigravity is not supported.** Sireum executors are `claude` and
`opencode` only.

## 6. Runner job

`sireum-runner` was **not registered** by the initial hub install. It is now
registered manually:

- Type: `batch`, parameterized (`job_id` required)
- Image: `sireum-runner:local-60a26db80a1b` (workers build it from the hub source)
- Bridge URL: `https://<hub>.<tailnet>.ts.net`
- Deployed file: `/opt/sireum/sireum-runner.nomad.hcl`
- Source: `nomad/sireum-runner.nomad.hcl`

**Fix applied:** Nomad 2.1 rejects interpolated `node.meta.*` in constraint
attributes. The `pool=idle` and `os=linux` constraints were removed. Re-add a
verified constraint before running a mixed fleet.

## 7. Verification evidence

- `ssh -i /opt/sireum/secrets/kanban_ed25519 ... list-remote` → `[]`, exit `0`.
- `make_source(SOURCE).list_remote()` → `source: kanban`, `0 cards`.
- `git ls-remote` for both private repos succeeds (see §4).
- `claude -p` with the stored token → `OK`.
- `opencode run` with the stored token → `OK`.
- `nomad job status sireum-runner` → parameterized job present.
- `nomad node status` → `No nodes registered` (expected until a worker joins).

## 8. Hermes backlog snapshot (2026-11-05)

Hermes has 6 boards. Open work, excluding done/archived:

| Board | Open | Breakdown |
|---|---:|---|
| `tax-app` | 7 | 6 todo, 1 blocked |
| `tka` | 4 | 3 todo, 1 blocked |
| `tka-mobile` | 1 | 1 blocked |
| `default`, `chatgptdump`, `tax-app-mobile` | 0 | — |

**0 ready tasks and 0 Sireum-marked tasks** at the time of writing. The 9 todo
cards need promotion/assignment; the 3 blocked cards need a human decision.
Per-profile `kanban.db` files are empty.

## 9. Worker node: not this Mac

Current hub resources:

| Metric | Value |
|---|---|
| RAM | 8 GB total, ~30% free |
| Swap | **9.3 GB used**, <1 GB free |
| Disk | **8.2 GB free** |
| Runner job request | **4 GB RAM** |

A Lima VM large enough for the 4 GB job would starve the hub. Use an idle PC:

- RAM: 8 GB free for the VM (16 GB total preferred)
- Disk: 30–50 GB free
- CPU: 4 cores
- Linux, macOS (Lima), or Windows (WSL2)

Install on that PC:

```bash
git clone https://github.com/malemalice/sireum-agent-workers.git
cd sireum-agent-workers/install
./install.sh "<join code from /opt/sireum/seats/join-code.txt>"
```

The join code is single-use, valid 24 h, and its contents are intentionally not
documented here. The installer shows a Tailscale login link (no Tailscale OAuth
client is configured). Seats do **not** need to be re-entered: they are already
on the hub for node `<node>`.

After install, verify on the hub:

```bash
nomad node status
sireum-hub nodes
```

## 10. Next steps

1. Enroll an idle PC with the saved join code.
2. Confirm `nomad node status` shows the client and `sireum-hub nodes` shows it.
3. Confirm the seat variable matches the enrolled node name.
4. Create one `Repo: sandbox` test card, then a `Repo: tka` card.
5. Onboard additional repos with `sireum-hub add-repo ... --auth file:/opt/sireum/secrets/github-adaptiv.key`.
6. Re-add a verified placement constraint before a mixed fleet.
7. Optional: remove the orphaned Claude daemon and stale opencode sessions on the
   hub after operator confirmation.

## 11. Security notes

- Tokens and the join code live only in `0600` files and Nomad Variables; never
  in this document, the repo, or card bodies.
- The GitHub token is a user OAuth token; if revoked, refresh with:
  `umask 077; gh auth token > /opt/sireum/secrets/github-adaptiv.key`.
- Subscription seats are shared with the operator's live sessions; concurrent
  jobs consume the same quota.
- No auto-merge: the relay opens MRs, a human reviews.

## 12. Changes applied in this session

| File / host | Change |
|---|---|
| `kanban/sireum-kanban` | Rewritten for Hermes v0.22.7 (marker + directives, claim/complete/block/release) |
| `kanban/sireum-kanban.conf.example` | v0.22.7 command templates |
| `kanban/install-on-hermes-host.sh` | `--local` install, target user, Hermes path args |
| `bridge/sireum_bridge/admin.py`, `cli.py` | `connect-hermes` supports `--local`, `--kanban-user`, `--hermes-bin`, `--hermes-home` |
| `nomad/sireum-runner.nomad.hcl` | Removed Nomad-2.1-incompatible `node.meta.*` constraints |
| `README.md` | Card format updated to body marker/directives |
| `hermes-host` | Installed `sireum-kanban`, config, restricted key line (backup made) |
| `/opt/sireum/secrets/bridge.env` | `SOURCE`, `REMOTE_TAGGERS`, CA bundle; backups made |
| `/opt/sireum/secrets/github-adaptiv.key` | Created from `gh auth token`, `0600` |
| `/opt/sireum/bridge/repos.yaml` | Onboarded `tka`, `tax-app` |
| `/opt/sireum/sandbox-host/sireum-sandbox.git` | Created local bare repo on `main` |
| `/opt/sireum/seats/*` | Claude and OpenCode seat files (`0600`) |
| Nomad Variables | `seat-<node>` created/updated |
| Nomad job | `sireum-runner` registered with local image tag |
| `/opt/sireum/src/...` | Updated kanban/installer/admin/cli/nomad files (backups made) |

## 13. Rollback

- Hub connection: stop `id.sireum.bridge`; clear `SOURCE` in `bridge.env`.
- Hermes host: remove the `sireum-kanban` line from
  `~<hermes-user>/.ssh/authorized_keys`; delete `~/.local/bin/sireum-kanban` and
  `~/.config/sireum-kanban.conf`.
- Runner job: `nomad job stop -purge sireum-runner`.
- Seats: delete `nomad/jobs/sireum-runner/seat-*` variables and `/opt/sireum/seats/*`.
- Backups from this session are suffixed `.bak-*` beside the original files.

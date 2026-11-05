# Operations Runbook — Sireum

> Status: draft rev 3 · Date: 2026-11-05 (WIB)
> Current deployed state and handoff: [`setup-status.md`](./setup-status.md).
> All commands are illustrative; verify versions and paths. Nothing is executed yet.

## 1. `sireum-hub` (always-on home PC)

**Scripted:** `sudo ./hub/install-hub.sh` does everything below (Nomad 2.1.1 server with
ACL bootstrap and the `sireum-bridge` / `sireum-worker-node` policies, registry on the
tailnet IP, bridge as a systemd service behind `tailscale serve --https=443`, the runner
Nomad job, snapshot / disk-alert / `git gc` crons, the kanban SSH key, and the
`sireum-hub` wrapper), then prints the manual steps. The hand-written steps are kept
for reference.

**Hub on macOS (native):** `./hub/install-hub-macos.sh` as your user. Requires Tailscale
(with its CLI), Xcode command line tools (git, python3 ≥ 3.9). It:

- installs everything under `/opt/sireum` (`src`, `venv`, `bin/nomad`, `bridge/` config,
  `state/`, `secrets/` 0700, `logs/`, `backups/`), owned by your user;
- downloads Nomad 2.1.1 for darwin and checks its SHA-256; binds Nomad to the tailnet IP
  (re-run the script if that IP changes);
- bootstraps Nomad ACLs and policies, writes `secrets/bridge.env`, registers the runner job
  once `RUNNER_IMAGE` is set;
- LaunchDaemons (start at boot, run as your user): `id.sireum.nomad`, `id.sireum.bridge`,
  `id.sireum.snapshot` (03:15), `id.sireum.disk-alert` (15 min), `id.sireum.git-gc`
  (Sunday 04:00), and `id.sireum.awake` (`caffeinate -s`); log rotation via newsyslog;
- `tailscale serve --https=443` → bridge; `sireum-hub` and `sireum-hub-uninstall [--purge]`.

No local registry: push the runner image to a private GHCR package and put a **read-only**
(`read:packages`) token in `REGISTRY_USERNAME` / `REGISTRY_PASSWORD`; workers receive it at
enrolment and `docker login` with it (Nomad's docker plugin uses the same auth file).
Restart the bridge after config changes: `sudo launchctl kickstart -k system/id.sireum.bridge`.

```bash
# Nomad server
sudo install -m 0755 nomad /usr/local/bin/nomad
sudo mkdir -p /etc/nomad.d /srv/sireum/{nomad,bridge,git,registry,secrets}
sudo chmod 0700 /srv/sireum/secrets
# server.hcl: bind_addr = tailscale IP, bootstrap_expect = 1, acl { enabled = true },
#             data_dir = "/srv/sireum/nomad"
sudo systemctl enable --now nomad

# Registry (tailnet only)
docker run -d --restart unless-stopped --name sireum-registry \
  -p <tailscale-ip>:5000:5000 -v /srv/sireum/registry:/var/lib/registry registry:<pinned>

# Git hosts: one key file per host + /srv/sireum/bridge/git_hosts.yaml
sudo install -m 0600 -o sireum /dev/stdin /srv/sireum/secrets/github-adaptiv.key

# Bridge (includes the git relay)
sudo systemctl enable --now sireum-bridge
tailscale cert sireum-hub.<tailnet>.ts.net
```

- `/srv/sireum` on its own partition/quota; alert at 80% (git mirrors grow).
- Cron: `nomad operator snapshot save` daily; copy snapshot, `state.db`, and an
  age-encrypted `secrets/` off the hub.
- UPS if available; BIOS "power on after AC loss"; disable sleep on the hub.

## 2. `hermes-host` (kanban access only, public SSH)

```bash
# Scripted: sudo kanban/install-on-hermes-host.sh "<hub public key>"
# (the user needs a real shell — sshd runs the forced command through it)
sudo useradd -m -s /bin/bash sireum
sudo install -m 0755 kanban/sireum-kanban /usr/local/bin/sireum-kanban
sudo install -m 0644 kanban/sireum-kanban.conf.example /etc/sireum-kanban.conf   # then match to `hermes kanban --help`
# ~sireum/.ssh/authorized_keys:
# command="/usr/local/bin/sireum-kanban",restrict ssh-ed25519 AAAA... sireum-hub
```

From the hub: `ssh sireum@<hermes-host-public> list-remote`. No Tailscale on
`hermes-host`.

## 3. Worker Linux env (manual install)

> Most people should use the one-command installer instead: `sireum-hub invite`
> on the hub, then `install/install.sh` (macOS/Linux) or `install/install.ps1`
> (Windows) on the PC. See the top-level `README.md` Part 2. This section
> describes the same steps by hand.

### 3a. Windows (WSL2)

```powershell
wsl --install -d Ubuntu-24.04
# %UserProfile%\.wslconfig
# [wsl2]
# processors=4
# memory=8GB
# vmIdleTimeout=-1        # verify on your WSL version; keeps the VM up
```

Inside Ubuntu: enable systemd (`/etc/wsl.conf` → `[boot] systemd=true`),
`wsl --shutdown`, reopen. Autostart: Task Scheduler task at logon running
`wsl -d Ubuntu-24.04 --exec /bin/true`. Power plan: no sleep while plugged in
(or accept sleep; Nomad handles it). The PC must be logged in.

### 3b. macOS (Lima)

```bash
brew install lima
limactl start --name=sireum --vm-type=vz --cpus=4 --memory=8 --disk=80 template://ubuntu-lts
limactl shell sireum
```

Autostart at login via a LaunchAgent running `limactl start sireum`. Apple
Silicon gives an arm64 env (images are multi-arch).

### 3c. Linux

Use the host directly.

### 3d. Inside every env

```bash
# Docker Engine (docs.docker.com/engine/install/ubuntu), Tailscale, Nomad
curl -fsSL https://tailscale.com/install.sh | sh     # review first; or use the apt repo
sudo tailscale up --advertise-tags=tag:sireum-worker
sudo ./nomad/client-bootstrap.sh
# /etc/nomad.d/client.hcl (see nomad/README.md), set host_os=windows|macos|linux
sudo systemctl enable --now nomad
```

On the hub, store this node's seat token:

```bash
nomad var put nomad/jobs/sireum-runner/seat-<node-name> token=<seat-token>
```

Egress checks from the env:

```bash
docker run --rm --network sireum-jobs curlimages/curl -m 5 https://example.com    # fail (no route)
docker run --rm --network sireum-jobs -e HTTPS_PROXY=http://egress:3128 \
  curlimages/curl -m 5 https://github.com                                        # fail (git hosts not allowed)
docker run --rm --network sireum-jobs -e HTTPS_PROXY=http://egress:3128 \
  curlimages/curl -m 5 https://registry.npmjs.org                                # succeed
```

## 4. Register and test

```bash
nomad job run nomad/sireum-runner.nomad.hcl
nomad job dispatch -meta job_id=test-001 sireum-runner '{"job_id":"test-001"}'
nomad alloc logs -f <alloc-id>
```

## 5. Limits

```bash
# /etc/sireum/bridge.env on the hub
EXECUTOR_LIMITS=claude=0,opencode=0    # 0 or unset = unlimited (default)
# example cap: EXECUTOR_LIMITS=claude=4,opencode=2
systemctl restart sireum-bridge
curl -s https://sireum-hub.<tailnet>.ts.net/status | jq '.executors'
# manual pause/resume
curl -X POST .../v1/executors/claude/pause?minutes=60
curl -X POST .../v1/executors/claude/resume
```

## 6. Drain / remove a PC

```bash
nomad node drain -enable -deadline 10m <node-id>
sudo systemctl stop nomad
nomad node deregister -force <node-id>
nomad var purge nomad/jobs/sireum-runner/seat-<node-name>
# delete the env: wsl --unregister Ubuntu-24.04  |  limactl delete sireum
```

## 7. Stop everything (rollback)

```bash
systemctl stop sireum-bridge
nomad job stop -purge sireum-runner
ssh sireum@<hermes-host-public> release <card>     # per claimed card
```

## 8. Hub outage

Untagged cards unaffected. Running jobs spool `results/<id>.{json,bundle}` on the
worker. On restart the bridge reconciles leases, accepts spooled uploads, and
pushes pending bundles.

## 9. Troubleshooting

| Symptom | Check | Likely cause |
|---|---|---|
| Card never claimed | bridge log; `ssh … list-remote` | tagger not allowlisted; SSH key; hub down |
| Cards released with "executor paused" | `/status` executors | usage limit hit; wait or resume manually |
| Node vanishes on Windows | `wsl -l -v` | WSL idle shutdown; user logged out |
| Node vanishes on macOS | `limactl list` | VM stopped; Mac asleep |
| `exec format error` | node `attr.cpu.arch` | image not multi-arch |
| Bundle rejected | bridge log | stale lease, extra ref, wrong base, too large |
| Push rejected by host | host API response | key scope/expiry; branch exists (non-FF) |
| MR not opened | host adapter log | `type: generic`, or API permission |
| Job can't clone | clone token, hub reachability | lease expired; Tailscale ACL |
| Disk full (hub) | `du -sh /srv/sireum/git` | mirrors not pruned/gc'd |

## 10. Upgrades and backups

- Runner image: `docker buildx build --platform linux/amd64,linux/arm64 --push`,
  update the digest in the HCL, `nomad job plan`, `nomad job run`.
- Nomad: snapshot, server first, then clients.
- Backups: Nomad snapshot, `state.db`, encrypted `secrets/` off the hub. Mirrors
  and caches are rebuildable.

## 11. Routine checks

- Daily: failures/releases, executor pauses, disk (hub + workers).
- Weekly: `git gc` on hub mirrors, prune worker caches, egress test, review MRs.

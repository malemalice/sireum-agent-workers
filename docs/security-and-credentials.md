# Security and Credentials

> Status: draft rev 3 · Date: 2026-11-05 (WIB)
> Treat every job as untrusted code execution, and every card as untrusted text.

## 1. Trust boundaries

| Boundary | Trust |
|---|---|
| `sireum-hub` (bridge, git relay, Nomad server, registry) | trusted — holds git API keys |
| Hermes on `hermes-host` | trusted, reached only via a forced command |
| Kanban card text | **untrusted** |
| Idle PC host OS (Windows/macOS/Linux) | semi-trusted |
| Worker Linux env (WSL2 / Lima / native) | semi-trusted |
| `sireum-runner` container | untrusted |
| Agent-generated code, repo content, issue/PR text | untrusted |
| Model providers, git hosts | external |

## 2. Container sandbox

- Non-root `10001:10001`, `cap_drop ALL`, `no-new-privileges`, read-only root,
  tmpfs `/tmp` and `$HOME`, CPU/memory limits, no `docker.sock`, no inbound ports.
- On Windows and macOS the Linux env is a VM, adding a second boundary.
- gVisor in Phase 5.

## 3. Egress control

```text
job container ──(sireum-jobs, --internal)──► sireum-egress:3128 ──► allowlist
```

- Allowlist (`nomad/egress/squid.conf`): **`sireum-hub`** (bridge + git relay),
  model endpoints, package registries. **No git hosts** — workers never reach them.
- squid denies LAN, Tailscale CGNAT, link-local, loopback (except the hub name).
- Tailscale ACL: workers → `sireum-hub:443,4647,5000` only.
- Phase 0 test: non-allowlisted domain, a git host, LAN IP, hub Nomad API from the
  container must fail.

## 4. Git credentials — hub only

```text
worker: clone (read-only, hub) → work → bundle ─PUT─► hub
hub:    verify → push with host API key → open MR via host API
```

- `git_hosts.yaml` on the hub:

```yaml
hosts:
  - id: github-adaptiv
    type: github            # github | gitlab | gitea | generic
    api_url: https://api.github.com
    git_url: https://github.com
    api_key_file: /srv/sireum/secrets/github-adaptiv.key   # 0600, owner sireum
  - id: gitlab-self
    type: gitlab
    api_url: https://gitlab.example.com/api/v4
    git_url: https://gitlab.example.com
    api_key_file: /srv/sireum/secrets/gitlab-self.key
```

- Key scope: minimum to read, push branches, and open MRs on the repos Sireum
  serves. Prefer fine-grained / project tokens; set expiry; rotate.
- Protect the default branch on each host (defence in depth).
- Bundle verification before push:
  1. `git bundle verify` against the hub mirror.
  2. Exactly one ref: `refs/heads/agent/<job_id>`.
  3. Tip descends from `base_commit`.
  4. Size cap (default 50 MB) and object count cap.
  5. Lease is live.
- Push: `git push <host> refs/sireum/incoming/<id>:refs/heads/agent/<id>` — no
  `--force`. Non-fast-forward ⇒ `needs_review`, bundle kept on the hub.
- `clone_token`: read-only, bound to one lease, served only over Tailscale.

## 5. Model credentials

### Seat mode (default): Claude Max + OpenCode Go

- One seat token per node (`claude setup-token` for Claude Code; verify the
  OpenCode Go equivalent), stored as a Nomad Variable
  `nomad/jobs/sireum-runner/seat-<node-name>`, rendered into the allocation's
  `secrets/` dir only.
- Never baked into images or written to a persistent `$HOME`.
- Revoke and delete when a PC is removed.

### Concurrency and terms

- Per-executor limits are configurable, **default unlimited**; usage-limit errors
  auto-pause the executor.
- Whether a consumer subscription may be used concurrently and automatically from
  several machines is not clearly covered by public sources. **The operator
  accepted this risk on 2026-11-05.** The exit is configuration: Claude Team seats
  or API keys (API mode) without code changes.

### API mode (opt-in)

- Provider key on the hub; workers reach a proxy on the hub; `max_cost_usd` enforced there.

## 6. Card text and test gate

- Only allowlisted Hermes users may tag `remote`.
- `test_target` is a name resolved from `.agent/tools.yaml` at `base_commit`.
- Diff touching `protected_paths` ⇒ `tests_modified=true` ⇒ `needs_review`.
- Human review; no auto-merge or auto-deploy.

## 7. Secrets storage

| Secret | Where |
|---|---|
| Git host API keys | hub `/srv/sireum/secrets/*.key`, 0600 |
| Seat tokens | Nomad Variables on the hub, per node |
| Worker→bridge token | Nomad Variable; rotate on PC removal |
| Clone tokens | bridge memory/SQLite, per lease, expire with the lease |
| SSH key `sireum@hermes-host` | hub, 0600 |
| Discord webhook | hub, bridge env |
| Nomad management token | offline |

Back up `/srv/sireum/secrets` encrypted (e.g. age) and off-host.

## 8. Supply chain

Runner image pinned by digest, multi-arch, CLIs pinned by npm version, no
`curl | bash`; lockfiles; installs only via the proxy; no `sudo`/`apt` at runtime.

## 9. `hermes-host` footprint

- Not on Tailscale. Hub connects over the existing public SSH.
- User `sireum`, no shell; `authorized_keys`:
  `restrict,command="/usr/local/bin/sireum-kanban" ssh-ed25519 AAAA... sireum-hub`
  (add `from="<ip>"` only if the hub gets a static IP).
- Wrapper accepts only `list-remote`, `claim`, `comment`, `complete`, `block`,
  `request-review`, `release`; validates ids; text via stdin; logs each call.

## 10. Node hygiene

- Worker Linux env with fixed resources; host disk encryption if portable.
- Auto-updates for Docker/Nomad inside the env; pinned image digests.
- Retiring a PC: drain, revoke seat token, rotate worker token, delete the env.

## 11. Checklist before enabling a worker

- [ ] Linux env running with fixed CPU/RAM; autostart + keep-alive configured.
- [ ] Docker Engine, Tailscale (worker ACL tag), Nomad client inside the env.
- [ ] `client-bootstrap.sh` run; `sireum-jobs` is `--internal`; squid loaded.
- [ ] Egress tests fail for non-allowlisted, git host, LAN, mesh targets.
- [ ] No git credential anywhere in the env.
- [ ] Seat token variable exists for this node only.
- [ ] Cache cap + alert set.

## Implementation deviations (2026-11-05)

- **Nomad worker token is shared.** Nomad ACLs can't scope `node:write` to one node, so
  every PC gets the same `sireum-worker-node` token (used only by `sireum pause/resume`).
  Impact: a holder can drain any node (stops new work only). Rotate it if a PC is lost.
  Per-PC tokens: `NOMAD_MINT_NODE_TOKENS=1` (the bridge then needs a management token).
- **Registry over plain HTTP on the tailnet.** Workers list the hub registry in Docker's
  `insecure-registries`. Transport is WireGuard; images are pinned by digest.
- **Without Nomad** (Phase 0), seat tokens are stored on the hub in
  `/srv/sireum/state/seats/<node>.json` (0600), still never on the PC.
- **Fake executor** exists for tests: enabled only when the worker env sets both
  `SIREUM_ALLOW_FAKE_EXECUTOR=1` and `SIREUM_EXECUTOR_OVERRIDE=fake`. Never set these on a
  real worker; the bridge never accepts `fake` as an executor.
- The `sireum` user on `hermes-host` needs a real shell (`/bin/bash`), not `nologin`:
  sshd runs the forced command through the user's shell.
- **Registry pull token on workers (macOS hub / external registry).** Enrolled workers get
  a read-only registry token (`read:packages` only) and keep it in
  `/root/.docker/config.json` (0600) inside the Linux env. It can pull the runner image and
  nothing else; rotate it in `bridge.env` and re-enrol or re-login workers if a PC is lost.
  Alternative: make the image public and leave `REGISTRY_USERNAME` empty.
- `NOMAD_SERVERS` is a comma-separated list; the bridge also tolerates `[a,b]` because
  `bash` and systemd strip quotes from env files.

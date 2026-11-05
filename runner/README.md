# sireum-runner

> Draft rev 3. Implements the worker side of `docs/job-contract.md`.

## Environment

| Variable | Meaning |
|---|---|
| `JOB_ID` | Job to execute (Nomad dispatch meta) |
| `BRIDGE_URL` | `https://sireum-hub.<tailnet>.ts.net` |
| `WORKER_TOKEN` | Worker→bridge token (Nomad Variable) |
| `CLAUDE_CODE_OAUTH_TOKEN` / `OPENCODE_TOKEN` | Per-node seat tokens (`secrets/` only; verify names) |
| `HTTPS_PROXY` | `http://egress:3128` — the only way out |
| `CACHE_DIR` | `/cache` |

No git-host credential is ever present.

## Algorithm

0. Upload spooled `/cache/results/*.bundle` and `*.json`; delete each on `2xx`.
1. `GET {BRIDGE_URL}/v1/jobs/{JOB_ID}` → `{job, executor, base_commit, lease_id,
   lease_ttl_sec, attempt, clone_url, clone_token}`. `409` ⇒ exit 0.
2. Heartbeat `POST /v1/leases/{lease_id}/heartbeat` every 60 s; lost ⇒ stop, `cancelled`.
3. `flock locks/<repo-hash>.lock`; fetch `mirrors/<repo-hash>.git` from `clone_url`
   (read-only, `clone_token` via `GIT_ASKPASS`, through the proxy).
4. Remove stale `work/{job_id}`; `git worktree add -b {branch} work/{job_id} {base_commit}`.
5. Resolve `test_target` from `git show {base_commit}:.agent/tools.yaml`.
6. Run the executor (`executor` from the lease response) with `timeout_sec` / `max_turns`.
   Detect usage-limit output / 429 ⇒ `error.code=usage_limit` (+ `reset_at` if printed).
7. Run the test target; compute `tests_modified` from `protected_paths`.
8. If changes: commit; `git bundle create /cache/results/{job_id}.bundle {base_commit}..{branch}`.
9. `PUT /v1/jobs/{job_id}/bundle` (lease header). `409/410` ⇒ lease lost ⇒ `cancelled`.
10. `POST /v1/results`; on network failure keep `.bundle` + write `.json` spool.
11. Cleanup: remove worktree, `git worktree prune`; keep the mirror.

The runner never runs `git push`. The hub pushes after verifying the bundle.

## Prepare (Phase 3)

Before the agent runs, the manifest at `base_commit` drives dependency setup in the worktree:

| Manifest key | Action |
|---|---|
| `apt: [...]` | Checked with `dpkg -s`; any missing ⇒ `blocked` (no apt in jobs) |
| `pip.install` | Creates `.sireum-venv` in the worktree (git-excluded), runs the command in it |
| `npm.install` | Runs the command (npm cache under `/cache/toolchains/npm`) |
| `playwright.version` | Uses `/ms-playwright` in `:browser` if versions match, else installs to `/cache/ms-playwright/<v>` |

Bounded by `prepare_timeout_sec` (and `SIREUM_PREPARE_TIMEOUT_SEC` on the worker, which can
only shorten it). Failure ⇒ `failed` with `error.code=prepare`. The venv/caches are also used
by the executor and the test target.

## Executor adapters

```text
claude   -> claude -p "<task>" --output-format stream-json --max-turns <n>
opencode -> opencode run "<task>"     # enforce turns/time from the runner if no flag
```

Verify flags and usage-limit messages against the pinned versions.

## Result mapping

| Situation | `status` |
|---|---|
| target passed, bundle accepted, no protected-path change | `success` |
| no changes | `success` (`commit=null`) |
| target failed / no target / `tests_modified` | `needs_review` |
| missing system package, needs human input | `blocked` |
| usage limit, timeout, max turns, crash, git error | `failed` (+ `error.code`) |
| lease lost, operator stop | `cancelled` |

## Phase 0 acceptance

Verified 2026-11-05 on the MBP M2 (arm64, Docker Desktop) with the fake executor:
`tests/test_e2e.py` (14 tests) and `scripts/phase0-smoke.sh` (12 checks).

- [x] Runs via plain `docker run --network sireum-jobs` (Docker Desktop; Lima VM still to do).
- [x] Clones from the hub; branch appears on the git host after hub push (local sandbox host).
- [x] No git-host credential in the container, image, or env.
- [x] Second concurrent run gets `409`; bundle after lease revoke is rejected.
- [~] `timeout_sec` enforced; simulated usage limit yields `usage_limit` + executor pause.
      `max_turns` relies on `claude --max-turns` — verify with a real seat token.
- [x] Non-allowlisted, git host, LAN, mesh targets unreachable.
- [~] Seat tokens absent from image and result; host-side check needs the Nomad `secrets/` dir.
- [x] Spool works when the bridge stub is stopped.
- [ ] Same image tag runs on amd64 and arm64 (only arm64 built so far).
- [ ] One job with a real Claude Code / OpenCode seat token.

### Test-only fake executor

`SIREUM_ALLOW_FAKE_EXECUTOR=1` **and** `SIREUM_EXECUTOR_OVERRIDE=fake` replace the leased
executor with a shell script (no model). It is set by the operator on the worker, never by a
card; the bridge only accepts `claude|opencode|auto`. Task keywords drive it: `usage-limit`,
`sleep`, `blocked`, `touch-tests`, `noop`.

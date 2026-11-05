# Job and Result Contract

> Status: draft v1 (rev 3) · Date: 2026-11-05 (WIB)
> Engine-agnostic. Nomad/Windmill/K8s details must not leak into it.

## 1. Job

```json
{
  "job_id": "kb-01HXYZ",
  "source": "kanban",
  "source_ref": "card:01HXYZ",
  "git_host": "github-adaptiv",
  "repo": "adaptiv/example",
  "base_ref": "main",
  "task": "Fix the login redirect loop.",
  "acceptance": "Auth test target passes; no change to public API.",
  "test_target": "auth",
  "executor": "auto",
  "model": "default",
  "timeout_sec": 1800,
  "max_turns": 40,
  "max_cost_usd": null,
  "job_class": "standard",
  "toolbox": "base",
  "tools_manifest": ".agent/tools.yaml",
  "prepare_timeout_sec": 900,
  "branch": "agent/kb-01HXYZ",
  "idempotency_key": "kb-01HXYZ",
  "labels": ["code", "repo:example"],
  "callback": { "kanban_card": "01HXYZ", "discord_webhook": "default" },
  "created_at": "2026-11-05T14:00:00+07:00"
}
```

The bridge returns the Job wrapped with a lease and hub clone details:

```json
{
  "job": { "...": "..." },
  "executor": "claude",
  "base_commit": "9f1c2e4",
  "lease_id": "ls-7f3a",
  "lease_ttl_sec": 120,
  "attempt": 1,
  "clone_url": "https://sireum-hub.<tailnet>.ts.net/git/3b7d….git",
  "clone_token": "<read-only, valid for this lease only>"
}
```

### Fields

| Field | Required | Notes |
|---|---|---|
| `job_id` | yes | Stable id; also the idempotency key |
| `source` / `source_ref` | yes | Task source and its reference |
| `git_host` | yes | Id of a host entry in the hub's `git_hosts.yaml` (URL, type, API key ref) |
| `repo` | yes | Repo path on that host; resolved per card from its `repo:` label via `repos.yaml` (onboarded repos only) |
| `base_ref` | yes | Branch/tag/commit; the bridge resolves it to `base_commit` |
| `task` | yes | Natural-language instruction (untrusted text) |
| `acceptance` | yes | What "done" means |
| `test_target` | no | **Name** of a target in the manifest at `base_commit`; never a command |
| `executor` | yes | `claude` \| `opencode` \| `auto` (bridge picks a non-paused one) |
| `model` | no | Alias; `default` = runner config |
| `timeout_sec` / `max_turns` | yes | Hard caps |
| `max_cost_usd` | no | API mode only; `null` in seat mode |
| `job_class` / `toolbox` | no | From the bridge allowlist |
| `tools_manifest` / `prepare_timeout_sec` | no | Phase 3 prepare |
| `branch` | yes | `agent/<job_id>` |
| `idempotency_key` | yes | Dedup across dispatch and reschedule |
| `labels` | no | Routing/filtering |
| `callback` | yes | Where the result goes |
| `created_at` | yes | ISO-8601 with timezone |

### Invariants

1. Workers never receive a git-host credential. `clone_token` is read-only, hub-only,
   and dies with the lease.
2. The hub accepts a bundle only for the **live lease**, containing exactly one ref
   `refs/heads/agent/<job_id>` that descends from `base_commit`.
3. The hub never pushes the default branch and never force-pushes.
4. A second `GET /v1/jobs/{id}` while a live lease exists returns `409`.
5. Empty diff is valid (`success`, `commit=null`, no bundle).
6. `timeout_sec` and `max_turns` are enforced by the runner.
7. Test commands come only from the manifest at `base_commit`.
8. `toolbox` comes from a fixed allowlist.
9. No `sudo`/`apt`; missing system packages ⇒ `blocked`.

## 2. Result

```json
{
  "job_id": "kb-01HXYZ",
  "lease_id": "ls-7f3a",
  "attempt": 1,
  "executor": "claude",
  "status": "success",
  "exit_code": 0,
  "branch": "agent/kb-01HXYZ",
  "commit": "a1b2c3d",
  "bundle_uploaded": true,
  "mr_url": null,
  "test_result": { "ran": true, "passed": true, "target": "auth", "command": "pytest -q tests/auth" },
  "tests_modified": false,
  "summary": "Fixed redirect loop in auth middleware; added regression test.",
  "logs_url": "https://sireum-hub.<tailnet>.ts.net/runs/kb-01HXYZ",
  "usage": { "turns": 9, "input_tokens": 41000, "output_tokens": 3200, "cost_usd": null },
  "started_at": "2026-11-05T14:01:00+07:00",
  "finished_at": "2026-11-05T14:09:00+07:00",
  "error": null
}
```

`mr_url` is filled in by the hub after it pushes and opens the MR.

`error`, when present: `{ "code": "<code>", "message": "<text>", "reset_at": "<ISO-8601|null>" }`.

| `error.code` | Meaning |
|---|---|
| `usage_limit` | Subscription cap / 429; `reset_at` if the CLI reports it |
| `timeout` | `timeout_sec` reached |
| `max_turns` | Turn cap reached |
| `git` | Clone/worktree/bundle error |
| `executor` | CLI crash / non-zero exit |
| `lease_lost` | Heartbeat or fencing failed |
| `no_capacity` | No worker leased the job within `CLAIM_GRACE` (bridge-generated) |
| `needs_access` | The job needs a secret, registry or egress host that isn't approved (see `needs`) |
| `access_denied` | The operator refused an access request the job was waiting on |
| `dispatch` | Nomad dispatch failed (bridge-generated) |
| `prepare` | Prepare phase failed (Phase 3) |

### Status values (booster semantics)

| Status | Meaning | Kanban effect |
|---|---|---|
| `success` | Target passed (or empty diff), branch pushed by the hub | comment + complete |
| `needs_review` | Pushed, but target failed / no target / `tests_modified` / partial branch | comment + request review |
| `blocked` | Needs human input / missing system package | comment + block |
| `failed` | Any `error.code`; `usage_limit` also pauses the executor | comment + release to Hermes |
| `cancelled` | Lease lost, operator stop | comment + release to Hermes |

### Invariants

1. A Result is always posted; if the hub is unreachable the runner spools
   `/cache/results/<job_id>.json` (+ `.bundle`) and the next runner on that node uploads them.
2. The bridge ignores Results and bundles whose `lease_id` is not current.
3. `usage` is best-effort.
4. Never include secrets, tokens, or environment dumps.

### Implementation notes (rev 3.1)

- `base_ref` must match `[A-Za-z0-9][A-Za-z0-9._/-]*` (no `..`, no `//`); `test_target` is a name.
- The bridge adds `dispatch_id` (Nomad dispatched job id) to the stored Job; runners ignore it.
- A lease is **current** while it is the job's latest lease and not released. An expired but
  not superseded lease may still deliver a spooled bundle/result; a newer lease fences it.
- The bridge adds `pushed` (bool) and, on push/MR failure, `hub_error` to the stored Result;
  a `success` whose push fails becomes `needs_review`.
- A Result's `status` must be one of the five values below or the bridge returns `400`.

### Access (Phase 6, rev 3.2)

- Job state `waiting_access`: the repo's manifest requests something not yet approved; the
  job isn't leasable (`409`) until every request is granted (`ACCESS_WAIT_SEC`, default 24 h).
- The lease response adds `grants`:
  `{secrets: [{name, value, expose}], registries: [{name, ecosystem, url, token, scopes,
  mode, auth_scheme, username}], egress: [...], services: [...], errors: [...]}`.
  Relay-mode registries carry the **lease token** (valid only at the hub); inject-mode carry
  the upstream token, used only during prepare.
- `expose`: `prepare` (prepare only) · `test` (prepare + test) · `agent` (all phases).
- A Result may carry `needs: [{kind: secret|registry|egress, name, reason, evidence}]`;
  the bridge opens access requests, sets `status=blocked`, `error.code=needs_access`, and
  adds `access_requests: [{id, kind, name, message}]`. Granting re-runs the job as
  `<job_id>-r<n>` (the old Result gets `rerun_as`).
- Runners and the bridge both replace granted secret/token values with `[redacted]` in
  results and logs. `GET /v1/runs/<id>` shows `granted` (names only).

## 3. Versioning

v1 omits `schema_version`; additive fields allowed; renames need a version bump.

## 4. Examples

`templates/job.example.json`, `templates/result.example.json`.

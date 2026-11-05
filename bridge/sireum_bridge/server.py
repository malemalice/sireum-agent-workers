"""sireum-bridge HTTP API (Phase 0 stub: no kanban polling, optional Nomad dispatch).

Run:  python -m sireum_bridge --config <dir>   (see bridge/README.md "Phase 0 stub")
"""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from .access import AccessStore
from .accessops import AccessOps
from .apis import APIError, Nomad, Tailscale
from .pkgrelay import Relay as PkgRelay
from . import runnersrc
from .booster import Booster
from .creds import Resolver
from .nodes import Nodes
from .pkgrelay import Registry
from .relay import GitHost, Relay, RelayError, Repo
from .sources import SourceError, make_source
from .store import Store

log = logging.getLogger("sireum-bridge")

EXECUTORS = ("claude", "opencode")
TOOLBOXES = ("base",)
JOB_CLASSES = ("standard", "browser", "heavy")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
REF_RE = re.compile(r"^(?!.*\.\.)(?!.*//)[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
STATUSES = ("success", "needs_review", "blocked", "failed", "cancelled")
BACKOFF_MIN = (15, 30, 60)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_limits(spec: str) -> dict[str, int]:
    out = {e: 0 for e in EXECUTORS}
    for part in filter(None, (p.strip() for p in (spec or "").split(","))):
        name, _, val = part.partition("=")
        out[name.strip()] = int(val or 0)
    return out


def parse_duration(v: str | None, default: int) -> int:
    """'10m', '30s', '2h' or plain seconds."""
    if not v:
        return default
    m = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", v)
    if not m:
        raise SystemExit(f"bad duration {v!r}")
    return int(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[m[2]]


def parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Bridge(AccessOps):
    def __init__(self, config_dir: Path, state_dir: Path):
        env = os.environ
        self.worker_token = env.get("WORKER_TOKEN") or ""
        self.operator_token = env.get("OPERATOR_TOKEN") or ""
        if not self.worker_token or not self.operator_token:
            raise SystemExit("WORKER_TOKEN and OPERATOR_TOKEN must be set")
        self.public_url = env.get("BRIDGE_PUBLIC_URL", "http://localhost:8787").rstrip("/")
        self.lease_ttl = int(env.get("LEASE_TTL_SEC", "120"))
        self.limits = parse_limits(env.get("EXECUTOR_LIMITS", ""))
        self.discord_url = env.get("DISCORD_WEBHOOK_URL")
        self.nomad = Nomad(env["NOMAD_ADDR"], env.get("NOMAD_TOKEN")) if env.get("NOMAD_ADDR") else None
        self.claim_grace = parse_duration(env.get("CLAIM_GRACE"), 600)
        self.poll_interval = parse_duration(env.get("POLL_INTERVAL"), 30)

        self.config_dir = config_dir
        self._config_mtimes: tuple = ()
        self.hosts: dict[str, GitHost] = {}
        self.repos: list[Repo] = []
        self.registries: dict[str, Registry] = {}
        self.load_config(initial=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(str(state_dir / "state.db"))
        self.relay = Relay(state_dir, self.hosts, int(env.get("BUNDLE_MAX_MB", "50")), Resolver())
        self.access = AccessStore(self.store)
        self.pkg = PkgRelay(self.relay.creds)
        from .mcp import Tools
        self.mcp = Tools(self)
        self.runs_dir = state_dir / "runs"
        self.runs_dir.mkdir(exist_ok=True)

        ts = (Tailscale(env["TS_OAUTH_CLIENT_ID"], env["TS_OAUTH_CLIENT_SECRET"], env.get("TS_TAILNET", "-"))
              if env.get("TS_OAUTH_CLIENT_ID") and env.get("TS_OAUTH_CLIENT_SECRET") else None)
        servers = env.get("NOMAD_SERVERS", "")
        self.nodes = Nodes(self.store, state_dir, self.nomad, ts, self.public_url, {
            # Comma-separated; also tolerates ["a","b"] after shell/systemd quote stripping.
            "nomad_servers": [x.strip().strip('"\'') for x in servers.strip("[] ").split(",")
                              if x.strip().strip('"\'')],
            "nomad_version": env.get("NOMAD_VERSION", ""),
            # Empty RUNNER_IMAGE ⇒ workers build sireum-runner:local-<hash> from the hub's source.
            "runner_image": env.get("RUNNER_IMAGE") or runnersrc.local_tag(),
            "squid_image": env.get("SQUID_IMAGE", ""),
            "registry": env.get("REGISTRY", ""),
            "registry_auth": ({"server": env.get("REGISTRY_SERVER") or "ghcr.io",
                               "username": env["REGISTRY_USERNAME"], "password": env["REGISTRY_PASSWORD"]}
                              if env.get("REGISTRY_USERNAME") and env.get("REGISTRY_PASSWORD") else None),
        })
        source = make_source(env.get("SOURCE", ""))
        taggers = {t.strip() for t in env.get("REMOTE_TAGGERS", "").split(",") if t.strip()}
        self.booster = Booster(self, source, taggers, self.claim_grace) if source else None

    # -- lookups ----------------------------------------------------------
    def load_config(self, initial: bool = False) -> bool:
        """(Re)load git_hosts.yaml, repos.yaml, registries.yaml (optional) when they change.
        A bad edit keeps the previous config."""
        files = [self.config_dir / "git_hosts.yaml", self.config_dir / "repos.yaml"]
        reg_file = self.config_dir / "registries.yaml"
        try:
            mtimes = tuple(f.stat().st_mtime_ns for f in files) + (
                reg_file.stat().st_mtime_ns if reg_file.exists() else 0,)
        except OSError as e:
            if initial:
                raise SystemExit(f"config missing: {e}")
            return False
        if mtimes == self._config_mtimes:
            return False
        try:
            hosts_doc = yaml.safe_load(files[0].read_text()) or {}
            repos_doc = yaml.safe_load(files[1].read_text()) or {}
            hosts = {h["id"]: GitHost(**h) for h in hosts_doc.get("hosts") or []}
            repos = [Repo(**r) for r in repos_doc.get("repos") or []]
            reg_doc = (yaml.safe_load(reg_file.read_text()) if reg_file.exists() else None) or {}
            registries = {r["name"]: Registry(**r) for r in reg_doc.get("registries") or []}
        except (yaml.YAMLError, TypeError, KeyError, ValueError) as e:
            if initial:
                raise SystemExit(f"bad config: {e}")
            log.error("config reload failed, keeping the previous config: %s", e)
            self._config_mtimes = mtimes
            return False
        self.hosts.clear()
        self.hosts.update(hosts)          # Relay holds the same dict object
        self.repos = repos
        self.registries = registries
        self._config_mtimes = mtimes
        if not initial:
            self.relay.creds.invalidate()
            log.info("config reloaded: hosts=%s repos=%s", ",".join(hosts),
                     ",".join(r.alias for r in repos if r.enabled))
        return True

    def resolve_repo(self, ref: str) -> Repo | None:
        self.load_config()
        for r in self.repos:
            if ref == r.alias or ref == f"{r.git_host}/{r.path}":
                return r if (r.enabled and r.git_host in self.hosts) else None
        return None

    def repo_by_alias(self, alias: str) -> Repo:
        self.load_config()
        return next(r for r in self.repos if r.alias == alias)

    def executor_available(self, name: str) -> bool:
        ex = self.store.executor(name)
        if ex["paused_until"] > time.time():
            return False
        limit = self.limits.get(name, 0)
        return limit <= 0 or self.store.running_count(name) < limit

    def pick_executor(self, wanted: str) -> str | None:
        if wanted == "auto":
            return next((e for e in EXECUTORS if self.executor_available(e)), None)
        return wanted if self.executor_available(wanted) else None

    # -- operations -------------------------------------------------------
    def dispatch(self, body: dict, idem: str | None, card_id: str | None = None) -> tuple[int, dict]:
        for f in ("repo", "task", "acceptance"):
            if not isinstance(body.get(f), str) or not body[f].strip():
                return 400, {"error": f"{f} is required"}
        executor = body.get("executor", "auto")
        if executor not in (*EXECUTORS, "auto"):
            return 400, {"error": "executor must be claude, opencode or auto"}
        tt = body.get("test_target")
        if tt is not None and (not isinstance(tt, str) or not NAME_RE.match(tt)):
            return 400, {"error": "test_target must be a target name, not a command"}
        job_class = body.get("job_class", "standard")
        if job_class not in JOB_CLASSES:
            return 400, {"error": f"job_class must be one of {JOB_CLASSES}"}
        toolbox = body.get("toolbox", "base")
        if toolbox not in TOOLBOXES:
            return 400, {"error": f"toolbox must be one of {TOOLBOXES}"}
        job_id = idem or body.get("job_id") or f"api-{secrets.token_hex(5)}"
        if not JOB_ID_RE.match(job_id):
            return 400, {"error": "invalid job_id / Idempotency-Key"}
        if self.store.job(job_id):
            return 409, {"error": "job already exists", "job_id": job_id}

        base_ref_in = body.get("base_ref")
        if base_ref_in is not None and (not isinstance(base_ref_in, str) or not REF_RE.match(base_ref_in)):
            return 400, {"error": "invalid base_ref"}
        repo = self.resolve_repo(body["repo"])
        if repo is None:
            return 404, {"error": "repo not onboarded"}
        chosen = self.pick_executor(executor)
        if chosen is None:
            return 503, {"error": "executor paused or at limit", "retry_after": 300}
        try:
            self.relay.fetch(repo)
            base_ref = body.get("base_ref") or repo.default_branch
            base_commit = self.relay.resolve(repo, base_ref)
        except RelayError as e:
            return e.code, {"error": str(e)}

        job = {
            "job_id": job_id,
            "source": body.get("source", "api"),
            "source_ref": body.get("source_ref", f"api:{job_id}"),
            "git_host": repo.git_host,
            "repo": repo.path,
            "base_ref": base_ref,
            "task": body["task"],
            "acceptance": body["acceptance"],
            "test_target": tt,
            "executor": executor,
            "model": body.get("model", "default"),
            "timeout_sec": min(int(body.get("timeout_sec", 1800)), 4 * 3600),
            "max_turns": min(int(body.get("max_turns", 40)), 200),
            "max_cost_usd": None,
            "job_class": job_class,
            "toolbox": toolbox,
            "tools_manifest": ".agent/tools.yaml",
            "prepare_timeout_sec": 900,
            "branch": f"agent/{job_id}",
            "idempotency_key": job_id,
            "labels": body.get("labels", [f"repo:{repo.alias}"]),
            "callback": body.get("callback", {"kanban_card": None, "discord_webhook": "default"}),
            "created_at": now_iso(),
        }
        if not self.store.add_job(job, repo.alias, repo.key, chosen, base_commit, card_id):
            return 409, {"error": "job already exists", "job_id": job_id}
        status_url = f"{self.public_url}/v1/runs/{job_id}"
        # Pre-flight: anything the repo's manifest requests but the operator hasn't approved
        # becomes an access request; the job waits instead of failing mid-run.
        miss = self.preflight(repo, base_commit)
        if miss:
            self.store.set_job(job_id, state="waiting_access")
            reqs = self.open_requests(repo.alias, job_id, [{"kind": k, "name": n,
                                                            "reason": "requested in .agent/tools.yaml"}
                                                           for k, n in miss])
            log.info("job %s waiting for access: %s", job_id, [(r["kind"], r["name"]) for r in reqs])
            return 202, {"job_id": job_id, "state": "waiting_access", "executor": chosen,
                         "status_url": status_url, "access_requests": reqs}
        err = self.dispatch_to_nomad(job_id)
        if err:
            return 503, {"error": f"nomad dispatch failed: {err}", "retry_after": 60}
        log.info("dispatched %s repo=%s executor=%s base=%s", job_id, repo.alias, chosen, base_commit[:10])
        return 202, {"job_id": job_id, "state": "dispatched", "executor": chosen, "status_url": status_url}

    def dispatch_to_nomad(self, job_id: str) -> str | None:
        """Start a runner allocation (no-op without Nomad). Returns an error message or None."""
        if not self.nomad:
            return None
        row = self.store.job(job_id)
        job = json.loads(row["job"])
        try:
            job["dispatch_id"] = self.nomad.dispatch(job_id)
            self.store.set_job(job_id, job=json.dumps(job))
            return None
        except APIError as e:
            self.finish(job_id, {"job_id": job_id, "status": "failed", "executor": row["executor"],
                                 "error": {"code": "dispatch", "message": str(e), "reset_at": None},
                                 "finished_at": now_iso()}, notify=False)
            return str(e)

    def discord(self, msg: str) -> None:
        if not self.discord_url:
            return
        try:
            req = urllib.request.Request(self.discord_url, data=json.dumps({"content": msg[:1900]}).encode(),
                                         method="POST", headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).close()
        except OSError as e:
            log.warning("discord webhook failed: %s", e)

    def lease_job(self, job_id: str) -> tuple[int, dict]:
        lease, why = self.store.acquire_lease(job_id, self.lease_ttl)
        if lease is None:
            return (404 if why == "unknown job" else 409), {"error": why}
        row = self.store.job(job_id)
        try:
            grants = self.build_grants(row, self.repo_by_alias(row["repo_alias"]), lease["clone_token"])
        except (StopIteration, RelayError) as e:
            log.warning("grants for %s: %s", job_id, e)
            grants = {"secrets": [], "registries": [], "egress": [], "services": [], "errors": [str(e)]}
        return 200, {
            "grants": grants,
            "job": json.loads(row["job"]),
            "executor": row["executor"],
            "base_commit": row["base_commit"],
            "lease_id": lease["lease_id"],
            "lease_ttl_sec": self.lease_ttl,
            "attempt": lease["attempt"],
            "clone_url": f"{self.public_url}/git/{row['repo_key']}.git",
            "clone_token": lease["clone_token"],
        }

    def accept_bundle(self, job_id: str, lease_id: str, data: bytes) -> tuple[int, dict]:
        with self.store.lock:
            lease = self.store.lease(lease_id)
            row = self.store.job(job_id)
            if row is None:
                return 404, {"error": "unknown job"}
            if not lease or lease["job_id"] != job_id or not self.store.is_current(lease):
                return 409, {"error": "lease not current"}
            if row["state"] == "finished":
                return 410, {"error": "job finished"}
            repo = self.repo_by_alias(row["repo_alias"])
            try:
                tip = self.relay.intake(repo, job_id, row["base_commit"], data)
            except RelayError as e:
                return e.code, {"error": str(e)}
            self.store.set_job(job_id, bundle_tip=tip)
        log.info("bundle accepted %s tip=%s", job_id, tip[:10])
        return 200, {"accepted": True, "commit": tip}

    def accept_result(self, res: dict) -> tuple[int, dict]:
        job_id, lease_id = res.get("job_id"), res.get("lease_id")
        with self.store.lock:
            row = self.store.job(job_id) if isinstance(job_id, str) else None
            lease = self.store.lease(lease_id) if isinstance(lease_id, str) else None
            if row is None:
                return 404, {"error": "unknown job"}
            if row["state"] == "finished":
                return 409, {"error": "result already recorded"}
            if res.get("status") not in STATUSES:
                return 400, {"error": f"status must be one of {STATUSES}"}
            if not lease or lease["job_id"] != job_id or not self.store.is_current(lease):
                return 409, {"error": "lease not current"}
            repo = self.repo_by_alias(row["repo_alias"])
            job = json.loads(row["job"])
            res = {k: v for k, v in res.items()}
            res["executor"] = row["executor"]
            res["logs_url"] = f"{self.public_url}/v1/runs/{job_id}"
            # Scrub any granted secret value the job might echo back.
            secret_vals = self.granted_secret_values(row)
            if secret_vals:
                blob = json.dumps(res)
                for v in secret_vals:
                    blob = blob.replace(json.dumps(v)[1:-1], "[redacted]")
                res = json.loads(blob)
            needs = [n for n in (res.get("needs") or []) if isinstance(n, dict)]
            if needs:
                res["access_requests"] = self.open_requests(row["repo_alias"], job_id, needs)
                if res.get("status") in ("success", "needs_review", "failed"):
                    res["status"] = "blocked"
                res["error"] = res.get("error") or {"code": "needs_access", "reset_at": None,
                                                     "message": "job needs access that was not granted"}
            if row["bundle_tip"] and res.get("status") in ("success", "needs_review", "blocked"):
                try:
                    self.relay.push(repo, job_id, row["base_commit"])
                    res["mr_url"] = self.relay.open_mr(
                        repo, job["branch"], f"[sireum] {job['task'][:70]}",
                        f"{res.get('summary') or ''}\n\nTask: {job['task']}\n\nAcceptance: {job['acceptance']}\n\n"
                        f"Tests: {json.dumps(res.get('test_result'))}\n\nGenerated by Sireum job `{job_id}`.")
                    res["pushed"] = True
                except (RelayError, OSError) as e:
                    log.warning("push/MR failed for %s: %s", job_id, e)
                    res["pushed"] = False
                    res["hub_error"] = str(e)
                    if res.get("status") == "success":
                        res["status"] = "needs_review"
            self.finish(job_id, res)
            self.store.release(lease_id)
        log.info("result %s status=%s mr=%s", job_id, res.get("status"), res.get("mr_url"))
        return 200, {"recorded": True, "status": res.get("status"), "mr_url": res.get("mr_url")}

    def finish(self, job_id: str, res: dict, notify: bool = True) -> None:
        """Record the final Result once, then fan out: executor state, card, Discord."""
        row = self.store.job(job_id)
        self.store.set_job(job_id, state="finished", result=json.dumps(res))
        (self.runs_dir / f"{job_id}.json").write_text(json.dumps(res, indent=2))
        self.after_result(row["executor"], res, notify)
        if notify and row["card_id"] and self.booster:
            self.booster.on_result(row["card_id"], res)

    def after_result(self, executor: str, res: dict, notify: bool = True) -> None:
        err = res.get("error") or {}
        if err.get("code") == "usage_limit":
            level = self.store.executor(executor)["backoff_level"]
            until = parse_iso(err.get("reset_at")) or time.time() + BACKOFF_MIN[min(level, 2)] * 60
            self.store.pause(executor, until, from_usage_limit=True)
            log.warning("executor %s paused until %s (usage_limit)", executor,
                        datetime.fromtimestamp(until).isoformat(timespec="minutes"))
        elif res.get("status") in ("success", "needs_review"):
            self.store.resume(executor, reset_backoff=True)
        if notify:
            msg = f"Sireum `{res.get('job_id')}` → **{res.get('status')}**"
            if res.get("mr_url"):
                msg += f" — {res['mr_url']}"
            if res.get("summary"):
                msg += f"\n{res['summary'][:300]}"
            self.discord(msg)

    def run_view(self, job_id: str) -> dict | None:
        row = self.store.job(job_id)
        if row is None:
            return None
        out = {"job_id": job_id, "state": row["state"], "executor": row["executor"],
               "base_commit": row["base_commit"]}
        lease = self.store.latest_lease(job_id)
        if lease:
            out["attempt"] = lease["attempt"]
        if row["result"]:
            out["result"] = json.loads(row["result"])
        if row["grants"]:
            out["granted"] = json.loads(row["grants"])
        reqs = self.access.open_for_job(job_id)
        if reqs:
            out["access_requests"] = [{"id": r["id"], "kind": r["kind"], "name": r["name"]} for r in reqs]
        return out

    def status(self) -> dict:
        now = time.time()
        execs = {}
        for e in EXECUTORS:
            ex = self.store.executor(e)
            execs[e] = {
                "running": self.store.running_count(e),
                "limit": self.limits.get(e, 0) or "unlimited",
                "paused_until": (datetime.fromtimestamp(ex["paused_until"]).astimezone().isoformat(timespec="seconds")
                                 if ex["paused_until"] > now else None),
                "limit_hits_24h": len([t for t in json.loads(ex["limit_hits"]) if t > now - 86400]),
            }
        return {"executors": execs,
                "nodes": [n["name"] for n in self.nodes.list()],
                "access_requests_pending": len(self.access.pending()),
                "kanban_outbox_pending": len(self.store.outbox_pending()),
                "active": [{"job_id": r["job_id"], "state": r["state"], "executor": r["executor"],
                            "repo": r["repo_alias"]} for r in self.store.active_jobs()],
                "recent": [{"job_id": r["job_id"], "state": r["state"],
                            "status": json.loads(r["result"]).get("status") if r["result"] else None}
                           for r in self.store.recent_jobs()]}

    def daily_check(self) -> None:
        """Credential health + expiry/unused warnings → Discord (problems only)."""
        from .maintenance import doctor
        problems = [f for f in doctor(self.config_dir, self.access.last_used, self.relay.creds)
                    if f["level"] != "ok"]
        for f in problems:
            log.warning("doctor: %s %s: %s", f["level"], f["what"], f["detail"])
        if problems:
            self.discord("Sireum credential check:\n" + "\n".join(
                f"{'✗' if f['level'] == 'fail' else '!'} {f['what']}: {f['detail']}" for f in problems[:20]))

    def reconcile(self) -> None:
        """Hand back jobs nobody picked up within CLAIM_GRACE; fail jobs that never reported."""
        self.expire_waiting()
        now = time.time()
        for row in self.store.active_jobs():
            job = json.loads(row["job"])
            lease = self.store.latest_lease(row["job_id"])
            if row["state"] == "waiting_access":
                continue
            if lease is None and now - row["created_at"] > self.claim_grace:
                if self.nomad and job.get("dispatch_id"):
                    try:
                        self.nomad.stop(job["dispatch_id"])
                    except APIError as e:
                        log.warning("nomad stop %s: %s", job["dispatch_id"], e)
                self.finish(row["job_id"], {
                    "job_id": row["job_id"], "status": "cancelled", "executor": row["executor"],
                    "error": {"code": "no_capacity", "reset_at": None,
                              "message": f"no worker picked the job up within {self.claim_grace}s"},
                    "finished_at": now_iso()})
                log.warning("reconciler cancelled %s (never leased)", row["job_id"])
            elif lease and now - row["created_at"] > job["timeout_sec"] + 600 and not self.store.is_live(lease):
                self.store.release(lease["lease_id"])
                self.finish(row["job_id"], {
                    "job_id": row["job_id"], "status": "failed", "executor": row["executor"],
                    "error": {"code": "lease_lost", "message": "no result before timeout", "reset_at": None},
                    "finished_at": now_iso()})
                log.warning("reconciler failed %s (no result)", row["job_id"])


# -- HTTP ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    bridge: Bridge
    server_version = "sireum-bridge/0.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: dict | None = None) -> None:
        data = json.dumps(body or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _token(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        if auth.startswith("Basic "):
            try:
                return base64.b64decode(auth[6:]).decode().partition(":")[2]
            except ValueError:
                return ""
        return ""

    def _authz(self, *allowed: str) -> bool:
        tok = self._token()
        ok = any(t and hmac.compare_digest(tok, t) for t in allowed)
        if not ok:
            self._send(401, {"error": "unauthorized"})
        return ok

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            out = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(out)
                out += self.rfile.read(size)
                self.rfile.readline()
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self) -> dict | None:
        try:
            body = json.loads(self._body() or b"{}")
            if isinstance(body, dict):
                return body
        except json.JSONDecodeError:
            pass
        self._send(400, {"error": "invalid JSON"})
        return None

    def do_GET(self):
        b, path = self.bridge, self.path.split("?")[0]
        try:
            if path == "/healthz":
                return self._send(200, {"ok": True})
            if path.startswith("/git/"):
                return self._git()
            if m := re.fullmatch(r"/pkg/([a-z0-9][a-z0-9_.-]*)(/.*)?", path):
                return self._pkg(m[1], m[2] or "/")
            if m := re.fullmatch(r"/v1/nodes/([a-z0-9-]+)/egress", path):
                if not b.nodes.auth_node(m[1], self._token()):
                    return self._send(401, {"error": "unauthorized"})
                return self._send(200, {"hosts": b.egress_union()})
            if path == "/v1/access":
                if self._authz(b.operator_token):
                    self._send(200, {"pending": b.list_requests()})
                return
            if path == "/v1/actions":
                if self._authz(b.operator_token):
                    self._send(200, {"pending": b.mcp.clients.actions()})
                return
            if m := re.fullmatch(r"/v1/jobs/([^/]+)", path):
                if self._authz(b.worker_token):
                    self._send(*b.lease_job(m[1]))
                return
            if m := re.fullmatch(r"/v1/leases/([^/]+)", path):
                if self._authz(b.worker_token):
                    lease = b.store.lease(m[1])
                    if not lease:
                        return self._send(404, {"error": "unknown lease"})
                    self._send(200, {"lease_id": m[1], "live": b.store.is_live(lease),
                                     "current": b.store.is_current(lease)})
                return
            if m := re.fullmatch(r"/v1/nodes/([a-z0-9-]+)/runner(\.tgz)?", path):
                if not b.nodes.auth_node(m[1], self._token()):
                    return self._send(401, {"error": "unauthorized"})
                if m[2]:
                    data = runnersrc.tarball()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/gzip")
                    self.send_header("X-Sireum-Runner-Image", b.nodes.settings["runner_image"])
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                return self._send(200, {"image": b.nodes.settings["runner_image"],
                                        "build_from_source": b.nodes.settings["runner_image"].startswith(
                                            "sireum-runner:local-")})
            if m := re.fullmatch(r"/v1/runs/([^/]+)", path):
                if self._authz(b.operator_token):
                    view = b.run_view(m[1])
                    self._send(200, view) if view else self._send(404, {"error": "unknown job"})
                return
            if path == "/status":
                if self._authz(b.operator_token):
                    self._send(200, b.status())
                return
            self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            log.exception("GET %s", path)
            self._send(500, {"error": str(e)})

    def do_POST(self):
        b, path = self.bridge, self.path.split("?")[0]
        try:
            if path.startswith("/git/"):
                return self._git()
            if path == "/v1/nodes/enroll":
                if (body := self._json()) is not None:
                    self._send(*b.nodes.enroll(self._token(), body))
                return
            if path == "/v1/dispatch":
                if self._authz(b.operator_token) and (body := self._json()) is not None:
                    self._send(*b.dispatch(body, self.headers.get("Idempotency-Key")))
                return
            if m := re.fullmatch(r"/v1/leases/([^/]+)/heartbeat", path):
                if self._authz(b.worker_token):
                    ok = b.store.heartbeat(m[1], b.lease_ttl)
                    self._send(200 if ok else 410, {"live": ok})
                return
            if path == "/v1/results":
                if self._authz(b.worker_token) and (body := self._json()) is not None:
                    self._send(*b.accept_result(body))
                return
            if m := re.fullmatch(r"/v1/access/(\d+)/(grant|deny)", path):
                if self._authz(b.operator_token) and (body := self._json()) is not None:
                    if m[2] == "grant":
                        self._send(*b.grant(int(m[1]), body))
                    else:
                        self._send(*b.deny(int(m[1]), str(body.get("note") or "no reason given")))
                return
            if m := re.fullmatch(r"/v1/actions/([A-Za-z0-9]{8})/(done|reject)", path):
                if self._authz(b.operator_token):
                    body = self._json() or {}
                    ok = b.mcp.clients.close(m[1], "done" if m[2] == "done" else "rejected",
                                             str(body.get("result") or ""))
                    if ok and m[2] == "reject":
                        b.discord(f"Sireum: proposal {m[1].upper()} rejected by the operator.")
                    self._send(200 if ok else 404, {"closed": ok})
                return
            if m := re.fullmatch(r"/v1/jobs/([^/]+)/rerun", path):
                if self._authz(b.operator_token):
                    body = self._json() or {}
                    self._send(*b.rerun(m[1], str(body.get("why") or "re-run requested by the operator")))
                return
            if m := re.fullmatch(r"/v1/executors/(claude|opencode)/(pause|resume)", path):
                if self._authz(b.operator_token):
                    body = self._json() or {}
                    if m[2] == "pause":
                        until = time.time() + int(body.get("minutes", 60)) * 60
                        b.store.pause(m[1], until, from_usage_limit=False)
                    else:
                        b.store.resume(m[1])
                    self._send(200, b.status()["executors"][m[1]])
                return
            self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            log.exception("POST %s", path)
            self._send(500, {"error": str(e)})

    def do_PUT(self):
        b, path = self.bridge, self.path.split("?")[0]
        try:
            if m := re.fullmatch(r"/v1/nodes/([a-z0-9-]+)/seats", path):
                if not b.nodes.auth_node(m[1], self._token()):
                    return self._send(401, {"error": "unauthorized"})
                if (body := self._json()) is not None:
                    self._send(*b.nodes.put_seats(m[1], body))
                return
            if m := re.fullmatch(r"/v1/jobs/([^/]+)/bundle", path):
                if self._authz(b.worker_token):
                    lease_id = self.headers.get("X-Sireum-Lease", "")
                    self._send(*b.accept_bundle(m[1], lease_id, self._body()))
                return
            self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            log.exception("PUT %s", path)
            self._send(500, {"error": str(e)})

    def do_DELETE(self):
        b, path = self.bridge, self.path.split("?")[0]
        try:
            if m := re.fullmatch(r"/v1/nodes/([a-z0-9-]+)", path):
                tok = self._token()
                if not (b.nodes.auth_node(m[1], tok) or (tok and hmac.compare_digest(tok, b.operator_token))):
                    return self._send(401, {"error": "unauthorized"})
                return self._send(*b.nodes.leave(m[1]))
            self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            log.exception("DELETE %s", path)
            self._send(500, {"error": str(e)})

    def _pkg(self, name: str, rest: str):
        """Package relay: lease token in, upstream credential added by the hub."""
        b = self.bridge
        lease = b.store.lease_by_token(self._token())
        row = b.store.job(lease["job_id"]) if lease else None
        granted = json.loads((row["grants"] if row else None) or "{}").get("registries", [])
        reg = b.registries.get(name)
        if not lease or not b.store.is_live(lease) or not row or name not in granted or not reg or reg.mode != "relay":
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="sireum-pkg"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            r = b.pkg.forward(reg, rest, self.path.partition("?")[2], f"{b.public_url}/pkg/{name}",
                              self.headers.get("Accept"))
        except Exception as e:  # noqa: BLE001
            log.warning("pkg relay %s %s: %s", name, rest, e)
            return self._send(502, {"error": "relay failed"})
        self.send_response(r.status)
        for k, v in r.headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(r.body)))
        self.end_headers()
        self.wfile.write(r.body)

    # Read-only smart HTTP for the lease's repo only.
    def _git(self):
        b = self.bridge
        m = re.fullmatch(r"/git/([0-9a-f]{16})\.git(/.*)", self.path.split("?")[0])
        query = self.path.partition("?")[2]
        if not m:
            return self._send(404, {"error": "not found"})
        key, rest = m[1], m[2]
        allowed = (rest == "/info/refs" and query == "service=git-upload-pack") or \
                  (rest == "/git-upload-pack" and self.command == "POST")
        if not allowed:
            return self._send(403, {"error": "read-only"})
        lease = b.store.lease_by_token(self._token())
        row = b.store.job(lease["job_id"]) if lease else None
        if not lease or not b.store.is_live(lease) or not row or row["repo_key"] != key:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="sireum"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self._body() if self.command == "POST" else b""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_PROJECT_ROOT": str(b.relay.root / "git"),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": f"/{key}.git{rest}",
            "QUERY_STRING": query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
            "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            "REMOTE_USER": lease["lease_id"],
            "REMOTE_ADDR": self.client_address[0],
        }
        proc = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True)
        head, _, payload = proc.stdout.partition(b"\r\n\r\n")
        if not _:
            head, _, payload = proc.stdout.partition(b"\n\n")
        status, headers = 200, []
        for line in head.decode(errors="replace").splitlines():
            k, _, v = line.partition(":")
            if k.lower() == "status":
                status = int(v.strip().split()[0])
            elif k:
                headers.append((k, v.strip()))
        self.send_response(status)
        for k, v in headers:
            if k.lower() != "content-length":
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="sireum-bridge")
    ap.add_argument("--config", type=Path, default=Path(os.environ.get("SIREUM_CONFIG", "/srv/sireum/bridge")))
    ap.add_argument("--state", type=Path, default=Path(os.environ.get("SIREUM_STATE", "/srv/sireum/state")))
    ap.add_argument("--listen", default=os.environ.get("LISTEN", "127.0.0.1:8787"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bridge = Bridge(args.config, args.state)
    Handler.bridge = bridge

    def loop(fn, every, name):
        def run():
            while True:
                time.sleep(every)
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    log.exception(name)
        threading.Thread(target=run, daemon=True, name=name).start()

    loop(bridge.reconcile, int(os.environ.get("RECONCILE_INTERVAL", "60")), "reconciler")
    if bridge.booster:
        loop(bridge.booster.tick, bridge.poll_interval, "booster")
    loop(bridge.daily_check, int(os.environ.get("DOCTOR_INTERVAL", str(24 * 3600))), "doctor")
    # MCP: a separate listener that serves only /mcp (publish it with Tailscale Funnel).
    mcp_listen = os.environ.get("MCP_LISTEN", "127.0.0.1:8790")
    if mcp_listen.lower() not in ("off", "0", "false", ""):
        from .mcp import make_handler
        mh, _, mp = mcp_listen.rpartition(":")
        msrv = ThreadingHTTPServer((mh or "127.0.0.1", int(mp)), make_handler(bridge.mcp))
        threading.Thread(target=msrv.serve_forever, daemon=True, name="mcp").start()
        log.info("MCP listening on %s (only /mcp)", mcp_listen)
    host, _, port = args.listen.rpartition(":")
    srv = ThreadingHTTPServer((host or "0.0.0.0", int(port)), Handler)
    log.info("listening on %s (repos: %s)", args.listen, ", ".join(r.alias for r in bridge.repos if r.enabled))
    srv.serve_forever()

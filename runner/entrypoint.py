"""sireum-runner: worker side of docs/job-contract.md.

Lease a job from the bridge, clone read-only from the hub, run the executor in a
worktree, run the repo's test target, upload a bundle, post the Result.
The runner never pushes and never holds a git-host credential.
"""
from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

BRIDGE_URL = os.environ.get("BRIDGE_URL", "").rstrip("/")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")
CACHE = Path(os.environ.get("CACHE_DIR", "/cache"))
TEST_TIMEOUT = int(os.environ.get("TEST_TIMEOUT_SEC", "900"))
# Test-only: replace the leased executor with a deterministic fake (no model).
# Set on the worker by the operator, never reachable from card text.
FAKE = os.environ.get("SIREUM_ALLOW_FAKE_EXECUTOR") == "1" and \
    os.environ.get("SIREUM_EXECUTOR_OVERRIDE") == "fake"
SECRET_ENV = ("WORKER_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "OPENCODE_TOKEN", "ANTHROPIC_API_KEY")

USAGE_LIMIT_RE = re.compile(
    r"(usage limit|limit reached|rate[ _-]?limit|too many requests|\b429\b|quota exceeded|insufficient balance)",
    re.I)
RESET_RE = re.compile(r"resets? (?:at )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)

AGENT_PROMPT = """You are running as a Sireum agent inside a sandboxed git worktree.

Task:
{task}

Acceptance criteria:
{acceptance}

Rules:
- Work only inside the current directory (a git worktree on branch {branch}).
- Commit your changes when done, or leave them in the working tree. Do NOT push.
- Network goes only through a proxy to model endpoints and package registries.
- No sudo or apt. If you need a system package or human input, stop and print
  a line starting with "BLOCKED:" and the reason.
- Do not edit tests, .agent/ or CI files unless the task requires it.
- Finish with a short summary of what you changed.
"""

FAKE_SCRIPT = (
    'printf "%s\\n" "$SIREUM_TASK" >> SIREUM_FAKE.md; '
    'case "$SIREUM_TASK" in '
    '*usage-limit*) echo "Claude usage limit reached. Your limit resets at 5pm" >&2; exit 1;; '
    '*sleep*) sleep 600;; '
    '*blocked*) echo "BLOCKED: needs libpq-dev";; '
    '*touch-tests*) mkdir -p tests && echo "# edited" >> tests/test_fake.py;; '
    '*noop*) git checkout -q -- . 2>/dev/null; rm -f SIREUM_FAKE.md;; '
    'esac; echo "fake executor edited SIREUM_FAKE.md"'
)


def log(msg: str) -> None:
    print(f"[sireum-runner {datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class HTTPStatus(Exception):
    def __init__(self, code: int, body: str):
        super().__init__(f"HTTP {code}: {body[:200]}")
        self.code = code


def api(method: str, path: str, body: bytes | dict | None = None, headers: dict | None = None,
        timeout: int = 60) -> dict:
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    hdrs = {"Authorization": f"Bearer {WORKER_TOKEN}", **(headers or {})}
    if isinstance(body, dict):
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{BRIDGE_URL}{path}", data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise HTTPStatus(e.code, e.read().decode(errors="replace")) from None


def clean_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env.update(extra or {})
    return env


def git(*args: str, cwd: Path | None = None, env: dict | None = None, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, env=clean_env({"GIT_TERMINAL_PROMPT": "0", **(env or {})}),
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {p.stderr.strip()}")
    return p.stdout.strip()


# -- spool ------------------------------------------------------------------

def flush_spool() -> None:
    """Upload results (and bundles) spooled by earlier runs while the hub was down."""
    spool = CACHE / "results"
    for res_file in sorted(spool.glob("*.json")):
        try:
            res = json.loads(res_file.read_text())
            bundle = spool / f"{res['job_id']}.bundle"
            if bundle.exists() and not res.get("bundle_uploaded"):
                api("PUT", f"/v1/jobs/{res['job_id']}/bundle", bundle.read_bytes(),
                    {"X-Sireum-Lease": res["lease_id"], "Content-Type": "application/x-git-bundle"})
                res["bundle_uploaded"] = True
            api("POST", "/v1/results", res)
            log(f"uploaded spooled result {res['job_id']}")
        except HTTPStatus as e:
            log(f"spooled {res_file.name} rejected ({e.code}); dropping")
        except (OSError, ValueError, KeyError) as e:
            log(f"spool upload failed for {res_file.name}: {e}; keeping")
            continue
        res_file.unlink(missing_ok=True)
        (spool / f"{res_file.stem}.bundle").unlink(missing_ok=True)


# -- heartbeat --------------------------------------------------------------

class Heartbeat(threading.Thread):
    def __init__(self, lease_id: str, ttl: int, on_lost):
        super().__init__(daemon=True)
        self.lease_id, self.interval = lease_id, max(5, min(60, ttl // 3))
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.on_lost = on_lost

    def run(self) -> None:
        while not self.stop.wait(self.interval):
            try:
                api("POST", f"/v1/leases/{self.lease_id}/heartbeat", {}, timeout=15)
            except HTTPStatus:
                log("lease lost (heartbeat rejected)")
                self.lost.set()
                self.on_lost()
                return
            except OSError as e:
                # Hub unreachable: keep working; bundle intake fences us later.
                log(f"heartbeat failed: {e}")


# -- executors --------------------------------------------------------------

def executor_cmd(executor: str, prompt: str, max_turns: int, model: str) -> list[str]:
    if FAKE:
        return ["sh", "-c", FAKE_SCRIPT]
    if executor == "claude":
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--max-turns", str(max_turns), "--dangerously-skip-permissions"]
        if model and model != "default":
            cmd += ["--model", model]
        return cmd
    if executor == "opencode":
        # No turn flag; the runner's timeout is the bound.
        cmd = ["opencode", "run", prompt]
        if model and model != "default":
            cmd += ["--model", model]
        return cmd
    raise ValueError(f"unknown executor {executor!r}")


def run_executor(cmd: list[str], cwd: Path, timeout: int, extra_env: dict, log_path: Path,
                 cancel: threading.Event) -> tuple[int | None, str, bool]:
    """Run in its own process group; returns (exit_code, output, timed_out)."""
    env = {**os.environ, **extra_env}
    env.pop("WORKER_TOKEN", None)
    start = log_path.stat().st_size if log_path.exists() else 0
    with open(log_path, "a") as lf:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=lf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + timeout
        timed_out = False
        while proc.poll() is None:
            if time.time() > deadline or cancel.is_set():
                timed_out = not cancel.is_set()
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(proc.pid, sig)
                        proc.wait(10)
                        break
                    except ProcessLookupError:
                        break
                    except subprocess.TimeoutExpired:
                        continue
                break
            time.sleep(0.5)
        rc = proc.poll()
    with open(log_path, "rb") as f:
        f.seek(start)
        out = f.read().decode(errors="replace")
    return rc, out, timed_out


def parse_claude_stream(out: str) -> dict:
    """Pull summary / usage / subtype from the final claude stream-json event."""
    info: dict = {}
    for line in out.splitlines():
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            u = ev.get("usage") or {}
            info = {"subtype": ev.get("subtype"), "summary": ev.get("result"), "is_error": ev.get("is_error"),
                    "turns": ev.get("num_turns"), "input_tokens": u.get("input_tokens"),
                    "output_tokens": u.get("output_tokens")}
    return info


def detect_usage_limit(out: str) -> tuple[bool, str | None]:
    tail = out[-4000:]
    if not USAGE_LIMIT_RE.search(tail):
        return False, None
    m = RESET_RE.search(tail)
    if not m:
        return True, None
    hour, minute, ampm = int(m[1]), int(m[2] or 0), (m[3] or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return True, None
    now = datetime.now().astimezone()
    reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return True, reset.isoformat(timespec="seconds")


# -- main job ---------------------------------------------------------------

def load_manifest(mirror: Path, base_commit: str, path: str) -> dict:
    p = subprocess.run(["git", "show", f"{base_commit}:{path}"], cwd=mirror, capture_output=True, text=True)
    if p.returncode != 0:
        return {}
    try:
        doc = yaml.safe_load(p.stdout) or {}
        return doc if isinstance(doc, dict) else {}
    except yaml.YAMLError:
        return {}


def prepare(manifest: dict, wt: Path, mirror: Path, job: dict, log_path: Path) -> tuple[dict, str | None, str | None]:
    """Phase 3 prepare: install repo dependencies from the base_commit manifest.

    Returns (env for executor + tests, error_code, message). error_code is
    `blocked` for a missing system package (no sudo/apt in jobs) or `prepare`.
    """
    env: dict = {
        "npm_config_cache": str(CACHE / "toolchains" / "npm"),
        "PIP_CACHE_DIR": str(CACHE / "toolchains" / "pip"),
    }
    # apt packages must already be in the image (image-build time only).
    missing = []
    for pkg in manifest.get("apt") or []:
        if not shutil.which("dpkg") or \
                subprocess.run(["dpkg", "-s", str(pkg)], capture_output=True).returncode != 0:
            missing.append(str(pkg))
    if missing:
        return env, "blocked", f"system packages not in image {job.get('toolbox', 'base')}: {', '.join(missing)}"

    steps: list[str] = []
    pip = manifest.get("pip") or {}
    if isinstance(pip, dict) and pip.get("install"):
        venv = wt / ".sireum-venv"
        excl = mirror / "info" / "exclude"
        excl.parent.mkdir(exist_ok=True)
        if ".sireum-venv/" not in (excl.read_text() if excl.exists() else ""):
            with open(excl, "a") as f:
                f.write("\n.sireum-venv/\n")
        steps.append(f"python3 -m venv {venv}")
        env["VIRTUAL_ENV"] = str(venv)
        env["PATH"] = f"{venv}/bin:" + os.environ.get("PATH", "")
        steps.append(str(pip["install"]))
    npm = manifest.get("npm") or {}
    if isinstance(npm, dict) and npm.get("install"):
        steps.append(str(npm["install"]))
    pw = manifest.get("playwright") or {}
    if isinstance(pw, dict) and pw.get("version"):
        version = str(pw["version"])
        if not re.fullmatch(r"[0-9][0-9A-Za-z.x-]{0,20}", version):
            return env, "prepare", f"invalid playwright version {version!r}"
        browsers = [b for b in (pw.get("browsers") or ["chromium"]) if b in ("chromium", "firefox", "webkit")]
        baked = Path("/ms-playwright")
        if baked.is_dir() and os.environ.get("PLAYWRIGHT_VERSION", "").startswith(version.rstrip("x")):
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(baked)
        else:
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(CACHE / "ms-playwright" / version)
            steps.append(f"npx -y playwright@{version} install {' '.join(browsers)}")
    if not steps:
        return env, None, None

    full_env = clean_env(env)
    # Worker-side cap (operator env) can only shorten the job's prepare_timeout_sec.
    limit = int(job.get("prepare_timeout_sec") or 900)
    if os.environ.get("SIREUM_PREPARE_TIMEOUT_SEC", "").isdigit():
        limit = min(limit, int(os.environ["SIREUM_PREPARE_TIMEOUT_SEC"]))
    deadline = time.time() + limit
    with open(log_path, "a") as lf:
        for cmd in steps:
            left = deadline - time.time()
            if left <= 0:
                return env, "prepare", f"prepare exceeded {limit}s"
            lf.write(f"\n--- prepare: {cmd}\n")
            lf.flush()
            try:
                p = subprocess.run(["sh", "-c", cmd], cwd=wt, env=full_env, stdout=lf, stderr=subprocess.STDOUT,
                                   timeout=left)
            except subprocess.TimeoutExpired:
                return env, "prepare", f"prepare step timed out: {cmd}"
            if p.returncode != 0:
                return env, "prepare", f"prepare step failed (rc={p.returncode}): {cmd}"
    return env, None, None


def is_protected(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        pat = str(pat)
        if (pat.endswith("/") and path.startswith(pat)) or fnmatch.fnmatch(path, pat):
            return True
    return False


def run_job(job_id: str) -> int:
    try:
        lease = api("GET", f"/v1/jobs/{job_id}")
    except HTTPStatus as e:
        if e.code == 409:
            log(f"job {job_id} already leased or finished; exiting")
            return 0
        log(f"cannot lease job: {e}")
        return 1
    except OSError as e:
        log(f"bridge unreachable: {e}")
        return 1

    job, executor = lease["job"], lease["executor"]
    lease_id, base_commit = lease["lease_id"], lease["base_commit"]
    branch = job["branch"]
    result = {
        "job_id": job_id, "lease_id": lease_id, "attempt": lease["attempt"], "executor": executor,
        "status": "failed", "exit_code": None, "branch": branch, "commit": None,
        "bundle_uploaded": False, "mr_url": None,
        "test_result": {"ran": False, "passed": None, "target": job.get("test_target"), "command": None},
        "tests_modified": False, "summary": None, "logs_url": None,
        "usage": {"turns": None, "input_tokens": None, "output_tokens": None, "cost_usd": None},
        "started_at": now_iso(), "finished_at": None, "error": None,
    }
    log(f"leased {job_id} attempt={lease['attempt']} executor={executor}{' (fake)' if FAKE else ''} "
        f"base={base_commit[:10]}")

    cancel = threading.Event()
    hb = Heartbeat(lease_id, int(lease.get("lease_ttl_sec", 120)), cancel.set)
    hb.start()

    key = hashlib.sha256(lease["clone_url"].encode()).hexdigest()[:16]
    for d in ("mirrors", "work", "locks", "results", "logs", "toolchains"):
        (CACHE / d).mkdir(parents=True, exist_ok=True)
    mirror = CACHE / "mirrors" / f"{key}.git"
    wt = CACHE / "work" / job_id
    bundle_path = CACHE / "results" / f"{job_id}.bundle"
    log_path = CACHE / "logs" / f"{job_id}.log"
    log_path.write_text(f"sireum job {job_id} attempt {lease['attempt']} executor {executor}\n")
    lock_f = open(CACHE / "locks" / f"{key}.lock", "w")
    ident = {"GIT_AUTHOR_NAME": "Sireum Agent", "GIT_AUTHOR_EMAIL": "sireum@localhost",
             "GIT_COMMITTER_NAME": "Sireum Agent", "GIT_COMMITTER_EMAIL": "sireum@localhost"}

    def fail(code: str, message: str, status: str = "failed") -> None:
        result["status"] = status
        result["error"] = {"code": code, "message": message[:1000], "reset_at": None}

    try:
        # Mirror + worktree, serialised per repo on this node.
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            if not mirror.exists():
                git("init", "--bare", "--quiet", str(mirror))
            auth = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {lease['clone_token']}"}
            # Hub refs go to refs/remotes/hub/* so a concurrent job's fetch/prune never
            # touches another job's local agent/<id> branch in this shared mirror.
            git("fetch", "--quiet", "--prune", lease["clone_url"], "+refs/heads/*:refs/remotes/hub/*",
                cwd=mirror, env=auth)
            git("cat-file", "-e", f"{base_commit}^{{commit}}", cwd=mirror)
            if wt.exists():
                git("worktree", "remove", "--force", str(wt), cwd=mirror, check=False)
                shutil.rmtree(wt, ignore_errors=True)
            git("worktree", "prune", cwd=mirror)
            git("branch", "-D", branch, cwd=mirror, check=False)
            git("worktree", "add", "--quiet", "-b", branch, str(wt), base_commit, cwd=mirror)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)

        # Manifest at base_commit — never from the card or the agent's edits.
        manifest = load_manifest(mirror, base_commit, job.get("tools_manifest") or ".agent/tools.yaml")
        targets = manifest.get("test_targets") or {}
        protected = manifest.get("protected_paths") or ["tests/", ".agent/", ".github/", ".gitlab-ci.yml"]

        # Prepare (deps from the manifest at base_commit), before the agent sees the tree.
        prep_env, prep_err, prep_msg = prepare(manifest, wt, mirror, job, log_path)
        if prep_err == "blocked":
            result["status"], result["summary"] = "blocked", prep_msg
            return 0
        if prep_err:
            fail("prepare", prep_msg or "prepare failed")
            return 0

        # Executor.
        prompt = AGENT_PROMPT.format(task=job["task"], acceptance=job["acceptance"], branch=branch)
        cmd = executor_cmd(executor, prompt, int(job["max_turns"]), job.get("model", "default"))
        log(f"running executor (timeout {job['timeout_sec']}s, max_turns {job['max_turns']})")
        with open(log_path, "a") as lf:
            lf.write("\n--- executor\n")
        rc, out, timed_out = run_executor(cmd, wt, int(job["timeout_sec"]),
                                          {**ident, **prep_env, "SIREUM_TASK": job["task"]}, log_path, cancel)
        result["exit_code"] = rc
        info = parse_claude_stream(out) if executor == "claude" and not FAKE else {}
        result["summary"] = (info.get("summary") or out.strip()[-1500:] or None)
        result["usage"].update({k: info.get(k) for k in ("turns", "input_tokens", "output_tokens")})

        if hb.lost.is_set():
            fail("lease_lost", "lease lost during execution", "cancelled")
            return 0
        limited, reset_at = detect_usage_limit(out) if (rc != 0 or info.get("is_error")) else (False, None)
        if timed_out:
            fail("timeout", f"executor exceeded {job['timeout_sec']}s")
        elif limited:
            fail("usage_limit", "executor reported a usage limit")
            result["error"]["reset_at"] = reset_at
        elif info.get("subtype") == "error_max_turns":
            fail("max_turns", f"executor hit max_turns={job['max_turns']}")
        elif rc != 0:
            fail("executor", f"executor exited {rc}: {out.strip()[-500:]}")
        # Partial work is kept for review only on max_turns.
        if result["error"] and result["error"]["code"] != "max_turns":
            return 0

        git("add", "-A", cwd=wt)
        if git("status", "--porcelain", cwd=wt):
            git("commit", "--quiet", "--no-verify", "-m", f"sireum: {job['task'][:60]}\n\nJob: {job_id}",
                cwd=wt, env=ident)
        blocked = bool(re.search(r"^\s*BLOCKED:", out, re.M))
        if git("rev-list", "--count", f"{base_commit}..HEAD", cwd=wt) == "0":
            if blocked:
                result["status"], result["error"] = "blocked", None
            elif not result["error"]:
                result["status"] = "success"  # empty diff is valid
            return 0

        result["commit"] = git("rev-parse", "HEAD", cwd=wt)
        changed = git("diff", "--name-only", base_commit, "HEAD", cwd=wt).splitlines()
        result["tests_modified"] = any(is_protected(p, protected) for p in changed if p)

        # Test target, by name, from the base_commit manifest.
        target = job.get("test_target")
        if target and target in targets and not result["error"]:
            tcmd = str(targets[target])
            result["test_result"].update(ran=True, command=tcmd)
            try:
                tp = subprocess.run(["sh", "-c", tcmd], cwd=wt, env=clean_env(prep_env), capture_output=True,
                                    text=True, timeout=TEST_TIMEOUT)
                result["test_result"]["passed"] = tp.returncode == 0
                with open(log_path, "a") as lf:
                    lf.write(f"\n--- test {target}: {tcmd} (rc={tp.returncode})\n"
                             f"{tp.stdout[-4000:]}{tp.stderr[-4000:]}")
            except subprocess.TimeoutExpired:
                result["test_result"]["passed"] = False

        # Bundle: exactly one ref agent/<job_id>, base excluded.
        git("bundle", "create", "--quiet", str(bundle_path), branch, f"^{base_commit}", cwd=wt)
        try:
            api("PUT", f"/v1/jobs/{job_id}/bundle", bundle_path.read_bytes(),
                {"X-Sireum-Lease": lease_id, "Content-Type": "application/x-git-bundle"}, timeout=300)
            result["bundle_uploaded"] = True
        except HTTPStatus as e:
            if e.code in (409, 410):
                fail("lease_lost", f"bundle rejected: {e}", "cancelled")
            else:
                fail("git", f"bundle rejected: {e}")
            return 0
        except OSError as e:
            log(f"bundle upload failed, will spool: {e}")

        if result["error"] or blocked:
            result["status"] = "blocked" if blocked and not result["error"] else "needs_review"
        elif result["test_result"]["passed"] and not result["tests_modified"]:
            result["status"] = "success"
        else:
            result["status"] = "needs_review"
        return 0
    except Exception as e:  # noqa: BLE001
        log(f"error: {e}")
        if not result["error"]:
            fail("git" if str(e).startswith("git ") else "executor", str(e))
        return 0
    finally:
        hb.stop.set()
        result["finished_at"] = now_iso()
        post_result(result, bundle_path)
        if mirror.exists():
            git("worktree", "remove", "--force", str(wt), cwd=mirror, check=False)
            git("worktree", "prune", cwd=mirror, check=False)
            git("branch", "-D", branch, cwd=mirror, check=False)
        shutil.rmtree(wt, ignore_errors=True)
        lock_f.close()


def post_result(result: dict, bundle_path: Path) -> None:
    try:
        resp = api("POST", "/v1/results", result)
        log(f"result {resp.get('status', result['status'])} recorded; mr_url={resp.get('mr_url')}")
        bundle_path.unlink(missing_ok=True)
    except HTTPStatus as e:
        log(f"result rejected: {e}")
        bundle_path.unlink(missing_ok=True)
    except OSError as e:
        log(f"bridge unreachable, spooling result: {e}")
        (CACHE / "results" / f"{result['job_id']}.json").write_text(json.dumps(result))
        if result["bundle_uploaded"]:
            bundle_path.unlink(missing_ok=True)  # keep it only if it still needs uploading


def main() -> int:
    job_id = os.environ.get("JOB_ID", "")
    if not (job_id and BRIDGE_URL and WORKER_TOKEN):
        log("JOB_ID, BRIDGE_URL and WORKER_TOKEN are required")
        return 2
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", job_id):
        log("invalid JOB_ID")
        return 2
    (CACHE / "results").mkdir(parents=True, exist_ok=True)
    try:
        flush_spool()
    except OSError as e:
        log(f"spool flush skipped: {e}")
    return run_job(job_id)


if __name__ == "__main__":
    raise SystemExit(main())

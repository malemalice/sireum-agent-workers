"""End-to-end tests: real bridge + real runner (fake executor) + local sandbox git host.

Run:  python3 -m unittest discover -s tests -v
Needs: git, PyYAML. No Docker, no network, no model.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "runner" / "entrypoint.py"
WORKER, OPERATOR = "w-test-token", "op-test-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sireum-e2e-"))
        host = cls.tmp / "host"
        host.mkdir()
        # Sandbox git host: bare repo with main + a tools manifest.
        seed = cls.tmp / "seed"
        sh("git", "init", "-q", "-b", "main", str(seed))
        (seed / ".agent").mkdir()
        (seed / ".agent" / "tools.yaml").write_text(
            "version: 1\n"
            "test_targets:\n"
            "  fake: test -f SIREUM_FAKE.md\n"
            "  fails: exit 1\n"
            "protected_paths: [tests/, .agent/]\n")
        (seed / "README.md").write_text("sandbox\n")
        sh("git", "add", "-A", cwd=seed)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=seed)
        sh("git", "clone", "-q", "--bare", str(seed), str(host / "sireum-sandbox.git"))
        cls.host_repo = host / "sireum-sandbox.git"

        cfg = cls.tmp / "config"
        cfg.mkdir()
        (cfg / "git_hosts.yaml").write_text(f"hosts:\n  - id: local\n    type: local\n    git_url: {host}\n")
        (cfg / "repos.yaml").write_text(
            "repos:\n"
            "  - {alias: sandbox, git_host: local, path: sireum-sandbox, default_branch: main, enabled: true}\n"
            "  - {alias: off, git_host: local, path: other, default_branch: main, enabled: false}\n")
        cls.cfg, cls.state = cfg, cls.tmp / "state"
        cls.port = free_port()
        cls.url = f"http://127.0.0.1:{cls.port}"
        cls.start_bridge()

    @classmethod
    def start_bridge(cls):
        env = {**os.environ, "WORKER_TOKEN": WORKER, "OPERATOR_TOKEN": OPERATOR,
               "BRIDGE_PUBLIC_URL": cls.url, "LEASE_TTL_SEC": "6", "PYTHONPATH": str(ROOT / "bridge")}
        cls.bridge_log = open(cls.tmp / "bridge.log", "a")
        cls.bridge = subprocess.Popen(
            [sys.executable, "-m", "sireum_bridge", "--config", str(cls.cfg), "--state", str(cls.state),
             "--listen", f"127.0.0.1:{cls.port}"], env=env, stdout=cls.bridge_log, stderr=subprocess.STDOUT)
        for _ in range(50):
            try:
                urllib.request.urlopen(f"{cls.url}/healthz", timeout=1).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("bridge did not start: " + (cls.tmp / "bridge.log").read_text())

    @classmethod
    def stop_bridge(cls):
        cls.bridge.terminate()
        cls.bridge.wait(5)
        cls.bridge_log.close()

    @classmethod
    def tearDownClass(cls):
        cls.stop_bridge()
        if os.environ.get("KEEP_TMP"):
            print("kept", cls.tmp)
        else:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers --
    def call(self, method, path, body=None, token=OPERATOR, headers=None):
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def dispatch(self, job_id, task="Add a note", **kw):
        body = {"repo": "sandbox", "task": task, "acceptance": "file exists", "executor": "claude", **kw}
        code, resp = self.call("POST", "/v1/dispatch", body, headers={"Idempotency-Key": job_id})
        self.assertEqual(code, 202, resp)
        return resp

    def run_runner(self, job_id, cache=None, timeout=60, **env):
        cache = cache or (self.tmp / "cache")
        full = {"PATH": os.environ["PATH"], "HOME": str(self.tmp), "JOB_ID": job_id, "BRIDGE_URL": self.url,
                "WORKER_TOKEN": WORKER, "CACHE_DIR": str(cache),
                "SIREUM_ALLOW_FAKE_EXECUTOR": "1", "SIREUM_EXECUTOR_OVERRIDE": "fake", **env}
        return subprocess.run([sys.executable, str(RUNNER)], env=full, capture_output=True, text=True,
                              timeout=timeout)

    def run_view(self, job_id):
        code, view = self.call("GET", f"/v1/runs/{job_id}")
        self.assertEqual(code, 200, view)
        return view

    def host_branch(self, job_id):
        p = subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/agent/{job_id}"],
                           cwd=self.host_repo, capture_output=True, text=True)
        return p.stdout.strip() or None

    # -- tests --
    def test_01_success_pushes_branch(self):
        self.dispatch("t-ok", test_target="fake")
        p = self.run_runner("t-ok")
        self.assertEqual(p.returncode, 0, p.stderr)
        res = self.run_view("t-ok")["result"]
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(res["test_result"]["passed"])
        self.assertTrue(res["pushed"])
        self.assertEqual(self.host_branch("t-ok"), res["commit"])
        main = sh("git", "rev-parse", "main", cwd=self.host_repo)
        self.assertEqual(sh("git", "merge-base", main, res["commit"], cwd=self.host_repo), main)

    def test_02_rerun_after_finish_is_noop(self):
        self.dispatch("t-again")
        self.run_runner("t-again")
        p = self.run_runner("t-again")
        self.assertIn("already leased or finished", p.stderr)

    def test_03_failing_target_needs_review(self):
        self.dispatch("t-fails", test_target="fails")
        self.run_runner("t-fails")
        res = self.run_view("t-fails")["result"]
        self.assertEqual(res["status"], "needs_review")
        self.assertFalse(res["test_result"]["passed"])
        self.assertIsNotNone(self.host_branch("t-fails"))

    def test_04_tests_modified_needs_review(self):
        self.dispatch("t-tests", task="touch-tests please", test_target="fake")
        self.run_runner("t-tests")
        res = self.run_view("t-tests")["result"]
        self.assertTrue(res["tests_modified"])
        self.assertEqual(res["status"], "needs_review")

    def test_05_empty_diff_success(self):
        self.dispatch("t-noop", task="noop")
        self.run_runner("t-noop")
        res = self.run_view("t-noop")["result"]
        self.assertEqual(res["status"], "success")
        self.assertIsNone(res["commit"])
        self.assertIsNone(self.host_branch("t-noop"))

    def test_06_blocked(self):
        self.dispatch("t-blocked", task="blocked")
        self.run_runner("t-blocked")
        self.assertEqual(self.run_view("t-blocked")["result"]["status"], "blocked")

    def test_07_timeout(self):
        self.dispatch("t-timeout", task="sleep", timeout_sec=3)
        t0 = time.time()
        self.run_runner("t-timeout")
        self.assertLess(time.time() - t0, 30)
        res = self.run_view("t-timeout")["result"]
        self.assertEqual((res["status"], res["error"]["code"]), ("failed", "timeout"))
        self.assertIsNone(self.host_branch("t-timeout"))

    def test_08_usage_limit_pauses_executor_and_auto_fails_over(self):
        self.dispatch("t-limit", task="usage-limit")
        self.run_runner("t-limit")
        res = self.run_view("t-limit")["result"]
        self.assertEqual(res["error"]["code"], "usage_limit")
        self.assertIsNotNone(res["error"]["reset_at"])
        _, st = self.call("GET", "/status")
        self.assertIsNotNone(st["executors"]["claude"]["paused_until"])
        code, _ = self.call("POST", "/v1/dispatch", {"repo": "sandbox", "task": "x", "acceptance": "y",
                                                     "executor": "claude"})
        self.assertEqual(code, 503)
        resp = self.dispatch("t-auto", executor="auto")
        self.assertEqual(resp["executor"], "opencode")
        self.call("POST", "/v1/executors/claude/resume", {})
        _, st = self.call("GET", "/status")
        self.assertIsNone(st["executors"]["claude"]["paused_until"])

    def test_09_second_lease_conflicts_and_stale_bundle_rejected(self):
        self.dispatch("t-fence")
        code, l1 = self.call("GET", "/v1/jobs/t-fence", token=WORKER)
        self.assertEqual(code, 200)
        code, _ = self.call("GET", "/v1/jobs/t-fence", token=WORKER)
        self.assertEqual(code, 409)
        time.sleep(7)  # lease TTL 6s, no heartbeat -> reschedule gets a new lease
        code, l2 = self.call("GET", "/v1/jobs/t-fence", token=WORKER)
        self.assertEqual((code, l2["attempt"]), (200, 2))
        code, _ = self.call("PUT", "/v1/jobs/t-fence/bundle", b"junk", token=WORKER,
                            headers={"X-Sireum-Lease": l1["lease_id"]})
        self.assertEqual(code, 409)
        code, _ = self.call("POST", "/v1/results", {"job_id": "t-fence", "lease_id": l1["lease_id"],
                                                    "status": "success"}, token=WORKER)
        self.assertEqual(code, 409)

    def test_10_bundle_rules(self):
        self.dispatch("t-rules")
        _, lease = self.call("GET", "/v1/jobs/t-rules", token=WORKER)
        work = self.tmp / "rules"
        sh("git", "clone", "-q", str(self.host_repo), str(work))
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x",
           cwd=work)
        # Wrong ref name.
        sh("git", "branch", "agent/other", cwd=work)
        sh("git", "bundle", "create", "-q", str(self.tmp / "b1"), "agent/other", "^origin/main", cwd=work)
        code, resp = self.call("PUT", "/v1/jobs/t-rules/bundle", (self.tmp / "b1").read_bytes(), token=WORKER,
                               headers={"X-Sireum-Lease": lease["lease_id"]})
        self.assertEqual(code, 422, resp)
        # Not descending from base_commit (orphan history).
        sh("git", "checkout", "-q", "--orphan", "agent/t-rules", cwd=work)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "orphan", cwd=work)
        sh("git", "bundle", "create", "-q", str(self.tmp / "b2"), "agent/t-rules", cwd=work)
        code, resp = self.call("PUT", "/v1/jobs/t-rules/bundle", (self.tmp / "b2").read_bytes(), token=WORKER,
                               headers={"X-Sireum-Lease": lease["lease_id"]})
        self.assertEqual(code, 422, resp)
        self.assertIn("descend", resp["error"])

    def test_11_dispatch_validation(self):
        base = {"repo": "sandbox", "task": "x", "acceptance": "y"}
        self.assertEqual(self.call("POST", "/v1/dispatch", {**base, "test_target": "rm -rf /"})[0], 400)
        self.assertEqual(self.call("POST", "/v1/dispatch", {**base, "base_ref": "--upload-pack=x"})[0], 400)
        self.assertEqual(self.call("POST", "/v1/dispatch", {**base, "base_ref": "a/../b"})[0], 400)
        self.assertEqual(self.call("POST", "/v1/dispatch", {**base, "repo": "off"})[0], 404)
        self.assertEqual(self.call("POST", "/v1/dispatch", {**base, "repo": "nope"})[0], 404)
        self.assertEqual(self.call("POST", "/v1/dispatch", base, token="bad")[0], 401)
        self.assertEqual(self.call("GET", "/v1/jobs/x", token=OPERATOR)[0], 401)
        self.dispatch("t-dup")
        self.assertEqual(self.call("POST", "/v1/dispatch", base, headers={"Idempotency-Key": "t-dup"})[0], 409)

    def test_12_git_endpoint_is_read_only_and_lease_scoped(self):
        self.dispatch("t-git")
        _, lease = self.call("GET", "/v1/jobs/t-git", token=WORKER)
        clone = lease["clone_url"]
        code, _ = self.call("GET", clone.replace(self.url, "") + "/info/refs?service=git-receive-pack",
                            token=lease["clone_token"])
        self.assertEqual(code, 403)
        p = subprocess.run(["git", "ls-remote", clone], capture_output=True, text=True,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        self.assertNotEqual(p.returncode, 0)  # no token
        p = subprocess.run(["git", "-c", f"http.extraHeader=Authorization: Bearer {lease['clone_token']}",
                            "ls-remote", clone], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("refs/heads/main", p.stdout)
        self.assertNotIn("refs/sireum", p.stdout)

    def test_13_spool_when_bridge_down(self):
        self.dispatch("t-spool", test_target="fake")
        _, lease = self.call("GET", "/v1/jobs/t-spool", token=WORKER)
        # Simulate: the runner finished while the hub was down and spooled its result.
        cache = self.tmp / "cache-spool"
        (cache / "results").mkdir(parents=True)
        spooled = {"job_id": "t-spool", "lease_id": lease["lease_id"], "attempt": 1, "status": "success",
                   "bundle_uploaded": False, "commit": None}
        (cache / "results" / "t-spool.json").write_text(json.dumps(spooled))
        self.stop_bridge()
        p = self.run_runner("t-other-missing", cache=cache)
        self.assertTrue((cache / "results" / "t-spool.json").exists(), p.stderr)
        self.start_bridge()
        self.dispatch("t-after")
        self.run_runner("t-after", cache=cache)
        self.assertFalse((cache / "results" / "t-spool.json").exists())
        self.assertEqual(self.run_view("t-spool")["result"]["status"], "success")

    def test_14_no_secrets_in_result_or_env_passthrough(self):
        self.dispatch("t-secret")
        self.run_runner("t-secret", CLAUDE_CODE_OAUTH_TOKEN="sk-seat-SECRET")
        view = json.dumps(self.run_view("t-secret"))
        self.assertNotIn("SECRET", view)
        self.assertNotIn(WORKER, view)


if __name__ == "__main__":
    unittest.main()

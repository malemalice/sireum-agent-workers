"""Phase 6: access requests, per-phase secrets, package relay, egress list, signing, doctor."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import yaml

import test_e2e
from test_e2e import OPERATOR, ROOT, WORKER, sh

sys.path.insert(0, str(ROOT / "bridge"))
from sireum_bridge.nodes import decode_join  # noqa: E402

UP_TOKEN = "upstream-secret-token-123"


def load_runner():
    spec = importlib.util.spec_from_file_location("sireum_runner", ROOT / "runner" / "entrypoint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Upstream(BaseHTTPRequestHandler):
    """A private registry: npm packument + tarball, PyPI simple page + file, Go module info."""
    base = ""

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Authorization") != f"Bearer {UP_TOKEN}":
            return self._send(401, b'{"error":"auth"}')
        p = unquote(urlparse(self.path).path)
        b = Upstream.base
        if p == "/npm/@company/pkg":
            doc = {"name": "@company/pkg", "versions": {"1.0.0": {
                "dist": {"tarball": f"{b}/npm/@company/pkg/-/pkg-1.0.0.tgz"}}}}
            return self._send(200, json.dumps(doc).encode())
        if p == "/npm/@company/pkg/-/pkg-1.0.0.tgz":
            return self._send(200, b"TARBALL-BYTES", "application/octet-stream")
        if p == "/pypi/simple/acme-lib/":
            html = f'<a href="{b}/files/acme_lib-1.0.tar.gz#sha256=ab">acme_lib-1.0.tar.gz</a>'
            return self._send(200, html.encode(), "text/html")
        if p == "/files/acme_lib-1.0.tar.gz":
            return self._send(200, b"SDIST-BYTES", "application/octet-stream")
        if p == "/go/example.com/acme/mod/@v/list":
            return self._send(200, b"v1.0.0\n", "text/plain")
        self._send(404, b'{"error":"nf"}')


class Phase6(test_e2e.E2E):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        Upstream.base = f"http://127.0.0.1:{cls.up.server_port}"
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        os.environ["SIREUM_TEST_UP_TOKEN"] = UP_TOKEN
        super().setUpClass()
        (cls.cfg / "registries.yaml").write_text(yaml.safe_dump({"registries": [
            {"name": "npm-co", "ecosystem": "npm", "url": f"{Upstream.base}/npm", "scopes": ["@company"],
             "mode": "relay", "api_key_env": "SIREUM_TEST_UP_TOKEN"},
            {"name": "pypi-co", "ecosystem": "pypi", "url": f"{Upstream.base}/pypi/simple", "scopes": ["acme-"],
             "mode": "relay", "api_key_env": "SIREUM_TEST_UP_TOKEN"},
            {"name": "go-co", "ecosystem": "go", "url": f"{Upstream.base}/go", "scopes": ["example.com/acme"],
             "mode": "relay", "api_key_env": "SIREUM_TEST_UP_TOKEN"},
            {"name": "inj", "ecosystem": "npm", "url": "https://npm.inject.example", "scopes": ["@inj"],
             "mode": "inject", "api_key_env": "SIREUM_TEST_UP_TOKEN"},
        ]}))

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.up.shutdown()

    # -- helpers --
    def branch(self, name, manifest: dict):
        work = self.tmp / f"w-{name}"
        sh("git", "clone", "-q", str(self.host_repo), str(work))
        sh("git", "checkout", "-q", "-b", name, cwd=work)
        (work / ".agent" / "tools.yaml").write_text(yaml.safe_dump(manifest))
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", name, cwd=work)
        sh("git", "push", "-q", "origin", name, cwd=work)

    def set_approvals(self, **fields):
        p = self.cfg / "repos.yaml"
        doc = yaml.safe_load(p.read_text())
        for r in doc["repos"]:
            if r["alias"] == "sandbox":
                for k, v in fields.items():
                    r[k] = v
        p.write_text(yaml.safe_dump(doc))
        time.sleep(0.02)

    def lease(self, job_id):
        code, body = self.call("GET", f"/v1/jobs/{job_id}", token=WORKER)
        self.assertEqual(code, 200, body)
        return body

    # -- tests --
    def test_01_preflight_wait_grant_run_with_test_exposure(self):
        self.branch("p6-wait", {"version": 1, "secrets": ["APP_TEST"],
                                "test_targets": {"t": 'test "$APP_TEST" = s3cret-test-value'}})
        code, r = self.call("POST", "/v1/dispatch", {"repo": "sandbox", "task": "echo-secrets", "acceptance": "a",
                                                     "base_ref": "p6-wait", "test_target": "t"},
                            headers={"Idempotency-Key": "t6-wait"})
        self.assertEqual((code, r["state"]), (202, "waiting_access"), r)
        rid = r["access_requests"][0]["id"]
        self.assertEqual(self.call("GET", "/v1/jobs/t6-wait", token=WORKER)[0], 409)  # not leasable yet
        pend = self.call("GET", "/v1/access")[1]["pending"]
        self.assertTrue(any(p["id"] == rid and p["name"] == "APP_TEST" for p in pend))
        code, g = self.call("POST", f"/v1/access/{rid}/grant", {"value": "s3cret-test-value", "expose": "test"})
        self.assertEqual((code, g["resumed"]), (200, ["t6-wait"]), g)
        repos_text = (self.cfg / "repos.yaml").read_text()
        self.assertNotIn("s3cret-test-value", repos_text)
        secret_file = Path(yaml.safe_load(repos_text)["repos"][0]["secrets"]["APP_TEST"]["api_key_file"])
        self.assertEqual(stat.S_IMODE(secret_file.stat().st_mode), 0o600)
        p = self.run_runner("t6-wait")
        res = self.run_view("t6-wait")["result"]
        self.assertEqual(res["status"], "success", (res, p.stderr))
        self.assertIn("APP_TEST=none", res["summary"])              # not exposed to the agent
        self.assertNotIn("s3cret-test-value", json.dumps(self.run_view("t6-wait")))

    def test_02_exposure_levels_and_redaction(self):
        sec = self.tmp / "secrets-src"
        sec.mkdir(exist_ok=True)
        for n, v in (("prep", "value-prep-1"), ("test", "value-test-2"), ("agent", "value-agent-3")):
            (sec / n).write_text(v)
        self.set_approvals(secrets={
            "APP_PREP": {"api_key_file": str(sec / "prep"), "expose": "prepare"},
            "APP_TEST": {"api_key_file": str(sec / "test"), "expose": "test"},
            "APP_AGENT": {"api_key_file": str(sec / "agent"), "expose": "agent"}})
        self.branch("p6-exp", {"version": 1, "secrets": ["APP_PREP", "APP_TEST", "APP_AGENT"],
                               "npm": {"install": 'test "$APP_PREP" = value-prep-1'},
                               "test_targets": {"t": 'test -z "$APP_PREP" && test "$APP_TEST" = value-test-2 '
                                                     '&& test "$APP_AGENT" = value-agent-3'}})
        self.dispatch("t6-exp", task="echo-secrets", base_ref="p6-exp", test_target="t")
        p = self.run_runner("t6-exp")
        view = self.run_view("t6-exp")
        res = view["result"]
        self.assertEqual(res["status"], "success", (res, p.stderr))
        self.assertIn("APP_PREP=none APP_TEST=none APP_AGENT=[redacted]", res["summary"])
        self.assertNotIn("value-agent-3", json.dumps(view))
        self.assertEqual(sorted(view["granted"]["secrets"]), ["APP_AGENT", "APP_PREP", "APP_TEST"])
        log = (self.tmp / "cache" / "logs" / "t6-exp.log").read_text()
        self.assertNotIn("value-agent-3", log)

    def test_03_agent_need_then_grant_reruns(self):
        self.dispatch("t6-need", task="need-secret")
        self.run_runner("t6-need")
        res = self.run_view("t6-need")["result"]
        self.assertEqual((res["status"], res["error"]["code"]), ("blocked", "needs_access"), res)
        req = res["access_requests"][0]
        self.assertEqual((req["kind"], req["name"]), ("secret", "STRIPE_TEST_KEY"))
        code, g = self.call("POST", f"/v1/access/{req['id']}/grant", {"api_key_env": "SIREUM_TEST_UP_TOKEN"})
        self.assertEqual(code, 200, g)
        self.assertEqual(self.run_view("t6-need")["result"]["rerun_as"], "t6-need-r2")
        self.assertEqual(self.run_view("t6-need-r2")["state"], "dispatched")
        self.lease("t6-need-r2")

    def test_04_deny_cancels_waiting_job(self):
        self.branch("p6-deny", {"version": 1, "egress": ["api.denied.example"]})
        code, r = self.call("POST", "/v1/dispatch", {"repo": "sandbox", "task": "x", "acceptance": "y",
                                                     "base_ref": "p6-deny"}, headers={"Idempotency-Key": "t6-deny"})
        rid = r["access_requests"][0]["id"]
        self.assertEqual(self.call("POST", f"/v1/access/{rid}/deny", {"note": "use mocks"})[0], 200)
        res = self.run_view("t6-deny")["result"]
        self.assertEqual((res["status"], res["error"]["code"]), ("cancelled", "access_denied"))
        self.assertIn("use mocks", res["error"]["message"])

    def test_05_package_relay(self):
        self.set_approvals(registries=["npm-co", "pypi-co", "go-co", "inj"])
        self.branch("p6-pkg", {"version": 1, "registries": ["npm-co", "pypi-co", "go-co", "inj"]})
        self.dispatch("t6-pkg", base_ref="p6-pkg")
        lease = self.lease("t6-pkg")
        regs = {r["name"]: r for r in lease["grants"]["registries"]}
        npm = regs["npm-co"]
        self.assertEqual(npm["token"], lease["clone_token"])              # lease token, not upstream
        self.assertNotIn(UP_TOKEN, json.dumps({k: v for k, v in regs.items() if k != "inj"}))
        self.assertEqual(regs["inj"]["token"], UP_TOKEN)                   # inject mode: real token
        tok = lease["clone_token"]

        def get(url, token=tok, follow=True):
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect())
            try:
                with opener.open(req, timeout=10) as r:
                    return r.status, r.read(), dict(r.headers)
            except urllib.error.HTTPError as e:
                return e.code, e.read(), dict(e.headers)

        code, body, _ = get(f"{npm['url']}/@company%2fpkg")
        self.assertEqual(code, 200, body)
        tarball = json.loads(body)["versions"]["1.0.0"]["dist"]["tarball"]
        self.assertTrue(tarball.startswith(npm["url"] + "/"), tarball)    # rewritten to the hub
        self.assertEqual(get(tarball)[:2], (200, b"TARBALL-BYTES"))
        self.assertEqual(get(f"{npm['url']}/left-pad")[0], 404)           # outside the scope
        self.assertEqual(get(f"{npm['url']}/@company%2fpkg", token="wrong")[0], 401)
        pypi = regs["pypi-co"]["url"]
        code, body, _ = get(f"{pypi}/simple/acme-lib/")
        self.assertEqual(code, 200)
        link = body.decode().split('href="')[1].split("#")[0]
        self.assertTrue(link.startswith(pypi + "/_o/files/"), link)
        self.assertEqual(get(link)[:2], (200, b"SDIST-BYTES"))
        code, _, hdr = get(f"{pypi}/simple/requests/", follow=False)
        self.assertEqual((code, hdr.get("Location")), (302, "https://pypi.org/simple/requests/"))
        go = regs["go-co"]["url"]
        self.assertEqual(get(f"{go}/example.com/acme/mod/@v/list")[:2], (200, b"v1.0.0\n"))
        self.assertEqual(get(f"{go}/example.com/acme/missing/@v/list")[0], 403)   # never fall back
        self.assertEqual(get(f"{go}/github.com/other/mod/@v/list")[0], 404)        # public: fall back
        # Inject registry can't be used through the relay.
        self.assertEqual(get(f"{self.url}/pkg/inj/@inj%2fx")[0], 401)
        self.set_approvals(registries=[])

    def test_06_egress_union_and_runner_registry_files(self):
        self.set_approvals(egress=["api.stripe.com", "*.sandbox.example"], registries=["inj"])
        env = {**os.environ, "PYTHONPATH": str(ROOT / "bridge"), "BRIDGE_PUBLIC_URL": self.url}
        code = subprocess.run([sys.executable, "-m", "sireum_bridge", "--state", str(self.state), "invite",
                               "--ts-auth-key", "k"], env=env, capture_output=True, text=True).stdout.strip()
        c, r = self.call("POST", "/v1/nodes/enroll", {"hostname": "e"}, token=decode_join(code)["enroll_token"])
        c, eg = self.call("GET", f"/v1/nodes/{r['node_name']}/egress", token=r["node_secret"])
        self.assertEqual(eg["hosts"], ["*.sandbox.example", "api.stripe.com", "npm.inject.example"])
        self.set_approvals(egress=[], registries=[])
        runner = load_runner()
        home = Path(tempfile.mkdtemp())
        g = runner.Grants({"registries": [
            {"name": "npm-co", "ecosystem": "npm", "mode": "relay", "url": "https://hub/pkg/npm-co",
             "token": "LEASE", "scopes": ["@company"]},
            {"name": "inj", "ecosystem": "npm", "mode": "inject", "url": "https://npm.inject.example",
             "token": "REAL", "scopes": ["@inj"]},
            {"name": "py", "ecosystem": "pypi", "mode": "relay", "url": "https://hub/pkg/py", "token": "LEASE",
             "username": "sireum"}]}, home)
        env1 = g.registry_env(include_inject=True)
        npmrc = (home / ".npmrc").read_text()
        self.assertIn("@company:registry=https://hub/pkg/npm-co/", npmrc)
        self.assertIn("//npm.inject.example/:_authToken=REAL", npmrc)
        self.assertEqual(env1["PIP_INDEX_URL"], "https://sireum:LEASE@hub/pkg/py/simple/")
        g.registry_env(include_inject=False)
        self.assertNotIn("REAL", (home / ".npmrc").read_text())             # gone before the agent runs

    def test_07_needs_detection(self):
        runner = load_runner()
        g = runner.Grants({"secrets": [{"name": "HAVE", "value": "x", "expose": "test"}]}, Path(tempfile.mkdtemp()))
        text = "\n".join([
            'SIREUM-NEED: registry npm-company "@company packages"',
            "KeyError: 'STRIPE_KEY'",
            "KeyError: 'HAVE'",
            "Error: environment variable SENTRY_DSN is not set",
            "npm ERR! code E401 ... GET https://npm.pkg.github.com/@acme%2fui",
            "curl: (56) CONNECT tunnel failed, response 403 for https://api.blocked.example/v1",
        ])
        got = {(n["kind"], n["name"]) for n in runner.detect_needs(text, g, "hub.ts.net", [])}
        self.assertEqual(got, {("registry", "npm-company"), ("secret", "STRIPE_KEY"), ("secret", "SENTRY_DSN"),
                               ("registry", "acme"), ("egress", "api.blocked.example")})

    def test_08_hub_ssh_signing(self):
        sys.path.insert(0, str(ROOT / "bridge"))
        from sireum_bridge.relay import GitHost, Relay, Repo
        root = Path(tempfile.mkdtemp())
        key = root / "sign_ed25519"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        relay = Relay(root / "hub", {"local": GitHost("local", "local", str(self.tmp / "host"))})
        repo = Repo("sandbox", "local", "sireum-sandbox", signing="hub-ssh", signing_key=str(key))
        relay.fetch(repo)
        mirror = relay.mirror_path(repo)
        base = sh("git", "rev-parse", "main", cwd=mirror)
        work = root / "w"
        sh("git", "clone", "-q", str(mirror), str(work))
        sh("git", "checkout", "-q", "-b", "agent/sig1", cwd=work)
        (work / "f.txt").write_text("x\n")
        sh("git", "add", "f.txt", cwd=work)
        sh("git", "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-qm", "c1", cwd=work)
        bundle = root / "b"
        sh("git", "bundle", "create", "-q", str(bundle), "agent/sig1", f"^{base}", cwd=work)
        relay.intake(repo, "sig1", base, bundle.read_bytes())
        tip = relay.sign(repo, "sig1", base)
        signers = root / "allowed"
        signers.write_text("sireum@localhost " + (key.with_suffix(".pub")).read_text())
        v = subprocess.run(["git", "-c", f"gpg.ssh.allowedSignersFile={signers}", "verify-commit", tip], cwd=mirror,
                           capture_output=True, text=True)
        self.assertEqual(v.returncode, 0, v.stderr)
        self.assertEqual(sh("git", "log", "-1", "--format=%an %s", tip, cwd=mirror), "a c1")

    def test_09_doctor_and_inventory(self):
        sec = self.tmp / "doc-secret"
        sec.write_text("v")
        self.set_approvals(secrets={"OK_ONE": {"api_key_file": str(sec), "expose": "test"},
                                    "BROKEN": {"api_key_file": str(self.tmp / "missing"), "expose": "test"}})
        env = {**os.environ, "PYTHONPATH": str(ROOT / "bridge"), "SIREUM_CONFIG": str(self.cfg),
               "SIREUM_ENV_FILE": str(self.tmp / "secrets" / "bridge.env")}
        inv = subprocess.run([sys.executable, "-m", "sireum_bridge", "--state", str(self.state), "secrets"],
                             env=env, capture_output=True, text=True)
        self.assertIn("OK_ONE", inv.stdout)
        self.assertNotIn("\nv\n", inv.stdout)
        doc = subprocess.run([sys.executable, "-m", "sireum_bridge", "--state", str(self.state), "doctor"],
                             env=env, capture_output=True, text=True)
        self.assertNotEqual(doc.returncode, 0)
        self.assertIn("✗ secret BROKEN", doc.stdout)
        self.assertIn("✓ secret OK_ONE", doc.stdout)
        self.assertIn("✓ registry npm-co", doc.stdout)
        self.set_approvals(secrets={})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


for _name in [n for n in dir(test_e2e.E2E) if n.startswith("test_")]:
    setattr(Phase6, _name, None)

if __name__ == "__main__":
    unittest.main()

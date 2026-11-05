"""Phase 1.5: credential sources, add-repo, hot-reload, connect-hermes (dry run), runner source, invites."""
from __future__ import annotations

import base64
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import test_e2e
from test_e2e import OPERATOR, ROOT, sh

sys.path.insert(0, str(ROOT / "bridge"))
from sireum_bridge import creds  # noqa: E402
from sireum_bridge.nodes import decode_join  # noqa: E402


class FakeGitHub(BaseHTTPRequestHandler):
    """Just enough of the GitHub API for add-repo and GitHub App tokens."""
    state: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body=None):
        data = json.dumps(body if body is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self):
        st = FakeGitHub.state
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        path = urlparse(self.path).path
        auth = self.headers.get("Authorization", "")
        st.setdefault("calls", []).append((self.command, path, auth, body))
        if path.startswith("/app/") or path.endswith("/installation"):
            if auth.count(".") != 2:                       # must be a JWT
                return self._send(401, {"message": "need JWT"})
            if path.endswith("/installation"):
                return self._send(200, {"id": 4242})
            if path == "/app/installations/4242/access_tokens":
                return self._send(201, {"token": "ghs_installation_token"})
            return self._send(404)
        if auth != f"Bearer {st['token']}":
            return self._send(401, {"message": "Bad credentials"})
        if path == "/repos/acme/shop":
            return self._send(200, {"default_branch": "trunk", "permissions": {"push": st.get("push", True)}})
        if path == "/repos/acme/shop/branches/trunk/protection":
            if self.command == "PUT":
                st["protected"] = True
                return self._send(200, {})
            return self._send(200 if st.get("protected") else 404, {})
        self._send(404, {"message": path})

    do_GET = do_POST = do_PUT = handle_any


def fake_github():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


class Credentials(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_file_env_cmd_and_cache(self):
        r = creds.Resolver()
        f = self.tmp / "k"
        f.write_text("tok-file\n")
        self.assertEqual(r.token({"api_key_file": str(f)}), "tok-file")
        os.environ["SIREUM_TEST_TOK"] = "tok-env"
        try:
            self.assertEqual(r.token({"api_key_env": "SIREUM_TEST_TOK"}), "tok-env")
        finally:
            del os.environ["SIREUM_TEST_TOK"]
        counter = self.tmp / "n"
        cmd = ["sh", "-c", f'echo x >> {counter}; echo tok-cmd']
        self.assertEqual(r.token({"api_key_cmd": cmd}), "tok-cmd")
        self.assertEqual(r.token({"api_key_cmd": cmd}), "tok-cmd")
        self.assertEqual(len(counter.read_text().split()), 1, "command result should be cached")
        r.invalidate()
        r.token({"api_key_cmd": cmd})
        self.assertEqual(len(counter.read_text().split()), 2)
        with self.assertRaises(creds.CredentialError):
            r.token({"api_key_cmd": ["false"]})
        with self.assertRaises(creds.CredentialError):
            r.token({"api_key_file": str(self.tmp / "missing")})
        self.assertIsNone(r.token({}))

    def test_github_app_token(self):
        key = self.tmp / "app.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
        jwt = creds.app_jwt(99, str(key))
        header, payload, sig = jwt.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        self.assertEqual(claims["iss"], "99")
        # Signature verifies with the public key.
        pub = self.tmp / "pub.pem"
        subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(pub)], check=True,
                       capture_output=True)
        (self.tmp / "sig").write_bytes(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)))
        (self.tmp / "msg").write_bytes(f"{header}.{payload}".encode())
        v = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(self.tmp / "sig"),
                            str(self.tmp / "msg")], capture_output=True, text=True)
        self.assertIn("Verified OK", v.stdout)
        srv, api = fake_github()
        try:
            FakeGitHub.state = {"token": "unused"}
            tok = creds.Resolver().token({"github_app": {"app_id": 99, "private_key_file": str(key)}}, api,
                                         "acme/shop")
            self.assertEqual(tok, "ghs_installation_token")
            post = [c for c in FakeGitHub.state["calls"] if c[0] == "POST"][0]
            self.assertEqual(post[3], {"repositories": ["shop"]})   # scoped to the one repo
        finally:
            srv.shutdown()

    def test_git_auth_env_never_in_argv(self):
        env = creds.git_auth_env("github", "secret-tok")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraHeader")
        self.assertNotIn("secret-tok", env["GIT_CONFIG_VALUE_0"])  # base64 in a Basic header
        self.assertEqual(creds.git_auth_env("local", "x"), {})


class AddRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config").mkdir()
        (self.tmp / "secrets").mkdir()
        (self.tmp / "secrets" / "bridge.env").write_text("WORKER_TOKEN=w\n")
        self.srv, self.api = fake_github()
        FakeGitHub.state = {"token": "ghp_test", "calls": []}

    def tearDown(self):
        self.srv.shutdown()

    def hub(self, *args, check=True):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "bridge"), "SIREUM_CONFIG": str(self.tmp / "config"),
               "SIREUM_ENV_FILE": str(self.tmp / "secrets" / "bridge.env")}
        p = subprocess.run([sys.executable, "-m", "sireum_bridge", "--state", str(self.tmp / "state"), *args],
                           env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if check and p.returncode != 0:
            raise AssertionError(p.stdout + p.stderr)
        return p

    def yaml(self, name):
        import yaml
        return yaml.safe_load((self.tmp / "config" / name).read_text())

    def test_add_repo_protects_and_enables(self):
        p = self.hub("add-repo", "https://github.com/acme/shop.git", "--auth", "token:ghp_test",
                     "--api-url", self.api, "--protect")
        self.assertIn("enabled: repo:shop", p.stdout)
        self.assertTrue(FakeGitHub.state.get("protected"))
        repos, hosts = self.yaml("repos.yaml")["repos"], self.yaml("git_hosts.yaml")["hosts"]
        self.assertEqual(repos[0], {"alias": "shop", "git_host": "github", "path": "acme/shop",
                                    "default_branch": "trunk", "enabled": True})
        key = Path(hosts[0]["api_key_file"])
        self.assertEqual(key.read_text().strip(), "ghp_test")
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertNotIn("ghp_test", (self.tmp / "config" / "git_hosts.yaml").read_text())
        self.assertIn("shop", self.hub("repos").stdout)

    def test_unprotected_is_disabled_without_tty(self):
        p = self.hub("add-repo", "acme/shop", "--alias", "s2", "--auth", "token:ghp_test", "--api-url", self.api)
        self.assertIn("DISABLED", p.stdout)
        self.assertFalse(self.yaml("repos.yaml")["repos"][0]["enabled"])
        self.assertFalse(any(c[0] == "PUT" for c in FakeGitHub.state["calls"]))

    def test_bad_credential_and_no_push(self):
        p = self.hub("add-repo", "acme/shop", "--auth", "token:wrong", "--api-url", self.api, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("HTTP 401", p.stderr)
        FakeGitHub.state["push"] = False
        p = self.hub("add-repo", "acme/shop", "--auth", "token:ghp_test", "--api-url", self.api, check=False)
        self.assertIn("can't push", p.stderr)

    def test_per_repo_credential_and_env_source(self):
        os.environ["SIREUM_GH_TEST"] = "ghp_test"
        try:
            self.hub("add-repo", "acme/shop", "--auth", "env:SIREUM_GH_TEST", "--per-repo", "--api-url", self.api,
                     "--allow-unprotected")
        finally:
            del os.environ["SIREUM_GH_TEST"]
        repo = self.yaml("repos.yaml")["repos"][0]
        self.assertEqual(repo["api_key_env"], "SIREUM_GH_TEST")
        self.assertTrue(repo["enabled"])

    def test_connect_hermes_dry_run_sets_source(self):
        (self.tmp / "secrets" / "kanban_ed25519.pub").write_text("ssh-ed25519 AAAA test\n")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "bridge"), "SIREUM_CONFIG": str(self.tmp / "config"),
               "SIREUM_ENV_FILE": str(self.tmp / "secrets" / "bridge.env"), "SIREUM_DRY_RUN": "1",
               "SIREUM_SRC": str(ROOT)}
        p = subprocess.run([sys.executable, "-m", "sireum_bridge", "connect-hermes", "ops@hermes.example",
                            "--taggers", "alice, bob"], env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("install-on-hermes-host.sh", p.stdout)
        self.assertIn("sireum@hermes.example list-remote", p.stdout)
        envf = (self.tmp / "secrets" / "bridge.env").read_text()
        self.assertIn("WORKER_TOKEN=w", envf)
        self.assertIn(f"SOURCE=kanban-ssh:sireum@hermes.example#{self.tmp}/secrets/kanban_ed25519", envf)
        self.assertIn("REMOTE_TAGGERS=alice,bob", envf)
        self.assertEqual(stat.S_IMODE((self.tmp / "secrets" / "bridge.env").stat().st_mode), 0o600)


class HubRunning(test_e2e.E2E):
    def test_hot_reload_new_repo(self):
        base = {"task": "x", "acceptance": "y"}
        self.assertEqual(self.call("POST", "/v1/dispatch", {**base, "repo": "hot"})[0], 404)
        repos = self.cfg / "repos.yaml"
        repos.write_text(repos.read_text() +
                         "  - {alias: hot, git_host: local, path: sireum-sandbox, default_branch: main, enabled: true}\n")
        time.sleep(0.05)
        code, resp = self.call("POST", "/v1/dispatch", {**base, "repo": "hot"}, headers={"Idempotency-Key": "t-hot"})
        self.assertEqual(code, 202, resp)
        # A broken edit keeps the previous config.
        good = repos.read_text()
        repos.write_text("repos: [ {broken")
        time.sleep(0.05)
        self.assertEqual(self.call("POST", "/v1/dispatch", {**base, "repo": "hot"})[0], 202)
        repos.write_text(good)

    def test_runner_source_and_invite_without_tailscale(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "bridge"), "BRIDGE_PUBLIC_URL": self.url}
        code = subprocess.run([sys.executable, "-m", "sireum_bridge", "--state", str(self.state), "invite"],
                              env=env, capture_output=True, text=True, check=True).stdout.strip()
        j = decode_join(code)
        self.assertEqual(j["ts_auth_key"], "")
        c, r = self.call("POST", "/v1/nodes/enroll", {"hostname": "box"}, token=j["enroll_token"])
        self.assertEqual(c, 200, r)
        self.assertTrue(r["runner_image"].startswith("sireum-runner:local-"))
        name, secret = r["node_name"], r["node_secret"]
        c, info = self.call("GET", f"/v1/nodes/{name}/runner", token=secret)
        self.assertEqual((c, info["image"], info["build_from_source"]), (200, r["runner_image"], True))
        self.assertEqual(self.call("GET", f"/v1/nodes/{name}/runner", token="nope")[0], 401)
        req = urllib.request.Request(f"{self.url}/v1/nodes/{name}/runner.tgz",
                                     headers={"Authorization": f"Bearer {secret}"})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.headers["X-Sireum-Runner-Image"], r["runner_image"])
            names = tarfile.open(fileobj=io.BytesIO(resp.read())).getnames()
        self.assertEqual(sorted(names), ["Dockerfile", "Dockerfile.browser", "entrypoint.py"])


for _name in [n for n in dir(test_e2e.E2E) if n.startswith("test_")]:
    setattr(HubRunning, _name, None)

if __name__ == "__main__":
    unittest.main()

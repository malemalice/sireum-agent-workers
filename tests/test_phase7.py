"""Phase 7: hub MCP server (separate /mcp-only listener, tiers, propose-then-confirm)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

import yaml

import test_e2e
from test_e2e import OPERATOR, ROOT, WORKER, free_port, sh

MCP_PORT = free_port()


class MCP(test_e2e.E2E):
    MCP_LISTEN = f"127.0.0.1:{MCP_PORT}"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mcp = f"http://127.0.0.1:{MCP_PORT}/mcp"
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.mcp, timeout=1)
            except urllib.error.HTTPError:
                break  # 405 = up
            except OSError:
                time.sleep(0.1)
        cls.op_token = cls.hub("mcp-client", "add", "hermes", "--tier", "operate").stdout.strip()
        cls.ro_token = cls.hub("mcp-client", "add", "viewer", "--tier", "read").stdout.strip()

    @classmethod
    def hub(cls, *args, input=None, check=True):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "bridge"), "SIREUM_CONFIG": str(cls.cfg),
               "SIREUM_ENV_FILE": str(cls.tmp / "secrets" / "bridge.env"), "OPERATOR_TOKEN": OPERATOR,
               "BRIDGE_LOCAL_URL": cls.url, "BRIDGE_PUBLIC_URL": cls.url}
        p = subprocess.run([sys.executable, "-m", "sireum_bridge", "--state", str(cls.state), *args], env=env,
                           capture_output=True, text=True, input=input, stdin=None if input else subprocess.DEVNULL)
        if check and p.returncode != 0:
            raise AssertionError(p.stdout + p.stderr)
        return p

    def rpc(self, method, params=None, token=None, mid=1, headers=None, raw=None, url=None):
        body = raw if raw is not None else json.dumps({"jsonrpc": "2.0", "id": mid, "method": method,
                                                       "params": params or {}}).encode()
        req = urllib.request.Request(url or self.mcp, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token or self.op_token}", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
                return r.status, (json.loads(data) if data else None)
        except urllib.error.HTTPError as e:
            data = e.read()
            return e.code, (json.loads(data) if data else None)

    def tool(self, name, args=None, token=None):
        code, r = self.rpc("tools/call", {"name": name, "arguments": args or {}}, token=token)
        self.assertEqual(code, 200, r)
        res = r["result"]
        return res["isError"], (res.get("structuredContent") or res["content"][0]["text"])

    # -- protocol & surface --
    def test_01_initialize_and_tools_by_tier(self):
        code, r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "hermes", "version": "x"}})
        self.assertEqual((code, r["result"]["protocolVersion"]), (200, "2025-06-18"))
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertEqual(self.rpc("notifications/initialized", raw=json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}).encode())[0], 202)
        names = {t["name"] for t in self.rpc("tools/list")[1]["result"]["tools"]}
        self.assertTrue({"status", "pause_executor", "propose_grant", "cancel_job"} <= names)
        ro = {t["name"] for t in self.rpc("tools/list", token=self.ro_token)[1]["result"]["tools"]}
        self.assertIn("status", ro)
        self.assertFalse(ro & {"pause_executor", "propose_grant", "cancel_job"})
        err, msg = self.tool("pause_executor", {"executor": "claude"}, token=self.ro_token)
        self.assertTrue(err)
        self.assertIn("needs tier operate", msg)
        self.assertEqual(self.rpc("no/such")[1]["error"]["code"], -32601)

    def test_02_only_mcp_is_served(self):
        base = f"http://127.0.0.1:{MCP_PORT}"
        for path in ("/v1/status", "/healthz", "/v1/jobs/x", "/git/x.git/info/refs", "/pkg/x/y", "/mcp/extra"):
            req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {OPERATOR}"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 404, path)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.mcp, timeout=5)
        self.assertEqual(cm.exception.code, 405)
        self.assertEqual(self.rpc("ping", headers={"Origin": "https://evil.example"})[0], 403)
        # The operator token is not an MCP token.
        self.assertEqual(self.rpc("ping", token=OPERATOR)[0], 401)

    def test_03_bad_tokens_lock_out_that_source_only(self):
        attacker = {"X-Forwarded-For": "203.0.113.9"}
        for _ in range(10):
            self.assertEqual(self.rpc("ping", token="smcp_wrong", headers=attacker)[0], 401)
        self.assertEqual(self.rpc("ping", token=self.op_token, headers=attacker)[0], 429)
        self.assertEqual(self.rpc("ping")[0], 200)   # other sources unaffected

    # -- read / operate --
    def test_04_read_and_operate(self):
        self.dispatch("t7-a")
        err, st = self.tool("status")
        self.assertFalse(err)
        self.assertIn("executors", st)
        err, runs = self.tool("runs", {"limit": 5})
        self.assertIn("t7-a", [r["job_id"] for r in runs["runs"]])
        err, d = self.tool("run_detail", {"job_id": "t7-a"})
        self.assertEqual(d["state"], "dispatched")
        err, ex = self.tool("pause_executor", {"executor": "opencode", "minutes": 5})
        self.assertIsNotNone(ex["paused_until"])
        err, ex = self.tool("resume_executor", {"executor": "opencode"})
        self.assertIsNone(ex["paused_until"])
        err, r = self.tool("cancel_job", {"job_id": "t7-a", "reason": "wrong repo"})
        self.assertFalse(err, r)
        res = self.run_view("t7-a")["result"]
        self.assertEqual((res["status"], res["error"]["code"]), ("cancelled", "operator"))
        err, r = self.tool("rerun_job", {"job_id": "t7-a"})
        self.assertEqual(r["job_id"], "t7-a-r2")
        err, msg = self.tool("drain_node", {"node": "x"})
        self.assertTrue(err)                                  # no Nomad in tests → clear error
        err, r = self.tool("refresh_mirror", {"repo": "sandbox"})
        self.assertTrue(r["refreshed"])

    # -- propose → confirm --
    def test_05_propose_grant_then_confirm_on_hub(self):
        work = self.tmp / "w7"
        sh("git", "clone", "-q", str(self.host_repo), str(work))
        sh("git", "checkout", "-q", "-b", "p7", cwd=work)
        (work / ".agent" / "tools.yaml").write_text(yaml.safe_dump({"version": 1, "secrets": ["MCP_SECRET"]}))
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "p7", cwd=work)
        sh("git", "push", "-q", "origin", "p7", cwd=work)
        code, r = self.call("POST", "/v1/dispatch", {"repo": "sandbox", "task": "x", "acceptance": "y",
                                                     "base_ref": "p7"}, headers={"Idempotency-Key": "t7-wait"})
        rid = r["access_requests"][0]["id"]
        before = (self.cfg / "repos.yaml").read_text()
        err, prop = self.tool("propose_grant", {"request_id": rid, "expose": "test", "why": "integration tests"})
        self.assertFalse(err, prop)
        code_ = prop["code"]
        self.assertEqual((self.cfg / "repos.yaml").read_text(), before)          # nothing changed yet
        self.assertEqual(self.run_view("t7-wait")["state"], "waiting_access")
        self.assertIn(code_, self.hub("actions").stdout)
        val = self.tmp / "mcp-secret-value"
        val.write_text("mcp-super-secret-77")
        out = self.hub("confirm", code_, "--value-file", str(val))
        self.assertIn("granted", out.stdout)
        self.assertEqual(self.run_view("t7-wait")["state"], "dispatched")
        self.assertNotIn(code_, self.hub("actions").stdout)                     # closed
        # Secret values never come back through MCP.
        for name, args in (("run_detail", {"job_id": "t7-wait"}), ("secrets_inventory", {}), ("needs", {}),
                           ("doctor", {})):
            err, out_ = self.tool(name, args)
            self.assertNotIn("mcp-super-secret-77", json.dumps(out_), name)

    def test_06_propose_invite_code_only_on_hub_and_reject(self):
        err, prop = self.tool("propose_invite", {"name": "Office PC"})
        self.assertNotIn("sireum1.", json.dumps(prop))
        out = self.hub("confirm", prop["code"])
        self.assertTrue(out.stdout.strip().startswith("sireum1."))
        err, prop2 = self.tool("propose_remove_node", {"node": "office-pc"})
        self.assertIn("rejected", self.hub("reject", prop2["code"]).stdout)
        self.assertNotIn(prop2["code"], self.hub("actions").stdout)
        self.assertIn("hermes", self.hub("mcp-client", "list").stdout)
        self.hub("mcp-client", "revoke", "viewer")
        self.assertEqual(self.rpc("ping", token=self.ro_token)[0], 401)


for _name in [n for n in dir(test_e2e.E2E) if n.startswith("test_")]:
    setattr(MCP, _name, None)

if __name__ == "__main__":
    unittest.main()

"""Phase 1 tests: booster loop over a file-backed source, node enrollment, sireum-kanban wrapper."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

import test_e2e
from test_e2e import OPERATOR, ROOT, WORKER, sh


KANBAN = ROOT / "kanban" / "sireum-kanban"


def card(cid, by="alice", tags=("remote", "repo:sandbox"), title="Add a note", body="Acceptance: note exists"):
    return {"id": cid, "title": title, "body": body, "tags": list(tags), "tagged_by": by}


class Booster(test_e2e.E2E):
    @classmethod
    def setUpClass(cls):
        cls.extra_env = {}
        super().setUpClass()

    @classmethod
    def start_bridge(cls):
        cls.board = cls.tmp / "board.json"
        cls.board.write_text(json.dumps({"cards": [
            card("c1"),
            card("c2", by="mallory"),
            card("c3", tags=("remote",)),
            card("c4", tags=("remote", "repo:nope")),
            card("c5", tags=("remote", "repo:sandbox", "executor:opencode")),
            card("c6", title="usage-limit please", tags=("remote", "repo:sandbox", "executor:claude")),
            card("c7", tags=("remote", "repo:sandbox", "test:fake")),
        ], "log": []}))
        os.environ.update({"SOURCE": f"file:{cls.board}", "REMOTE_TAGGERS": "alice", "CLAIM_GRACE": "8s",
                           "POLL_INTERVAL": "1", "RECONCILE_INTERVAL": "1"})
        try:
            super().start_bridge()
            import urllib.request
            urllib.request.urlopen(urllib.request.Request(
                f"{cls.url}/v1/executors/opencode/pause", data=b'{"minutes": 60}', method="POST",
                headers={"Authorization": f"Bearer {OPERATOR}"}), timeout=5).close()
        finally:
            for k in ("SOURCE", "REMOTE_TAGGERS", "CLAIM_GRACE", "POLL_INTERVAL", "RECONCILE_INTERVAL"):
                os.environ.pop(k)

    def start_runner(self, job_id):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.tmp), "JOB_ID": job_id, "BRIDGE_URL": self.url,
               "WORKER_TOKEN": WORKER, "CACHE_DIR": str(self.tmp / "cache"),
               "SIREUM_ALLOW_FAKE_EXECUTOR": "1", "SIREUM_EXECUTOR_OVERRIDE": "fake"}
        return subprocess.Popen([sys.executable, str(ROOT / "runner" / "entrypoint.py")], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def board_state(self):
        return json.loads(self.board.read_text())

    def wait(self, pred, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.3)
        return False

    def log_for(self, cid):
        return [e for e in self.board_state()["log"] if e[1] == cid]

    def test_booster_flow(self):
        # opencode was paused right after start, so c5 (pinned to it) waits out the grace window.
        self.assertTrue(self.wait(lambda: any(e[0] == "claim" for e in self.log_for("c1"))), self.board_state())

        # Not allowed / no repo / unknown repo -> released right away with a comment.
        for cid, why in (("c2", "not allowed"), ("c3", "no `repo:`"), ("c4", "not onboarded")):
            self.assertTrue(self.wait(lambda: any(e[0] == "release" for e in self.log_for(cid))), cid)
            self.assertTrue(any(why in e[2] for e in self.log_for(cid) if e[0] == "comment"), self.log_for(cid))

        # Run the claimed jobs' runners right away, in parallel (as Nomad would), inside CLAIM_GRACE.
        for cid in ("c1", "c6", "c7"):
            self.assertTrue(self.wait(lambda: any(e[0] == "claim" for e in self.log_for(cid))), cid)
        procs = [self.start_runner(j) for j in ("kb-c1", "kb-c6", "kb-c7")]
        for pr in procs:
            pr.wait(60)

        # c1: no test target => needs_review => request-review, and never re-claimed.
        self.assertTrue(any("picked this up as job `kb-c1`" in e[2] for e in self.log_for("c1") if e[0] == "comment"))
        self.assertTrue(self.wait(lambda: any(e[0] == "request-review" for e in self.log_for("c1"))),
                        self.log_for("c1"))
        self.assertIsNotNone(self.host_branch("kb-c1"))

        # c7: test target from the label => success => complete.
        self.assertTrue(self.wait(lambda: any(e[0] == "complete" for e in self.log_for("c7"))), self.log_for("c7"))
        self.assertEqual(self.run_view("kb-c7")["result"]["test_result"]["target"], "fake")

        # c6: usage limit -> failed -> released back to Hermes; claude paused.
        self.assertTrue(self.wait(lambda: any(e[0] == "release" for e in self.log_for("c6"))), self.log_for("c6"))
        self.assertEqual(self.run_view("kb-c6")["result"]["error"]["code"], "usage_limit")
        _, st = self.call("GET", "/status")
        self.assertIsNotNone(st["executors"]["claude"]["paused_until"])
        time.sleep(2)
        for cid in ("c1", "c6", "c7"):
            self.assertEqual(sum(e[0] == "claim" for e in self.log_for(cid)), 1, self.log_for(cid))

        # c5: opencode paused -> never claimed -> released after CLAIM_GRACE.
        self.assertFalse(any(e[0] == "claim" for e in self.log_for("c5")))
        self.assertTrue(self.wait(lambda: any(e[0] == "release" for e in self.log_for("c5")), 20), self.log_for("c5"))
        self.assertTrue(any("no executor available" in e[2] for e in self.log_for("c5") if e[0] == "comment"))
        self.call("POST", "/v1/executors/claude/resume", {})
        self.call("POST", "/v1/executors/opencode/resume", {})

    def test_never_leased_is_cancelled_and_released(self):
        self.call("POST", "/v1/executors/claude/resume", {})
        d = self.board_state()
        d["cards"].append(card("c9"))
        self.board.write_text(json.dumps(d))
        self.assertTrue(self.wait(lambda: any(e[0] == "claim" for e in self.log_for("c9"))))
        # No runner ever leases kb-c9 -> after CLAIM_GRACE: cancelled, no_capacity, released.
        self.assertTrue(self.wait(lambda: any(e[0] == "release" for e in self.log_for("c9")), 20), self.log_for("c9"))
        res = self.run_view("kb-c9")["result"]
        self.assertEqual((res["status"], res["error"]["code"]), ("cancelled", "no_capacity"))
        # Runner arriving late gets 409 and exits cleanly.
        self.assertIn("already leased or finished", self.run_runner("kb-c9").stderr)


for _name in [n for n in dir(test_e2e.E2E) if n.startswith("test_")]:
    setattr(Booster, _name, None)


class Enrollment(test_e2e.E2E):
    def hub(self, *args):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "bridge"), "BRIDGE_PUBLIC_URL": self.url}
        return subprocess.run([sys.executable, "-m", "sireum_bridge", "--state", str(self.state), *args],
                              env=env, capture_output=True, text=True, check=True).stdout.strip()

    def test_invite_enroll_seats_leave(self):
        code = self.hub("invite", "--name", "MBP M2", "--ts-auth-key", "tskey-auth-TEST")
        self.assertTrue(code.startswith("sireum1."))
        sys.path.insert(0, str(ROOT / "bridge"))
        from sireum_bridge.nodes import decode_join
        j = decode_join(code)
        self.assertEqual((j["hub"], j["ts_auth_key"]), (self.url, "tskey-auth-TEST"))
        info = {"host_os": "macos", "arch": "aarch64", "hostname": "mbp", "cpus": 2, "mem_mb": 4096}
        c, r = self.call("POST", "/v1/nodes/enroll", info, token=j["enroll_token"])
        self.assertEqual(c, 200, r)
        self.assertEqual(r["node_name"], "mbp-m2")
        self.assertEqual(self.call("POST", "/v1/nodes/enroll", info, token=j["enroll_token"])[0], 401)
        name, secret = r["node_name"], r["node_secret"]
        self.assertEqual(self.call("PUT", f"/v1/nodes/{name}/seats", {"claude_token": "x"}, token="bad")[0], 401)
        c, r = self.call("PUT", f"/v1/nodes/{name}/seats", {"claude_token": "sk-ant-oat-1"}, token=secret)
        self.assertEqual((c, r["saved"]), (200, ["claude_token"]))
        seat = self.state / "seats" / f"{name}.json"
        self.assertEqual(stat.S_IMODE(seat.stat().st_mode), 0o600)
        self.assertIn(name, self.hub("nodes"))
        self.assertEqual(self.call("DELETE", f"/v1/nodes/{name}", token=secret)[0], 200)
        self.assertFalse(seat.exists())
        self.assertEqual(self.call("PUT", f"/v1/nodes/{name}/seats", {"claude_token": "x"}, token=secret)[0], 401)

    def test_revoked_invite_rejected(self):
        code = self.hub("invite", "--ts-auth-key", "tskey-auth-X")
        self.hub("invite", "--revoke")
        from sireum_bridge.nodes import decode_join
        c, _ = self.call("POST", "/v1/nodes/enroll", {"hostname": "x"}, token=decode_join(code)["enroll_token"])
        self.assertEqual(c, 401)


for _name in [n for n in dir(test_e2e.E2E) if n.startswith("test_")]:
    setattr(Enrollment, _name, None)


FAKE_HERMES = r'''#!/usr/bin/env python3
import json, sys, os
db = os.environ["FAKE_BOARD"]
d = json.load(open(db))
a = sys.argv[1:]
assert a[0] == "kanban", a
cmd = a[1]
def find(i): return next(c for c in d["cards"] if c["id"] == i)
if cmd == "list": print(json.dumps(d["cards"]))
elif cmd == "show": print(json.dumps(find(a[2])))
elif cmd == "tag":
    c = find(a[2]); t = a[4]
    if a[3] == "--add" and t not in c["tags"]: c["tags"].append(t)
    if a[3] == "--remove" and t in c["tags"]: c["tags"].remove(t)
elif cmd == "comment": find(a[2]).setdefault("comments", []).append(open(a[a.index("--file")+1]).read())
elif cmd == "move": find(a[2])["column"] = a[3]
json.dump(d, open(db, "w"))
'''


class KanbanWrapper(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "hermes").write_text(FAKE_HERMES)
        (self.tmp / "hermes").chmod(0o755)
        self.board = self.tmp / "board.json"
        self.board.write_text(json.dumps({"cards": [
            {"id": "42", "title": "T", "description": "B", "tags": ["remote", "repo:x"], "tagged_by": "alice"},
            {"id": "43", "title": "U", "description": "", "tags": ["other"]}]}))
        (self.tmp / "conf").write_text(f"HERMES={sys.executable} {self.tmp / 'hermes'}\nLOCK={self.tmp}/lock\n")

    def k(self, cmd, stdin=None):
        env = {**os.environ, "SSH_ORIGINAL_COMMAND": cmd, "FAKE_BOARD": str(self.board),
               "SIREUM_KANBAN_CONF": str(self.tmp / "conf")}
        return subprocess.run([sys.executable, str(KANBAN)], env=env, input=stdin, capture_output=True, text=True)

    def test_verbs(self):
        cards = json.loads(self.k("list-remote").stdout)
        self.assertEqual([c["id"] for c in cards], ["42"])
        self.assertEqual(cards[0]["body"], "B")
        self.assertEqual(self.k("claim 42").returncode, 0)
        self.assertEqual(self.k("claim 42").returncode, 1)
        self.assertEqual(self.k("claim 43").returncode, 1)
        self.assertEqual(json.loads(self.k("list-remote").stdout), [])
        self.assertEqual(self.k("comment 42", stdin="hello").returncode, 0)
        self.assertEqual(self.k("complete 42").returncode, 0)
        c = json.loads(self.board.read_text())["cards"][0]
        self.assertEqual((c["column"], c["comments"], c["tags"]), ("done", ["hello"], ["repo:x"]))

    def test_refuses_other_commands(self):
        for bad in ("bash", "claim 42; rm -rf /", "claim ../x", "list-remote extra", "release"):
            self.assertNotEqual(self.k(bad).returncode, 0, bad)


if __name__ == "__main__":
    unittest.main()

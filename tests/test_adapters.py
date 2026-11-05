"""GitHub Issues source + GitHub/GitLab/Gitea MR adapters against a local fake API."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))

from sireum_bridge.relay import GitHost, Relay, Repo  # noqa: E402
from sireum_bridge.sources import Card, GitHubIssues, parse_card  # noqa: E402

TOKEN = "ghp_fake"


class FakeAPI(BaseHTTPRequestHandler):
    issues: dict = {}
    calls: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth_ok(self):
        h = self.headers
        return h.get("Authorization") in (f"Bearer {TOKEN}", f"token {TOKEN}") or h.get("PRIVATE-TOKEN") == TOKEN

    def handle_any(self):
        if not self._auth_ok():
            return self._send(401, {"message": "bad creds"})
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        path = urlparse(self.path).path
        FakeAPI.calls.append((self.command, path, body))
        parts = path.strip("/").split("/")
        # GitLab MR
        if "merge_requests" in parts:
            return self._send(201, {"web_url": "https://gl/mr/1"})
        # GitHub / Gitea PR
        if parts[-1] == "pulls":
            return self._send(201, {"html_url": f"https://gh/pull/{body['head']}"})
        # /repos/o/r/issues...
        if parts[3:4] == ["issues"]:
            if len(parts) == 4:
                return self._send(200, list(FakeAPI.issues.values()))
            num = parts[4]
            issue = FakeAPI.issues[num]
            if parts[5:] == ["events"]:
                return self._send(200, [{"event": "labeled", "label": {"name": "remote"},
                                         "actor": {"login": issue["_tagger"]}}])
            if parts[5:] == ["labels"] and self.command == "GET":
                return self._send(200, issue["labels"])
            if parts[5:] == ["labels"] and self.command == "POST":
                for lb in body["labels"]:
                    if {"name": lb} not in issue["labels"]:
                        issue["labels"].append({"name": lb})
                return self._send(200, issue["labels"])
            if parts[5:6] == ["labels"] and self.command == "DELETE":
                issue["labels"] = [lb for lb in issue["labels"] if lb["name"] != unquote(parts[6])]
                return self._send(200, issue["labels"])
            if parts[5:] == ["comments"]:
                issue.setdefault("_comments", []).append(body["body"])
                return self._send(201, {})
        self._send(404, {"message": path})

    do_GET = do_POST = do_DELETE = handle_any


class Adapters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.api = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.tmp = Path(tempfile.mkdtemp())
        cls.key = cls.tmp / "key"
        cls.key.write_text(TOKEN + "\n")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeAPI.calls.clear()
        FakeAPI.issues = {
            "7": {"number": 7, "title": "Fix login", "body": "Details\n\nAcceptance: login works",
                  "labels": [{"name": "remote"}, {"name": "repo:example"}, {"name": "test:auth"}],
                  "created_at": "2026-11-05T00:00:00Z", "_tagger": "alice"},
            "8": {"number": 8, "title": "PR", "body": "", "labels": [{"name": "remote"}], "pull_request": {},
                  "_tagger": "alice"},
        }

    def test_github_issues_source(self):
        src = GitHubIssues("adaptiv/tasks", str(self.key), self.api)
        cards = src.list_remote()
        self.assertEqual([c.id for c in cards], ["7"])  # PRs skipped
        self.assertEqual(cards[0].tagged_by, "alice")
        body = parse_card(cards[0])
        self.assertEqual((body["repo"], body["test_target"], body["acceptance"]), ("example", "auth", "login works"))
        self.assertTrue(src.claim("7"))
        self.assertFalse(src.claim("7"))
        self.assertEqual(src.list_remote(), [])
        src.comment("7", "hi")
        src.action("7", "complete")
        names = {lb["name"] for lb in FakeAPI.issues["7"]["labels"]}
        self.assertEqual(names, {"repo:example", "test:auth", "sireum:done"})
        self.assertEqual(FakeAPI.issues["7"]["_comments"], ["hi"])

    def test_mr_adapters(self):
        relay = Relay(self.tmp / "relay", {
            "gh": GitHost("gh", "github", "https://x", self.api, str(self.key)),
            "gl": GitHost("gl", "gitlab", "https://x", self.api, str(self.key)),
            "gt": GitHost("gt", "gitea", "https://x", self.api, str(self.key)),
            "lo": GitHost("lo", "local", str(self.tmp)),
        })
        self.assertEqual(relay.open_mr(Repo("a", "gh", "o/r"), "agent/j1", "t", "b"), "https://gh/pull/agent/j1")
        self.assertEqual(relay.open_mr(Repo("b", "gl", "grp/r"), "agent/j2", "t", "b"), "https://gl/mr/1")
        self.assertEqual(relay.open_mr(Repo("c", "gt", "o/r"), "agent/j3", "t", "b"), "https://gh/pull/agent/j3")
        self.assertIsNone(relay.open_mr(Repo("d", "lo", "r"), "agent/j4", "t", "b"))
        gl = [c for c in FakeAPI.calls if "merge_requests" in c[1]][0]
        self.assertEqual(gl[1], "/projects/grp%2Fr/merge_requests")
        self.assertEqual(gl[2]["target_branch"], "main")

    def test_parse_card_defaults_and_caps(self):
        b = parse_card(Card(id="1", title="T", body="", tags=["remote", "timeout:999", "executor:opencode",
                                                               "base:dev", "class:browser"]))
        self.assertEqual((b["timeout_sec"], b["executor"], b["base_ref"], b["job_class"]),
                         (120 * 60, "opencode", "dev", "browser"))
        self.assertNotIn("repo", b)


if __name__ == "__main__":
    unittest.main()

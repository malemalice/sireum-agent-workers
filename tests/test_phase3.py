"""Phase 3: prepare step driven by .agent/tools.yaml at base_commit."""
from __future__ import annotations

import subprocess
import unittest

import test_e2e
from test_e2e import sh


class Prepare(test_e2e.E2E):
    def branch_with_manifest(self, name, manifest):
        work = self.tmp / f"w-{name}"
        sh("git", "clone", "-q", str(self.host_repo), str(work))
        sh("git", "checkout", "-q", "-b", name, cwd=work)
        (work / ".agent" / "tools.yaml").write_text(manifest)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", name, cwd=work)
        sh("git", "push", "-q", "origin", name, cwd=work)

    def test_pip_venv_used_by_tests_and_not_committed(self):
        self.branch_with_manifest("prep-pip", "version: 1\npip:\n  install: pip --version\n"
                                  "test_targets:\n  venv: test -n \"$VIRTUAL_ENV\" && test -x \"$VIRTUAL_ENV/bin/python\"\n")
        self.dispatch("t-pip", base_ref="prep-pip", test_target="venv")
        p = self.run_runner("t-pip", timeout=180)
        res = self.run_view("t-pip")["result"]
        self.assertEqual(res["status"], "success", (res, p.stderr))
        files = sh("git", "ls-tree", "-r", "--name-only", res["commit"], cwd=self.host_repo)
        self.assertNotIn(".sireum-venv", files)

    def test_missing_system_package_blocks(self):
        self.branch_with_manifest("prep-apt", "version: 1\napt: [sireum-no-such-package]\n")
        self.dispatch("t-apt", base_ref="prep-apt")
        self.run_runner("t-apt")
        res = self.run_view("t-apt")["result"]
        self.assertEqual(res["status"], "blocked")
        self.assertIn("sireum-no-such-package", res["summary"])
        self.assertIsNone(self.host_branch("t-apt"))

    def test_failing_prepare_fails(self):
        self.branch_with_manifest("prep-bad", "version: 1\nnpm:\n  install: exit 3\n")
        self.dispatch("t-prepbad", base_ref="prep-bad")
        self.run_runner("t-prepbad")
        res = self.run_view("t-prepbad")["result"]
        self.assertEqual((res["status"], res["error"]["code"]), ("failed", "prepare"))

    def test_prepare_timeout(self):
        self.branch_with_manifest("prep-slow", "version: 1\nnpm:\n  install: sleep 30\n")
        self.dispatch("t-prepslow", base_ref="prep-slow")
        # The worker-side cap SIREUM_PREPARE_TIMEOUT_SEC shortens the bridge default (900 s).
        p = self.run_runner("t-prepslow", timeout=120, SIREUM_PREPARE_TIMEOUT_SEC="2")
        res = self.run_view("t-prepslow")["result"]
        self.assertEqual(res["error"]["code"], "prepare", p.stderr)


for _name in [n for n in dir(test_e2e.E2E) if n.startswith("test_")]:
    setattr(Prepare, _name, None)

if __name__ == "__main__":
    unittest.main()

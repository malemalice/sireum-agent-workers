"""Git relay: hub mirrors, bundle verification, push + MR to the real git host.

Workers never hold git-host credentials. They clone read-only from the hub
mirror and upload a bundle; this module verifies it and pushes with the host's
API key.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .creds import SOURCES, CredentialError, Resolver, git_auth_env


class RelayError(Exception):
    """Bundle or git operation rejected. `code` maps to an HTTP status."""

    def __init__(self, message: str, code: int = 422):
        super().__init__(message)
        self.code = code


def _auth_of(obj) -> dict:
    return {k: getattr(obj, k) for k in (*SOURCES, "cache_sec") if getattr(obj, k, None)}


@dataclass
class GitHost:
    id: str
    type: str  # github | gitlab | gitea | generic | local
    git_url: str
    api_url: str | None = None
    # Exactly one credential source (see creds.py): file, env, command, or GitHub App.
    api_key_file: str | None = None
    api_key_env: str | None = None
    api_key_cmd: list | str | None = None
    github_app: dict | None = None
    cache_sec: int | None = None

    @property
    def auth(self) -> dict:
        return _auth_of(self)

    def remote_url(self, path: str) -> str:
        if self.type == "local":
            # git_url is a directory of bare repos (Phase 0 sandbox host).
            return str(Path(self.git_url) / f"{path}.git")
        return f"{self.git_url.rstrip('/')}/{path}.git"


@dataclass
class Repo:
    alias: str
    git_host: str
    path: str
    default_branch: str = "main"
    enabled: bool = False
    # Optional per-repo credential, overriding the host's (same keys as GitHost).
    api_key_file: str | None = None
    api_key_env: str | None = None
    api_key_cmd: list | str | None = None
    github_app: dict | None = None
    cache_sec: int | None = None
    # Phase 6 approvals (see access.py): what jobs on this repo may receive.
    registries: list = field(default_factory=list)
    secrets: dict = field(default_factory=dict)       # NAME -> {source..., expose}
    egress: list = field(default_factory=list)
    signing: str = "merge"                             # merge | hub-ssh
    signing_key: str | None = None                     # hub-ssh: private key path

    @property
    def auth(self) -> dict:
        return _auth_of(self)

    @property
    def key(self) -> str:
        return repo_hash(self.git_host, self.path)


def repo_hash(git_host: str, path: str) -> str:
    return hashlib.sha256(f"{git_host}/{path}".encode()).hexdigest()[:16]


def git(*args: str, cwd: str | Path | None = None, env: dict | None = None,
        check: bool = True, input: bytes | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    proc = subprocess.run(["git", *args], cwd=cwd, env=full_env, input=input,
                          capture_output=True)
    if check and proc.returncode != 0:
        raise RelayError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}", 502)
    return proc


class Relay:
    def __init__(self, root: Path, hosts: dict[str, GitHost], bundle_max_mb: int = 50,
                 resolver: Resolver | None = None):
        self.root = Path(root)
        self.hosts = hosts
        self.creds = resolver or Resolver()
        self.bundle_max = bundle_max_mb * 1024 * 1024
        (self.root / "git").mkdir(parents=True, exist_ok=True)
        (self.root / "bundles").mkdir(parents=True, exist_ok=True)

    def token(self, repo: Repo) -> str | None:
        host = self.hosts[repo.git_host]
        if host.type == "local":
            return None
        try:
            return self.creds.token(repo.auth or host.auth, host.api_url, repo.path)
        except CredentialError as e:
            raise RelayError(f"credentials for {repo.git_host}/{repo.path}: {e}", 502) from None

    def _env(self, repo: Repo) -> dict:
        return git_auth_env(self.hosts[repo.git_host].type, self.token(repo))

    def mirror_path(self, repo: Repo) -> Path:
        return self.root / "git" / f"{repo.key}.git"

    def fetch(self, repo: Repo) -> Path:
        """Create (lazily) and refresh the hub mirror of a repo's branches."""
        host = self.hosts[repo.git_host]
        mirror = self.mirror_path(repo)
        if not mirror.exists():
            git("init", "--bare", "--quiet", str(mirror))
            for k, v in (("uploadpack.allowReachableSHA1InWant", "true"),
                         ("uploadpack.hideRefs", "refs/sireum"),
                         ("http.receivepack", "false"),
                         ("gc.auto", "0")):
                git("config", k, v, cwd=mirror)
        git("fetch", "--prune", "--quiet", host.remote_url(repo.path),
            "+refs/heads/*:refs/heads/*", cwd=mirror, env=self._env(repo))
        return mirror

    def resolve(self, repo: Repo, ref: str) -> str:
        mirror = self.mirror_path(repo)
        for cand in (f"refs/heads/{ref}", ref):
            p = git("rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", cwd=mirror, check=False)
            if p.returncode == 0:
                return p.stdout.decode().strip()
        raise RelayError(f"cannot resolve ref {ref!r}", 400)

    def read_file(self, repo: Repo, commit: str, path: str) -> str | None:
        p = git("show", f"{commit}:{path}", cwd=self.mirror_path(repo), check=False)
        return p.stdout.decode(errors="replace") if p.returncode == 0 else None

    def intake(self, repo: Repo, job_id: str, base_commit: str, bundle: bytes) -> str:
        """Verify an uploaded bundle and store it as refs/sireum/incoming/<job_id>.

        Returns the tip commit. Raises RelayError on any rule violation.
        """
        if len(bundle) > self.bundle_max:
            raise RelayError(f"bundle exceeds {self.bundle_max // (1024 * 1024)} MB", 413)
        mirror = self.mirror_path(repo)
        path = self.root / "bundles" / f"{job_id}.bundle"
        path.write_bytes(bundle)
        p = git("bundle", "verify", "--quiet", str(path), cwd=mirror, check=False)
        if p.returncode != 0:
            raise RelayError("bundle verify failed: " + p.stderr.decode(errors="replace").strip())
        heads = git("bundle", "list-heads", str(path), cwd=mirror).stdout.decode().split("\n")
        heads = [h.split() for h in heads if h.strip()]
        want = f"refs/heads/agent/{job_id}"
        if len(heads) != 1 or heads[0][1] != want:
            raise RelayError(f"bundle must contain exactly one ref {want}, got {[h[1] for h in heads]}")
        tip = heads[0][0]
        incoming = f"refs/sireum/incoming/{job_id}"
        git("fetch", "--quiet", str(path), f"+{want}:{incoming}", cwd=mirror)
        if git("merge-base", "--is-ancestor", base_commit, tip, cwd=mirror, check=False).returncode != 0:
            git("update-ref", "-d", incoming, cwd=mirror, check=False)
            raise RelayError("bundle tip does not descend from base_commit")
        return tip

    def sign(self, repo: Repo, job_id: str, base_commit: str) -> str:
        """Re-create the agent's commits signed with the hub's SSH key (signing: hub-ssh).

        Commit ids change; content, messages and authors are kept. The committer becomes
        the hub identity. Returns the new tip.
        """
        key = repo.signing_key
        if not key or not Path(key).exists():
            raise RelayError(f"signing key for {repo.alias} missing: {key}", 500)
        import shutil
        import tempfile
        mirror = self.mirror_path(repo)
        incoming = f"refs/sireum/incoming/{job_id}"
        wt = Path(tempfile.mkdtemp(prefix=f"sireum-sign-{job_id}-"))
        shutil.rmtree(wt)
        env = {"GIT_COMMITTER_NAME": os.environ.get("SIREUM_COMMITTER_NAME", "Sireum Hub"),
               "GIT_COMMITTER_EMAIL": os.environ.get("SIREUM_COMMITTER_EMAIL", "sireum@localhost")}
        try:
            git("worktree", "add", "--quiet", "--detach", str(wt), incoming, cwd=mirror)
            git("-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "-c", "commit.gpgsign=true",
                "rebase", "--quiet", "--force-rebase", "--gpg-sign", "--committer-date-is-author-date",
                base_commit, cwd=wt, env=env)
            tip = git("rev-parse", "HEAD", cwd=wt).stdout.decode().strip()
            git("update-ref", incoming, tip, cwd=mirror)
            return tip
        finally:
            git("worktree", "remove", "--force", str(wt), cwd=mirror, check=False)
            git("worktree", "prune", cwd=mirror, check=False)

    def push(self, repo: Repo, job_id: str, base_commit: str | None = None) -> str:
        """Push the accepted bundle to the host as agent/<job_id>. Never forces."""
        branch = f"agent/{job_id}"
        if branch == repo.default_branch or not branch.startswith("agent/"):
            raise RelayError("refusing to push default branch", 403)
        if repo.signing == "hub-ssh" and base_commit:
            self.sign(repo, job_id, base_commit)
        host = self.hosts[repo.git_host]
        mirror = self.mirror_path(repo)
        p = git("push", "--quiet", "--porcelain", host.remote_url(repo.path),
                f"refs/sireum/incoming/{job_id}:refs/heads/{branch}",
                cwd=mirror, env=self._env(repo), check=False)
        if p.returncode != 0:
            raise RelayError("push rejected: " + p.stderr.decode(errors="replace").strip(), 409)
        return branch

    def open_mr(self, repo: Repo, branch: str, title: str, body: str) -> str | None:
        host = self.hosts[repo.git_host]
        if host.type in ("local", "generic"):
            return None
        key = self.token(repo)
        if host.type == "github":
            owner_repo = repo.path
            url = f"{host.api_url.rstrip('/')}/repos/{owner_repo}/pulls"
            data = {"title": title, "head": branch, "base": repo.default_branch, "body": body}
            headers = {"Authorization": f"Bearer {key}", "Accept": "application/vnd.github+json"}
            return _post_json(url, data, headers).get("html_url")
        if host.type == "gitlab":
            pid = urllib.parse.quote(repo.path, safe="")
            url = f"{host.api_url.rstrip('/')}/projects/{pid}/merge_requests"
            data = {"title": title, "source_branch": branch,
                    "target_branch": repo.default_branch, "description": body}
            return _post_json(url, data, {"PRIVATE-TOKEN": key}).get("web_url")
        if host.type == "gitea":
            url = f"{host.api_url.rstrip('/')}/repos/{repo.path}/pulls"
            data = {"title": title, "head": branch, "base": repo.default_branch, "body": body}
            return _post_json(url, data, {"Authorization": f"token {key}"}).get("html_url")
        return None  # generic / local: no MR API


def _post_json(url: str, data: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

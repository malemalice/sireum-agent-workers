"""Git host credentials, resolved on the fly.

A host in git_hosts.yaml (or a single repo in repos.yaml, which overrides its host)
names exactly one source:

  api_key_file: /opt/sireum/secrets/github.key     static token in a 0600 file
  api_key_env:  GITHUB_TOKEN                       token from the bridge's environment
  api_key_cmd:  ["gh", "auth", "token"]            command printing a token (gh, op, security,
                                                   pass, vault …); cached for `cache_sec`
  github_app:                                      short-lived installation tokens
    app_id: 123456
    private_key_file: /opt/sireum/secrets/app.pem
    installation_id: 789        (optional — looked up per repo if omitted)

Tokens are only ever kept in memory (plus the source itself) and passed to git
through environment-only config, never argv.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SOURCES = ("api_key_file", "api_key_env", "api_key_cmd", "github_app")


class CredentialError(Exception):
    pass


def describe(auth: dict) -> str:
    """Human description without secrets, e.g. for `sireum-hub repos`."""
    for k in SOURCES:
        if auth.get(k):
            v = auth[k]
            if k == "github_app":
                return f"github_app (app {v.get('app_id')})"
            return f"{k}: {' '.join(v) if isinstance(v, list) else v}"
    return "none"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def app_jwt(app_id: str | int, private_key_file: str, now: float | None = None) -> str:
    """RS256 JWT for a GitHub App, signed with openssl (no Python crypto dependency)."""
    now = int(now or time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)},
                                 separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    p = subprocess.run(["openssl", "dgst", "-sha256", "-sign", private_key_file], input=signing_input,
                       capture_output=True)
    if p.returncode != 0:
        raise CredentialError(f"openssl sign failed: {p.stderr.decode(errors='replace').strip()[:200]}")
    return f"{header}.{payload}.{_b64url(p.stdout)}"


def _gh_api(method: str, url: str, token: str, scheme: str = "Bearer", body: dict | None = None) -> dict:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"{scheme} {token}",
                                          "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise CredentialError(f"GitHub {method} {url}: {e.code}") from None
    except urllib.error.URLError as e:
        raise CredentialError(f"GitHub {method} {url}: {e.reason}") from None


class Resolver:
    """Resolves and caches tokens per (source, repo). Thread-safe."""

    def __init__(self, default_cache_sec: int = 300):
        self.default_cache = default_cache_sec
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def token(self, auth: dict, api_url: str | None = None, repo_path: str | None = None) -> str | None:
        if not auth or not any(auth.get(k) for k in SOURCES):
            return None
        key = json.dumps([auth, repo_path], sort_keys=True, default=str)
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[1] > time.time():
                return hit[0]
        tok, ttl = self._fetch(auth, api_url, repo_path)
        if not tok:
            raise CredentialError(f"empty token from {describe(auth)}")
        with self._lock:
            self._cache[key] = (tok, time.time() + ttl)
        return tok

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def _fetch(self, auth: dict, api_url: str | None, repo_path: str | None) -> tuple[str, float]:
        cache = int(auth.get("cache_sec", self.default_cache))
        if auth.get("api_key_file"):
            try:
                return Path(auth["api_key_file"]).expanduser().read_text().strip(), min(cache, 60)
            except OSError as e:
                raise CredentialError(f"cannot read {auth['api_key_file']}: {e.strerror}") from None
        if auth.get("api_key_env"):
            return os.environ.get(auth["api_key_env"], "").strip(), cache
        if auth.get("api_key_cmd"):
            cmd = auth["api_key_cmd"]
            argv = cmd if isinstance(cmd, list) else ["sh", "-c", str(cmd)]
            try:
                p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise CredentialError(f"api_key_cmd failed: {e}") from None
            if p.returncode != 0:
                raise CredentialError(f"api_key_cmd exit {p.returncode}: {p.stderr.strip()[:200]}")
            return p.stdout.strip().splitlines()[0] if p.stdout.strip() else "", cache
        if auth.get("github_app"):
            return self._app_token(auth["github_app"], api_url or "https://api.github.com", repo_path)
        raise CredentialError("no credential source configured")

    def _app_token(self, app: dict, api_url: str, repo_path: str | None) -> tuple[str, float]:
        api = api_url.rstrip("/")
        jwt = app_jwt(app["app_id"], str(Path(app["private_key_file"]).expanduser()))
        inst = app.get("installation_id")
        if not inst:
            if not repo_path:
                raise CredentialError("github_app without installation_id needs a repo")
            inst = _gh_api("GET", f"{api}/repos/{repo_path}/installation", jwt)["id"]
        body = {"repositories": [repo_path.split("/", 1)[1]]} if repo_path and "/" in repo_path else None
        r = _gh_api("POST", f"{api}/app/installations/{inst}/access_tokens", jwt, body=body)
        # Tokens live 1 h; refresh 5 min early.
        return r["token"], 55 * 60


def git_auth_env(host_type: str, token: str | None) -> dict:
    """http.extraHeader through env-only git config (never argv, never on disk)."""
    if not token or host_type == "local":
        return {}
    if host_type == "github":
        header = "Authorization: Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    elif host_type == "gitlab":
        header = "Authorization: Basic " + base64.b64encode(f"oauth2:{token}".encode()).decode()
    else:
        header = f"Authorization: token {token}"
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": header}


def write_secret(path: Path, value: str) -> None:
    """Atomically write a 0600 secret file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value.strip() + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

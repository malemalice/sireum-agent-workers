"""Private package registries: hub auth relay (no upstream token on workers) or inject.

registries.yaml (hub):

  registries:
    - name: npm-company
      ecosystem: npm            # npm | pypi | go | generic
      url: https://npm.pkg.github.com
      scopes: ["@company"]      # npm scopes / PyPI name prefixes / Go module prefixes
      mode: relay               # relay (default) | inject
      auth_scheme: bearer       # bearer | basic | token
      username: __token__       # for basic
      api_key_file: /opt/sireum/secrets/ghpkg.key   # any creds.py source

Relay: jobs talk to https://<hub>/pkg/<name>/… with their lease token; the hub
forwards to the upstream with the real credential and rewrites upstream URLs in
responses back to itself.

Scopes guard against dependency confusion:
  - npm: only the listed scopes are routed to the private registry (npm config).
  - PyPI: the relay serves names matching a scope prefix from the private index and
    redirects every other name to pypi.org — a private name is never looked up publicly
    and vice versa.
  - Go: modules outside the scopes get 404 (Go falls back to proxy.golang.org); a
    scoped module the upstream lacks gets 403, so Go never falls back for it.
"""
from __future__ import annotations

import base64
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .creds import SOURCES, Resolver

ECOSYSTEMS = ("npm", "pypi", "go", "generic")
MAX_BYTES = 300 * 1024 * 1024
TEXT_TYPES = ("json", "html", "text", "xml")


@dataclass
class Registry:
    name: str
    ecosystem: str
    url: str
    scopes: list = field(default_factory=list)
    mode: str = "relay"
    auth_scheme: str = "bearer"
    username: str | None = None
    api_key_file: str | None = None
    api_key_env: str | None = None
    api_key_cmd: list | str | None = None
    github_app: dict | None = None
    cache_sec: int | None = None

    def __post_init__(self):
        if self.ecosystem not in ECOSYSTEMS:
            raise ValueError(f"registry {self.name}: ecosystem must be one of {ECOSYSTEMS}")
        if self.mode not in ("relay", "inject"):
            raise ValueError(f"registry {self.name}: mode must be relay or inject")
        self.url = self.url.rstrip("/")
        self.scopes = [str(s) for s in (self.scopes or [])]

    @property
    def auth(self) -> dict:
        return {k: getattr(self, k) for k in (*SOURCES, "cache_sec") if getattr(self, k, None)}

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.url).hostname or ""

    @property
    def origin(self) -> str:
        u = urllib.parse.urlsplit(self.url)
        return f"{u.scheme}://{u.netloc}"

    def auth_header(self, token: str | None) -> dict:
        if not token:
            return {}
        if self.auth_scheme == "basic":
            cred = base64.b64encode(f"{self.username or '__token__'}:{token}".encode()).decode()
            return {"Authorization": f"Basic {cred}"}
        if self.auth_scheme == "token":
            return {"Authorization": f"token {token}"}
        return {"Authorization": f"Bearer {token}"}


def pypi_norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def in_scope(reg: Registry, name: str) -> bool:
    if not reg.scopes:
        return True
    if reg.ecosystem == "pypi":
        n = pypi_norm(name)
        return any(n == pypi_norm(s) or n.startswith(pypi_norm(s).rstrip("-") + "-") or
                   n.startswith(pypi_norm(s)) for s in reg.scopes)
    if reg.ecosystem == "npm":
        return any(name == s or name.startswith(s.rstrip("/") + "/") for s in reg.scopes)
    if reg.ecosystem == "go":
        return any(name == s or name.startswith(s.rstrip("/") + "/") for s in reg.scopes)
    return True


class Response:
    def __init__(self, status: int, headers: dict, body: bytes):
        self.status, self.headers, self.body = status, headers, body


class Relay:
    def __init__(self, resolver: Resolver):
        self.creds = resolver

    def forward(self, reg: Registry, rest: str, query: str, relay_base: str, accept: str | None) -> Response:
        """rest: path after /pkg/<name>, starting with '/'. relay_base: public URL of /pkg/<name>."""
        rest = "/" + rest.lstrip("/")
        # Ecosystem routing and scope checks.
        if reg.ecosystem == "pypi":
            m = re.match(r"^/simple/([^/]+)/?$", rest)
            if m and not in_scope(reg, urllib.parse.unquote(m[1])):
                return Response(302, {"Location": f"https://pypi.org/simple/{pypi_norm(m[1])}/"}, b"")
            if rest.rstrip("/") == "/simple":
                return Response(403, {}, b"index listing not relayed")
        elif reg.ecosystem == "npm" and not rest.startswith("/_o/"):
            pkg = urllib.parse.unquote(rest.lstrip("/"))
            name = "/".join(pkg.split("/")[:2]) if pkg.startswith("@") else pkg.split("/")[0]
            if not in_scope(reg, name):
                return Response(404, {}, b"not a private scope")
        elif reg.ecosystem == "go" and not rest.startswith("/_o/"):
            mod = urllib.parse.unquote(rest.lstrip("/")).split("/@")[0]
            if not in_scope(reg, mod):
                return Response(404, {}, b"not a private module")

        if rest.startswith("/_o/"):
            target = reg.origin + "/" + rest[len("/_o/"):]
        elif reg.ecosystem == "pypi" and rest.startswith("/simple/"):
            # url points at the index root (…/simple); keep the package path below it.
            target = reg.url + rest[len("/simple"):]
        else:
            target = reg.url + rest
        if query:
            target += "?" + query
        token = self.creds.token(reg.auth, None, None) if reg.auth else None
        headers = {"Accept-Encoding": "identity", "User-Agent": "sireum-relay", **reg.auth_header(token)}
        if accept:
            headers["Accept"] = accept
        req = urllib.request.Request(target, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read(MAX_BYTES + 1)
                status, ctype = r.status, r.headers.get("Content-Type", "application/octet-stream")
        except urllib.error.HTTPError as e:
            status, ctype, body = e.code, e.headers.get("Content-Type", "text/plain"), e.read(65536)
        except urllib.error.URLError as e:
            return Response(502, {"Content-Type": "text/plain"}, f"upstream: {e.reason}".encode())
        if len(body) > MAX_BYTES:
            return Response(502, {"Content-Type": "text/plain"}, b"upstream response too large")
        if reg.ecosystem == "go" and status == 404:
            status = 403  # scoped module missing upstream: stop Go from trying the public proxy
        if any(t in ctype for t in TEXT_TYPES):
            text = body.decode("utf-8", errors="surrogateescape")
            text = text.replace(reg.url + "/", relay_base + "/").replace(reg.url, relay_base)
            if reg.ecosystem == "pypi":
                # Links relative to the index root stay valid; absolute upstream links go via _o.
                pass
            text = text.replace(reg.origin + "/", relay_base + "/_o/")
            body = text.encode("utf-8", errors="surrogateescape")
        return Response(status, {"Content-Type": ctype}, body)


def runner_view(reg: Registry, relay_base: str, lease_token: str, resolver: Resolver) -> dict:
    """What a job receives for an approved registry. Relay mode: no upstream credential."""
    view = {"name": reg.name, "ecosystem": reg.ecosystem, "scopes": reg.scopes, "mode": reg.mode}
    if reg.mode == "relay":
        view.update(url=relay_base, token=lease_token, auth_scheme="bearer" if reg.ecosystem == "npm" else "basic",
                    username="sireum")
    else:
        view.update(url=reg.url, token=resolver.token(reg.auth, None, None) if reg.auth else None,
                    auth_scheme=reg.auth_scheme, username=reg.username or "__token__")
    return view

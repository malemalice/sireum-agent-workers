"""Credential inventory and health checks (`sireum-hub secrets`, `sireum-hub doctor`).

Used by the CLI and by the bridge's daily check (findings go to Discord).
Never prints or returns secret values.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .creds import CredentialError, Resolver, describe
from .pkgrelay import Registry

EXPIRY_WARN_DAYS = 7
UNUSED_WARN_DAYS = 60
AUTH_KEYS = ("api_key_file", "api_key_env", "api_key_cmd", "github_app", "cache_sec")


def _load(config: Path, name: str, key: str) -> list:
    p = config / name
    return ((yaml.safe_load(p.read_text()) if p.exists() else None) or {}).get(key) or []


def inventory(config: Path, last_used=None) -> list[dict]:
    """Every credential the hub knows: where it comes from, who uses it, when it was last used."""
    hosts = _load(config, "git_hosts.yaml", "hosts")
    repos = _load(config, "repos.yaml", "repos")
    regs = _load(config, "registries.yaml", "registries")
    out = []
    for h in hosts:
        if h.get("type") == "local":
            continue
        users = [r["alias"] for r in repos if r.get("git_host") == h["id"] and not any(r.get(k) for k in AUTH_KEYS[:4])]
        out.append({"kind": "git-host", "name": h["id"], "source": describe(h), "used_by": users})
    for r in repos:
        if any(r.get(k) for k in AUTH_KEYS[:4]):
            out.append({"kind": "git-repo", "name": r["alias"], "source": describe(r), "used_by": [r["alias"]]})
        for name, spec in (r.get("secrets") or {}).items():
            out.append({"kind": "secret", "name": name, "source": describe(spec or {}),
                        "expose": (spec or {}).get("expose", "prepare"), "used_by": [r["alias"]]})
    for g in regs:
        out.append({"kind": "registry", "name": g["name"], "source": describe(g), "mode": g.get("mode", "relay"),
                    "used_by": [r["alias"] for r in repos if g["name"] in (r.get("registries") or [])]})
    if last_used:
        for item in out:
            k = {"secret": "secret", "registry": "registry"}.get(item["kind"])
            t = last_used(k, item["name"]) if k else None
            item["last_used"] = datetime.fromtimestamp(t).astimezone().isoformat(timespec="minutes") if t else None
    return out


def _probe(url: str, headers: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "sireum-doctor", **headers})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)
    except urllib.error.URLError as e:
        return 0, {"error": str(e.reason)}


def _expiry(headers: dict) -> float | None:
    v = headers.get("github-authentication-token-expiration") or headers.get("GitHub-Authentication-Token-Expiration")
    if not v:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def doctor(config: Path, last_used=None, resolver: Resolver | None = None) -> list[dict]:
    """Check every credential. Returns findings: {level: ok|warn|fail, what, detail}."""
    res = resolver or Resolver()
    hosts = {h["id"]: h for h in _load(config, "git_hosts.yaml", "hosts")}
    repos = _load(config, "repos.yaml", "repos")
    regs = _load(config, "registries.yaml", "registries")
    findings = []

    def add(level, what, detail=""):
        findings.append({"level": level, "what": what, "detail": detail})

    for r in repos:
        if not r.get("enabled"):
            continue
        h = hosts.get(r.get("git_host"))
        if not h:
            add("fail", f"repo {r['alias']}", f"unknown git host {r.get('git_host')}")
            continue
        auth = {k: r[k] for k in AUTH_KEYS if r.get(k)} or {k: h[k] for k in AUTH_KEYS if h.get(k)}
        tok, cred_err = None, None
        if h.get("type") != "local":
            try:
                tok = res.token(auth, h.get("api_url"), r.get("path"))
            except CredentialError as e:
                cred_err = str(e)
        if h.get("type") == "local":
            pass
        elif cred_err:
            add("fail", f"repo {r['alias']} git credential", cred_err)
        elif h.get("type") == "github":
            code, hdr = _probe(f"{h.get('api_url', 'https://api.github.com').rstrip('/')}/repos/{r['path']}",
                               {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
            exp = _expiry(hdr)
            if code != 200:
                add("fail", f"repo {r['alias']} git credential", f"GitHub answered {code or hdr.get('error')}")
            elif exp and exp - time.time() < EXPIRY_WARN_DAYS * 86400:
                days = max(0, int((exp - time.time()) // 86400))
                add("warn", f"repo {r['alias']} git credential", f"token expires in {days} day(s)")
            else:
                add("ok", f"repo {r['alias']} git credential", describe(auth))
        else:
            add("ok", f"repo {r['alias']} git credential", f"resolved ({describe(auth)}); API not probed")
        for name, spec in (r.get("secrets") or {}).items():
            src = {k: v for k, v in (spec or {}).items() if k != "expose"}
            try:
                ok = bool(res.token(src, None, None))
                add("ok" if ok else "fail", f"secret {name} (repo {r['alias']})", describe(src) if ok else "empty")
            except CredentialError as e:
                add("fail", f"secret {name} (repo {r['alias']})", str(e))
            t = last_used("secret", name) if last_used else None
            if last_used and (t is None or time.time() - t > UNUSED_WARN_DAYS * 86400):
                add("warn", f"secret {name} (repo {r['alias']})",
                    f"not used by any job in {UNUSED_WARN_DAYS} days — revoke if no longer needed")
    for g in regs:
        try:
            reg = Registry(**g)
            tok = res.token(reg.auth, None, None) if reg.auth else None
        except (CredentialError, TypeError, ValueError) as e:
            add("fail", f"registry {g.get('name')}", str(e))
            continue
        code, hdr = _probe(reg.url + "/", reg.auth_header(tok))
        if code in (401, 403):
            add("fail", f"registry {reg.name}", f"upstream refused the credential ({code})")
        elif code == 0:
            add("fail", f"registry {reg.name}", f"unreachable: {hdr.get('error')}")
        else:
            add("ok", f"registry {reg.name}", f"{reg.mode}, upstream answered {code}")
    return findings

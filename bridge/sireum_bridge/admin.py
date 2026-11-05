"""Operator commands that edit hub config: add-repo, repos, connect-hermes, runner-image.

The CLI runs either as the operator (macOS hub: the operator is also the service user)
or as root via sudo (Linux hub: service user `sireum`). Files it writes are handed to
SIREUM_SERVICE_USER; remote/SSH steps that need the operator's own keys run as the
invoking user (SUDO_USER).
"""
from __future__ import annotations

import getpass
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from .creds import CredentialError, Resolver, write_secret

HEADER = {
    "git_hosts.yaml": "# Git hosts. One credential source per host (see bridge/sireum_bridge/creds.py).\n"
                      "# Edited by `sireum-hub add-repo`; changes apply without a restart.\n",
    "repos.yaml": "# Repos Sireum may work on. Edited by `sireum-hub add-repo`; applied without a restart.\n",
}


# -- environment & ownership ------------------------------------------------------

class Ctx:
    def __init__(self):
        e = os.environ
        self.config = Path(e.get("SIREUM_CONFIG", "/srv/sireum/bridge"))
        self.env_file = Path(e.get("SIREUM_ENV_FILE", "/srv/sireum/secrets/bridge.env"))
        self.secrets = self.env_file.parent
        self.src = Path(e.get("SIREUM_SRC") or Path(__file__).resolve().parents[2])
        self.service_user = e.get("SIREUM_SERVICE_USER", "")
        self.is_root = os.geteuid() == 0
        self.operator = e.get("SUDO_USER") if self.is_root else None
        self.dry_run = bool(e.get("SIREUM_DRY_RUN"))

    def own(self, path: Path) -> None:
        """Give a file we wrote (as root) back to the service user."""
        if self.is_root and self.service_user:
            pw = pwd.getpwnam(self.service_user)
            os.chown(path, pw.pw_uid, pw.pw_gid)

    def as_user(self, user: str | None, argv: list[str]) -> list[str]:
        if self.is_root and user and user != "root":
            return ["sudo", "-u", user, "-H", *argv]
        return argv

    def run(self, argv: list[str], user: str | None = None, check: bool = True, **kw) -> subprocess.CompletedProcess:
        argv = self.as_user(user, argv)
        if self.dry_run:
            print("DRY-RUN:", " ".join(shlex.quote(a) for a in argv))
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(argv, check=check, **kw)


def read_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def set_env(ctx: Ctx, values: dict) -> None:
    """Set KEY=VALUE lines in bridge.env, keeping everything else (and 0600)."""
    path = ctx.env_file
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*#?\s*([A-Z_][A-Z0-9_]*)=", line)
        if m and m[1] in values and m[1] not in seen:
            lines[i] = f"{m[1]}={values[m[1]]}"
            seen.add(m[1])
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    write_secret(path, "\n".join(lines))
    ctx.own(path)


def restart_bridge(ctx: Ctx) -> None:
    if platform.system() == "Darwin":
        argv = ["sudo", "launchctl", "kickstart", "-k", "system/id.sireum.bridge"]
    else:
        argv = ["systemctl", "restart", "sireum-bridge"] if ctx.is_root else ["sudo", "systemctl", "restart",
                                                                               "sireum-bridge"]
    try:
        ctx.run(argv)
        print("bridge restarted")
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"could not restart the bridge ({e}); run: {' '.join(argv)}", file=sys.stderr)


def load_yaml(ctx: Ctx, name: str, key: str) -> dict:
    p = ctx.config / name
    doc = (yaml.safe_load(p.read_text()) if p.exists() else None) or {}
    doc.setdefault(key, [])
    if doc[key] is None:
        doc[key] = []
    return doc


def save_yaml(ctx: Ctx, name: str, doc: dict) -> None:
    p = ctx.config / name
    tmp = p.with_suffix(".tmp")
    tmp.write_text(HEADER.get(name, "") + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))
    os.replace(tmp, p)
    ctx.own(p)


# -- GitHub API --------------------------------------------------------------------

def gh_api(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def parse_repo(spec: str) -> str:
    """owner/name from 'owner/name', 'https://github.com/owner/name(.git)', or 'git@github.com:owner/name.git'."""
    s = spec.strip()
    s = re.sub(r"^(https?://[^/]+/|git@[^:]+:)", "", s)
    s = re.sub(r"\.git$", "", s).strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", s):
        raise SystemExit(f"not a repo: {spec!r} (expected owner/name or a GitHub URL)")
    return s


def auth_from_flag(ctx: Ctx, flag: str, host_id: str, alias: str | None, per_repo: bool) -> dict:
    """Turn --auth into a credential-source dict. Snapshots (gh, token) go to a 0600 file."""
    key_file = ctx.secrets / (f"{host_id}-{alias}.key" if per_repo and alias else f"{host_id}.key")
    kind, _, rest = flag.partition(":")
    if kind == "gh":
        if not shutil.which("gh"):
            raise SystemExit("`gh` not found; install GitHub CLI and `gh auth login`, or use --auth token")
        p = ctx.run(["gh", "auth", "token"], user=ctx.operator, capture_output=True, text=True, check=False)
        tok = (p.stdout or "").strip()
        if p.returncode != 0 or not tok:
            raise SystemExit("`gh auth token` failed; run `gh auth login` first")
        write_secret(key_file, tok)
        ctx.own(key_file)
        return {"api_key_file": str(key_file)}
    if kind == "token":
        tok = rest or getpass.getpass("GitHub token (input hidden): ")
        if not tok.strip():
            raise SystemExit("empty token")
        write_secret(key_file, tok)
        ctx.own(key_file)
        return {"api_key_file": str(key_file)}
    if kind == "file":
        return {"api_key_file": rest}
    if kind == "env":
        return {"api_key_env": rest}
    if kind == "cmd":
        return {"api_key_cmd": shlex.split(rest)}
    if kind == "app":
        parts = rest.split(":")
        if len(parts) < 2:
            raise SystemExit("--auth app:<app_id>:<private_key_file>[:<installation_id>]")
        app = {"app_id": int(parts[0]), "private_key_file": parts[1]}
        if len(parts) > 2 and parts[2]:
            app["installation_id"] = int(parts[2])
        return {"github_app": app}
    raise SystemExit(f"unknown --auth {flag!r}: gh | token[:VALUE] | file:PATH | env:VAR | cmd:'…' | app:ID:KEY[:INST]")


MIN_PROTECTION = {"required_status_checks": None, "enforce_admins": False,
                  "required_pull_request_reviews": None, "restrictions": None,
                  "allow_force_pushes": False, "allow_deletions": False}


def add_repo(ctx: Ctx, args) -> int:
    path = parse_repo(args.repo)
    alias = args.alias or path.split("/", 1)[1].lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,40}", alias):
        raise SystemExit(f"bad alias {alias!r}")
    host_id, api = args.host, args.api_url.rstrip("/")
    git_url = args.git_url or ("https://github.com" if "api.github.com" in api else api.split("/api/")[0])

    hosts_doc = load_yaml(ctx, "git_hosts.yaml", "hosts")
    host = next((h for h in hosts_doc["hosts"] if h.get("id") == host_id), None)
    if host and host.get("type") != "github":
        raise SystemExit(f"host {host_id!r} is type {host.get('type')}; add-repo supports GitHub hosts")
    flag = args.auth or (None if host else "gh")   # new host with no --auth ⇒ reuse your gh login
    auth = auth_from_flag(ctx, flag, host_id, alias, args.per_repo) if flag else None
    if not host:
        host = {"id": host_id, "type": "github", "api_url": api, "git_url": git_url}
        hosts_doc["hosts"].append(host)
    if auth and not args.per_repo:
        for k in ("api_key_file", "api_key_env", "api_key_cmd", "github_app"):
            host.pop(k, None)
        host.update(auth)
    effective = auth if (auth and args.per_repo) else {k: v for k, v in host.items()
                                                      if k in ("api_key_file", "api_key_env", "api_key_cmd",
                                                               "github_app", "cache_sec")}
    try:
        token = Resolver().token(effective, api, path)
    except CredentialError as e:
        raise SystemExit(f"credential check failed: {e}") from None

    code, info = gh_api("GET", f"{api}/repos/{path}", token)
    if code != 200:
        raise SystemExit(f"cannot read {path} with this credential (HTTP {code}: {info.get('message', '')})")
    branch = args.branch or info.get("default_branch") or "main"
    perms = info.get("permissions")
    if perms is not None and not perms.get("push"):
        raise SystemExit(f"this credential can't push to {path}; Sireum needs push (branches) + pull requests")

    code, _ = gh_api("GET", f"{api}/repos/{path}/branches/{urllib.parse.quote(branch, safe='')}/protection", token)
    protected = {200: True, 404: False}.get(code)  # 403 etc. ⇒ unknown (needs admin to read)
    if protected is False:
        do_protect = args.protect
        if not do_protect and not args.no_protect and sys.stdin.isatty():
            ans = input(f"`{branch}` on {path} is not protected. Protect it now (block force-push and "
                        f"deletion only; your normal workflow is unchanged)? [Y/n] ").strip().lower()
            do_protect = ans in ("", "y", "yes")
        if do_protect:
            c, r = gh_api("PUT", f"{api}/repos/{path}/branches/{urllib.parse.quote(branch, safe='')}/protection",
                          token, MIN_PROTECTION)
            if c in (200, 201):
                protected = True
                print(f"protected {branch}: force-push and deletion blocked")
            else:
                print(f"could not protect {branch} (HTTP {c}: {r.get('message', '')})", file=sys.stderr)
    enabled = bool(protected) or protected is None or args.allow_unprotected
    note = {True: "protected", None: "protection unknown (credential can't read it)",
            False: "NOT protected"}[protected]

    repos_doc = load_yaml(ctx, "repos.yaml", "repos")
    entry = next((r for r in repos_doc["repos"] if r.get("alias") == alias), None)
    if entry is None:
        entry = {"alias": alias}
        repos_doc["repos"].append(entry)
    entry.update({"git_host": host_id, "path": path, "default_branch": branch, "enabled": enabled})
    for k in ("api_key_file", "api_key_env", "api_key_cmd", "github_app"):
        entry.pop(k, None)
    if auth and args.per_repo:
        entry.update(auth)
    save_yaml(ctx, "git_hosts.yaml", hosts_doc)
    save_yaml(ctx, "repos.yaml", repos_doc)

    print(f"{'enabled' if enabled else 'added but DISABLED'}: repo:{alias} → {host_id}/{path} "
          f"(default branch {branch}, {note})")
    if not enabled:
        print(f"  Protect {branch} (or re-run with --protect / --allow-unprotected) to enable it.")
    print("  Applied without a restart. Cards use the label  repo:" + alias)
    return 0


def list_repos(ctx: Ctx, args) -> int:
    from .creds import describe
    hosts = {h["id"]: h for h in load_yaml(ctx, "git_hosts.yaml", "hosts")["hosts"]}
    for r in load_yaml(ctx, "repos.yaml", "repos")["repos"]:
        h = hosts.get(r.get("git_host"), {})
        own = {k: r[k] for k in ("api_key_file", "api_key_env", "api_key_cmd", "github_app") if r.get(k)}
        print(f"{'on ' if r.get('enabled') else 'off'}  {r.get('alias'):<16} {r.get('git_host')}/{r.get('path')}"
              f"  [{r.get('default_branch', 'main')}]  auth: {describe(own or h)}{' (repo)' if own else ''}")
    return 0


def connect_hermes(ctx: Ctx, args) -> int:
    """Install sireum-kanban on hermes-host over SSH and point the bridge at it."""
    target = args.target
    m = re.fullmatch(r"(?:([A-Za-z0-9._-]+)@)?([A-Za-z0-9._-]+)", target)
    if not m:
        raise SystemExit("target must be user@host or host")
    host = m[2]
    key = ctx.secrets / "kanban_ed25519"
    pub = (key.with_suffix(".pub")).read_text().strip() if key.with_suffix(".pub").exists() else None
    if not pub:
        raise SystemExit(f"{key}.pub missing (the hub installer creates it)")
    taggers = ",".join(t.strip() for t in args.taggers.split(",") if t.strip())
    if not taggers:
        raise SystemExit("--taggers is required: Hermes users allowed to send cards to Sireum")
    kdir = ctx.src / "kanban"
    files = [str(kdir / f) for f in ("sireum-kanban", "sireum-kanban.conf.example", "install-on-hermes-host.sh")]
    ssh_opts = ["-p", str(args.port)] if args.port else []
    remote_tmp = "/tmp/sireum-kanban-install"
    op = ctx.operator  # remote install uses the operator's own SSH access + sudo there

    print(f"==> Installing sireum-kanban on {target} (you may be asked for your sudo password there)")
    ctx.run(["ssh", *ssh_opts, target, f"rm -rf {remote_tmp} && mkdir -p {remote_tmp}"], user=op)
    ctx.run(["scp", "-q", *(["-P", str(args.port)] if args.port else []), *files, f"{target}:{remote_tmp}/"],
            user=op)
    ctx.run(["ssh", "-t", *ssh_opts, target,
             f"sudo bash {remote_tmp}/install-on-hermes-host.sh {shlex.quote(pub)}; rm -rf {remote_tmp}"], user=op)

    print("==> Testing the restricted key (accepting hermes-host's SSH host key on first use)")
    test = ["ssh", "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new", *ssh_opts, f"sireum@{host}", "list-remote"]
    p = ctx.run(test, user=ctx.service_user or None, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        print(f"list-remote failed ({p.returncode}): {(p.stderr or '').strip()[:300]}", file=sys.stderr)
        print("Most likely the hermes kanban commands in /etc/sireum-kanban.conf on hermes-host need adjusting "
              "(compare with `hermes kanban --help`). The bridge was not changed.", file=sys.stderr)
        return 1
    try:
        n = len(json.loads(p.stdout or "[]"))
        print(f"list-remote OK ({n} card(s) tagged remote right now)")
    except ValueError:
        if not ctx.dry_run:
            print("list-remote returned non-JSON; check /etc/sireum-kanban.conf on hermes-host", file=sys.stderr)
            return 1

    port = f" -p {args.port}" if args.port else ""
    source = f"kanban-ssh:sireum@{host}#{key}"
    set_env(ctx, {"SOURCE": source, "REMOTE_TAGGERS": taggers})
    if port:
        print(f"note: non-standard SSH port; add `Port {args.port}` for {host} to the service user's ~/.ssh/config")
    print(f"==> bridge.env: SOURCE={source}  REMOTE_TAGGERS={taggers}")
    restart_bridge(ctx)
    return 0


def runner_image(ctx: Ctx, args) -> int:
    from . import runnersrc
    env = read_env(ctx.env_file)
    print(env.get("RUNNER_IMAGE") or runnersrc.local_tag())
    return 0


# -- Phase 6: access requests, registries, inventory, health ----------------------

def bridge_api(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    base = os.environ.get("BRIDGE_LOCAL_URL", "http://127.0.0.1:8787").rstrip("/")
    tok = os.environ.get("OPERATOR_TOKEN", "")
    if not tok:
        raise SystemExit("OPERATOR_TOKEN not set (run through the sireum-hub wrapper)")
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except urllib.error.URLError as e:
        raise SystemExit(f"bridge not reachable at {base}: {e.reason}") from None


def needs(ctx: Ctx, args) -> int:
    code, r = bridge_api("GET", "/v1/access")
    pending = r.get("pending", [])
    if not pending:
        print("No pending access requests.")
    for p in pending:
        jobs = ", ".join(p.get("jobs") or []) or "-"
        print(f"#{p['id']}  {p['kind']:<8} {p['name']:<28} repo:{p['repo_alias']:<14} jobs: {jobs}")
        if p.get("reason"):
            print(f"      why: {p['reason']}")
    return 0


def _source_from_flag(flag: str) -> dict:
    kind, _, rest = flag.partition(":")
    if kind == "file":
        return {"api_key_file": rest}
    if kind == "env":
        return {"api_key_env": rest}
    if kind == "cmd":
        return {"api_key_cmd": shlex.split(rest)}
    raise SystemExit("--source must be file:PATH, env:VAR or cmd:'COMMAND'")


def grant(ctx: Ctx, args) -> int:
    code, r = bridge_api("GET", "/v1/access")
    req = next((p for p in r.get("pending", []) if p["id"] == args.id), None)
    if not req:
        raise SystemExit(f"no pending request #{args.id} (see: sireum-hub needs)")
    body: dict = {}
    if req["kind"] == "secret":
        if args.source:
            body.update(_source_from_flag(args.source))
        elif args.value_file:
            body["value"] = Path(args.value_file).read_text().strip()
        else:
            if not sys.stdin.isatty():
                raise SystemExit("secret value needed: use --source file:/…|env:…|cmd:'…' or --value-file")
            print(f"Secret {req['name']} for repo:{req['repo_alias']}"
                  + (f" — {req['reason']}" if req.get("reason") else ""))
            print("Paste the value (hidden), or leave empty to give a source instead.")
            v = getpass.getpass("value: ")
            if v.strip():
                body["value"] = v.strip()
            else:
                body.update(_source_from_flag(input("source (file:PATH | env:VAR | cmd:'op read …'): ").strip()))
        expose = args.expose
        if not expose and sys.stdin.isatty():
            expose = input("expose to [prepare] / test / agent: ").strip() or "prepare"
        body["expose"] = expose or "prepare"
    code, r = bridge_api("POST", f"/v1/access/{args.id}/grant", body)
    if code != 200:
        raise SystemExit(f"grant failed: {r.get('error')}")
    resumed = ", ".join(r.get("resumed") or []) or "none waiting"
    print(f"granted #{args.id}: {r['kind']} {r['name']} for repo:{r['repo']} (jobs resumed: {resumed})")
    return 0


def deny(ctx: Ctx, args) -> int:
    code, r = bridge_api("POST", f"/v1/access/{args.id}/deny", {"note": args.note})
    if code != 200:
        raise SystemExit(f"deny failed: {r.get('error')}")
    print(f"denied #{args.id}")
    return 0


def rerun(ctx: Ctx, args) -> int:
    code, r = bridge_api("POST", f"/v1/jobs/{args.job_id}/rerun", {"why": args.why})
    if code != 202:
        raise SystemExit(f"rerun failed: {r.get('error')}")
    print(f"re-running as {r['job_id']} ({r['state']})")
    return 0


def registry_add(ctx: Ctx, args) -> int:
    from .pkgrelay import ECOSYSTEMS, Registry
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,40}", args.name):
        raise SystemExit("registry name: lowercase letters, digits, . _ -")
    if args.ecosystem not in ECOSYSTEMS:
        raise SystemExit(f"--ecosystem must be one of {ECOSYSTEMS}")
    if args.ecosystem == "npm" and not args.scope:
        raise SystemExit("npm registries need at least one --scope (e.g. @company) so public packages "
                         "never resolve to the private registry")
    entry = {"name": args.name, "ecosystem": args.ecosystem, "url": args.url.rstrip("/"),
             "scopes": args.scope or [], "mode": args.mode, "auth_scheme": args.auth_scheme}
    if args.username:
        entry["username"] = args.username
    if args.auth:
        entry.update(auth_from_flag(ctx, args.auth, f"registry-{args.name}", None, False))
    Registry(**entry)  # validate
    doc = load_yaml(ctx, "registries.yaml", "registries")
    doc["registries"] = [g for g in doc["registries"] if g.get("name") != args.name] + [entry]
    HEADER["registries.yaml"] = ("# Private package registries (see bridge/sireum_bridge/pkgrelay.py).\n"
                                 "# Edited by `sireum-hub registry add`; applied without a restart.\n")
    save_yaml(ctx, "registries.yaml", doc)
    print(f"registry {args.name}: {args.ecosystem} {args.mode} {entry['url']} scopes={entry['scopes'] or 'all'}")
    print(f"  Repos opt in by listing it in .agent/tools.yaml (registries: [{args.name}]); "
          f"approve per repo with `sireum-hub grant <id>` when asked.")
    return 0


def registry_list(ctx: Ctx, args) -> int:
    from .creds import describe
    for g in load_yaml(ctx, "registries.yaml", "registries")["registries"]:
        print(f"{g['name']:<20} {g.get('ecosystem'):<8} {g.get('mode', 'relay'):<7} {g.get('url')}  "
              f"scopes={g.get('scopes') or 'all'}  auth: {describe(g)}")
    return 0


def _last_used_fn(args):
    try:
        from .access import AccessStore
        from .store import Store
        st = AccessStore(Store(str(Path(args.state) / "state.db")))
        return st.last_used
    except Exception:  # noqa: BLE001
        return None


def secrets_inventory(ctx: Ctx, args) -> int:
    from .maintenance import inventory
    items = inventory(ctx.config, _last_used_fn(args))
    if not items:
        print("No credentials configured.")
    for i in items:
        extra = f" expose={i['expose']}" if i.get("expose") else (f" mode={i['mode']}" if i.get("mode") else "")
        lu = i.get("last_used") or "never"
        print(f"{i['kind']:<9} {i['name']:<24} {i['source']:<48}{extra}  used by: "
              f"{', '.join(i['used_by']) or '-'}  last used: {lu}")
    return 0


def doctor(ctx: Ctx, args) -> int:
    from .maintenance import doctor as run
    findings = run(ctx.config, _last_used_fn(args))
    mark = {"ok": "✓", "warn": "!", "fail": "✗"}
    for f in findings:
        print(f"  {mark[f['level']]} {f['what']}: {f['detail']}")
    if not findings:
        print("  nothing to check yet (no enabled repos or registries)")
    return 1 if any(f["level"] == "fail" for f in findings) else 0


def revoke(ctx: Ctx, args) -> int:
    doc = load_yaml(ctx, "repos.yaml", "repos")
    entry = next((r for r in doc["repos"] if r.get("alias") == args.repo), None)
    if not entry:
        raise SystemExit(f"no repo {args.repo}")
    key = {"secret": "secrets", "registry": "registries", "egress": "egress"}[args.kind]
    cur = entry.get(key) or ({} if key == "secrets" else [])
    if args.name not in cur:
        raise SystemExit(f"repo {args.repo} has no {args.kind} {args.name}")
    if key == "secrets":
        spec = cur.pop(args.name)
        f = Path((spec or {}).get("api_key_file") or "")
        if f.name.endswith(".secret") and f.parent == ctx.secrets and f.exists():
            f.unlink()  # value pasted through `grant`; sources you manage yourself are left alone
    else:
        cur.remove(args.name)
    entry[key] = cur
    save_yaml(ctx, "repos.yaml", doc)
    print(f"revoked {args.kind} {args.name} from repo:{args.repo} (applies to the next job)")
    return 0


# -- Phase 7: MCP clients, proposals, Funnel ---------------------------------------

def _clients(args):
    from .mcp import ClientStore
    from .store import Store
    return ClientStore(Store(str(Path(args.state) / "state.db")))


def mcp_client(ctx: Ctx, args) -> int:
    cs = _clients(args)
    if args.mcp_cmd == "add":
        tok = cs.add(args.name, args.tier)
        print(tok)
        print(f"\nMCP client {args.name} ({args.tier}). This token is shown once; give it to the client as\n"
              f"  Authorization: Bearer <token>\nEndpoint: https://<hub>.ts.net:8443/mcp (after `sireum-hub mcp funnel on`)",
              file=sys.stderr)
    elif args.mcp_cmd == "list":
        for c in cs.list():
            print(f"{c['name']:<16} {c['tier']:<8} {'REVOKED' if c['revoked'] else 'active'}")
    elif args.mcp_cmd == "revoke":
        print("revoked" if cs.revoke(args.name) else f"no client {args.name}")
    return 0


def mcp_funnel(ctx: Ctx, args) -> int:
    port = os.environ.get("MCP_FUNNEL_PORT", "8443")
    listen = os.environ.get("MCP_LISTEN", "127.0.0.1:8790")
    if args.state_arg == "on":
        argv = ["tailscale", "funnel", "--bg", f"--https={port}", f"http://{listen}"]
        print("This makes ONLY the MCP endpoint reachable from the public internet (token required).")
    elif args.state_arg == "off":
        argv = ["tailscale", "funnel", f"--https={port}", "off"]
    else:
        argv = ["tailscale", "funnel", "status"]
    try:
        ctx.run(argv, user=ctx.operator)
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    return 0


def actions(ctx: Ctx, args) -> int:
    code, r = bridge_api("GET", "/v1/actions")
    pend = r.get("pending", [])
    if not pend:
        print("No pending proposals.")
    for a in pend:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(a["created_at"]))
        print(f"{a['code']}  {a['summary']}   (from {a['client']}, {when})")
    return 0


def confirm(ctx: Ctx, args) -> int:
    """Execute a proposed action here, as the operator, then mark it done."""
    from argparse import Namespace
    code, r = bridge_api("GET", "/v1/actions")
    act = next((a for a in r.get("pending", []) if a["code"] == args.code.upper()), None)
    if not act:
        raise SystemExit(f"no pending proposal {args.code} (see: sireum-hub actions)")
    p = act["params"]
    print(f"Confirming: {act['summary']} (proposed by {act['client']})", file=sys.stderr)
    kind = act["action"]
    if kind == "grant":
        rc = grant(ctx, Namespace(id=p["id"], source=args.source, value_file=args.value_file,
                                  expose=args.expose or p.get("expose")))
    elif kind == "deny":
        rc = deny(ctx, Namespace(id=p["id"], note=p["note"]))
    elif kind == "add_repo":
        rc = add_repo(ctx, Namespace(repo=p["repo"], alias=p.get("alias"), host="github",
                                     api_url=os.environ.get("SIREUM_GITHUB_API", "https://api.github.com"),
                                     git_url=None, auth=None, per_repo=False, branch=None, protect=False,
                                     no_protect=False, allow_unprotected=False))
    elif kind == "invite":
        from .cli import _nodes
        print(_nodes(Path(args.state)).invite(p["name"]))
        print("Join code above — send it privately; it was not shared with the MCP client.", file=sys.stderr)
        rc = 0
    elif kind == "revoke":
        rc = revoke(ctx, Namespace(repo=p["repo"], kind=p["kind"], name=p["name"]))
    elif kind == "remove_node":
        c, resp = bridge_api("DELETE", f"/v1/nodes/{p['node']}")
        if c != 200:
            raise SystemExit(f"remove failed: {resp.get('error')}")
        print(f"removed {p['node']}")
        rc = 0
    else:
        raise SystemExit(f"unknown action {kind}")
    if rc == 0:
        bridge_api("POST", f"/v1/actions/{act['code']}/done", {"result": "confirmed by the operator"})
    return rc


def reject(ctx: Ctx, args) -> int:
    code, r = bridge_api("POST", f"/v1/actions/{args.code.upper()}/reject", {})
    print("rejected" if r.get("closed") else f"no pending proposal {args.code}")
    return 0

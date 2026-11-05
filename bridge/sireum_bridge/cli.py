"""sireum-hub: operator commands on the hub.

  sireum-hub serve   [--config DIR] [--state DIR] [--listen HOST:PORT]
  sireum-hub invite  [--name PC] [--ts-auth-key KEY] [--hours 24] | invite --revoke
  sireum-hub nodes
  sireum-hub add-repo OWNER/NAME [--alias A] [--auth gh|token|file:P|env:V|cmd:'…'|app:ID:KEY]
                     [--per-repo] [--protect|--no-protect] [--allow-unprotected]
  sireum-hub repos
  sireum-hub connect-hermes USER@HERMES_HOST --taggers alice,bob [--port N]
  sireum-hub runner-image
  sireum-hub needs | grant ID [--source …|--value-file F] [--expose prepare|test|agent] | deny ID "note"
  sireum-hub rerun JOB_ID
  sireum-hub registry add NAME --ecosystem npm|pypi|go|generic --url URL [--scope S]… [--mode relay|inject]
                      [--auth …] | registry list
  sireum-hub secrets | doctor | revoke REPO secret|registry|egress NAME
  sireum-hub mcp-client add NAME --tier read|operate | mcp-client list | mcp-client revoke NAME
  sireum-hub mcp funnel on|off|status
  sireum-hub actions | confirm CODE [--source …|--value-file F] [--expose …] | reject CODE
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .apis import Nomad, Tailscale
from .nodes import Nodes
from .store import Store

COMMANDS = ("invite", "nodes", "add-repo", "repos", "connect-hermes", "runner-image", "needs", "grant", "deny",
            "rerun", "registry", "secrets", "doctor", "revoke", "mcp-client", "mcp", "actions", "confirm", "reject")
ADMIN = ("add-repo", "repos", "connect-hermes", "runner-image", "needs", "grant", "deny", "rerun", "registry",
         "secrets", "doctor", "revoke", "mcp-client", "mcp", "actions", "confirm", "reject")


def _nodes(state: Path) -> Nodes:
    env = os.environ
    hub = env.get("BRIDGE_PUBLIC_URL")
    if not hub:
        raise SystemExit("BRIDGE_PUBLIC_URL must be set (the https://<hub>.ts.net URL workers use)")
    nomad = Nomad(env["NOMAD_ADDR"], env.get("NOMAD_TOKEN")) if env.get("NOMAD_ADDR") else None
    ts = (Tailscale(env["TS_OAUTH_CLIENT_ID"], env["TS_OAUTH_CLIENT_SECRET"], env.get("TS_TAILNET", "-"))
          if env.get("TS_OAUTH_CLIENT_ID") and env.get("TS_OAUTH_CLIENT_SECRET") else None)
    state.mkdir(parents=True, exist_ok=True)
    return Nodes(Store(str(state / "state.db")), state, nomad, ts, hub.rstrip("/"), {})


def _fix_state_owner(state: Path) -> None:
    """When run as root (Linux hub), SQLite side files we create must stay writable by the bridge."""
    user = os.environ.get("SIREUM_SERVICE_USER")
    if os.geteuid() != 0 or not user:
        return
    import pwd
    pw = pwd.getpwnam(user)
    for p in [state, *state.glob("state.db*"), *(state / "seats").glob("*")]:
        try:
            os.chown(p, pw.pw_uid, pw.pw_gid)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(a in COMMANDS for a in argv):
        from .server import main as serve
        return serve([a for a in argv if a != "serve"])
    ap = argparse.ArgumentParser(prog="sireum-hub")
    ap.add_argument("--state", type=Path, default=Path(os.environ.get("SIREUM_STATE", "/srv/sireum/state")))
    sub = ap.add_subparsers(dest="cmd", required=True)
    inv = sub.add_parser("invite", help="print a one-time join code for a new worker PC")
    inv.add_argument("--name", help="node name (default: from the PC's hostname)")
    inv.add_argument("--ts-auth-key", help="Tailscale auth key to embed (default: mint one if an OAuth client "
                                           "is configured, else the worker logs in interactively)")
    inv.add_argument("--hours", type=int, default=24)
    inv.add_argument("--revoke", action="store_true", help="revoke all unused join codes")
    sub.add_parser("nodes", help="list enrolled worker PCs")
    ar = sub.add_parser("add-repo", help="onboard a GitHub repo (applied without a restart)")
    ar.add_argument("repo", help="owner/name or a GitHub URL")
    ar.add_argument("--alias", help="label used on cards: repo:<alias> (default: repo name)")
    ar.add_argument("--host", default="github", help="git host id in git_hosts.yaml (default: github)")
    ar.add_argument("--api-url", default="https://api.github.com")
    ar.add_argument("--git-url", help="default: https://github.com")
    ar.add_argument("--auth", help="gh (default for a new host) | token[:VALUE] | file:PATH | env:VAR | "
                                   "cmd:'COMMAND' | app:APP_ID:KEY_FILE[:INSTALLATION_ID]")
    ar.add_argument("--per-repo", action="store_true", help="store the credential on this repo, not the host")
    ar.add_argument("--branch", help="default: the repo's default branch")
    g = ar.add_mutually_exclusive_group()
    g.add_argument("--protect", action="store_true", help="protect the default branch if it isn't")
    g.add_argument("--no-protect", action="store_true", help="never touch branch protection")
    ar.add_argument("--allow-unprotected", action="store_true", help="enable even if the branch is unprotected")
    sub.add_parser("repos", help="list onboarded repos and their credential sources")
    ch = sub.add_parser("connect-hermes", help="install sireum-kanban on hermes-host and use it as the task source")
    ch.add_argument("target", help="user@hermes-host (your own SSH access there)")
    ch.add_argument("--taggers", required=True, help="Hermes users/profiles allowed to opt cards in (matches created_by)")
    ch.add_argument("--port", type=int)
    ch.add_argument("--kanban-user", help="user the forced command runs as (default: target's user)")
    ch.add_argument("--hermes-bin", help="path to the hermes CLI on hermes-host")
    ch.add_argument("--hermes-home", help="HERMES_HOME on hermes-host")
    ch.add_argument("--local", action="store_true", help="install under the target user's home (no sudo)")
    sub.add_parser("runner-image", help="print the runner image tag workers must have")
    sub.add_parser("needs", help="list pending access requests")
    gr = sub.add_parser("grant", help="approve an access request (and resume/re-run its jobs)")
    gr.add_argument("id", type=int)
    gr.add_argument("--source", help="secret source: file:PATH | env:VAR | cmd:'COMMAND'")
    gr.add_argument("--value-file", help="read the secret value from this file (stored 0600 on the hub)")
    gr.add_argument("--expose", choices=("prepare", "test", "agent"),
                    help="phases that see the secret (default prepare; test includes prepare; agent includes all)")
    dn = sub.add_parser("deny", help="refuse an access request")
    dn.add_argument("id", type=int)
    dn.add_argument("note", nargs="?", default="no reason given")
    rr = sub.add_parser("rerun", help="run a finished job again (new job id)")
    rr.add_argument("job_id")
    rr.add_argument("--why", default="re-run requested by the operator")
    rg = sub.add_parser("registry", help="private package registries")
    rgs = rg.add_subparsers(dest="reg_cmd", required=True)
    ra = rgs.add_parser("add")
    ra.add_argument("name")
    ra.add_argument("--ecosystem", required=True)
    ra.add_argument("--url", required=True, help="registry base (PyPI: the /simple index URL)")
    ra.add_argument("--scope", action="append", help="npm scope / PyPI name prefix / Go module prefix (repeat)")
    ra.add_argument("--mode", choices=("relay", "inject"), default="relay")
    ra.add_argument("--auth", help="gh | token[:VALUE] | file:PATH | env:VAR | cmd:'COMMAND'")
    ra.add_argument("--auth-scheme", choices=("bearer", "basic", "token"), default="bearer")
    ra.add_argument("--username", help="for --auth-scheme basic (default __token__)")
    rgs.add_parser("list")
    sub.add_parser("secrets", help="inventory of credentials (never values)")
    sub.add_parser("doctor", help="check every credential")
    rv = sub.add_parser("revoke", help="remove an approval from a repo")
    rv.add_argument("repo")
    rv.add_argument("kind", choices=("secret", "registry", "egress"))
    rv.add_argument("name")
    mc = sub.add_parser("mcp-client", help="MCP clients (e.g. Hermes)")
    mcs = mc.add_subparsers(dest="mcp_cmd", required=True)
    mca = mcs.add_parser("add")
    mca.add_argument("name")
    mca.add_argument("--tier", choices=("read", "operate"), default="read")
    mcs.add_parser("list")
    mcr = mcs.add_parser("revoke")
    mcr.add_argument("name")
    mf = sub.add_parser("mcp", help="publish the MCP endpoint")
    mfs = mf.add_subparsers(dest="mcp_sub", required=True)
    mff = mfs.add_parser("funnel", help="Tailscale Funnel for /mcp only")
    mff.add_argument("state_arg", choices=("on", "off", "status"))
    sub.add_parser("actions", help="sensitive actions proposed via MCP, waiting for you")
    cf = sub.add_parser("confirm", help="carry out a proposed action")
    cf.add_argument("code")
    cf.add_argument("--source")
    cf.add_argument("--value-file")
    cf.add_argument("--expose", choices=("prepare", "test", "agent"))
    rj = sub.add_parser("reject", help="refuse a proposed action")
    rj.add_argument("code")
    args = ap.parse_args(argv)

    if args.cmd in ADMIN:
        from . import admin
        ctx = admin.Ctx()
        if args.cmd == "registry":
            fn = admin.registry_add if args.reg_cmd == "add" else admin.registry_list
        else:
            fn = {"add-repo": admin.add_repo, "repos": admin.list_repos, "connect-hermes": admin.connect_hermes,
                  "runner-image": admin.runner_image, "needs": admin.needs, "grant": admin.grant,
                  "deny": admin.deny, "rerun": admin.rerun, "secrets": admin.secrets_inventory,
                  "doctor": admin.doctor, "revoke": admin.revoke, "mcp-client": admin.mcp_client,
                  "mcp": admin.mcp_funnel, "actions": admin.actions, "confirm": admin.confirm,
                  "reject": admin.reject}[args.cmd]
        try:
            rc = fn(ctx, args)
        finally:
            _fix_state_owner(args.state)
        raise SystemExit(rc)

    nodes = _nodes(args.state)
    try:
        if args.cmd == "invite":
            if args.revoke:
                print(f"revoked {nodes.revoke_invites()} unused join code(s)")
                return
            code = nodes.invite(args.name, args.ts_auth_key, args.hours * 3600)
            print(code)
            how = "embedded Tailscale key" if (args.ts_auth_key or nodes.ts) else \
                "no Tailscale key: the worker installer shows a login link to click"
            print(f"\nSingle use, expires in {args.hours} h ({how}). Treat it like a password.", file=sys.stderr)
        elif args.cmd == "nodes":
            for n in nodes.list():
                print(json.dumps({k: n[k] for k in ("name", "host_os", "arch", "cpus", "mem_mb")}))
    finally:
        _fix_state_owner(args.state)

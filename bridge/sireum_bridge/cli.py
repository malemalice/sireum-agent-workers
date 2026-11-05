"""sireum-hub: operator commands on the hub.

  sireum-hub serve   [--config DIR] [--state DIR] [--listen HOST:PORT]
  sireum-hub invite  [--name PC] [--ts-auth-key KEY] [--hours 24]
  sireum-hub invite --revoke
  sireum-hub nodes
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


def _nodes(state: Path) -> Nodes:
    env = os.environ
    hub = env.get("BRIDGE_PUBLIC_URL")
    if not hub:
        raise SystemExit("BRIDGE_PUBLIC_URL must be set (the https://sireum-hub.<tailnet>.ts.net URL workers use)")
    nomad = Nomad(env["NOMAD_ADDR"], env.get("NOMAD_TOKEN")) if env.get("NOMAD_ADDR") else None
    ts = (Tailscale(env["TS_OAUTH_CLIENT_ID"], env["TS_OAUTH_CLIENT_SECRET"], env.get("TS_TAILNET", "-"))
          if env.get("TS_OAUTH_CLIENT_ID") and env.get("TS_OAUTH_CLIENT_SECRET") else None)
    state.mkdir(parents=True, exist_ok=True)
    return Nodes(Store(str(state / "state.db")), state, nomad, ts, hub.rstrip("/"), {})


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(a in ("invite", "nodes") for a in argv):
        from .server import main as serve
        return serve([a for a in argv if a != "serve"])
    ap = argparse.ArgumentParser(prog="sireum-hub")
    ap.add_argument("--state", type=Path, default=Path(os.environ.get("SIREUM_STATE", "/srv/sireum/state")))
    sub = ap.add_subparsers(dest="cmd", required=True)
    inv = sub.add_parser("invite", help="print a one-time join code for a new worker PC")
    inv.add_argument("--name", help="node name (default: from the PC's hostname)")
    inv.add_argument("--ts-auth-key", help="use this Tailscale auth key instead of minting one")
    inv.add_argument("--hours", type=int, default=24)
    inv.add_argument("--revoke", action="store_true", help="revoke all unused join codes")
    sub.add_parser("nodes", help="list enrolled worker PCs")
    args = ap.parse_args(argv)
    nodes = _nodes(args.state)
    if args.cmd == "invite":
        if args.revoke:
            print(f"revoked {nodes.revoke_invites()} unused join code(s)")
            return
        code = nodes.invite(args.name, args.ts_auth_key, args.hours * 3600)
        print(code)
        print(f"\nSingle use, expires in {args.hours} h. Treat it like a password.", file=sys.stderr)
    elif args.cmd == "nodes":
        for n in nodes.list():
            print(json.dumps({k: n[k] for k in ("name", "host_os", "arch", "cpus", "mem_mb")}))

"""Worker enrollment: join codes, enroll, seat tokens, leave.

Join code = `sireum1.` + base64url(JSON {hub, ts_auth_key, enroll_token, expires}).
The enroll token is single use and stored only as a hash. Seat tokens go to
Nomad Variables (or, without Nomad, a 0600 file on the hub) — never to the PC.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

from .apis import APIError, Nomad, Tailscale
from .store import Store

NAME_RE = re.compile(r"[^a-z0-9-]+")
SEAT_PATH = "nomad/jobs/sireum-runner/seat-{name}"


def h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def encode_join(d: dict) -> str:
    return "sireum1." + base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_join(code: str) -> dict:
    p = code.split(".", 1)[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


class Nodes:
    def __init__(self, store: Store, state_dir: Path, nomad: Nomad | None, tailscale: Tailscale | None,
                 hub_url: str, settings: dict):
        self.store, self.nomad, self.ts = store, nomad, tailscale
        self.hub_url = hub_url
        self.settings = settings  # nomad_servers, nomad_version, runner_image, squid_image
        self.seats_dir = state_dir / "seats"
        self.seats_dir.mkdir(mode=0o700, exist_ok=True)

    # -- operator (CLI on the hub) -----------------------------------------
    def invite(self, name: str | None = None, ts_auth_key: str | None = None, ttl: int = 86400) -> str:
        token = secrets.token_urlsafe(32)
        key_id = None
        if not ts_auth_key and self.ts:
            key_id, ts_auth_key = self.ts.create_auth_key(description=f"sireum {name or 'worker'}")
        # No key and no OAuth client: the worker installer runs an interactive `tailscale up`
        # and the person at the PC clicks the login link.
        expires = time.time() + ttl
        with self.store.lock:
            self.store.db.execute("INSERT INTO enroll_tokens(token_hash, name, expires_at, ts_key_id) VALUES (?,?,?,?)",
                                  (h(token), self._clean(name) if name else None, expires, key_id))
        return encode_join({"hub": self.hub_url, "ts_auth_key": ts_auth_key or "", "enroll_token": token,
                            "expires": datetime.fromtimestamp(expires).astimezone().isoformat(timespec="minutes")})

    def revoke_invites(self) -> int:
        rows = self.store.db.execute(
            "SELECT * FROM enroll_tokens WHERE used=0 AND revoked=0 AND expires_at > ?", (time.time(),)).fetchall()
        for r in rows:
            if r["ts_key_id"] and self.ts:
                try:
                    self.ts.delete_auth_key(r["ts_key_id"])
                except APIError:
                    pass
        with self.store.lock:
            self.store.db.execute("UPDATE enroll_tokens SET revoked=1 WHERE used=0")
        return len(rows)

    def list(self) -> list[dict]:
        return [dict(r) | {"secret_hash": None} for r in
                self.store.db.execute("SELECT * FROM nodes WHERE revoked=0 ORDER BY created_at")]

    # -- API ---------------------------------------------------------------
    @staticmethod
    def _clean(s: str) -> str:
        return NAME_RE.sub("-", s.lower()).strip("-")[:40] or "pc"

    def enroll(self, token: str, info: dict) -> tuple[int, dict]:
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM enroll_tokens WHERE token_hash=?", (h(token),)).fetchone()
            if not row or row["used"] or row["revoked"] or row["expires_at"] < time.time():
                return 401, {"error": "join code invalid, used, revoked or expired"}
            self.store.db.execute("UPDATE enroll_tokens SET used=1 WHERE token_hash=?", (h(token),))
            base = row["name"] or f"sireum-{self._clean(str(info.get('hostname', 'pc')))}"
            name, n = base, 1
            while self.store.db.execute("SELECT 1 FROM nodes WHERE name=?", (name,)).fetchone():
                n += 1
                name = f"{base}-{n}"
            secret = secrets.token_urlsafe(32)
            # Nomad can't scope node:write to one node, so by default every PC gets the same
            # shared token (NOMAD_NODE_TOKEN). NOMAD_MINT_NODE_TOKENS=1 mints one per PC instead
            # (revocable per PC, but the bridge then needs a management token).
            accessor, nomad_token = None, os.environ.get("NOMAD_NODE_TOKEN", "")
            if self.nomad and os.environ.get("NOMAD_MINT_NODE_TOKENS") == "1":
                accessor, nomad_token = self.nomad.acl_token_create(f"sireum-node-{name}", ["sireum-worker-node"])
            self.store.db.execute(
                "INSERT INTO nodes(name, host_os, arch, hostname, cpus, mem_mb, secret_hash, nomad_accessor,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (name, str(info.get("host_os", ""))[:16], str(info.get("arch", ""))[:16],
                 str(info.get("hostname", ""))[:64], int(info.get("cpus") or 0), int(info.get("mem_mb") or 0),
                 h(secret), accessor, time.time()))
        return 200, {"node_name": name, "node_secret": secret, "nomad_client_token": nomad_token,
                     **{k: self.settings.get(k) for k in
                        ("nomad_servers", "nomad_version", "runner_image", "squid_image", "registry",
                         "registry_auth")}}

    def auth_node(self, name: str, secret: str) -> bool:
        row = self.store.db.execute("SELECT secret_hash FROM nodes WHERE name=? AND revoked=0", (name,)).fetchone()
        return bool(row) and hmac.compare_digest(row["secret_hash"], h(secret))

    def put_seats(self, name: str, body: dict) -> tuple[int, dict]:
        items = {k: str(body[k]).strip() for k in ("claude_token", "opencode_token")
                 if isinstance(body.get(k), str) and body[k].strip()}
        if not items:
            return 400, {"error": "no tokens given"}
        if self.nomad:
            self.nomad.var_put(SEAT_PATH.format(name=name), items)
        else:
            path = self.seats_dir / f"{name}.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(items, f)
        return 200, {"saved": sorted(items)}

    def leave(self, name: str) -> tuple[int, dict]:
        row = self.store.db.execute("SELECT * FROM nodes WHERE name=?", (name,)).fetchone()
        if not row:
            return 404, {"error": "unknown node"}
        if self.nomad:
            self.nomad.var_delete(SEAT_PATH.format(name=name))
            if row["nomad_accessor"]:
                self.nomad.acl_token_delete(row["nomad_accessor"])
        (self.seats_dir / f"{name}.json").unlink(missing_ok=True)
        with self.store.lock:
            self.store.db.execute("UPDATE nodes SET revoked=1 WHERE name=?", (name,))
        return 200, {"removed": name}

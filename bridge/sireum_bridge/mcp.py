"""Hub MCP server (Phase 7): lets Hermes (or any MCP client) help operate the hub.

Transport: MCP streamable HTTP, JSON responses, on a separate listener that serves
ONLY /mcp (default 127.0.0.1:8790). Publish it with Tailscale Funnel:
    tailscale funnel --bg --https=8443 http://127.0.0.1:8790
The bridge API (/v1/*, /git, /pkg) is never reachable through this listener.

Clients: `sireum-hub mcp-client add hermes --tier operate` (token shown once, stored
hashed). Tiers:
  read     — status, runs, run_detail, nodes, needs, doctor, secrets_inventory
  operate  — read + pause/resume executor, drain/undrain node, cancel/rerun job,
             release card, refresh mirror, and propose_* (sensitive actions)
Sensitive actions (grant/deny access, add repo, invite, revoke, remove node) are only
*proposed*: the hub records a pending action and the operator runs
`sireum-hub confirm <code>`. Secret values and join codes never pass through MCP.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler

log = logging.getLogger("sireum-bridge.mcp")

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
TIERS = ("read", "operate")
RATE_PER_MIN = int(os.environ.get("MCP_RATE_PER_MIN", "60"))
FAIL_LIMIT, FAIL_WINDOW, BLOCK_SEC = 10, 600, 600

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_clients (
  name        TEXT PRIMARY KEY,
  token_hash  TEXT NOT NULL,
  tier        TEXT NOT NULL,
  created_at  REAL NOT NULL,
  revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending_actions (
  code        TEXT PRIMARY KEY,
  client      TEXT NOT NULL,
  action      TEXT NOT NULL,
  params      TEXT NOT NULL,
  summary     TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending',   -- pending | done | rejected | expired
  result      TEXT,
  created_at  REAL NOT NULL,
  decided_at  REAL
);
CREATE TABLE IF NOT EXISTS mcp_log (
  at REAL NOT NULL, client TEXT, tool TEXT, ok INTEGER, detail TEXT
);
"""
ACTION_TTL = 24 * 3600


def _h(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


class ClientStore:
    def __init__(self, store):
        self.s = store
        store.db.executescript(SCHEMA)

    def add(self, name: str, tier: str) -> str:
        if tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
            raise ValueError("client name: lowercase letters, digits, - _")
        tok = "smcp_" + secrets.token_urlsafe(32)
        with self.s.lock:
            self.s.db.execute("INSERT OR REPLACE INTO mcp_clients VALUES (?,?,?,?,0)", (name, _h(tok), tier, time.time()))
        return tok

    def revoke(self, name: str) -> bool:
        with self.s.lock:
            return self.s.db.execute("UPDATE mcp_clients SET revoked=1 WHERE name=?", (name,)).rowcount > 0

    def list(self) -> list[dict]:
        return [{"name": r["name"], "tier": r["tier"], "revoked": bool(r["revoked"])}
                for r in self.s.db.execute("SELECT * FROM mcp_clients ORDER BY name")]

    def auth(self, tok: str):
        if not tok:
            return None
        th = _h(tok)
        for r in self.s.db.execute("SELECT * FROM mcp_clients WHERE revoked=0"):
            if hmac.compare_digest(r["token_hash"], th):
                return r
        return None

    def log(self, client: str, tool: str, ok: bool, detail: str = "") -> None:
        with self.s.lock:
            self.s.db.execute("INSERT INTO mcp_log VALUES (?,?,?,?,?)", (time.time(), client, tool, int(ok), detail[:300]))

    # -- pending actions --
    def propose(self, client: str, action: str, params: dict, summary: str) -> str:
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        with self.s.lock:
            self.s.db.execute("INSERT INTO pending_actions(code, client, action, params, summary, created_at)"
                              " VALUES (?,?,?,?,?,?)", (code, client, action, json.dumps(params), summary, time.time()))
        return code

    def actions(self, status: str = "pending") -> list[dict]:
        now = time.time()
        with self.s.lock:
            self.s.db.execute("UPDATE pending_actions SET status='expired' WHERE status='pending' AND created_at < ?",
                              (now - ACTION_TTL,))
        return [dict(r) | {"params": json.loads(r["params"])}
                for r in self.s.db.execute("SELECT * FROM pending_actions WHERE status=? ORDER BY created_at", (status,))]

    def action(self, code: str):
        r = self.s.db.execute("SELECT * FROM pending_actions WHERE code=?", (code.upper(),)).fetchone()
        return (dict(r) | {"params": json.loads(r["params"])}) if r else None

    def close(self, code: str, status: str, result: str = "") -> bool:
        with self.s.lock:
            return self.s.db.execute("UPDATE pending_actions SET status=?, result=?, decided_at=? "
                                     "WHERE code=? AND status='pending'",
                                     (status, result[:1000], time.time(), code.upper())).rowcount > 0


# -- tools ---------------------------------------------------------------------------

def _s(desc, props=None, required=()):
    return {"description": desc, "inputSchema": {"type": "object", "properties": props or {},
                                                 "required": list(required), "additionalProperties": False}}


STR = {"type": "string"}
INT = {"type": "integer"}

TOOLS = {
    # read
    "status": ("read", _s("Hub overview: executors (running, limits, pauses), active jobs, nodes, pending access.")),
    "runs": ("read", _s("Recent jobs with state and result status.", {"limit": INT})),
    "run_detail": ("read", _s("One job: state, result (summary, tests, MR link, error), what it was granted (names "
                              "only), open access requests.", {"job_id": STR}, ["job_id"])),
    "nodes": ("read", _s("Enrolled worker PCs and their Nomad status (ready, drain, eligibility).")),
    "needs": ("read", _s("Pending access requests (secrets, registries, egress hosts) waiting for the operator.")),
    "doctor": ("read", _s("Check every credential: failures, tokens expiring within 7 days, unused secrets.")),
    "secrets_inventory": ("read", _s("Every credential the hub uses: kind, name, source type, repos, last use. "
                                     "Never values.")),
    # operate
    "pause_executor": ("operate", _s("Stop starting new jobs on an executor for N minutes (default 60).",
                                     {"executor": {"type": "string", "enum": ["claude", "opencode"]}, "minutes": INT},
                                     ["executor"])),
    "resume_executor": ("operate", _s("Resume a paused executor.",
                                      {"executor": {"type": "string", "enum": ["claude", "opencode"]}}, ["executor"])),
    "drain_node": ("operate", _s("Stop sending new jobs to a worker PC; running jobs get N minutes (default 10).",
                                 {"node": STR, "deadline_minutes": INT}, ["node"])),
    "undrain_node": ("operate", _s("Let a drained worker PC take jobs again.", {"node": STR}, ["node"])),
    "cancel_job": ("operate", _s("Cancel an active job; its card goes back to Hermes.",
                                 {"job_id": STR, "reason": STR}, ["job_id"])),
    "rerun_job": ("operate", _s("Run a finished job again as a new job id.", {"job_id": STR, "why": STR}, ["job_id"])),
    "release_card": ("operate", _s("Hand a kanban card back to Hermes (removes `remote`).",
                                   {"card_id": STR, "note": STR}, ["card_id"])),
    "refresh_mirror": ("operate", _s("Fetch the latest commits of an onboarded repo into the hub mirror.",
                                     {"repo": STR}, ["repo"])),
    # sensitive → propose only
    "propose_grant": ("operate", _s("PROPOSE approving an access request. The operator confirms on the hub "
                                    "(and supplies any secret value there).",
                                    {"request_id": INT, "expose": {"type": "string",
                                                                   "enum": ["prepare", "test", "agent"]},
                                     "why": STR}, ["request_id"])),
    "propose_deny": ("operate", _s("PROPOSE refusing an access request.", {"request_id": INT, "note": STR},
                                   ["request_id", "note"])),
    "propose_add_repo": ("operate", _s("PROPOSE onboarding a GitHub repo (owner/name).", {"repo": STR, "alias": STR,
                                                                                       "why": STR}, ["repo"])),
    "propose_invite": ("operate", _s("PROPOSE creating a join code for a new worker PC. The code is shown only to "
                                     "the operator.", {"name": STR, "why": STR}, ["name"])),
    "propose_revoke": ("operate", _s("PROPOSE removing an approval from a repo.",
                                     {"repo": STR, "kind": {"type": "string", "enum": ["secret", "registry", "egress"]},
                                      "name": STR, "why": STR}, ["repo", "kind", "name"])),
    "propose_remove_node": ("operate", _s("PROPOSE removing a worker PC (deletes its seat tokens on the hub).",
                                          {"node": STR, "why": STR}, ["node"])),
}


class Tools:
    def __init__(self, bridge):
        self.b = bridge
        self.clients = ClientStore(bridge.store)

    def list_for(self, tier: str) -> list[dict]:
        allowed = TIERS[: TIERS.index(tier) + 1]
        return [{"name": n, **spec} for n, (t, spec) in TOOLS.items() if t in allowed]

    def call(self, client, name: str, args: dict) -> dict:
        if name not in TOOLS:
            raise ToolError(f"unknown tool {name}")
        need = TOOLS[name][0]
        if TIERS.index(client["tier"]) < TIERS.index(need):
            raise ToolError(f"tool {name} needs tier {need}; this client is {client['tier']}")
        return getattr(self, "t_" + name)(client, args or {})

    # -- read --
    def t_status(self, c, a):
        return self.b.status()

    def t_runs(self, c, a):
        n = max(1, min(int(a.get("limit") or 20), 100))
        out = []
        for r in self.b.store.recent_jobs(n):
            res = json.loads(r["result"]) if r["result"] else {}
            out.append({"job_id": r["job_id"], "repo": r["repo_alias"], "state": r["state"], "executor": r["executor"],
                        "status": res.get("status"), "mr_url": res.get("mr_url"), "card": r["card_id"]})
        return {"runs": out}

    def t_run_detail(self, c, a):
        v = self.b.run_view(str(a["job_id"]))
        if v is None:
            raise ToolError("unknown job")
        return v

    def t_nodes(self, c, a):
        nodes = [{k: n[k] for k in ("name", "host_os", "arch", "cpus", "mem_mb")} for n in self.b.nodes.list()]
        if self.b.nomad:
            try:
                st = {n["Name"]: n for n in self.b.nomad.nodes()}
                for n in nodes:
                    s = st.get(n["name"]) or {}
                    n.update(status=s.get("Status"), drain=s.get("Drain"), eligibility=s.get("SchedulingEligibility"))
            except Exception as e:  # noqa: BLE001
                return {"nodes": nodes, "nomad_error": str(e)}
        return {"nodes": nodes}

    def t_needs(self, c, a):
        return {"pending": [{k: r[k] for k in ("id", "repo_alias", "kind", "name", "reason", "jobs", "message")}
                            for r in self.b.list_requests()]}

    def t_doctor(self, c, a):
        from .maintenance import doctor
        return {"findings": doctor(self.b.config_dir, self.b.access.last_used, self.b.relay.creds)}

    def t_secrets_inventory(self, c, a):
        from .maintenance import inventory
        return {"items": inventory(self.b.config_dir, self.b.access.last_used)}

    # -- operate --
    def _notify(self, c, text):
        self.b.discord(f"Sireum (via MCP client `{c['name']}`): {text}")

    def t_pause_executor(self, c, a):
        mins = max(1, min(int(a.get("minutes") or 60), 24 * 60))
        self.b.store.pause(a["executor"], time.time() + mins * 60, from_usage_limit=False)
        self._notify(c, f"paused {a['executor']} for {mins} min")
        return self.b.status()["executors"][a["executor"]]

    def t_resume_executor(self, c, a):
        self.b.store.resume(a["executor"])
        self._notify(c, f"resumed {a['executor']}")
        return self.b.status()["executors"][a["executor"]]

    def _drain(self, c, node, deadline):
        if not self.b.nomad:
            raise ToolError("Nomad is not configured on this hub")
        try:
            self.b.nomad.drain(node, deadline)
        except Exception as e:  # noqa: BLE001
            raise ToolError(str(e)) from None

    def t_drain_node(self, c, a):
        mins = max(1, min(int(a.get("deadline_minutes") or 10), 240))
        self._drain(c, a["node"], mins * 60)
        self._notify(c, f"draining node {a['node']} ({mins} min deadline)")
        return {"node": a["node"], "draining": True}

    def t_undrain_node(self, c, a):
        self._drain(c, a["node"], None)
        self._notify(c, f"node {a['node']} takes jobs again")
        return {"node": a["node"], "draining": False}

    def t_cancel_job(self, c, a):
        row = self.b.store.job(str(a["job_id"]))
        if not row:
            raise ToolError("unknown job")
        if row["state"] == "finished":
            raise ToolError("job already finished")
        job = json.loads(row["job"])
        if self.b.nomad and job.get("dispatch_id"):
            try:
                self.b.nomad.stop(job["dispatch_id"])
            except Exception as e:  # noqa: BLE001
                log.warning("nomad stop %s: %s", job["dispatch_id"], e)
        lease = self.b.store.latest_lease(row["job_id"])
        if lease:
            self.b.store.release(lease["lease_id"])
        reason = str(a.get("reason") or "cancelled by the operator")[:300]
        from .server import now_iso
        self.b.finish(row["job_id"], {"job_id": row["job_id"], "status": "cancelled", "executor": row["executor"],
                                      "finished_at": now_iso(),
                                      "error": {"code": "operator", "reset_at": None, "message": reason}})
        self._notify(c, f"cancelled {row['job_id']}: {reason}")
        return {"cancelled": row["job_id"]}

    def t_rerun_job(self, c, a):
        code, resp = self.b.rerun(str(a["job_id"]), str(a.get("why") or f"re-run requested via {c['name']}"))
        if code != 202:
            raise ToolError(resp.get("error", f"HTTP {code}"))
        self._notify(c, f"re-running {a['job_id']} as {resp['job_id']}")
        return resp

    def t_release_card(self, c, a):
        if not self.b.booster:
            raise ToolError("no task source configured")
        cid = str(a["card_id"])
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", cid):
            raise ToolError("invalid card id")
        self.b.booster.give_back(cid, str(a.get("note") or f"released via {c['name']}")[:300])
        self.b.booster.flush_outbox()
        return {"released": cid}

    def t_refresh_mirror(self, c, a):
        repo = self.b.resolve_repo(str(a["repo"]))
        if repo is None:
            raise ToolError("repo not onboarded or disabled")
        self.b.relay.fetch(repo)
        return {"repo": repo.alias, "refreshed": True}

    # -- propose --
    def _propose(self, c, action, params, summary):
        code = self.clients.propose(c["name"], action, params, summary)
        self.b.discord(f"Sireum: `{c['name']}` proposes **{summary}**.\nConfirm on the hub: `sireum-hub confirm {code}`"
                       f"  ·  reject: `sireum-hub reject {code}`")
        log.info("mcp %s proposed %s (%s): %s", c["name"], action, code, summary)
        return {"proposed": summary, "code": code, "status": "pending operator confirmation",
                "how": f"The operator runs `sireum-hub confirm {code}` on the hub. Nothing changes until then."}

    def t_propose_grant(self, c, a):
        r = self.b.access.get(int(a["request_id"]))
        if not r or r["status"] != "pending":
            raise ToolError("no pending access request with that id")
        expose = a.get("expose") or "prepare"
        return self._propose(c, "grant", {"id": r["id"], "expose": expose},
                             f"grant {r['kind']} {r['name']} to repo:{r['repo_alias']}"
                             + (f" (expose {expose})" if r["kind"] == "secret" else "")
                             + (f" — {a['why']}" if a.get("why") else ""))

    def t_propose_deny(self, c, a):
        r = self.b.access.get(int(a["request_id"]))
        if not r or r["status"] != "pending":
            raise ToolError("no pending access request with that id")
        return self._propose(c, "deny", {"id": r["id"], "note": str(a["note"])[:300]},
                             f"deny {r['kind']} {r['name']} for repo:{r['repo_alias']}: {a['note']}")

    def t_propose_add_repo(self, c, a):
        from .admin import parse_repo
        try:
            path = parse_repo(str(a["repo"]))
        except SystemExit as e:
            raise ToolError(str(e)) from None
        params = {"repo": path, **({"alias": str(a["alias"])} if a.get("alias") else {})}
        return self._propose(c, "add_repo", params, f"onboard repo {path}" + (f" — {a['why']}" if a.get("why") else ""))

    def t_propose_invite(self, c, a):
        name = re.sub(r"[^a-z0-9-]+", "-", str(a["name"]).lower()).strip("-")[:40] or "pc"
        return self._propose(c, "invite", {"name": name}, f"create a join code for worker {name}"
                             + (f" — {a['why']}" if a.get("why") else ""))

    def t_propose_revoke(self, c, a):
        return self._propose(c, "revoke", {"repo": str(a["repo"]), "kind": a["kind"], "name": str(a["name"])},
                             f"revoke {a['kind']} {a['name']} from repo:{a['repo']}"
                             + (f" — {a['why']}" if a.get("why") else ""))

    def t_propose_remove_node(self, c, a):
        return self._propose(c, "remove_node", {"node": str(a["node"])}, f"remove worker {a['node']}"
                             + (f" — {a['why']}" if a.get("why") else ""))


class ToolError(Exception):
    pass


# -- HTTP (streamable HTTP, JSON responses only) ---------------------------------------

class Limiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls: dict[str, list[float]] = {}
        self.fails: dict[str, list[float]] = {}

    def blocked(self, source: str) -> bool:
        now = time.time()
        with self.lock:
            f = [t for t in self.fails.get(source, []) if t > now - FAIL_WINDOW]
            self.fails[source] = f
            return len(f) >= FAIL_LIMIT and now - f[-1] < BLOCK_SEC

    def fail(self, source: str) -> None:
        with self.lock:
            self.fails.setdefault(source, []).append(time.time())

    def allow(self, client: str) -> bool:
        now = time.time()
        with self.lock:
            c = [t for t in self.calls.get(client, []) if t > now - 60]
            c.append(now)
            self.calls[client] = c
            return len(c) <= RATE_PER_MIN


def make_handler(tools: Tools):
    limiter = Limiter()
    allow_ips = [x.strip() for x in os.environ.get("MCP_ALLOW_IPS", "").split(",") if x.strip()]
    allow_origins = [x.strip() for x in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if x.strip()]

    class MCPHandler(BaseHTTPRequestHandler):
        server_version = "sireum-mcp/0.1"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _source(self) -> str:
            # Funnel / serve proxy from localhost; the original client is in X-Forwarded-For if provided.
            xff = self.headers.get("X-Forwarded-For", "")
            return xff.split(",")[0].strip() or self.client_address[0]

        def _send(self, code: int, body: dict | None = None, headers: dict | None = None):
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _deny_path(self) -> bool:
            if self.path.split("?")[0] != "/mcp":
                self._send(404, {"error": "not found"})
                return True
            return False

        def do_GET(self):
            if not self._deny_path():
                self._send(405, {"error": "this server does not offer an SSE stream"}, {"Allow": "POST"})

        def do_DELETE(self):
            if not self._deny_path():
                self._send(405, {"error": "sessions are stateless"}, {"Allow": "POST"})

        def do_POST(self):
            if self._deny_path():
                return
            src = self._source()
            origin = self.headers.get("Origin")
            if origin and origin not in allow_origins:
                return self._send(403, {"error": "origin not allowed"})
            if allow_ips and src not in allow_ips:
                return self._send(403, {"error": "source not allowed"})
            if limiter.blocked(src):
                return self._send(429, {"error": "too many failed attempts; try later"}, {"Retry-After": str(BLOCK_SEC)})
            auth = self.headers.get("Authorization", "")
            client = tools.clients.auth(auth[7:].strip() if auth.startswith("Bearer ") else "")
            if not client:
                limiter.fail(src)
                log.warning("mcp: rejected token from %s", src)
                return self._send(401, {"error": "unauthorized"}, {"WWW-Authenticate": 'Bearer realm="sireum-mcp"'})
            if not limiter.allow(client["name"]):
                return self._send(429, {"error": "rate limit"}, {"Retry-After": "60"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                return self._send(413, {"error": "too large"})
            try:
                msg = json.loads(self.rfile.read(n) or b"null")
            except json.JSONDecodeError:
                return self._send(400, _err(None, -32700, "parse error"))
            if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                return self._send(400, _err(None, -32600, "invalid request (single JSON-RPC 2.0 message expected)"))
            if "id" not in msg:  # notification
                return self._send(202)
            if "method" not in msg:  # a response from the client; nothing to do
                return self._send(202)
            self._send(200, self._dispatch(client, msg))

        def _dispatch(self, client, msg) -> dict:
            mid, method, params = msg["id"], msg.get("method"), msg.get("params") or {}
            if method == "initialize":
                want = params.get("protocolVersion")
                return _ok(mid, {"protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {"name": "sireum-hub", "version": "0.1"},
                                 "instructions": "Operate the Sireum hub. Read tools are safe. Sensitive changes are "
                                                 "only proposed (propose_*); a human confirms them on the hub."})
            if method == "ping":
                return _ok(mid, {})
            if method == "tools/list":
                return _ok(mid, {"tools": tools.list_for(client["tier"])})
            if method == "tools/call":
                name, args = params.get("name"), params.get("arguments") or {}
                try:
                    out = tools.call(client, name, args)
                    tools.clients.log(client["name"], name, True)
                    return _ok(mid, {"content": [{"type": "text", "text": json.dumps(out, indent=2, default=str)}],
                                     "structuredContent": out if isinstance(out, dict) else {"result": out},
                                     "isError": False})
                except ToolError as e:
                    tools.clients.log(client["name"], str(name), False, str(e))
                    return _ok(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
                except (KeyError, ValueError, TypeError) as e:
                    tools.clients.log(client["name"], str(name), False, str(e))
                    return _ok(mid, {"content": [{"type": "text", "text": f"bad arguments: {e}"}], "isError": True})
            return _err(mid, -32601, f"method not found: {method}")

    return MCPHandler


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

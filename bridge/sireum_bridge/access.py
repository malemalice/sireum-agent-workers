"""Access policy: what a repo requests vs what the operator approved.

  repo manifest (.agent/tools.yaml at base_commit)  →  requests
      registries: [npm-company]      secrets: [STRIPE_TEST_KEY]
      egress: [api.stripe.com]       services: [postgres, redis]
  repos.yaml entry (operator)                       →  approvals
      registries: [npm-company]
      secrets: {STRIPE_TEST_KEY: {api_key_cmd: [...], expose: test}}
      egress: [api.stripe.com]

A job receives the intersection. Services are local throwaway processes inside the job
(no secrets, no network) and need no approval. Anything requested but not approved
becomes an access request the operator grants or denies.
"""
from __future__ import annotations

import fnmatch
import json
import re
import time
from dataclasses import dataclass, field

KINDS = ("secret", "registry", "egress")
SERVICES = ("postgres", "redis")
EXPOSURES = ("prepare", "test", "agent")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
REG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,40}$")
HOST_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


@dataclass
class Requests:
    secrets: list[str] = field(default_factory=list)
    registries: list[str] = field(default_factory=list)
    egress: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)

    @classmethod
    def from_manifest(cls, m: dict) -> "Requests":
        r = cls()

        def take(key, rx, dest, norm=lambda x: x):
            for v in m.get(key) or []:
                v = norm(str(v).strip())
                (dest.append(v) if rx.match(v) else r.invalid.append(f"{key}:{v}"))

        take("secrets", NAME_RE, r.secrets)
        take("registries", REG_RE, r.registries, str.lower)
        take("egress", HOST_RE, r.egress, str.lower)
        for s in m.get("services") or []:
            name = str(s).split(":", 1)[0].strip().lower()
            (r.services.append(name) if name in SERVICES else r.invalid.append(f"services:{s}"))
        return r


def approved(repo) -> dict:
    """Operator approvals from a Repo (fields may be absent on older configs)."""
    secrets = getattr(repo, "secrets", None) or {}
    return {"secret": {k: v for k, v in secrets.items() if isinstance(v, dict)},
            "registry": [str(x).lower() for x in (getattr(repo, "registries", None) or [])],
            "egress": [str(x).lower() for x in (getattr(repo, "egress", None) or [])]}


def host_allowed(host: str, patterns: list[str]) -> bool:
    return any(host == p or (p.startswith("*.") and fnmatch.fnmatch(host, p)) for p in patterns)


def missing(req: Requests, appr: dict) -> list[tuple[str, str]]:
    """(kind, name) pairs requested but not approved."""
    out = [("secret", s) for s in req.secrets if s not in appr["secret"]]
    out += [("registry", r) for r in req.registries if r not in appr["registry"]]
    out += [("egress", h) for h in req.egress if not host_allowed(h, appr["egress"])]
    return out


def granted(req: Requests, appr: dict) -> dict:
    return {"secrets": {s: appr["secret"][s] for s in req.secrets if s in appr["secret"]},
            "registries": [r for r in req.registries if r in appr["registry"]],
            "egress": [h for h in req.egress if host_allowed(h, appr["egress"])],
            "services": list(dict.fromkeys(req.services))}


# -- access request records --------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS access_requests (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_alias  TEXT NOT NULL,
  kind        TEXT NOT NULL,
  name        TEXT NOT NULL,
  reason      TEXT,
  evidence    TEXT,
  job_id      TEXT,
  status      TEXT NOT NULL DEFAULT 'pending',   -- pending | granted | denied
  note        TEXT,
  created_at  REAL NOT NULL,
  decided_at  REAL
);
CREATE TABLE IF NOT EXISTS request_jobs (       -- jobs waiting on / blocked by a request
  request_id  INTEGER NOT NULL,
  job_id      TEXT NOT NULL,
  PRIMARY KEY (request_id, job_id)
);
CREATE TABLE IF NOT EXISTS audit (
  at          REAL NOT NULL,
  job_id      TEXT,
  repo_alias  TEXT,
  kind        TEXT NOT NULL,                     -- secret | registry | egress | service | git
  name        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_name ON audit(kind, name);
"""


class AccessStore:
    def __init__(self, store):
        self.s = store
        store.db.executescript(SCHEMA)

    def open_request(self, repo_alias: str, kind: str, name: str, job_id: str | None,
                     reason: str = "", evidence: str = "") -> tuple[int, bool]:
        """Return (request id, created). One pending request per repo/kind/name."""
        with self.s.lock:
            row = self.s.db.execute(
                "SELECT id FROM access_requests WHERE repo_alias=? AND kind=? AND name=? AND status='pending'",
                (repo_alias, kind, name)).fetchone()
            if row:
                rid, created = row["id"], False
            else:
                cur = self.s.db.execute(
                    "INSERT INTO access_requests(repo_alias, kind, name, reason, evidence, job_id, created_at)"
                    " VALUES (?,?,?,?,?,?,?)", (repo_alias, kind, name, reason[:500], evidence[:1000], job_id,
                                                time.time()))
                rid, created = cur.lastrowid, True
            if job_id:
                self.s.db.execute("INSERT OR IGNORE INTO request_jobs VALUES (?,?)", (rid, job_id))
            return rid, created

    def get(self, rid: int):
        return self.s.db.execute("SELECT * FROM access_requests WHERE id=?", (rid,)).fetchone()

    def pending(self) -> list:
        return self.s.db.execute("SELECT * FROM access_requests WHERE status='pending' ORDER BY id").fetchall()

    def decide(self, rid: int, status: str, note: str | None = None) -> None:
        with self.s.lock:
            self.s.db.execute("UPDATE access_requests SET status=?, note=?, decided_at=? WHERE id=?",
                              (status, note, time.time(), rid))

    def jobs_for(self, rid: int) -> list[str]:
        return [r["job_id"] for r in self.s.db.execute("SELECT job_id FROM request_jobs WHERE request_id=?", (rid,))]

    def open_for_job(self, job_id: str) -> list:
        return self.s.db.execute(
            "SELECT a.* FROM access_requests a JOIN request_jobs j ON j.request_id=a.id "
            "WHERE j.job_id=? AND a.status='pending'", (job_id,)).fetchall()

    def audit(self, job_id: str, repo_alias: str, kind: str, names) -> None:
        now = time.time()
        with self.s.lock:
            self.s.db.executemany("INSERT INTO audit VALUES (?,?,?,?,?)",
                                  [(now, job_id, repo_alias, kind, n) for n in names])

    def last_used(self, kind: str, name: str) -> float | None:
        r = self.s.db.execute("SELECT MAX(at) AS t FROM audit WHERE kind=? AND name=?", (kind, name)).fetchone()
        return r["t"] if r else None

    def audit_for_job(self, job_id: str) -> dict:
        out: dict = {}
        for r in self.s.db.execute("SELECT kind, name FROM audit WHERE job_id=? ORDER BY at", (job_id,)):
            out.setdefault(r["kind"], []).append(r["name"])
        return out


def request_message(rid: int, repo: str, kind: str, name: str, reason: str) -> str:
    why = f" — {reason}" if reason else ""
    return (f"needs {kind} `{name}` for repo:{repo}{why}\n"
            f"  approve: `sireum-hub grant {rid}`   ·   refuse: `sireum-hub deny {rid} \"<note>\"`")


def parse_need_line(line: str) -> tuple[str, str, str] | None:
    """`SIREUM-NEED: secret STRIPE_TEST_KEY "tests call Stripe"` → (kind, name, reason)."""
    m = re.match(r'^\s*SIREUM-NEED:\s*(secret|registry|egress)\s+(\S+)\s*(?:"([^"]*)"|(.*))?\s*$', line)
    if not m:
        return None
    return m[1], m[2], (m[3] if m[3] is not None else (m[4] or "")).strip()


def needs_json(needs: list[dict]) -> str:
    return json.dumps([{k: n.get(k) for k in ("kind", "name", "reason", "evidence")} for n in needs])

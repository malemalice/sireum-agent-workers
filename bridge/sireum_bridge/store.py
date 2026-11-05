"""SQLite state for jobs, leases and executor pauses."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id      TEXT PRIMARY KEY,
  job         TEXT NOT NULL,
  repo_alias  TEXT NOT NULL,
  repo_key    TEXT NOT NULL,
  executor    TEXT NOT NULL,
  base_commit TEXT NOT NULL,
  state       TEXT NOT NULL,          -- dispatched | running | finished
  bundle_tip  TEXT,
  result      TEXT,
  card_id     TEXT,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
  lease_id    TEXT PRIMARY KEY,
  job_id      TEXT NOT NULL,
  attempt     INTEGER NOT NULL,
  clone_token TEXT NOT NULL UNIQUE,
  expires_at  REAL NOT NULL,
  released    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS leases_job ON leases(job_id);
CREATE TABLE IF NOT EXISTS cards (
  card_id     TEXT PRIMARY KEY,
  first_seen  REAL NOT NULL,
  job_id      TEXT,                   -- set once claimed + dispatched
  rounds      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS outbox (     -- kanban writes, retried until done
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id     TEXT NOT NULL,
  verb        TEXT NOT NULL,          -- comment | complete | request-review | block | release
  text        TEXT,
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  done        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS enroll_tokens (
  token_hash  TEXT PRIMARY KEY,
  name        TEXT,
  expires_at  REAL NOT NULL,
  used        INTEGER NOT NULL DEFAULT 0,
  revoked     INTEGER NOT NULL DEFAULT 0,
  ts_key_id   TEXT
);
CREATE TABLE IF NOT EXISTS nodes (
  name         TEXT PRIMARY KEY,
  host_os      TEXT, arch TEXT, hostname TEXT, cpus INTEGER, mem_mb INTEGER,
  secret_hash  TEXT NOT NULL,
  nomad_accessor TEXT,
  created_at   REAL NOT NULL,
  revoked      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS executors (
  name          TEXT PRIMARY KEY,
  paused_until  REAL NOT NULL DEFAULT 0,
  backoff_level INTEGER NOT NULL DEFAULT 0,
  limit_hits    TEXT NOT NULL DEFAULT '[]'
);
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        # Migrations for databases created by earlier versions.
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        if "grants" not in cols:
            self.db.execute("ALTER TABLE jobs ADD COLUMN grants TEXT")

    # -- jobs -------------------------------------------------------------
    def add_job(self, job: dict, repo_alias: str, repo_key: str, executor: str, base_commit: str,
                card_id: str | None = None) -> bool:
        now = time.time()
        with self.lock:
            try:
                self.db.execute(
                    "INSERT INTO jobs (job_id, job, repo_alias, repo_key, executor, base_commit, state,"
                    " card_id, created_at, updated_at) VALUES (?,?,?,?,?,?, 'dispatched', ?, ?, ?)",
                    (job["job_id"], json.dumps(job), repo_alias, repo_key, executor, base_commit, card_id,
                     now, now))
                return True
            except sqlite3.IntegrityError:
                return False

    def job(self, job_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()

    def set_job(self, job_id: str, **fields) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE jobs SET {cols} WHERE job_id=?", (*fields.values(), job_id))

    def active_jobs(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM jobs WHERE state != 'finished' ORDER BY created_at").fetchall()

    def recent_jobs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    def running_count(self, executor: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE executor=? AND state IN ('dispatched', 'running')",
            (executor,)).fetchone()[0]

    # -- leases -----------------------------------------------------------
    def latest_lease(self, job_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM leases WHERE job_id=? ORDER BY attempt DESC LIMIT 1", (job_id,)).fetchone()

    def acquire_lease(self, job_id: str, ttl: int) -> tuple[sqlite3.Row | None, str]:
        """Atomically issue a new lease unless a live one exists or the job is finished."""
        with self.lock:
            job = self.job(job_id)
            if job is None:
                return None, "unknown job"
            if job["state"] == "finished":
                return None, "job finished"
            if job["state"] == "waiting_access":
                return None, "waiting for access approval"
            cur = self.latest_lease(job_id)
            now = time.time()
            if cur and not cur["released"] and cur["expires_at"] > now:
                return None, "lease held"
            if cur and not cur["released"]:
                self.db.execute("UPDATE leases SET released=1 WHERE lease_id=?", (cur["lease_id"],))
            attempt = (cur["attempt"] + 1) if cur else 1
            lease_id = "ls-" + secrets.token_hex(6)
            self.db.execute("INSERT INTO leases VALUES (?,?,?,?,?,0)",
                            (lease_id, job_id, attempt, secrets.token_urlsafe(32), now + ttl))
            self.set_job(job_id, state="running")
            return self.lease(lease_id), ""

    def lease(self, lease_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()

    def lease_by_token(self, token: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM leases WHERE clone_token=?", (token,)).fetchone()

    def is_live(self, lease: sqlite3.Row | None) -> bool:
        return bool(lease) and not lease["released"] and lease["expires_at"] > time.time()

    def is_current(self, lease: sqlite3.Row | None) -> bool:
        """Latest lease for its job and not released (fencing token).

        A lease that expired but was not superseded is still current, so a
        result spooled while the hub was down can be accepted later.
        """
        if not lease or lease["released"]:
            return False
        latest = self.latest_lease(lease["job_id"])
        return latest is not None and latest["lease_id"] == lease["lease_id"]

    def heartbeat(self, lease_id: str, ttl: int) -> bool:
        with self.lock:
            lease = self.lease(lease_id)
            if not self.is_live(lease) or not self.is_current(lease):
                return False
            self.db.execute("UPDATE leases SET expires_at=? WHERE lease_id=?", (time.time() + ttl, lease_id))
            return True

    def release(self, lease_id: str) -> None:
        with self.lock:
            self.db.execute("UPDATE leases SET released=1 WHERE lease_id=?", (lease_id,))

    # -- executors --------------------------------------------------------
    def executor(self, name: str) -> sqlite3.Row:
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO executors(name) VALUES (?)", (name,))
            return self.db.execute("SELECT * FROM executors WHERE name=?", (name,)).fetchone()

    def pause(self, name: str, until: float, from_usage_limit: bool) -> None:
        ex = self.executor(name)
        with self.lock:
            hits = [t for t in json.loads(ex["limit_hits"]) if t > time.time() - 86400]
            level = ex["backoff_level"]
            if from_usage_limit:
                hits.append(time.time())
                level = min(level + 1, 3)
            self.db.execute("UPDATE executors SET paused_until=?, backoff_level=?, limit_hits=? WHERE name=?",
                            (until, level, json.dumps(hits), name))

    def resume(self, name: str, reset_backoff: bool = False) -> None:
        self.executor(name)
        with self.lock:
            if reset_backoff:
                self.db.execute("UPDATE executors SET backoff_level=0 WHERE name=?", (name,))
            else:
                self.db.execute("UPDATE executors SET paused_until=0 WHERE name=?", (name,))

    # -- cards + outbox ---------------------------------------------------
    def card_seen(self, card_id: str) -> sqlite3.Row:
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO cards(card_id, first_seen) VALUES (?,?)", (card_id, time.time()))
            return self.db.execute("SELECT * FROM cards WHERE card_id=?", (card_id,)).fetchone()

    def card_attach(self, card_id: str, job_id: str) -> None:
        with self.lock:
            self.db.execute("UPDATE cards SET job_id=?, rounds=rounds+1 WHERE card_id=?", (job_id, card_id))

    def card_forget(self, card_id: str) -> None:
        """Card left Sireum (released or finished): a later re-tag starts a fresh grace window."""
        with self.lock:
            self.db.execute("UPDATE cards SET job_id=NULL, first_seen=? WHERE card_id=?", (time.time(), card_id))

    def outbox_add(self, card_id: str, verb: str, text: str | None = None) -> None:
        with self.lock:
            self.db.execute("INSERT INTO outbox(card_id, verb, text) VALUES (?,?,?)", (card_id, verb, text))

    def outbox_pending(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM outbox WHERE done=0 AND attempts < 50 ORDER BY id").fetchall()

    def outbox_mark(self, oid: int, ok: bool, err: str | None = None) -> None:
        with self.lock:
            if ok:
                self.db.execute("UPDATE outbox SET done=1 WHERE id=?", (oid,))
            else:
                self.db.execute("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?", (err, oid))

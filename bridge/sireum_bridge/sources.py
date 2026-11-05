"""Task sources: where opt-in cards come from and where results are written back.

Every source speaks the same small contract, so the booster loop doesn't care
whether a card lives in the Hermes kanban, GitHub Issues, or a test fixture.

    list_remote()                 -> [Card]   cards currently tagged `remote`
    claim(card_id)                -> bool     atomic; False if someone else has it
    comment(card_id, text)
    complete / request_review / block / release(card_id)
                                              release = remove `remote`, hand back to Hermes
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Card:
    id: str
    title: str
    body: str = ""
    tags: list[str] = field(default_factory=list)
    tagged_by: str | None = None
    created_at: str | None = None


class SourceError(Exception):
    pass


class Source:
    name = "source"

    def list_remote(self) -> list[Card]:
        raise NotImplementedError

    def claim(self, card_id: str) -> bool:
        raise NotImplementedError

    def comment(self, card_id: str, text: str) -> None:
        raise NotImplementedError

    def action(self, card_id: str, verb: str) -> None:
        """verb: complete | request-review | block | release"""
        raise NotImplementedError


# -- Hermes kanban over SSH (forced command on hermes-host) ------------------

class KanbanSSH(Source):
    """Talks to `sireum-kanban` on hermes-host. The SSH key is restricted to that command."""
    name = "kanban"

    def __init__(self, target: str, key_path: str | None = None, extra_opts: str = ""):
        self.base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                     "-o", "StrictHostKeyChecking=yes"]
        if key_path:
            self.base += ["-i", key_path, "-o", "IdentitiesOnly=yes"]
        self.base += shlex.split(extra_opts) + [target]

    def _run(self, *args: str, stdin: str | None = None, ok_codes=(0,)) -> subprocess.CompletedProcess:
        p = subprocess.run(self.base + list(args), input=stdin, capture_output=True, text=True, timeout=60)
        if p.returncode not in ok_codes:
            raise SourceError(f"sireum-kanban {args[0]} exit {p.returncode}: {p.stderr.strip()[:300]}")
        return p

    def list_remote(self) -> list[Card]:
        out = self._run("list-remote").stdout
        return [Card(**{k: c.get(k) for k in ("id", "title", "body", "tags", "tagged_by", "created_at")})
                for c in json.loads(out or "[]")]

    def claim(self, card_id: str) -> bool:
        return self._run("claim", card_id, ok_codes=(0, 1)).returncode == 0

    def comment(self, card_id: str, text: str) -> None:
        self._run("comment", card_id, stdin=text)

    def action(self, card_id: str, verb: str) -> None:
        self._run(verb, card_id)


# -- GitHub Issues (Phase 4) -------------------------------------------------

class GitHubIssues(Source):
    """Issues labelled `remote` in one repo. Claim = add label `sireum:claimed`.

    Card id = issue number. `tagged_by` = who applied the `remote` label (from the
    issue's timeline), so the REMOTE_TAGGERS allowlist works the same way.
    """
    name = "github"

    def __init__(self, repo: str, token_file: str, api_url: str = "https://api.github.com"):
        self.repo, self.api = repo, api_url.rstrip("/")
        self.token = Path(token_file).read_text().strip()

    def _req(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            f"{self.api}/repos/{self.repo}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise SourceError(f"github {method} {path}: {e.code}") from None

    def _labeler(self, number: str) -> str | None:
        events = self._req("GET", f"/issues/{number}/events?per_page=100") or []
        who = None
        for ev in events:
            if ev.get("event") == "labeled" and (ev.get("label") or {}).get("name") == "remote":
                who = (ev.get("actor") or {}).get("login")
        return who

    def list_remote(self) -> list[Card]:
        issues = self._req("GET", "/issues?labels=remote&state=open&per_page=50") or []
        cards = []
        for i in issues:
            if "pull_request" in i:
                continue
            labels = [lb["name"] for lb in i.get("labels", [])]
            if "sireum:claimed" in labels:
                continue
            cards.append(Card(id=str(i["number"]), title=i["title"], body=i.get("body") or "", tags=labels,
                              tagged_by=self._labeler(str(i["number"])), created_at=i.get("created_at")))
        return cards

    def claim(self, card_id: str) -> bool:
        labels = [lb["name"] for lb in self._req("GET", f"/issues/{card_id}/labels") or []]
        if "sireum:claimed" in labels or "remote" not in labels:
            return False
        self._req("POST", f"/issues/{card_id}/labels", {"labels": ["sireum:claimed"]})
        return True

    def comment(self, card_id: str, text: str) -> None:
        self._req("POST", f"/issues/{card_id}/comments", {"body": text})

    def action(self, card_id: str, verb: str) -> None:
        def drop(label):
            try:
                self._req("DELETE", f"/issues/{card_id}/labels/{urllib.parse.quote(label)}")
            except SourceError:
                pass
        # Every terminal verb takes the issue out of Sireum.
        drop("sireum:claimed")
        drop("remote")
        if verb == "complete":
            self._req("POST", f"/issues/{card_id}/labels", {"labels": ["sireum:done"]})
        elif verb == "request-review":
            self._req("POST", f"/issues/{card_id}/labels", {"labels": ["sireum:needs-review"]})
        elif verb == "block":
            self._req("POST", f"/issues/{card_id}/labels", {"labels": ["sireum:blocked"]})


# -- File-backed source (tests, dry runs) -------------------------------------

class FileSource(Source):
    """JSON file {"cards": [...], "log": [...]}; mimics sireum-kanban semantics."""
    name = "file"

    def __init__(self, path: str):
        self.path = Path(path)

    def _load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {"cards": [], "log": []}

    def _save(self, d: dict) -> None:
        self.path.write_text(json.dumps(d, indent=2))

    def list_remote(self) -> list[Card]:
        return [Card(**{k: c.get(k) for k in ("id", "title", "body", "tags", "tagged_by", "created_at")})
                for c in self._load()["cards"] if "remote" in c.get("tags", []) and not c.get("claimed")]

    def claim(self, card_id: str) -> bool:
        d = self._load()
        for c in d["cards"]:
            if c["id"] == card_id and not c.get("claimed") and "remote" in c.get("tags", []):
                c["claimed"] = True
                d["log"].append(["claim", card_id])
                self._save(d)
                return True
        return False

    def comment(self, card_id: str, text: str) -> None:
        d = self._load()
        d["log"].append(["comment", card_id, text])
        self._save(d)

    def action(self, card_id: str, verb: str) -> None:
        d = self._load()
        for c in d["cards"]:
            if c["id"] == card_id:
                # Every terminal verb takes the card out of Sireum (same as sireum-kanban).
                c["claimed"] = False
                c["tags"] = [t for t in c.get("tags", []) if t != "remote"]
                c["state"] = verb
        d["log"].append([verb, card_id])
        self._save(d)


def make_source(spec: str) -> Source | None:
    """SOURCE env: `kanban-ssh:<user@host>[#<key>]`, `github:<owner/repo>#<token_file>`, `file:<path>`."""
    if not spec:
        return None
    kind, _, rest = spec.partition(":")
    if kind == "kanban-ssh":
        target, _, key = rest.partition("#")
        return KanbanSSH(target, key or None)
    if kind == "github":
        repo, _, token = rest.partition("#")
        return GitHubIssues(repo, token)
    if kind == "file":
        return FileSource(rest)
    raise ValueError(f"unknown source {spec!r}")


# -- card → dispatch body ------------------------------------------------------

LABEL_RE = re.compile(r"^(repo|executor|test|base|timeout|class):(.+)$")


def parse_card(card: Card, default_timeout_min: int = 30, max_timeout_min: int = 120) -> dict:
    """Map a card to a /v1/dispatch body. Labels win over body directives."""
    body = card.body or ""
    acceptance = ""
    m = re.search(r"^\s*Acceptance:\s*(.+(?:\n(?!\s*\n).+)*)", body, re.M | re.I)
    if m:
        acceptance = m[1].strip()
        body = (body[:m.start()] + body[m.end():]).strip()
    out: dict = {
        "task": f"{card.title.strip()}\n\n{body}".strip(),
        "acceptance": acceptance or "The task as described is done and existing tests still pass.",
        "executor": "auto",
        "timeout_sec": default_timeout_min * 60,
    }
    for tag in card.tags or []:
        lm = LABEL_RE.match(tag.strip())
        if not lm:
            continue
        key, val = lm[1], lm[2].strip()
        if key == "repo":
            out["repo"] = val
        elif key == "executor":
            out["executor"] = val
        elif key == "test":
            out["test_target"] = val
        elif key == "base":
            out["base_ref"] = val
        elif key == "class":
            out["job_class"] = val
        elif key == "timeout" and val.isdigit():
            out["timeout_sec"] = min(int(val), max_timeout_min) * 60
    return out

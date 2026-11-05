"""Booster loop: pick up `remote` cards, claim, dispatch; write results back; hand
anything Sireum can't run back to Hermes."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

from .sources import Card, Source, SourceError, parse_card

log = logging.getLogger("sireum-bridge.booster")

# status -> card verb after the result comment
STATUS_VERB = {"success": "complete", "needs_review": "request-review", "blocked": "block",
               "failed": "release", "cancelled": "release"}


CARD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")  # same rule as sireum-kanban


def card_job_id(card_id: str, rounds: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", card_id)[:48]
    return f"kb-{safe}" + (f"-r{rounds + 1}" if rounds else "")


class Booster:
    def __init__(self, bridge, source: Source, taggers: set[str], claim_grace: int):
        self.b, self.src = bridge, source
        self.taggers, self.grace = taggers, claim_grace

    # -- outbound to the card ------------------------------------------------
    def give_back(self, card_id: str, reason: str) -> None:
        self.b.store.outbox_add(card_id, "comment", f"Sireum: {reason}. Handing this card back to Hermes.")
        self.b.store.outbox_add(card_id, "release")
        self.b.store.card_forget(card_id)
        log.info("card %s released: %s", card_id, reason)

    def on_result(self, card_id: str, res: dict) -> None:
        status = res.get("status", "failed")
        lines = [f"Sireum job `{res.get('job_id')}` finished: **{status}** (executor {res.get('executor')})."]
        if res.get("mr_url"):
            lines.append(f"Merge request: {res['mr_url']}")
        elif res.get("pushed"):
            lines.append(f"Branch: `{res.get('branch')}`")
        tr = res.get("test_result") or {}
        if tr.get("ran"):
            lines.append(f"Test target `{tr.get('target')}`: {'passed' if tr.get('passed') else 'FAILED'}")
        if res.get("tests_modified"):
            lines.append("The change touches protected paths (tests/CI) — review carefully.")
        if res.get("error"):
            lines.append(f"Error: {res['error'].get('code')} — {str(res['error'].get('message'))[:300]}")
        for r in res.get("access_requests") or []:
            lines.append("Access request: " + r["message"])
        if res.get("summary"):
            lines.append("\n" + str(res["summary"])[:1500])
        if STATUS_VERB.get(status) == "release":
            lines.append("\nHanding this card back to Hermes.")
        self.b.store.outbox_add(card_id, "comment", "\n".join(lines))
        self.b.store.outbox_add(card_id, STATUS_VERB.get(status, "release"))
        self.b.store.card_forget(card_id)

    def flush_outbox(self) -> None:
        for row in self.b.store.outbox_pending():
            try:
                if row["verb"] == "comment":
                    self.src.comment(row["card_id"], row["text"] or "")
                else:
                    self.src.action(row["card_id"], row["verb"])
                self.b.store.outbox_mark(row["id"], True)
            except (SourceError, OSError) as e:
                self.b.store.outbox_mark(row["id"], False, str(e)[:300])
                log.warning("kanban %s %s failed: %s", row["verb"], row["card_id"], e)
                return  # keep order per card; retry next tick

    # -- intake ----------------------------------------------------------------
    def tick(self) -> None:
        self.flush_outbox()
        try:
            cards = self.src.list_remote()
        except (SourceError, OSError, ValueError) as e:
            log.warning("list-remote failed: %s", e)
            return
        for card in cards:
            try:
                self.consider(card)
            except Exception:  # noqa: BLE001
                log.exception("card %s", card.id)
        self.flush_outbox()

    def consider(self, card: Card) -> None:
        if not CARD_ID_RE.match(card.id or ""):
            log.warning("ignoring card with unsafe id %r", card.id)
            return
        row = self.b.store.card_seen(card.id)
        if row["job_id"]:
            job = self.b.store.job(row["job_id"])
            if job and job["state"] != "finished":
                return  # already ours
        if not self.taggers or card.tagged_by not in self.taggers:
            return self.give_back(card.id, f"`remote` was added by {card.tagged_by or 'unknown'}, "
                                           "who is not allowed to send cards to Sireum")
        body = parse_card(card)
        if not body.get("repo"):
            return self.give_back(card.id, "no `repo:` label, so Sireum doesn't know which repo to use")
        repo = self.b.resolve_repo(body["repo"])
        if repo is None:
            return self.give_back(card.id, f"repo `{body['repo']}` is not onboarded")
        if body["executor"] not in ("claude", "opencode", "auto"):
            return self.give_back(card.id, f"unknown executor `{body['executor']}`")
        if self.b.pick_executor(body["executor"]) is None:
            age = time.time() - row["first_seen"]
            if age > self.grace:
                return self.give_back(card.id, f"no executor available for {int(age)}s "
                                               "(paused after a usage limit, or at its limit)")
            return  # wait inside the grace window
        if not self.src.claim(card.id):
            return
        job_id = card_job_id(card.id, row["rounds"])
        body.update(source=self.src.name, source_ref=f"card:{card.id}",
                    callback={"kanban_card": card.id, "discord_webhook": "default"},
                    labels=list(card.tags or []))
        code, resp = self.b.dispatch(body, job_id, card_id=card.id)
        if code != 202:
            return self.give_back(card.id, f"dispatch failed ({code}: {resp.get('error')})")
        self.b.store.card_attach(card.id, job_id)
        msg = (f"Sireum picked this up as job `{job_id}` on {resp['executor']} "
               f"({datetime.now().astimezone().strftime('%H:%M %Z')}).")
        if resp.get("state") == "waiting_access":
            msg += "\nIt is waiting for access the repo asks for:\n" + "\n".join(
                "- " + r["message"] for r in resp.get("access_requests", []))
        self.b.store.outbox_add(card.id, "comment", msg)
        log.info("card %s -> job %s", card.id, job_id)

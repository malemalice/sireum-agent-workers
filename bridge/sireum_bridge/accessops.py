"""Bridge operations for Phase 6: pre-flight, grants, access requests, re-runs, egress list.

Mixed into server.Bridge (uses self.store, self.relay, self.registries, self.repos …).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import yaml

from . import access
from .creds import CredentialError, write_secret
from .pkgrelay import runner_view

log = logging.getLogger("sireum-bridge.access")
ACCESS_WAIT_SEC = int(os.environ.get("ACCESS_WAIT_SEC", str(24 * 3600)))


class AccessOps:
    # -- config files the bridge may edit ------------------------------------------
    @property
    def secrets_dir(self) -> Path:
        return Path(os.environ.get("SIREUM_SECRETS_DIR") or self.config_dir.parent / "secrets")

    def _edit_repo(self, alias: str, fn) -> None:
        p = self.config_dir / "repos.yaml"
        doc = yaml.safe_load(p.read_text()) or {}
        entry = next((r for r in doc.get("repos") or [] if r.get("alias") == alias), None)
        if entry is None:
            raise KeyError(f"repo {alias} not in repos.yaml")
        fn(entry)
        tmp = p.with_suffix(".tmp")
        tmp.write_text("# Repos Sireum may work on. Edited by sireum-hub; applied without a restart.\n"
                       + yaml.safe_dump(doc, sort_keys=False))
        os.replace(tmp, p)
        self.load_config()

    # -- manifest / grants -----------------------------------------------------------
    def manifest_requests(self, repo, base_commit: str) -> access.Requests:
        text = self.relay.read_file(repo, base_commit, ".agent/tools.yaml")
        try:
            doc = yaml.safe_load(text) if text else {}
        except yaml.YAMLError:
            doc = {}
        req = access.Requests.from_manifest(doc if isinstance(doc, dict) else {})
        if req.invalid:
            log.warning("repo %s manifest has invalid requests ignored: %s", repo.alias, req.invalid)
        return req

    def preflight(self, repo, base_commit: str) -> list[tuple[str, str]]:
        req = self.manifest_requests(repo, base_commit)
        miss = access.missing(req, access.approved(repo))
        # A registry the repo approves but the hub doesn't define is also missing.
        miss += [("registry", r) for r in req.registries
                 if r in access.approved(repo)["registry"] and r not in self.registries]
        return list(dict.fromkeys(miss))

    def build_grants(self, row, repo, lease_token: str) -> dict:
        req = self.manifest_requests(repo, row["base_commit"])
        g = access.granted(req, access.approved(repo))
        out = {"secrets": [], "registries": [], "egress": g["egress"], "services": g["services"], "errors": []}
        for name, spec in g["secrets"].items():
            src = {k: v for k, v in spec.items() if k != "expose"}
            expose = spec.get("expose", "prepare")
            if expose not in access.EXPOSURES:
                expose = "prepare"
            try:
                value = self.relay.creds.token(src, None, None)
            except CredentialError as e:
                out["errors"].append(f"secret {name}: {e}")
                continue
            if value:
                out["secrets"].append({"name": name, "value": value, "expose": expose})
        for name in g["registries"]:
            reg = self.registries.get(name)
            if not reg:
                out["errors"].append(f"registry {name}: not defined in registries.yaml")
                continue
            try:
                out["registries"].append(runner_view(reg, f"{self.public_url}/pkg/{name}", lease_token,
                                                     self.relay.creds))
            except CredentialError as e:
                out["errors"].append(f"registry {name}: {e}")
        names = {"secrets": [s["name"] for s in out["secrets"]],
                 "registries": [r["name"] for r in out["registries"]],
                 "egress": out["egress"], "services": out["services"]}
        self.store.set_job(row["job_id"], grants=json.dumps(names))
        a = self.access
        a.audit(row["job_id"], repo.alias, "secret", names["secrets"])
        a.audit(row["job_id"], repo.alias, "registry", names["registries"])
        a.audit(row["job_id"], repo.alias, "egress", names["egress"])
        a.audit(row["job_id"], repo.alias, "service", names["services"])
        for e in out["errors"]:
            log.warning("job %s grant error: %s", row["job_id"], e)
        return out

    def granted_secret_values(self, row) -> list[str]:
        """Re-resolve the values a job received, for scrubbing what it sends back."""
        try:
            names = json.loads(row["grants"] or "{}").get("secrets", [])
            repo = self.repo_by_alias(row["repo_alias"])
        except (ValueError, StopIteration, KeyError):
            return []
        vals = []
        for n in names:
            spec = (repo.secrets or {}).get(n) or {}
            try:
                v = self.relay.creds.token({k: x for k, x in spec.items() if k != "expose"}, None, None)
                if v and len(v) >= 4:
                    vals.append(v)
            except CredentialError:
                pass
        return vals

    # -- requests ----------------------------------------------------------------------
    def open_requests(self, repo_alias: str, job_id: str | None, needs: list[dict]) -> list[dict]:
        out = []
        for n in needs:
            kind, name = n.get("kind"), str(n.get("name", "")).strip()
            if kind not in access.KINDS or not name:
                continue
            if kind == "secret" and not access.NAME_RE.match(name):
                continue
            if kind == "egress":
                name = name.lower()
                if not access.HOST_RE.match(name):
                    continue
            rid, created = self.access.open_request(repo_alias, kind, name, job_id, n.get("reason") or "",
                                                    n.get("evidence") or "")
            out.append({"id": rid, "kind": kind, "name": name, "reason": n.get("reason") or "",
                        "message": access.request_message(rid, repo_alias, kind, name, n.get("reason") or "")})
            if created:
                self.discord(f"Sireum access request #{rid}: " + out[-1]["message"])
        return out

    def list_requests(self) -> list[dict]:
        return [dict(r) | {"jobs": self.access.jobs_for(r["id"]),
                           "message": access.request_message(r["id"], r["repo_alias"], r["kind"], r["name"],
                                                             r["reason"] or "")}
                for r in self.access.pending()]

    def grant(self, rid: int, body: dict) -> tuple[int, dict]:
        r = self.access.get(rid)
        if not r:
            return 404, {"error": "unknown request"}
        if r["status"] != "pending":
            return 409, {"error": f"request already {r['status']}"}
        alias, kind, name = r["repo_alias"], r["kind"], r["name"]
        try:
            if kind == "secret":
                expose = body.get("expose", "prepare")
                if expose not in access.EXPOSURES:
                    return 400, {"error": f"expose must be one of {access.EXPOSURES}"}
                if body.get("value"):
                    path = self.secrets_dir / f"{alias}-{name}.secret"
                    write_secret(path, str(body["value"]))
                    src = {"api_key_file": str(path)}
                else:
                    src = {k: body[k] for k in ("api_key_file", "api_key_env", "api_key_cmd") if body.get(k)}
                    if len(src) != 1:
                        return 400, {"error": "give `value`, or exactly one of api_key_file/api_key_env/api_key_cmd"}
                try:
                    if not self.relay.creds.token(src, None, None):
                        return 400, {"error": "the source returned an empty value"}
                except CredentialError as e:
                    return 400, {"error": f"source check failed: {e}"}
                self._edit_repo(alias, lambda e: e.setdefault("secrets", {}).__setitem__(name, {**src, "expose": expose}))
            elif kind == "registry":
                if name not in self.registries:
                    return 409, {"error": f"registry {name} is not defined; add it first: "
                                          f"sireum-hub registry add {name} --ecosystem … --url …"}
                self._edit_repo(alias, lambda e: e.setdefault("registries", []).append(name)
                                if name not in (e.get("registries") or []) else None)
            elif kind == "egress":
                self._edit_repo(alias, lambda e: e.setdefault("egress", []).append(name)
                                if name not in (e.get("egress") or []) else None)
        except KeyError as e:
            return 404, {"error": str(e)}
        self.access.decide(rid, "granted", body.get("note"))
        log.info("access request %s granted: %s %s for %s", rid, kind, name, alias)
        started = [j for j in self.access.jobs_for(rid) if self.continue_job(j)]
        return 200, {"granted": rid, "kind": kind, "name": name, "repo": alias, "resumed": started}

    def deny(self, rid: int, note: str) -> tuple[int, dict]:
        r = self.access.get(rid)
        if not r:
            return 404, {"error": "unknown request"}
        if r["status"] != "pending":
            return 409, {"error": f"request already {r['status']}"}
        self.access.decide(rid, "denied", note)
        for job_id in self.access.jobs_for(rid):
            row = self.store.job(job_id)
            if row and row["state"] == "waiting_access":
                self.finish(job_id, {"job_id": job_id, "status": "cancelled", "executor": row["executor"],
                                     "error": {"code": "access_denied", "reset_at": None,
                                               "message": f"{r['kind']} {r['name']} denied: {note}"},
                                     "finished_at": _now()})
            elif row and row["card_id"] and self.booster:
                self.store.outbox_add(row["card_id"], "comment",
                                      f"Sireum: access to {r['kind']} `{r['name']}` was refused: {note}")
        return 200, {"denied": rid}

    def continue_job(self, job_id: str) -> bool:
        """After a grant: start a waiting job, or re-run a job that ended needs_access."""
        row = self.store.job(job_id)
        if not row or self.access.open_for_job(job_id):
            return False
        if row["state"] == "waiting_access":
            self.store.set_job(job_id, state="dispatched", created_at=time.time())
            if row["card_id"] and self.booster:
                self.store.outbox_add(row["card_id"], "comment", f"Sireum: access granted; job `{job_id}` starting.")
            self.dispatch_to_nomad(job_id)
            return True
        res = json.loads(row["result"] or "{}")
        if row["state"] == "finished" and (res.get("error") or {}).get("code") == "needs_access" \
                and not res.get("rerun_as"):
            code, resp = self.rerun(job_id, "access it was missing has now been granted")
            return code == 202
        return False

    def rerun(self, job_id: str, why: str = "re-run requested") -> tuple[int, dict]:
        row = self.store.job(job_id)
        if not row:
            return 404, {"error": "unknown job"}
        if row["state"] != "finished":
            return 409, {"error": "job is still active"}
        job = json.loads(row["job"])
        base = re.sub(r"-r\d+$", "", job_id)
        n = 2
        while self.store.job(f"{base}-r{n}"):
            n += 1
        new_id = f"{base}-r{n}"[:64]
        prev = json.loads(row["result"] or "{}")
        note = (f"\n\n(Previous attempt `{job_id}` ended {prev.get('status')}"
                f"{' with branch ' + prev['branch'] if prev.get('pushed') else ''}; {why}. "
                f"Start again from the base commit.)")
        body = {k: job.get(k) for k in ("acceptance", "executor", "test_target", "base_ref", "timeout_sec",
                                        "max_turns", "job_class", "toolbox", "source", "source_ref", "callback",
                                        "labels", "model")}
        body = {k: v for k, v in body.items() if v is not None}
        body.update(repo=row["repo_alias"], task=job["task"] + note)
        code, resp = self.dispatch(body, new_id, card_id=row["card_id"])
        if code == 202:
            prev["rerun_as"] = new_id
            self.store.set_job(job_id, result=json.dumps(prev))
            if row["card_id"] and self.booster:
                self.store.card_attach(row["card_id"], new_id)
                self.store.outbox_add(row["card_id"], "comment", f"Sireum: re-running as `{new_id}` ({why}).")
        return code, resp

    def expire_waiting(self) -> None:
        now = time.time()
        for row in self.store.active_jobs():
            if row["state"] == "waiting_access" and now - row["created_at"] > ACCESS_WAIT_SEC:
                self.finish(row["job_id"], {"job_id": row["job_id"], "status": "cancelled",
                                            "executor": row["executor"], "finished_at": _now(),
                                            "error": {"code": "needs_access", "reset_at": None,
                                                      "message": "access was not granted in time"}})

    def egress_union(self) -> list[str]:
        self.load_config()
        hosts = set()
        for r in self.repos:
            if not r.enabled:
                continue
            hosts.update(str(h).lower() for h in (r.egress or []))
            for name in r.registries or []:
                reg = self.registries.get(name)
                if reg and reg.mode == "inject":
                    hosts.add(reg.host)
        return sorted(h for h in hosts if access.HOST_RE.match(h))


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

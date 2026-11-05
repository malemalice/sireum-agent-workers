"""Thin clients for the Nomad and Tailscale HTTP APIs (stdlib only)."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request


class APIError(Exception):
    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


def _call(method: str, url: str, body=None, headers: dict | None = None, form: bool = False, timeout: int = 20):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        raise APIError(f"{method} {url}: {e.code} {e.read()[:300]!r}", e.code) from None
    except urllib.error.URLError as e:
        raise APIError(f"{method} {url}: {e.reason}") from None


class Nomad:
    def __init__(self, addr: str, token: str | None = None, job: str = "sireum-runner"):
        self.addr, self.token, self.job = addr.rstrip("/"), token, job

    def _h(self):
        return {"X-Nomad-Token": self.token} if self.token else {}

    def dispatch(self, job_id: str, payload: dict | None = None) -> str:
        body = {"Meta": {"job_id": job_id}}
        if payload is not None:
            body["Payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
        r = _call("POST", f"{self.addr}/v1/job/{self.job}/dispatch", body, self._h())
        return r["DispatchedJobID"]

    def stop(self, dispatched_id: str) -> None:
        _call("DELETE", f"{self.addr}/v1/job/{urllib.parse.quote(dispatched_id, safe='')}", None, self._h())

    def alloc_states(self, dispatched_id: str) -> list[str]:
        r = _call("GET", f"{self.addr}/v1/job/{urllib.parse.quote(dispatched_id, safe='')}/allocations",
                  None, self._h()) or []
        return [a.get("ClientStatus", "") for a in r]

    def var_put(self, path: str, items: dict) -> None:
        _call("PUT", f"{self.addr}/v1/var/{path}", {"Path": path, "Items": items}, self._h())

    def var_delete(self, path: str) -> None:
        try:
            _call("DELETE", f"{self.addr}/v1/var/{path}", None, self._h())
        except APIError as e:
            if e.code != 404:
                raise

    def acl_token_create(self, name: str, policies: list[str]) -> tuple[str, str]:
        r = _call("POST", f"{self.addr}/v1/acl/token",
                  {"Name": name, "Type": "client", "Policies": policies}, self._h())
        return r["AccessorID"], r["SecretID"]

    def acl_token_delete(self, accessor: str) -> None:
        try:
            _call("DELETE", f"{self.addr}/v1/acl/token/{accessor}", None, self._h())
        except APIError as e:
            if e.code != 404:
                raise


class Tailscale:
    """Mints single-use, pre-authorised, tagged auth keys with an OAuth client (scope: auth_keys)."""
    API = "https://api.tailscale.com/api/v2"

    def __init__(self, client_id: str, client_secret: str, tailnet: str = "-"):
        self.cid, self.secret, self.tailnet = client_id, client_secret, tailnet

    def _token(self) -> str:
        r = _call("POST", f"{self.API}/oauth/token",
                  {"client_id": self.cid, "client_secret": self.secret, "grant_type": "client_credentials"},
                  form=True)
        return r["access_token"]

    def create_auth_key(self, tag: str = "tag:sireum-worker", expiry_sec: int = 86400,
                        description: str = "sireum worker") -> tuple[str, str]:
        body = {"capabilities": {"devices": {"create": {
                    "reusable": False, "ephemeral": False, "preauthorized": True, "tags": [tag]}}},
                "expirySeconds": expiry_sec, "description": description[:50]}
        r = _call("POST", f"{self.API}/tailnet/{self.tailnet}/keys", body,
                  {"Authorization": f"Bearer {self._token()}"})
        return r["id"], r["key"]

    def delete_auth_key(self, key_id: str) -> None:
        try:
            _call("DELETE", f"{self.API}/tailnet/{self.tailnet}/keys/{key_id}", None,
                  {"Authorization": f"Bearer {self._token()}"})
        except APIError as e:
            if e.code != 404:
                raise

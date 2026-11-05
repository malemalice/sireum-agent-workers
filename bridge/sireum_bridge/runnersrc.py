"""Runner source served by the hub, so each worker builds the exact image the hub expects.

No registry needed: the image tag is derived from the content of the runner files
(`sireum-runner:local-<hash12>`), the hub renders the Nomad job with that tag, and a
worker builds it from `GET /v1/nodes/<name>/runner.tgz`. Any change to the runner
files changes the tag, so a stale worker image can never be used by mistake.
"""
from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path

FILES = ("Dockerfile", "Dockerfile.browser", "entrypoint.py")


def src_dir() -> Path:
    env = os.environ.get("SIREUM_RUNNER_SRC")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "runner"


def digest(d: Path | None = None) -> str:
    d = d or src_dir()
    h = hashlib.sha256()
    for name in FILES:
        p = d / name
        h.update(name.encode() + b"\0")
        h.update(p.read_bytes() if p.exists() else b"")
        h.update(b"\0")
    return h.hexdigest()[:12]


def local_tag(d: Path | None = None) -> str:
    return f"sireum-runner:local-{digest(d)}"


def tarball(d: Path | None = None) -> bytes:
    d = d or src_dir()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in FILES:
            p = d / name
            if p.exists():
                info = tar.gettarinfo(str(p), arcname=name)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                with open(p, "rb") as f:
                    tar.addfile(info, f)
    return buf.getvalue()

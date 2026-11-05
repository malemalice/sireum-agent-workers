#!/usr/bin/env bash
# Draft — Sireum client bootstrap. Run INSIDE the worker Linux env
# (WSL2 Ubuntu on Windows, Lima Ubuntu on macOS, or native Linux). Review before running.
# Creates the internal job network, starts the egress proxy, prepares the cache.
set -euo pipefail

SQUID_IMAGE="${SQUID_IMAGE:-ubuntu/squid@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029}"
RUNNER_IMAGE="${RUNNER_IMAGE:-sireum-hub.<tailnet>.ts.net:5000/sireum-runner@sha256:<multi-arch-index-digest>}"
CACHE_DIR="${CACHE_DIR:-/srv/sireum/cache}"
CONF_DIR="/etc/sireum"
HERE="$(cd "$(dirname "$0")" && pwd)"

[[ "$(uname -s)" == "Linux" ]] || { echo "Run this inside the worker Linux env (WSL2 / Lima / Linux)" >&2; exit 1; }
command -v docker >/dev/null || { echo "Docker Engine required inside the Linux env" >&2; exit 1; }
command -v tailscale >/dev/null || { echo "Tailscale required inside the Linux env" >&2; exit 1; }

# Detect host OS for node meta (informational).
if grep -qi microsoft /proc/version 2>/dev/null; then HOST_OS=windows
elif [[ -n "${LIMA_INSTANCE:-}" ]] || [[ -d /mnt/lima-cidata ]]; then HOST_OS=macos
else HOST_OS=linux; fi
echo "host_os=${HOST_OS} arch=$(uname -m)"

# 1. Internal network: no route to anything but peers on it.
docker network inspect sireum-jobs >/dev/null 2>&1 \
  || docker network create --internal sireum-jobs

# 2. Egress proxy on the default bridge, attached to sireum-jobs as "egress".
install -d -m 0755 "$CONF_DIR"
# Point the allowlist's hub entry at this fleet's hub (from the enrolment, if known).
HUB_HOST="$(sed -E 's#^https?://##; s#[:/].*##' /etc/sireum/hub_url 2>/dev/null || true)"
if [[ -n "$HUB_HOST" ]]; then
  sed "s/sireum-hub\.<tailnet>\.ts\.net/$HUB_HOST/" "$HERE/egress/squid.conf" > "$CONF_DIR/squid.conf"
else
  install -m 0644 "$HERE/egress/squid.conf" "$CONF_DIR/squid.conf"
fi
docker rm -f sireum-egress >/dev/null 2>&1 || true
docker run -d --name sireum-egress --restart unless-stopped \
  -v "$CONF_DIR/squid.conf:/etc/squid/squid.conf:ro" \
  "$SQUID_IMAGE"
docker network connect --alias egress sireum-jobs sireum-egress

# 3. Cache volume owned by the runner uid.
install -d -o 10001 -g 10001 -m 0750 \
  "$CACHE_DIR" "$CACHE_DIR/mirrors" "$CACHE_DIR/work" "$CACHE_DIR/toolchains" \
  "$CACHE_DIR/results" "$CACHE_DIR/locks" "$CACHE_DIR/logs"

# 4. Pre-pull the runner image (multi-arch index resolves to this env's arch).
docker pull "$RUNNER_IMAGE"

echo "Bootstrap done. Next: client.hcl with meta host_os=${HOST_OS}, start nomad, store this node's seat tokens on the hub."

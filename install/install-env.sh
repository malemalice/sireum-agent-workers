#!/usr/bin/env bash
# Runs INSIDE the worker Linux env (Lima VM, WSL2 distro, or a Linux host), as root.
# Called by install.sh / install.ps1. DRAFT — not tested yet.
#
#   sudo SIREUM_HOST_OS=macos|windows|linux bash install-env.sh <join-code>
#
# Steps: decode join code → install Docker, Tailscale, Nomad → join tailnet →
#        enroll with the hub → write Nomad client config → bootstrap network,
#        egress proxy and cache → start Nomad → self-check → install `sireum` helper.
set -euo pipefail

JOIN_CODE="${1:?join code required}"
HOST_OS="${SIREUM_HOST_OS:-linux}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

say()  { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "Run as root (sudo)"
[[ "$(uname -s)" == "Linux" ]] || fail "This step runs inside Linux only"
. /etc/os-release
[[ "${ID:-}" == "ubuntu" || "${ID:-}" == "debian" ]] || fail "Ubuntu or Debian required (found ${ID:-unknown})"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg jq python3 >/dev/null

# 1. Decode the join code: sireum1.<base64url(json)>
say "Reading join code"
JOIN_JSON="$(python3 -c 'import sys,base64,json; p=sys.argv[1].split(".",1)[1]; p+="="*(-len(p)%4); print(json.dumps(json.loads(base64.urlsafe_b64decode(p))))' "$JOIN_CODE")" \
  || fail "Join code is not valid"
HUB_URL="$(jq -r .hub <<<"$JOIN_JSON")"
TS_KEY="$(jq -r .ts_auth_key <<<"$JOIN_JSON")"
ENROLL_TOKEN="$(jq -r .enroll_token <<<"$JOIN_JSON")"
EXPIRES="$(jq -r .expires <<<"$JOIN_JSON")"
echo "Hub: $HUB_URL (code expires $EXPIRES)"

# 2. Docker Engine (official apt repo)
if ! command -v docker >/dev/null; then
  say "Installing Docker Engine"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io >/dev/null
  systemctl enable --now docker
fi

# 3. Tailscale (official apt repo)
if ! command -v tailscale >/dev/null; then
  say "Installing Tailscale"
  curl -fsSL "https://pkgs.tailscale.com/stable/${ID}/${VERSION_CODENAME}.noarmor.gpg" -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL "https://pkgs.tailscale.com/stable/${ID}/${VERSION_CODENAME}.tailscale-keyring.list" -o /etc/apt/sources.list.d/tailscale.list
  apt-get update -qq
  apt-get install -y -qq tailscale >/dev/null
  systemctl enable --now tailscaled
fi
say "Joining the Sireum tailnet"
if tailscale status >/dev/null 2>&1; then
  echo "Already on a tailnet: $(tailscale status --json | jq -r '.Self.DNSName')"
elif [[ -n "$TS_KEY" ]]; then
  tailscale up --auth-key="$TS_KEY" --advertise-tags=tag:sireum-worker --hostname="sireum-$(hostname -s)"
else
  echo "Open the link below and log in to the same Tailscale account as the hub:"
  tailscale up --hostname="sireum-$(hostname -s)"
fi

# 4. Enroll with the hub → node name, Nomad settings, image digests
say "Registering this PC with the hub"
ENROLL="$(curl -fsS -X POST "$HUB_URL/v1/nodes/enroll" \
  -H "Authorization: Bearer $ENROLL_TOKEN" -H 'Content-Type: application/json' \
  -d "$(jq -n --arg os "$HOST_OS" --arg arch "$(uname -m)" --arg host "$(hostname -s)" \
        --argjson cpus "$(nproc)" --argjson mem_mb "$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 ))" \
        '{host_os:$os, arch:$arch, hostname:$host, cpus:$cpus, mem_mb:$mem_mb}')")" \
  || fail "The hub refused the join code (expired or already used?). Ask the operator for a new one."
NODE_NAME="$(jq -r .node_name <<<"$ENROLL")"
NOMAD_VERSION="$(jq -r .nomad_version <<<"$ENROLL")"
NOMAD_SERVERS="$(jq -c .nomad_servers <<<"$ENROLL")"
NOMAD_TOKEN="$(jq -r .nomad_client_token <<<"$ENROLL")"
NODE_SECRET="$(jq -r .node_secret <<<"$ENROLL")"
export RUNNER_IMAGE="$(jq -r .runner_image <<<"$ENROLL")"
export SQUID_IMAGE="$(jq -r .squid_image <<<"$ENROLL")"
REGISTRY="$(jq -r '.registry // empty' <<<"$ENROLL")"
REG_SERVER="$(jq -r '.registry_auth.server // empty' <<<"$ENROLL")"
REG_USER="$(jq -r '.registry_auth.username // empty' <<<"$ENROLL")"
REG_PASS="$(jq -r '.registry_auth.password // empty' <<<"$ENROLL")"

# The hub registry is plain HTTP inside the tailnet (WireGuard-encrypted).
if [[ -n "$REGISTRY" ]]; then
  say "Trusting the hub registry $REGISTRY"
  python3 - "$REGISTRY" <<'PY'
import json, os, sys
p = "/etc/docker/daemon.json"
d = json.load(open(p)) if os.path.exists(p) else {}
regs = set(d.get("insecure-registries", [])) | {sys.argv[1]}
d["insecure-registries"] = sorted(regs)
json.dump(d, open(p, "w"), indent=2)
PY
  systemctl restart docker
fi

# Private external registry (e.g. GHCR): read-only pull token from the hub.
DOCKER_AUTH_HCL=""
if [[ -n "$REG_USER" && -n "$REG_PASS" ]]; then
  say "Logging in to $REG_SERVER (read-only pull token)"
  printf '%s' "$REG_PASS" | docker login "$REG_SERVER" -u "$REG_USER" --password-stdin >/dev/null
  chmod 0600 /root/.docker/config.json
  DOCKER_AUTH_HCL='
plugin "docker" {
  config {
    auth {
      config = "/root/.docker/config.json"
    }
  }
}'
fi

# 5. Nomad (HashiCorp apt repo, pinned version from the hub)
if ! command -v nomad >/dev/null; then
  say "Installing Nomad $NOMAD_VERSION"
  curl -fsSL https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com ${VERSION_CODENAME} main" \
    > /etc/apt/sources.list.d/hashicorp.list
  apt-get update -qq
  apt-get install -y -qq "nomad=${NOMAD_VERSION}*" >/dev/null
fi

say "Writing Nomad client config"
install -d -m 0750 /etc/nomad.d /etc/sireum
cat > /etc/nomad.d/client.hcl <<EOF
datacenter = "dc1"
name       = "${NODE_NAME}"
data_dir   = "/opt/nomad/data"
bind_addr  = "{{ GetInterfaceIP \"tailscale0\" }}"

client {
  enabled = true
  servers = ${NOMAD_SERVERS}
  host_volume "sireum-cache" {
    path      = "/srv/sireum/cache"
    read_only = false
  }
  meta {
    pool    = "idle"
    os      = "linux"
    host_os = "${HOST_OS}"
    agents  = "claude,opencode"
  }
}

acl {
  enabled = true
}
${DOCKER_AUTH_HCL}
EOF
umask 077
printf 'NOMAD_TOKEN=%s\n' "$NOMAD_TOKEN" > /etc/sireum/nomad.env
printf '%s\n' "$HUB_URL" > /etc/sireum/hub_url
printf '%s\n' "$NODE_NAME" > /etc/sireum/node_name
printf '%s\n' "$NODE_SECRET" > /etc/sireum/node_secret
umask 022

# Runner image: built here from the hub's own source when the hub uses a local tag
# (no registry), so the tag always matches the Nomad job on the hub.
if [[ "$RUNNER_IMAGE" == sireum-runner:local-* ]]; then
  if docker image inspect "$RUNNER_IMAGE" >/dev/null 2>&1; then
    echo "Runner image $RUNNER_IMAGE already present"
  else
    say "Building the runner image $RUNNER_IMAGE (a few minutes, once)"
    BUILD_DIR="$(mktemp -d)"
    curl -fsS "$HUB_URL/v1/nodes/$NODE_NAME/runner.tgz" -H "Authorization: Bearer $NODE_SECRET" \
      | tar -xzf - -C "$BUILD_DIR" || fail "Could not download the runner source from the hub"
    docker build -q -t "$RUNNER_IMAGE" "$BUILD_DIR" >/dev/null || fail "Runner image build failed"
    rm -rf "$BUILD_DIR"
  fi
fi

# 6. Network, egress proxy, cache
say "Setting up the sandbox network and egress proxy"
bash "$ROOT/nomad/client-bootstrap.sh"

# 7. Start Nomad
mkdir -p /etc/systemd/system/nomad.service.d
printf '[Service]\nEnvironmentFile=/etc/sireum/nomad.env\n' > /etc/systemd/system/nomad.service.d/sireum.conf
systemctl daemon-reload
systemctl enable --now nomad

# 8. Helper command + disk alarm on the cache
install -m 0755 "$HERE/sireum" /usr/local/bin/sireum
printf '%s\n' "$RUNNER_IMAGE" > /etc/sireum/runner_image
install -m 0755 "$ROOT/hub/sireum-disk-alert.sh" /usr/local/bin/sireum-disk-alert
cat > /etc/cron.d/sireum-worker <<'CRON'
*/15 * * * * root /usr/local/bin/sireum-disk-alert /srv/sireum/cache
*/5 * * * *  root /usr/local/bin/sireum sync-egress >/dev/null 2>&1
CRON

# 9. Self-check
say "Checking"
sireum check || fail "Self-check failed; see above. Nothing is offered to the fleet until it passes."

say "This PC is now a Sireum worker: $NODE_NAME"

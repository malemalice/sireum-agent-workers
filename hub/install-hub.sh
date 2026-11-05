#!/usr/bin/env bash
# Set up sireum-hub on an always-on Ubuntu/Debian PC that is already on the tailnet.
#
#   sudo ./hub/install-hub.sh
#
# Idempotent: re-running keeps existing tokens, keys and config.
# Installs: Docker, Nomad server (+ ACL bootstrap and policies), registry (tailnet only),
# sireum-bridge (systemd, behind `tailscale serve` HTTPS), the sireum-runner Nomad job,
# snapshot + disk-alert crons. Prints the remaining manual steps at the end.
set -euo pipefail

NOMAD_VERSION="${NOMAD_VERSION:-2.1.1}"
REGISTRY_IMAGE="${REGISTRY_IMAGE:-registry@sha256:d4032d4cc323c1731c707931ecada07a53076eac38e3b61ae57533f170f2c6c3}"
SQUID_IMAGE="${SQUID_IMAGE:-ubuntu/squid@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
SECRETS=/srv/sireum/secrets
ENV_FILE=$SECRETS/bridge.env

say()  { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || fail "this installer is for Linux. On a Mac run (without sudo): ./hub/install-hub-macos.sh"
[[ $EUID -eq 0 ]] || fail "run as root"
. /etc/os-release
[[ "$ID" == ubuntu || "$ID" == debian ]] || fail "Ubuntu or Debian required"
command -v tailscale >/dev/null && tailscale status >/dev/null 2>&1 || fail "join this PC to the tailnet first (tailscale up)"
TS_IP="$(tailscale ip -4 | head -1)"
TS_NAME="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
HUB_URL="https://$TS_NAME"
echo "hub: $TS_NAME ($TS_IP)"

export DEBIAN_FRONTEND=noninteractive
say "Packages"
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg git jq python3 python3-venv sqlite3 rsync >/dev/null
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq && apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin >/dev/null
fi
if ! command -v nomad >/dev/null; then
  curl -fsSL https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $VERSION_CODENAME main" \
    > /etc/apt/sources.list.d/hashicorp.list
  apt-get update -qq && apt-get install -y -qq "nomad=${NOMAD_VERSION}*" >/dev/null
fi

say "User, directories, source"
id sireum >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin sireum
install -d -m 0755 /srv/sireum /srv/sireum/bridge /srv/sireum/registry /opt/sireum
install -d -m 0700 -o sireum -g sireum /srv/sireum/state $SECRETS
install -d -m 0700 /srv/sireum/nomad /srv/sireum/backups
rsync -a --delete --exclude .git "$SRC/" /opt/sireum/src/
[[ -d /opt/sireum/venv ]] || python3 -m venv /opt/sireum/venv
/opt/sireum/venv/bin/pip install -q -r /opt/sireum/src/bridge/requirements.txt
for f in git_hosts.yaml repos.yaml; do
  [[ -f /srv/sireum/bridge/$f ]] || install -m 0644 "/opt/sireum/src/bridge/config.example/$f" /srv/sireum/bridge/$f
done
# The bridge edits repos.yaml when you grant access, so it owns its config directory.
chown -R sireum:sireum /srv/sireum/bridge

say "Nomad server"
install -m 0644 /opt/sireum/src/hub/nomad-server.hcl /etc/nomad.d/nomad.hcl
systemctl enable --now nomad
export NOMAD_ADDR="http://$TS_IP:4646"
for _ in $(seq 60); do nomad status >/dev/null 2>&1 && break; curl -fs "$NOMAD_ADDR/v1/status/leader" >/dev/null && break; sleep 1; done
if [[ ! -f $SECRETS/nomad-mgmt.token ]]; then
  nomad acl bootstrap -json | jq -r .SecretID > $SECRETS/nomad-mgmt.token
  chmod 0600 $SECRETS/nomad-mgmt.token
fi
export NOMAD_TOKEN="$(cat $SECRETS/nomad-mgmt.token)"
nomad acl policy apply -description "sireum-bridge" sireum-bridge /opt/sireum/src/hub/policies/sireum-bridge.hcl
nomad acl policy apply -description "worker sireum helper" sireum-worker-node /opt/sireum/src/hub/policies/sireum-worker-node.hcl

# Optional own registry (USE_REGISTRY=1). Default: none — workers build the runner image
# from this hub's source (tag sireum-runner:local-<hash>).
if [[ -n "${USE_REGISTRY:-}" ]]; then
  say "Registry (tailnet only, $TS_IP:5000)"
  docker inspect sireum-registry >/dev/null 2>&1 || docker run -d --restart unless-stopped --name sireum-registry \
    -p "$TS_IP:5000:5000" -v /srv/sireum/registry:/var/lib/registry "$REGISTRY_IMAGE" >/dev/null
fi

say "Bridge config"
gen() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
if [[ ! -f $ENV_FILE ]]; then
  BRIDGE_NOMAD_TOKEN="$(nomad acl token create -name sireum-bridge -policy sireum-bridge -json | jq -r .SecretID)"
  NODE_TOKEN="$(nomad acl token create -name sireum-workers -policy sireum-worker-node -json | jq -r .SecretID)"
  sed -e "s#https://sireum-hub.TAILNET.ts.net#$HUB_URL#" \
      -e "s#sireum-hub.TAILNET.ts.net#$TS_NAME#g" \
      -e "s#TAILSCALE_IP#$TS_IP#g" \
      -e "s#^WORKER_TOKEN=.*#WORKER_TOKEN=$(gen)#" \
      -e "s#^OPERATOR_TOKEN=.*#OPERATOR_TOKEN=$(gen)#" \
      -e "s#^NOMAD_TOKEN=.*#NOMAD_TOKEN=$BRIDGE_NOMAD_TOKEN#" \
      -e "s#^NOMAD_NODE_TOKEN=.*#NOMAD_NODE_TOKEN=$NODE_TOKEN#" \
      -e "s#^NOMAD_VERSION=.*#NOMAD_VERSION=$NOMAD_VERSION#" \
      -e "s#^SQUID_IMAGE=.*#SQUID_IMAGE=$SQUID_IMAGE#" \
      -e "s#^REGISTRY=.*#REGISTRY=${USE_REGISTRY:+$TS_NAME:5000}#" \
      -e "s#^RUNNER_IMAGE=.*#RUNNER_IMAGE=${USE_REGISTRY:+$TS_NAME:5000/sireum-runner:base}#" \
      /opt/sireum/src/hub/bridge.env.example > $ENV_FILE
  chown sireum:sireum $ENV_FILE; chmod 0600 $ENV_FILE
fi
[[ -f $SECRETS/kanban_ed25519 ]] || { ssh-keygen -q -t ed25519 -N '' -C sireum-hub -f $SECRETS/kanban_ed25519; \
  chown sireum:sireum $SECRETS/kanban_ed25519*; }
set -a; . $ENV_FILE; set +a

say "Runner job"
nomad var put -force nomad/jobs/sireum-runner worker_token="$WORKER_TOKEN" >/dev/null
JOB_IMAGE="${RUNNER_IMAGE:-$(PYTHONPATH=/opt/sireum/src/bridge /opt/sireum/venv/bin/python -c \
  'from sireum_bridge import runnersrc; print(runnersrc.local_tag())')}"
sed -e "s#https://sireum-hub.<tailnet>.ts.net#$HUB_URL#" \
    -e "s#sireum-hub.<tailnet>.ts.net:5000/sireum-runner@sha256:<multi-arch-index-digest>#$JOB_IMAGE#" \
    /opt/sireum/src/nomad/sireum-runner.nomad.hcl > /srv/sireum/sireum-runner.nomad.hcl
nomad job run -detach /srv/sireum/sireum-runner.nomad.hcl >/dev/null

say "Bridge service + HTTPS"
install -m 0644 /opt/sireum/src/hub/sireum-bridge.service /etc/systemd/system/sireum-bridge.service
systemctl daemon-reload
systemctl enable sireum-bridge >/dev/null
systemctl restart sireum-bridge
tailscale serve --bg --https=443 http://127.0.0.1:8787 >/dev/null

say "Cron"
cat > /etc/cron.d/sireum <<EOF
15 3 * * *  root  NOMAD_MGMT_TOKEN=\$(cat $SECRETS/nomad-mgmt.token) /opt/sireum/src/hub/sireum-snapshot.sh >>/var/log/sireum-snapshot.log 2>&1
*/15 * * * * root /opt/sireum/src/hub/sireum-disk-alert.sh /srv/sireum
0 4 * * 0   sireum for m in /srv/sireum/state/git/*.git; do git -C "\$m" gc --quiet --prune=now; done
EOF

for _ in $(seq 20); do curl -fs http://127.0.0.1:8787/healthz >/dev/null && break; sleep 0.5; done
curl -fs http://127.0.0.1:8787/healthz >/dev/null && echo "bridge: healthy" || echo "bridge: NOT healthy — journalctl -u sireum-bridge"

cat <<EOF

Hub is up: $HUB_URL
Next:
  sudo sireum-hub add-repo owner/repo                          # per repo (uses your gh login)
  sudo sireum-hub connect-hermes you@hermes-host --taggers a,b # once
  sudo sireum-hub invite --name <pc>                           # per worker PC
Optional, later: Tailscale ACL (tag:sireum-worker -> $TS_NAME:443,4647), OFFSITE backups in $ENV_FILE.
Operator token for /status and /v1/dispatch: grep OPERATOR_TOKEN $ENV_FILE
EOF

cat > /usr/local/bin/sireum-hub <<EOF
#!/usr/bin/env bash
# Operator CLI on the hub (run with sudo): sireum-hub --help
[[ \$EUID -eq 0 ]] || exec sudo "\$0" "\$@"
set -a; . $ENV_FILE; set +a
export PYTHONPATH=/opt/sireum/src/bridge SIREUM_CONFIG=/srv/sireum/bridge SIREUM_ENV_FILE=$ENV_FILE \\
  SIREUM_SRC=/opt/sireum/src SIREUM_SERVICE_USER=sireum
cd /opt/sireum/src/bridge && exec /opt/sireum/venv/bin/python -m sireum_bridge --state /srv/sireum/state "\$@"
EOF
chmod 0755 /usr/local/bin/sireum-hub

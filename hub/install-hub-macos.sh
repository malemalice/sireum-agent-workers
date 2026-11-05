#!/usr/bin/env bash
# sireum-hub on macOS, natively: no VM, no Docker.
#
#   ./hub/install-hub-macos.sh            # run as your normal user; asks for sudo when needed
#
# Installs under /opt/sireum, runs as the current user via LaunchDaemons (start at boot,
# no login needed):
#   - Nomad 2.1.1 server (pinned binary from releases.hashicorp.com, SHA-256 checked),
#     ACL bootstrap + the sireum-bridge / sireum-worker-node policies
#   - sireum-bridge (Python venv) on 127.0.0.1:8787, published by `tailscale serve` (HTTPS)
#   - the sireum-runner Nomad job (needs RUNNER_IMAGE, e.g. a private GHCR image)
#   - snapshot (daily), disk alert (15 min), git gc (weekly), keep-awake (caffeinate -s)
#   - `sireum-hub` operator command
# The runner image lives in an external registry (GHCR by default). Workers pull it with a
# read-only token that the hub hands out at enrolment (REGISTRY_USERNAME / REGISTRY_PASSWORD).
#
# Optional env on first run (all can be set later in /opt/sireum/secrets/bridge.env):
#   RUNNER_IMAGE=ghcr.io/<org>/sireum-runner@sha256:<digest>
#   REGISTRY_SERVER=ghcr.io REGISTRY_USERNAME=<user> REGISTRY_PASSWORD=<PAT with read:packages>
# Re-runnable: keeps existing tokens, keys and config.
# Debug aid: `./hub/install-hub-macos.sh --render-plists DIR` writes the launchd plists to DIR only.
set -euo pipefail

NOMAD_VERSION="2.1.1"
NOMAD_SHA_arm64="5aa9e30a8649608b790daad92177a40e7ca0109b03205a587ede9850811d1a77"
NOMAD_SHA_amd64="c63035fb9d8fa760189a664d7e25a1083d6f5dcf520060b454ace263f779ef0e"
SQUID_IMAGE="${SQUID_IMAGE:-ubuntu/squid@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029}"
HOME_DIR=/opt/sireum
SECRETS=$HOME_DIR/secrets
ENV_FILE=$SECRETS/bridge.env
LD=/Library/LaunchDaemons
RUN_USER="${SUDO_USER:-$(id -un)}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# -- launchd plists ---------------------------------------------------------------
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
# plist <label> <run-as-user|-> <keepalive 0|1> <schedule-xml|-> <args...>
plist() {
  local label=$1 user=$2 keep=$3 sched=$4; shift 4
  printf '<?xml version="1.0" encoding="UTF-8"?>\n'
  printf '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
  printf '<plist version="1.0"><dict>\n  <key>Label</key><string>%s</string>\n' "$label"
  [[ "$user" != - ]] && printf '  <key>UserName</key><string>%s</string>\n' "$user"
  printf '  <key>ProgramArguments</key><array>\n'
  local a; for a in "$@"; do printf '    <string>%s</string>\n' "$(xml "$a")"; done
  printf '  </array>\n'
  if [[ "$sched" != - ]]; then printf '  %s\n' "$sched"; else printf '  <key>RunAtLoad</key><true/>\n'; fi
  [[ "$keep" == 1 ]] && printf '  <key>KeepAlive</key><true/>\n  <key>ThrottleInterval</key><integer>10</integer>\n'
  printf '  <key>StandardOutPath</key><string>%s/logs/%s.log</string>\n' "$HOME_DIR" "${label#id.sireum.}"
  printf '  <key>StandardErrorPath</key><string>%s/logs/%s.log</string>\n' "$HOME_DIR" "${label#id.sireum.}"
  printf '</dict></plist>\n'
}

render_plists() {  # render_plists <dir>
  local d=$1; mkdir -p "$d"
  plist id.sireum.nomad  "$RUN_USER" 1 - "$HOME_DIR/bin/nomad" agent -config "$HOME_DIR/nomad.hcl" > "$d/id.sireum.nomad.plist"
  plist id.sireum.bridge "$RUN_USER" 1 - "$HOME_DIR/bin/run-bridge" > "$d/id.sireum.bridge.plist"
  plist id.sireum.awake  - 1 - /usr/bin/caffeinate -s > "$d/id.sireum.awake.plist"
  plist id.sireum.snapshot "$RUN_USER" 0 \
    '<key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>15</integer></dict>' \
    /bin/bash -c "export PATH=$HOME_DIR/bin:/usr/bin:/bin:/usr/sbin:/sbin; set -a; . $ENV_FILE; set +a; NOMAD_MGMT_TOKEN=\$(cat $SECRETS/nomad-mgmt.token) SIREUM_HOME=$HOME_DIR exec $HOME_DIR/src/hub/sireum-snapshot.sh" \
    > "$d/id.sireum.snapshot.plist"
  plist id.sireum.disk-alert "$RUN_USER" 0 '<key>StartInterval</key><integer>900</integer>' \
    /usr/bin/env SIREUM_HOME=$HOME_DIR $HOME_DIR/src/hub/sireum-disk-alert.sh $HOME_DIR > "$d/id.sireum.disk-alert.plist"
  plist id.sireum.git-gc "$RUN_USER" 0 \
    '<key>StartCalendarInterval</key><dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>4</integer></dict>' \
    /bin/bash -c "for m in $HOME_DIR/state/git/*.git; do [ -d \"\$m\" ] && git -C \"\$m\" gc --quiet --prune=now; done" \
    > "$d/id.sireum.git-gc.plist"
}

if [[ "${1:-}" == --render-plists ]]; then
  render_plists "${2:?directory}"
  exit 0
fi

# -- checks -----------------------------------------------------------------------
[[ "$(uname -s)" == Darwin ]] || fail "macOS only. On Linux run: sudo ./hub/install-hub.sh"
[[ $EUID -ne 0 ]] || fail "run as your normal user (not with sudo); it asks for sudo when needed"
case "$(uname -m)" in arm64) ARCH=arm64; NOMAD_SHA=$NOMAD_SHA_arm64 ;; x86_64) ARCH=amd64; NOMAD_SHA=$NOMAD_SHA_amd64 ;;
  *) fail "unsupported CPU $(uname -m)" ;; esac
command -v tailscale >/dev/null || fail "Tailscale CLI not found. Install Tailscale and enable its CLI (Settings → Install CLI)"
tailscale status >/dev/null 2>&1 || fail "Tailscale is not connected; log in first"
PY=$(command -v python3) || fail "python3 required (xcode-select --install, or brew install python)"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || fail "python3 >= 3.9 required"
for t in git sqlite3 rsync ssh-keygen curl shasum unzip; do command -v $t >/dev/null || fail "$t required"; done
[[ -x "$(git --exec-path)/git-http-backend" ]] || fail "git http-backend missing (install Xcode command line tools or brew git)"

TS_IP="$(tailscale ip -4 | head -1)"
TS_NAME="$(tailscale status --json | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
HUB_URL="https://$TS_NAME"
echo "hub: $TS_NAME ($TS_IP), runs as $RUN_USER"

# -- files ------------------------------------------------------------------------
say "Directories and source ($HOME_DIR)"
sudo install -d -m 0755 -o "$RUN_USER" -g staff $HOME_DIR
install -d -m 0755 $HOME_DIR/bin $HOME_DIR/bridge $HOME_DIR/logs
install -d -m 0700 $HOME_DIR/state $HOME_DIR/nomad $HOME_DIR/backups $SECRETS
rsync -a --delete --exclude .git --exclude __pycache__ "$SRC/" $HOME_DIR/src/
[[ -d $HOME_DIR/venv ]] || "$PY" -m venv $HOME_DIR/venv
$HOME_DIR/venv/bin/pip install -q --upgrade pip >/dev/null
$HOME_DIR/venv/bin/pip install -q -r $HOME_DIR/src/bridge/requirements.txt
for f in git_hosts.yaml repos.yaml; do
  [[ -f $HOME_DIR/bridge/$f ]] || sed "s#/srv/sireum#$HOME_DIR#g" "$HOME_DIR/src/bridge/config.example/$f" > $HOME_DIR/bridge/$f
done

say "Nomad $NOMAD_VERSION (darwin_$ARCH)"
if [[ "$($HOME_DIR/bin/nomad version 2>/dev/null | head -1)" != "Nomad v$NOMAD_VERSION"* ]]; then
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/nomad.zip" "https://releases.hashicorp.com/nomad/$NOMAD_VERSION/nomad_${NOMAD_VERSION}_darwin_${ARCH}.zip"
  echo "$NOMAD_SHA  $tmp/nomad.zip" | shasum -a 256 -c - >/dev/null || fail "Nomad download checksum mismatch"
  unzip -q -o "$tmp/nomad.zip" -d "$tmp" && install -m 0755 "$tmp/nomad" $HOME_DIR/bin/nomad
  rm -rf "$tmp"
fi
# macOS has no tailscale0 interface name; bind to the tailnet IP (re-run if it ever changes).
sed -e "s#^data_dir .*#data_dir   = \"$HOME_DIR/nomad\"#" \
    -e "s#^bind_addr .*#bind_addr  = \"$TS_IP\"#" \
    $HOME_DIR/src/hub/nomad-server.hcl > $HOME_DIR/nomad.hcl

cat > $HOME_DIR/bin/run-bridge <<EOF
#!/bin/bash
set -a; . $ENV_FILE; set +a
export HOME="$HOME" PATH="$HOME_DIR/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH=$HOME_DIR/src/bridge
cd $HOME_DIR/src/bridge
exec $HOME_DIR/venv/bin/python -m sireum_bridge serve --config $HOME_DIR/bridge --state $HOME_DIR/state --listen 127.0.0.1:8787
EOF
chmod 0755 $HOME_DIR/bin/run-bridge

# install_stdin <dest> <mode>: BSD install(1) on macOS can't read a pipe, so stage via a file.
install_stdin() {
  local tmp; tmp=$(mktemp)
  cat > "$tmp"
  sudo install -d -m 0755 "$(dirname "$1")"
  sudo install -m "$2" "$tmp" "$1"
  rm -f "$tmp"
}

# -- launchd ----------------------------------------------------------------------
load() {  # load <label> (from the rendered dir)
  sudo launchctl bootout system/"$1" 2>/dev/null || true
  sudo install -m 0644 -o root -g wheel "$RENDER/$1.plist" "$LD/$1.plist"
  sudo launchctl bootstrap system "$LD/$1.plist"
}
RENDER=$(mktemp -d)
render_plists "$RENDER"
for p in "$RENDER"/*.plist; do plutil -lint -s "$p" || fail "bad plist $p"; done

say "Starting Nomad"
load id.sireum.nomad
export NOMAD_ADDR="http://$TS_IP:4646"
for _ in $(seq 60); do curl -fs "$NOMAD_ADDR/v1/status/leader" | grep -q : && break; sleep 1; done
curl -fs "$NOMAD_ADDR/v1/status/leader" | grep -q : || fail "Nomad did not elect a leader; see $HOME_DIR/logs/nomad.log"
NOMAD=$HOME_DIR/bin/nomad
js() { "$PY" -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }
if [[ ! -s $SECRETS/nomad-mgmt.token ]]; then
  ( umask 077; $NOMAD acl bootstrap -json | js SecretID > $SECRETS/nomad-mgmt.token )
fi
export NOMAD_TOKEN="$(cat $SECRETS/nomad-mgmt.token)"
$NOMAD acl policy apply -description "sireum-bridge" sireum-bridge $HOME_DIR/src/hub/policies/sireum-bridge.hcl
$NOMAD acl policy apply -description "worker sireum helper" sireum-worker-node $HOME_DIR/src/hub/policies/sireum-worker-node.hcl

say "Bridge config"
gen() { "$PY" -c 'import secrets; print(secrets.token_urlsafe(32))'; }
if [[ ! -f $ENV_FILE ]]; then
  BRIDGE_NOMAD_TOKEN="$($NOMAD acl token create -name sireum-bridge -policy sireum-bridge -json | js SecretID)"
  NODE_TOKEN="$($NOMAD acl token create -name sireum-workers -policy sireum-worker-node -json | js SecretID)"
  ( umask 077
    sed -e "s#https://sireum-hub.TAILNET.ts.net#$HUB_URL#" \
        -e "s#TAILSCALE_IP#$TS_IP#g" \
        -e "s#/srv/sireum#$HOME_DIR#g" \
        -e "s#^WORKER_TOKEN=.*#WORKER_TOKEN=$(gen)#" \
        -e "s#^OPERATOR_TOKEN=.*#OPERATOR_TOKEN=$(gen)#" \
        -e "s#^NOMAD_TOKEN=.*#NOMAD_TOKEN=$BRIDGE_NOMAD_TOKEN#" \
        -e "s#^NOMAD_NODE_TOKEN=.*#NOMAD_NODE_TOKEN=$NODE_TOKEN#" \
        -e "s#^NOMAD_VERSION=.*#NOMAD_VERSION=$NOMAD_VERSION#" \
        -e "s#^SQUID_IMAGE=.*#SQUID_IMAGE=$SQUID_IMAGE#" \
        -e "s#^REGISTRY=.*#REGISTRY=#" \
        -e "s#^RUNNER_IMAGE=.*#RUNNER_IMAGE=${RUNNER_IMAGE:-}#" \
        -e "s#^REGISTRY_SERVER=.*#REGISTRY_SERVER=${REGISTRY_SERVER:-ghcr.io}#" \
        -e "s#^REGISTRY_USERNAME=.*#REGISTRY_USERNAME=${REGISTRY_USERNAME:-}#" \
        -e "s#^REGISTRY_PASSWORD=.*#REGISTRY_PASSWORD=${REGISTRY_PASSWORD:-}#" \
        $HOME_DIR/src/hub/bridge.env.example > $ENV_FILE )
fi
[[ -f $SECRETS/kanban_ed25519 ]] || ssh-keygen -q -t ed25519 -N '' -C sireum-hub -f $SECRETS/kanban_ed25519
set -a; . $ENV_FILE; set +a
export NOMAD_TOKEN="$(cat $SECRETS/nomad-mgmt.token)"

say "Runner job"
$NOMAD var put -force nomad/jobs/sireum-runner worker_token="$WORKER_TOKEN" >/dev/null
# Empty RUNNER_IMAGE (default) ⇒ workers build sireum-runner:local-<hash> from this hub's source.
JOB_IMAGE="${RUNNER_IMAGE:-$(PYTHONPATH=$HOME_DIR/src/bridge $HOME_DIR/venv/bin/python -c \
  'from sireum_bridge import runnersrc; print(runnersrc.local_tag())')}"
sed -e "s#https://sireum-hub.<tailnet>.ts.net#$HUB_URL#" \
    -e "s#sireum-hub.<tailnet>.ts.net:5000/sireum-runner@sha256:<multi-arch-index-digest>#$JOB_IMAGE#" \
    $HOME_DIR/src/nomad/sireum-runner.nomad.hcl > $HOME_DIR/sireum-runner.nomad.hcl
$NOMAD job run -detach $HOME_DIR/sireum-runner.nomad.hcl >/dev/null && echo "sireum-runner job registered ($JOB_IMAGE)"

say "Bridge + HTTPS + housekeeping"
for l in id.sireum.bridge id.sireum.awake id.sireum.snapshot id.sireum.disk-alert id.sireum.git-gc; do load $l; done
tailscale serve --bg --https=443 http://127.0.0.1:8787 >/dev/null
sudo tee /etc/newsyslog.d/sireum.conf >/dev/null <<EOF
# logfile                         owner:group      mode count size(KB) when flags
$HOME_DIR/logs/*.log              $RUN_USER:staff  644  5     10240    *    GJ
EOF

install_stdin /usr/local/bin/sireum-hub 0755 <<EOF
#!/bin/bash
# Operator CLI: sireum-hub --help
set -a; . $ENV_FILE; set +a
export PYTHONPATH=$HOME_DIR/src/bridge SIREUM_CONFIG=$HOME_DIR/bridge SIREUM_ENV_FILE=$ENV_FILE SIREUM_SRC=$HOME_DIR/src
export SIREUM_SERVICE_USER=
exec $HOME_DIR/venv/bin/python -m sireum_bridge --state $HOME_DIR/state "\$@"
EOF
install_stdin /usr/local/bin/sireum-hub-uninstall 0755 <<EOF
#!/bin/bash
# Remove the macOS hub (keeps $HOME_DIR unless --purge).
for l in id.sireum.nomad id.sireum.bridge id.sireum.awake id.sireum.snapshot id.sireum.disk-alert id.sireum.git-gc; do
  sudo launchctl bootout system/\$l 2>/dev/null; sudo rm -f $LD/\$l.plist
done
tailscale serve --https=443 off 2>/dev/null || true
sudo rm -f /etc/newsyslog.d/sireum.conf /usr/local/bin/sireum-hub
[[ "\${1:-}" == --purge ]] && sudo rm -rf $HOME_DIR
sudo rm -f /usr/local/bin/sireum-hub-uninstall
echo "Removed. Also remove this machine's Sireum workers' join codes and rotate git host keys if retiring the hub."
EOF

for _ in $(seq 30); do curl -fs http://127.0.0.1:8787/healthz >/dev/null && break; sleep 0.5; done
curl -fs http://127.0.0.1:8787/healthz >/dev/null && echo "bridge: healthy" || echo "bridge: NOT healthy — see $HOME_DIR/logs/bridge.log"

cat <<EOF

Hub is up natively on this Mac: $HUB_URL
Logs: $HOME_DIR/logs/   Config: $HOME_DIR/bridge/   Secrets: $ENV_FILE

Next:
  sireum-hub add-repo owner/repo                          # per repo (uses your gh login)
  sireum-hub connect-hermes you@hermes-host --taggers a,b # once
  sireum-hub invite --name <pc>                           # per worker PC
Optional, later: Tailscale ACL (tag:sireum-worker -> $TS_NAME:443,4647), OFFSITE backups in
$ENV_FILE, sudo pmset -c sleep 0 (keep the Mac awake on power).
Uninstall: sireum-hub-uninstall [--purge]
EOF

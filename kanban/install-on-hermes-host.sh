#!/usr/bin/env bash
# Install sireum-kanban on hermes-host. Adds a user and one authorized_keys line; no service.
#   sudo ./install-on-hermes-host.sh "<sireum-hub public key line>"
set -euo pipefail
PUB="${1:?pass the hub public key (contents of /srv/sireum/secrets/kanban_ed25519.pub)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
id sireum >/dev/null 2>&1 || useradd --create-home --shell /bin/bash sireum
install -m 0755 "$HERE/sireum-kanban" /usr/local/bin/sireum-kanban
[[ -f /etc/sireum-kanban.conf ]] || install -m 0644 "$HERE/sireum-kanban.conf.example" /etc/sireum-kanban.conf
install -d -m 0700 -o sireum -g sireum ~sireum/.ssh
LINE="command=\"/usr/local/bin/sireum-kanban\",restrict $PUB"
grep -qxF "$LINE" ~sireum/.ssh/authorized_keys 2>/dev/null || echo "$LINE" >> ~sireum/.ssh/authorized_keys
chown sireum:sireum ~sireum/.ssh/authorized_keys; chmod 0600 ~sireum/.ssh/authorized_keys
echo "Installed. The sireum user must be able to run the hermes CLI against the kanban DB"
echo "(group membership or a sudoers rule for exactly that binary). Then edit /etc/sireum-kanban.conf"
echo "and test from the hub:  ssh -i /srv/sireum/secrets/kanban_ed25519 sireum@<hermes-host> list-remote"
echo "Rollback: remove the line from ~sireum/.ssh/authorized_keys."

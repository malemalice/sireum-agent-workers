#!/usr/bin/env bash
# Install sireum-kanban on hermes-host. Adds one authorized_keys line; no service.
#
# Root install (adds/uses a dedicated user, needs sudo):
#   sudo ./install-on-hermes-host.sh "<hub pubkey>" [target_user] [hermes_bin] [hermes_home]
#
# Local install (no sudo; runs as the SSH login user):
#   ./install-on-hermes-host.sh --local "<hub pubkey>" [ignored] [hermes_bin] [hermes_home]
set -euo pipefail

LOCAL=0
if [[ "${1:-}" == "--local" ]]; then LOCAL=1; shift; fi
PUB="${1:?pass the hub public key line (contents of the hub's kanban_ed25519.pub)}"
TARGET_USER="${2:-sireum}"
HERMES_BIN="${3:-hermes}"
HERMES_HOME="${4:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ "$LOCAL" == "1" ]]; then
  BIN_DIR="$HOME/.local/bin"
  CONF="${XDG_CONFIG_HOME:-$HOME/.config}/sireum-kanban.conf"
  AUTH="$HOME/.ssh/authorized_keys"
  OWNR="$(id -un)"
  GRP="$(id -gn)"
  install -d -m 0755 "$BIN_DIR"
  install -d -m 0700 "$HOME/.ssh"
else
  [[ "$TARGET_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "invalid target user" >&2; exit 2; }
  id "$TARGET_USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$TARGET_USER"
  BIN_DIR="/usr/local/bin"
  CONF="/etc/sireum-kanban.conf"
  HOME_DIR="$(eval echo "~$TARGET_USER")"
  AUTH="$HOME_DIR/.ssh/authorized_keys"
  OWNR="$TARGET_USER"
  GRP="$(id -gn "$TARGET_USER")"
  install -d -m 0700 -o "$OWNR" -g "$GRP" "$HOME_DIR/.ssh"
fi

install -m 0755 "$HERE/sireum-kanban" "$BIN_DIR/sireum-kanban"

if [[ ! -f "$CONF" ]]; then
  mkdir -p "$(dirname "$CONF")"
  sed -e "s|^HERMES=.*|HERMES=$HERMES_BIN|" \
      -e "s|^HERMES_HOME=.*|HERMES_HOME=$HERMES_HOME|" \
      "$HERE/sireum-kanban.conf.example" > "$CONF"
  chmod 0644 "$CONF"
fi

touch "$AUTH"
if [[ "$LOCAL" == "1" ]]; then
  LINE="command=\"SIREUM_KANBAN_CONF=$(printf %q "$CONF") $(printf %q "$BIN_DIR/sireum-kanban")\",restrict $PUB"
else
  LINE="command=\"$(printf %q "$BIN_DIR/sireum-kanban")\",restrict $PUB"
fi
grep -qxF "$LINE" "$AUTH" 2>/dev/null || echo "$LINE" >> "$AUTH"
chown "$OWNR:$GRP" "$AUTH"
chmod 0600 "$AUTH"

echo "Installed $BIN_DIR/sireum-kanban"
echo "Config:  $CONF"
echo "Key:     $AUTH"
echo "Test from the hub: ssh -i <kanban_ed25519> ${TARGET_USER}@<hermes-host> list-remote"
echo "Rollback: remove the sireum-kanban line from $AUTH"

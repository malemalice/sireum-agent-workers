#!/usr/bin/env bash
# Daily: Nomad snapshot + bridge DB copy, then ship off the hub. Cron:  15 3 * * *  root  /opt/sireum/src/hub/sireum-snapshot.sh
set -euo pipefail
H="${SIREUM_HOME:-/srv/sireum}"   # /opt/sireum on a macOS hub
. "$H/secrets/bridge.env"
OUT="$H/backups"; mkdir -p "$OUT"; chmod 0700 "$OUT"
TS=$(date +%Y%m%d-%H%M)
NOMAD_ADDR="$NOMAD_ADDR" NOMAD_TOKEN="${NOMAD_MGMT_TOKEN:-$NOMAD_TOKEN}" nomad operator snapshot save "$OUT/nomad-$TS.snap"
sqlite3 "$H/state/state.db" ".backup '$OUT/state-$TS.db'"
find "$OUT" -type f -mtime +14 -delete
# Off-host copy (set OFFSITE, e.g. user@nas:/backups/sireum or an rclone remote).
if [[ -n "${OFFSITE:-}" ]]; then
  rsync -a "$OUT/" "$OFFSITE/"
else
  echo "WARNING: OFFSITE not set; snapshots exist only on the hub" >&2
fi

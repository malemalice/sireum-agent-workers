#!/usr/bin/env bash
# Disk alarm at 80% for /srv/sireum (hub) or /srv/sireum/cache (worker). Cron: */15 * * * *
set -euo pipefail
DIR="${1:-/srv/sireum}"; LIMIT="${LIMIT:-80}"
USE=$(df -P "$DIR" | awk 'NR==2 {gsub("%","",$5); print $5}')
(( USE < LIMIT )) && exit 0
MSG="Sireum disk alarm on $(hostname): $DIR at ${USE}% (limit ${LIMIT}%)"
logger -t sireum "$MSG"
if [[ -f /srv/sireum/secrets/bridge.env ]]; then . /srv/sireum/secrets/bridge.env; fi
if [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
  curl -fsS -m 10 -H 'Content-Type: application/json' -d "{\"content\":\"$MSG\"}" "$DISCORD_WEBHOOK_URL" >/dev/null || true
fi

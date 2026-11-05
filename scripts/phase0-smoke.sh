#!/usr/bin/env bash
# Phase 0 smoke test on one machine (MBP M2): bridge stub on the host, runner in a
# hardened container on an internal Docker network, egress only via squid.
#
#   scripts/phase0-smoke.sh            # fake executor (no model, no tokens)
#   EXECUTOR=claude CLAUDE_CODE_OAUTH_TOKEN=... scripts/phase0-smoke.sh   # real agent
#
# Needs: docker, git, python3 + PyYAML. Leaves state in $WORK (printed at the end).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$(mktemp -d -t sireum-phase0)}"
PORT="${PORT:-18787}"   # not 8787: a real hub on this machine may be listening there
IMAGE="${IMAGE:-sireum-runner:base-dev}"
SQUID_IMAGE="${SQUID_IMAGE:-ubuntu/squid@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029}"
EXECUTOR="${EXECUTOR:-fake}"
HUB_HOST="${HUB_HOST:-host.docker.internal}"   # how containers reach the stub hub
WORKER_TOKEN="w-$(openssl rand -hex 12)"
OPERATOR_TOKEN="op-$(openssl rand -hex 12)"
JOB_ID="p0-$(date +%H%M%S)"
pass=0; fail=0
ok()  { echo "  ✓ $*"; pass=$((pass+1)); }
bad() { echo "  ✗ $*"; fail=$((fail+1)); }

cleanup() {
  [[ -n "${BRIDGE_PID:-}" ]] && kill "$BRIDGE_PID" 2>/dev/null || true
  docker rm -f sireum-egress >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== workdir $WORK"
mkdir -p "$WORK"/{host,config,state,cache}
chmod 0777 "$WORK/cache"   # runner is uid 10001 inside the container

# 1. Sandbox git host (bare repo) with a tools manifest.
if [[ ! -d "$WORK/host/sireum-sandbox.git" ]]; then
  seed="$WORK/seed"; git init -q -b main "$seed"
  mkdir -p "$seed/.agent"
  cat > "$seed/.agent/tools.yaml" <<'EOF'
version: 1
services: [postgres, redis]
test_targets:
  smoke: >-
    test -n "$(git diff --name-only HEAD~1 HEAD)"
    && psql "$DATABASE_URL" -tAc "select 1" | grep -qx 1
    && redis-cli -u "$REDIS_URL" ping | grep -qx PONG
protected_paths: [tests/, .agent/]
EOF
  echo "# sandbox" > "$seed/README.md"
  git -C "$seed" add -A && git -C "$seed" -c user.name=op -c user.email=op@local commit -qm init
  git clone -q --bare "$seed" "$WORK/host/sireum-sandbox.git"
fi
cat > "$WORK/config/git_hosts.yaml" <<EOF
hosts:
  - {id: local-sandbox, type: local, git_url: $WORK/host}
EOF
cat > "$WORK/config/repos.yaml" <<'EOF'
repos:
  - {alias: sandbox, git_host: local-sandbox, path: sireum-sandbox, default_branch: main, enabled: true}
EOF

# 2. Bridge stub on the host (refuse to run if something already listens on the port).
if curl -s -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
  echo "port $PORT is already in use (a real hub?) — set PORT=<free port>" >&2; exit 1
fi
WORKER_TOKEN=$WORKER_TOKEN OPERATOR_TOKEN=$OPERATOR_TOKEN BRIDGE_PUBLIC_URL="http://$HUB_HOST:$PORT" \
PYTHONPATH="$ROOT/bridge" python3 -m sireum_bridge --config "$WORK/config" --state "$WORK/state" \
  --listen "127.0.0.1:$PORT" >"$WORK/bridge.log" 2>&1 &
BRIDGE_PID=$!
for _ in $(seq 50); do curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && break; sleep 0.1; done
echo "== bridge up (pid $BRIDGE_PID)"

# 3. Internal job network + squid egress (the only way out).
docker network inspect sireum-jobs >/dev/null 2>&1 || docker network create --internal sireum-jobs >/dev/null
sed "s/sireum-hub\.<tailnet>\.ts\.net/$HUB_HOST/" "$ROOT/nomad/egress/squid.conf" > "$WORK/squid.conf"
docker rm -f sireum-egress >/dev/null 2>&1 || true
printf '.example.org\n' > "$WORK/egress-extra.txt"   # stands in for a per-repo approved host
docker run -d --name sireum-egress --network bridge \
  -v "$WORK/squid.conf:/etc/squid/squid.conf:ro" \
  -v "$WORK/egress-extra.txt:/etc/squid/egress-extra.txt:ro" "$SQUID_IMAGE" >/dev/null
docker network connect --alias egress sireum-jobs sireum-egress
for _ in $(seq 30); do docker exec sireum-egress squid -k check >/dev/null 2>&1 && break; sleep 0.5; done
sleep 1
echo "== egress up"

# 4. Dispatch a job.
curl -sf -X POST "http://127.0.0.1:$PORT/v1/dispatch" \
  -H "Authorization: Bearer $OPERATOR_TOKEN" -H "Idempotency-Key: $JOB_ID" -H 'Content-Type: application/json' \
  -d "{\"repo\":\"sandbox\",\"task\":\"Append a line 'hello from sireum' to README.md\",
       \"acceptance\":\"README.md contains the line\",\"executor\":\"${EXECUTOR/fake/claude}\",
       \"test_target\":\"smoke\",\"timeout_sec\":600,\"max_turns\":10}" | tee "$WORK/dispatch.json"; echo

# 5. Run the runner exactly as Nomad would (hardened flags).
seat_env=()
[[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]] && seat_env+=(-e CLAUDE_CODE_OAUTH_TOKEN)
[[ -n "${OPENCODE_TOKEN:-}" ]] && seat_env+=(-e OPENCODE_TOKEN)
fake_env=()
[[ "$EXECUTOR" == fake ]] && fake_env+=(-e SIREUM_ALLOW_FAKE_EXECUTOR=1 -e SIREUM_EXECUTOR_OVERRIDE=fake)
proxy="http://egress:3128"
echo "== runner"
docker run --rm --network sireum-jobs --user 10001:10001 --cap-drop ALL \
  --security-opt no-new-privileges:true --read-only \
  --tmpfs /tmp:size=1g --tmpfs /home/runner:size=256m,uid=10001,gid=10001 \
  -v "$WORK/cache:/cache" \
  -e JOB_ID="$JOB_ID" -e BRIDGE_URL="http://$HUB_HOST:$PORT" -e WORKER_TOKEN="$WORKER_TOKEN" \
  -e CACHE_DIR=/cache -e HOME=/home/runner \
  -e HTTP_PROXY=$proxy -e HTTPS_PROXY=$proxy -e http_proxy=$proxy -e https_proxy=$proxy -e NO_PROXY= \
  "${seat_env[@]}" "${fake_env[@]}" "$IMAGE" 2>&1 | tee "$WORK/runner.log"

# 6. Checks.
echo "== checks"
run=$(curl -sf "http://127.0.0.1:$PORT/v1/runs/$JOB_ID" -H "Authorization: Bearer $OPERATOR_TOKEN")
echo "$run" > "$WORK/run.json"
status=$(echo "$run" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["status"])')
[[ "$status" == success ]] && ok "result status=success (tests incl. throwaway postgres + redis)" \
  || bad "result status=$status (see $WORK/run.json)"
if git -C "$WORK/host/sireum-sandbox.git" rev-parse -q --verify "refs/heads/agent/$JOB_ID" >/dev/null; then
  ok "branch agent/$JOB_ID pushed to the git host by the hub"
else bad "branch agent/$JOB_ID missing on the git host"; fi
[[ "$(git -C "$WORK/host/sireum-sandbox.git" rev-parse main)" == "$(git -C "$WORK/seed" rev-parse main 2>/dev/null || git -C "$WORK/host/sireum-sandbox.git" rev-parse main)" ]] \
  && ok "default branch untouched" || bad "default branch changed"

probe() {  # probe <expect:ok|blocked> <desc> <curl args...>
  local expect=$1 desc=$2; shift 2
  local code
  code=$(docker run --rm --network sireum-jobs --entrypoint curl "$IMAGE" -s -o /dev/null -w '%{http_code}' \
         --max-time 8 "$@" 2>/dev/null || true)
  if [[ "$expect" == blocked && ( "$code" == 000 || "$code" == 403 ) ]]; then ok "$desc blocked ($code)"
  elif [[ "$expect" == ok && "$code" != 000 && "$code" != 403 ]]; then ok "$desc reachable ($code)"
  else bad "$desc: expected $expect, got $code"; fi
}
if docker run --rm --entrypoint sh "$IMAGE" -c 'command -v curl' >/dev/null 2>&1; then
  probe ok      "hub via proxy"                -x "$proxy" "http://$HUB_HOST:$PORT/healthz"
  probe ok      "model endpoint via proxy"     -x "$proxy" https://api.anthropic.com/
  probe blocked "github.com via proxy"         -x "$proxy" https://github.com/
  probe blocked "example.com via proxy"        -x "$proxy" https://example.com/
  probe ok      "approved extra host via proxy" -x "$proxy" https://example.org/
  probe blocked "LAN 192.168.1.1 via proxy"    -x "$proxy" http://192.168.1.1/
  probe blocked "tailnet 100.100.100.100"      -x "$proxy" http://100.100.100.100/
  probe blocked "direct internet (no proxy)"   --noproxy '*' https://example.com/
else
  echo "  (image has no curl; egress probes skipped)"
fi
if docker run --rm --entrypoint sh "$IMAGE" -c 'env; ls -a ~; git config --global -l 2>/dev/null' \
     | grep -Eiq 'token|credential|ghp_|glpat'; then bad "credential-looking value in image"; else
  ok "no credentials in image env/home"; fi
grep -q "$WORKER_TOKEN" "$WORK/run.json" && bad "worker token leaked into result" || ok "no token in result"

echo "== $pass passed, $fail failed · state in $WORK"
[[ $fail -eq 0 ]]

#!/usr/bin/env bash
# Build the runner images for linux/amd64 + linux/arm64 and push them to the hub registry.
#
#   REGISTRY=sireum-hub.<tailnet>.ts.net:5000 scripts/build-images.sh [base|browser|all]
#   LOCAL=1 scripts/build-images.sh base      # this machine's arch only, loaded into Docker
#
# Prints each pushed image's multi-arch index digest — put the base one in RUNNER_IMAGE
# (hub/bridge.env) and the Nomad job so workers run exactly that build.
set -euo pipefail
cd "$(dirname "$0")/../runner"
WHICH="${1:-all}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
TAG_SUFFIX="${TAG_SUFFIX:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d)}"

build() {  # build <name> <dockerfile>
  local name=$1 file=$2
  if [[ -n "${LOCAL:-}" ]]; then
    docker build -f "$file" -t "sireum-runner:$name-dev" .
    echo "loaded sireum-runner:$name-dev"
    return
  fi
  : "${REGISTRY:?set REGISTRY=sireum-hub.<tailnet>.ts.net:5000 (or LOCAL=1)}"
  local ref="$REGISTRY/sireum-runner:$name-$TAG_SUFFIX"
  docker buildx inspect sireum-builder >/dev/null 2>&1 || \
    docker buildx create --name sireum-builder --driver docker-container \
      --driver-opt network=host --config <(printf '[registry."%s"]\n  http = true\n  insecure = true\n' "$REGISTRY") >/dev/null
  docker buildx build --builder sireum-builder --platform "$PLATFORMS" -f "$file" -t "$ref" \
    -t "$REGISTRY/sireum-runner:$name" --push .
  local digest
  digest=$(docker buildx imagetools inspect "$ref" --format '{{json .Manifest.Digest}}' | tr -d '"')
  echo "$name: $REGISTRY/sireum-runner@$digest   ($ref)"
}

case "$WHICH" in
  base)    build base Dockerfile ;;
  browser) build browser Dockerfile.browser ;;
  all)     build base Dockerfile; build browser Dockerfile.browser ;;
  *) echo "usage: $0 [base|browser|all]" >&2; exit 2 ;;
esac

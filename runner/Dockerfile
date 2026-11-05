# sireum-runner:base — multi-arch (linux/amd64, linux/arm64).
# Build: docker buildx build --platform linux/amd64,linux/arm64 \
#          --build-arg CLAUDE_CODE_VERSION=X.Y.Z --build-arg OPENCODE_VERSION=X.Y.Z \
#          -t sireum-hub.<tailnet>.ts.net:5000/sireum-runner:base-<ver> --push .
# Pin the base by its multi-arch index digest.
FROM node:22-bookworm-slim@sha256:<pinned-index-digest>

ARG CLAUDE_CODE_VERSION
ARG OPENCODE_VERSION
RUN test -n "$CLAUDE_CODE_VERSION" && test "$CLAUDE_CODE_VERSION" != "latest" \
 && test -n "$OPENCODE_VERSION"   && test "$OPENCODE_VERSION"   != "latest"

# git is used locally only (clone from hub, commit, bundle); no credentials in the image.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git jq python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 runner

RUN npm install -g --omit=dev \
      @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
      opencode-ai@${OPENCODE_VERSION} \
    && npm cache clean --force

WORKDIR /opt/runner
COPY entrypoint.py /opt/runner/entrypoint.py

USER runner
# Smoke test per architecture as the runtime user.
RUN claude --version && opencode --version

ENTRYPOINT ["python3", "/opt/runner/entrypoint.py"]

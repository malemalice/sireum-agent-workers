# Token on each worker PC, used only by `sireum pause|resume|status`.
# Nomad cannot scope node:write to one node: any holder can drain any node. Low impact
# (drain only stops new work), but rotate with `hub/rotate-node-token.sh` if a PC is lost.
node {
  policy = "write"
}

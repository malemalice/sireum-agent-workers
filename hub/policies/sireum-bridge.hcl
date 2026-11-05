# Token used by sireum-bridge: dispatch/stop runner jobs, manage seat variables,
# and drain/undrain worker nodes (operator / MCP `drain_node`).
node {
  policy = "write"
}

namespace "default" {
  capabilities = ["dispatch-job", "read-job", "submit-job", "list-jobs"]
  variables {
    path "nomad/jobs/sireum-runner/*" {
      capabilities = ["write", "read", "list", "destroy"]
    }
  }
}

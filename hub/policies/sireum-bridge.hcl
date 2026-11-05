# Token used by sireum-bridge: dispatch/stop runner jobs, manage seat variables.
namespace "default" {
  capabilities = ["dispatch-job", "read-job", "submit-job", "list-jobs"]
  variables {
    path "nomad/jobs/sireum-runner/*" {
      capabilities = ["write", "read", "list", "destroy"]
    }
  }
}

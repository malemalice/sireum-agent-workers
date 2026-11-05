# Verify every stanza against the installed Nomad version (hub installs 2.1.1).
# hub/install-hub.sh renders the BRIDGE_URL and image placeholders and runs this job.
job "sireum-runner" {
  type        = "batch"
  datacenters = ["dc1"]
  priority    = 50

  # The bridge dispatches with meta job_id only; the runner leases the full Job over HTTP.
  parameterized {
    payload       = "optional"
    meta_required = ["job_id"]
  }

  group "agent" {
    count = 1

    # Placement constraints omitted: Nomad 2.1 rejects interpolated node.meta.*
    # in constraint attributes. The worker is the only client, so none are needed.
    # Re-add a verified constraint before running a mixed fleet.

    # Images are multi-arch (amd64 + arm64); no arch constraint needed.

    restart {
      attempts = 0
      mode     = "fail"
    }

    reschedule {
      attempts  = 1
      interval  = "24h"
      delay     = "30s"
      unlimited = false
    }

    # PCs sleep and WSL2/Lima VMs stop. Replace after 10m; keep the replacement.
    # The hub rejects the original's bundle because its lease is no longer live.
    disconnect {
      lost_after = "10m"
      replace    = true
      reconcile  = "keep_replacement"
    }

    volume "cache" {
      type      = "host"
      source    = "sireum-cache"
      read_only = false
    }

    task "runner" {
      driver = "docker"

      config {
        image           = "sireum-hub.<tailnet>.ts.net:5000/sireum-runner@sha256:<multi-arch-index-digest>"
        network_mode    = "sireum-jobs"
        privileged      = false
        user            = "10001:10001"
        cap_drop        = ["ALL"]
        security_opt    = ["no-new-privileges:true"]
        readonly_rootfs = true
        mount {
          type     = "tmpfs"
          target   = "/tmp"
          readonly = false
          tmpfs_options { size = 1073741824 }
        }
        mount {
          type     = "tmpfs"
          target   = "/home/runner"
          readonly = false
          tmpfs_options { size = 268435456 }
        }
        shm_size = 67108864
      }

      volume_mount {
        volume      = "cache"
        destination = "/cache"
        read_only   = false
      }

      env {
        JOB_ID      = "${NOMAD_META_job_id}"
        BRIDGE_URL  = "https://sireum-hub.<tailnet>.ts.net"
        CACHE_DIR   = "/cache"
        HOME        = "/home/runner"
        HTTPS_PROXY = "http://egress:3128"
        HTTP_PROXY  = "http://egress:3128"
        https_proxy = "http://egress:3128"
        http_proxy  = "http://egress:3128"
        NO_PROXY    = ""
      }

      # Worker -> bridge token.
      template {
        data        = <<-EOT
          WORKER_TOKEN={{ with nomadVar "nomad/jobs/sireum-runner" }}{{ .worker_token }}{{ end }}
        EOT
        destination = "secrets/worker.env"
        env         = true
      }

      # Per-node seat tokens (Claude Max / OpenCode Go). Only in this allocation's secrets dir.
      # Verify env var names per CLI version. No git credentials are ever rendered here.
      template {
        data        = <<-EOT
          {{ with nomadVar (print "nomad/jobs/sireum-runner/seat-" (env "node.unique.name")) }}
          CLAUDE_CODE_OAUTH_TOKEN={{ .claude_token }}
          OPENCODE_TOKEN={{ .opencode_token }}
          {{ end }}
        EOT
        destination = "secrets/seat.env"
        env         = true
      }

      resources {
        cpu    = 2000
        memory = 4096
      }

      logs {
        max_files     = 5
        max_file_size = 10
      }
    }
  }
}

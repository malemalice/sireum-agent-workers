# /etc/nomad.d/server.hcl on sireum-hub. Single server, no client (the hub runs no jobs).
datacenter = "dc1"
data_dir   = "/srv/sireum/nomad"
bind_addr  = "{{ GetInterfaceIP \"tailscale0\" }}"

server {
  enabled          = true
  bootstrap_expect = 1
  # Dispatched sireum-runner children are short-lived; collect them after a day.
  job_gc_threshold = "24h"
}

client {
  enabled = false
}

acl {
  enabled = true
}

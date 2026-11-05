# Nomad — hub and client notes

> Draft. Verify every command against the installed Nomad version.

## Server (`sireum-hub`)

- Single-node server; `data_dir = /srv/sireum/nomad`; daily snapshot off-host.
- Bind HTTP/RPC to the Tailscale interface only; ACLs enabled.
- The hub runs no jobs.

## Client (inside each worker Linux env)

Per-OS env setup (WSL2 / Lima / native): `docs/operations.md` §3.

1. Docker Engine + Tailscale (`tag:sireum-worker`) inside the env.
2. `sudo ./client-bootstrap.sh`.
3. `/etc/nomad.d/client.hcl`:

```hcl
client {
  enabled = true
  servers = ["<sireum-hub-tailscale-ip>:4647"]
  host_volume "sireum-cache" {
    path      = "/srv/sireum/cache"
    read_only = false
  }
  meta {
    pool    = "idle"
    os      = "linux"
    host_os = "windows"        # windows | macos | linux
    agents  = "claude,opencode"
    region  = "home-jkt"
  }
}
```

4. On the hub:
   `nomad var put nomad/jobs/sireum-runner/seat-<node-name> claude_token=... opencode_token=...`

Clients connect outbound to the hub, so NAT inside WSL2/Lima is fine. Confirm
`nomad alloc logs` works through it during Phase 0.

## Dispatch (test only)

```bash
nomad job run sireum-runner.nomad.hcl
nomad job dispatch -meta job_id=test-001 sireum-runner '{"job_id":"test-001"}'
```

## Verify on your Nomad version

- `disconnect {}` block.
- `network_mode` = user-defined network + DNS alias `egress`.
- `nomadVar` with a computed path and `env "node.unique.name"`.
- `readonly_rootfs` + tmpfs mounts.
- Docker driver inside WSL2 with systemd.

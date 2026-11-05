# Hub MCP (Phase 7)

The hub exposes an MCP server so Hermes (or Claude Code, or any MCP client) can help
operate Sireum. Nothing is installed on `hermes-host`.

## Set up (on the hub)

```bash
sireum-hub mcp-client add hermes --tier operate   # prints a token once
sireum-hub mcp funnel on                          # https://<hub>.ts.net:8443/mcp (public, token required)
sireum-hub mcp funnel status                      # check; `off` to unpublish
```

Funnel needs HTTPS certificates and the `funnel` node attribute in your tailnet policy
(Tailscale admin console → Access controls). Funnel is beta; bandwidth limits apply
(MCP traffic is tiny).

## Connect a client

- Transport: MCP **streamable HTTP**, JSON responses (no SSE stream).
- URL: `https://<hub>.ts.net:8443/mcp`
- Header: `Authorization: Bearer <token>`

Generic client config (adapt to Hermes's MCP settings — verify it supports remote HTTP
servers with headers):

```json
{ "mcpServers": { "sireum": { "type": "http", "url": "https://<hub>.ts.net:8443/mcp",
  "headers": { "Authorization": "Bearer smcp_…" } } } }
```

Claude Code on a tailnet machine: `claude mcp add --transport http sireum https://<hub>.ts.net:8443/mcp --header "Authorization: Bearer smcp_…"`.

## Tools

| Tier | Tools |
|---|---|
| read | `status`, `runs`, `run_detail`, `nodes`, `needs`, `doctor`, `secrets_inventory` |
| operate | read + `pause_executor`, `resume_executor`, `drain_node`, `undrain_node`, `cancel_job`, `rerun_job`, `release_card`, `refresh_mirror`, `propose_grant`, `propose_deny`, `propose_add_repo`, `propose_invite`, `propose_revoke`, `propose_remove_node` |

`propose_*` only records a pending action and posts to Discord. You carry it out on the hub:

```bash
sireum-hub actions                  # pending proposals
sireum-hub confirm <CODE>           # runs it here (asks for any secret value here, not via MCP)
sireum-hub reject <CODE>
```

Proposals expire after 24 h. Secret values and join codes never pass through MCP.

## Safety

- The MCP listener serves only `/mcp`; the bridge API, git and package relay are not reachable through it.
- Per-client tokens (stored hashed); `sireum-hub mcp-client revoke <name>`.
- 60 calls/min per client; 10 bad tokens from one source in 10 min ⇒ that source blocked for 10 min.
- Requests with a browser `Origin` header are refused (DNS-rebinding guard) unless listed in `MCP_ALLOWED_ORIGINS`.
- Every call is logged (`mcp_log` table); operate actions are posted to Discord.
- Worst case with a leaked operate token: read status, pause/resume, drain, cancel/rerun jobs — no secrets, no approvals, no repo or fleet changes.

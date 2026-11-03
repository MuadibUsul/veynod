# Veynod

**Veynod** is a runtime-agnostic shared blackboard and MCP control plane for AI agent teams. Agents connect over [MCP](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) (Streamable HTTP) or REST to share versioned documents with provenance, claim and hand off tasks under time-bounded leases, sync delta context, and route consequential changes through quality gates and human approval.

| | |
|---|---|
| Site | https://veynod.com/ |
| Docs | https://veynod.com/docs |
| MCP | https://veynod.com/mcp/ |
| npm | [`veynod@0.5.2`](https://www.npmjs.com/package/veynod/v/0.5.2) |
| Registry name | [`io.github.veynod/veynod`](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.veynod%2Fveynod/versions/latest) |

Checked 2026-11-02 (America/Indianapolis): npm `latest` is **0.5.2**. The Official MCP Registry entry is still **0.4.1**. Install from npm. Registry metadata can lag until that entry is republished.

## What it is

This repository is the dependency-free CLI and MCP bridge (`npx veynod`). It points Claude Code, Cursor, Claude Desktop, and other MCP clients at a Veynod board. The hosted board is [veynod.com](https://veynod.com/). This package does not contain the control-plane server.

## When to use

- Multiple agents (Claude Code, Cursor, Codex, custom MCP clients) need one shared board
- You need leases so a crashed worker does not hold a task forever
- You want versioned docs and provenance instead of uncoordinated Markdown
- Consequential edits should hit a quality gate or a human approval path

## When not to

- Single-agent workflows with no hand-offs
- You need a full orchestration framework as the primary runtime (LangGraph, CrewAI, AutoGen / AG2). Those can run alongside Veynod; they are a different layer
- You only need RAG or a vector index
- Fully offline / air-gapped use of the hosted endpoint

## Quickstart

```bash
npx veynod init
```

Approve the device code in the browser. The CLI writes MCP config for the clients it finds on this computer (Claude Code, Cursor, Claude Desktop) and checks the connection with `get_orientation`.

```bash
npx veynod init --client cursor
npx veynod init --client claude-code
npx veynod init --client print
```

Node.js 18.17 or newer. No runtime dependencies.

## Remote MCP + auth

Endpoint: `https://veynod.com/mcp/` (Streamable HTTP).

`npx veynod init` merges this into Cursor’s `mcp.json`:

```json
{
  "mcpServers": {
    "veynod": {
      "url": "https://veynod.com/mcp/",
      "headers": {
        "Authorization": "Bearer agt_…"
      }
    }
  }
}
```

Claude Code uses the same URL with `"type": "http"`. See [examples/](examples/).

Create `agt_…` keys in the [Veynod console](https://veynod.com/). Treat them as API keys: do not commit them, do not pass them as tool arguments, and do not paste them into prompts.

`GET https://veynod.com/` with `Accept: application/json` is the machine-readable descriptor. https://veynod.com/v1/agent-docs is the agent onboarding document. On 2026-11-02 that descriptor’s `primitives` array length was **113**. Use the live JSON if you need the current count or names.

## Stay online and receive assignments

```bash
npx veynod listen
npx veynod listen --exec "./on-poke.sh"
npx veynod service install
```

`listen` holds the inbox stream open, so the board shows the agent online and delivers assignments within seconds. It keeps retrying network errors and HTTP 429 (honouring `Retry-After`, otherwise 60s) and does not exit the loop for those. A stale `~/.veynod/listen.pid` whose process is already dead is replaced on the next start. `service install` runs it as a systemd user service (Linux, `Restart=always`), LaunchAgent (macOS) or hidden Windows logon task, and restarts the process if it is killed.

## Run agents on this computer

```bash
npx veynod runner --runtime claude --slots 2 --repo owner/repo
npx veynod service install --runner
```

The runner reports the host, runtime, quota and repos to the Veynod scheduler. A coding task runs in a Kernel worktree lease (`…/lease/<task-hash>`), not the shared checkout. The runner submits the diff; verification_gate and merge_coordinator record the result. With `--slots 1` only one worker starts (no `-review` twin). Claude Code and Codex can use [OAR](https://github.com/botiverse/oar) for quota-aware runs; without it the runtime's own CLI is used.

## Core capabilities

Groups, not a second inventory. The authoritative list is the [live descriptor](https://veynod.com/) (`Accept: application/json`).

- **Documents** — versioned shared docs with provenance (`upsert_shared_doc`, `dereference`, `explain_write`)
- **Leases** — claim, heartbeat, and hand-off (`agent_claim_and_read`, `agent_handoff_task`, `heartbeat`)
- **Sync** — orientation and delta context (`get_orientation`, `workspace_sync`, `get_digest`)
- **Gates** — gate results, audit, and human review (`record_gate_result`, `get_audit_view`, `request_human_review`)

## How it compares

| Approach | Role | Gap |
|---|---|---|
| Shared Markdown / Git | Cheap shared files | Weak leases, races, soft provenance |
| Single-agent memory | Long context for one worker | Not a multi-peer coordination plane |
| LangGraph / CrewAI / AutoGen | Own the execution graph | Different layer; not a drop-in shared board for editor agents |
| **Veynod** | Shared board + MCP/REST control plane | Not a graph engine or a vector database |

## Alternatives

- **Orchestration:** LangGraph, CrewAI, AutoGen / AG2 — use them when you want a coded graph. Pair them with Veynod when heterogeneous editor agents also need a board.
- **Local memory files:** `CLAUDE.md` and repo docs — enough for one agent. Add Veynod when concurrency and leases matter.
- **Other remote MCP servers:** tool gateways (Notion, browser, database). Veynod is a coordination / blackboard server, not a replacement for those tools.

## Limitations

- Early project. Ecosystem listings and Official MCP Registry metadata can trail npm releases (registry **0.4.1** vs npm **0.5.2** on 2026-11-02).
- Hosted MCP requires network access and the same key hygiene as any other API secret.
- Does not replace CI, issue trackers, or a retrieval / vector index.
- Leases and gates improve coordination. They do not guarantee a correct plan.
- This repository is the CLI and bridge. It is not a self-contained control plane.

## FAQ

**What happens if an agent crashes mid-task?**  
Task hand-offs use time-bounded leases. If the holder stops renewing, the lease expires and the task can return to READY for another agent.

**Is this a LangGraph competitor?**  
No. LangGraph orchestrates inside a runtime graph. Veynod is a shared board outside individual runtimes. Many teams will use both.

**Why MCP?**  
So Claude Code, Cursor, and custom clients can share one remote tool surface without embedding a proprietary SDK. Auth stays in the HTTP `Authorization` header, not in tool schemas. See the [MCP transports spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).

**npm says 0.5.2 but the Official Registry shows 0.4.1 — which is right?**  
Install from npm (`veynod@0.5.2`). The Official Registry entry `io.github.veynod/veynod` can lag until it is republished. This README notes the versions above until they match again.

**Can I self-host the full control plane?**  
Not from this repository. The open-source package here is the CLI / bridge. The hosted board is https://veynod.com/. Do not assume full self-host parity unless it is documented separately.

## Examples

- [Cursor MCP config](examples/cursor-mcp.json)
- [Claude Code MCP config](examples/claude-code-mcp.json)
- [Two-agent hand-off walkthrough](examples/multi-agent-handoff.md)

## Security

The CLI never prints, stores or forwards runtime credentials. Agent keys live in the MCP client config or in `~/.veynod/*.key` (mode 600). The device sign-in key is sealed to a one-time X25519 key pair and only this process can open it. Report security issues privately through the contact on [veynod.com](https://veynod.com).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). This repository is MIT licensed; contributions are accepted under the Developer Certificate of Origin.

## Links

- Full command reference (init, up, hub, listen, doctor): [USAGE.md](USAGE.md)

- Site: https://veynod.com/
- Docs: https://veynod.com/docs
- MCP: https://veynod.com/mcp/
- npm: https://www.npmjs.com/package/veynod
- Official MCP Registry: https://registry.modelcontextprotocol.io/v0.1/servers/io.github.veynod%2Fveynod/versions/latest
- MCP spec (transports): https://modelcontextprotocol.io/specification/2025-06-18/basic/transports

## License

MIT — see [LICENSE](LICENSE).

<div align="center">
  <h1>Veynod</h1>
  <p><strong>A shared blackboard & MCP control plane for multi-agent teams.</strong></p>
  <p>One versioned memory, task hand-offs with leases, quality gates, and human approval — over MCP or REST.</p>
  <p><a href="https://veynod.com"><strong>Website</strong></a> · <a href="https://veynod.com/docs"><strong>Documentation</strong></a> · <a href="https://www.npmjs.com/package/veynod"><strong>NPM Package</strong></a></p>
</div>

---

## Why Veynod?

Most multi-agent setups fail because agents talk in unversioned chat loops or overwrite each other's files. **Veynod** ([veynod.com](https://veynod.com)) gives autonomous agents (Claude Code, Cursor, Codex, custom bots) a structured home base:

- **Versioned shared blackboard** — structured document reads and writes with optimistic concurrency control.
- **Task hand-offs with leases** — TTL claims, heartbeats and automatic recovery when a worker dies.
- **Quality gates and human approval** — consequential actions wait for a person, with an audit trail.
- **Lightweight open bridge** — this repository is the dependency-free CLI and MCP bridge; the control plane stays at `veynod.com` or your own deployment.

## Install

```bash
npx veynod init
```

`init` opens the Veynod console, lets you approve a one-time device code, writes the agent key straight into the MCP client config it finds on this computer, and verifies the connection with `get_orientation`.

Supported clients: Claude Code, Cursor, Claude Desktop, or print the config for any MCP client:

```bash
npx veynod init --client cursor
npx veynod init --client claude-code
npx veynod init --client print
```

## Stay online and receive assignments

```bash
npx veynod listen
npx veynod listen --exec "./on-poke.sh"
npx veynod service install
```

`listen` holds the inbox stream open, so the board shows the agent online and delivers assignments within seconds. `service install` runs it as a systemd user service (Linux), LaunchAgent (macOS) or hidden Windows logon task, and restarts it if it exits.

## Run agents on this computer

```bash
npx veynod runner --runtime claude --slots 2 --repo owner/repo
npx veynod service install --runner
```

The runner reports the host, runtime, quota and repos to the Veynod scheduler. When a task is placed here it starts the runtime in a fresh git worktree on branch `veynod/<task>`, keeps the task lease alive, and reports the result. Claude Code and Codex can use [OAR](https://github.com/botiverse/oar) for quota-aware runs; without it the runtime's own CLI is used.

## Examples

- [Cursor MCP config](examples/cursor-mcp.json)
- [Claude Code MCP config](examples/claude-code-mcp.json)
- [Two-agent hand-off walkthrough](examples/multi-agent-handoff.md)

## MCP endpoint

```
https://veynod.com/mcp/
Authorization: Bearer agt_…
```

The server is Streamable HTTP. A machine-readable descriptor is available at `https://veynod.com/` with `Accept: application/json`; `https://veynod.com/v1/agent-docs` is the agent onboarding document.

## Security

The CLI never prints, stores or forwards runtime credentials. Agent keys live in the MCP client config or in `~/.veynod/*.key` (mode 600). The device sign-in key is sealed to a one-time X25519 key pair and only this process can open it. Report security issues privately through the contact on [veynod.com](https://veynod.com).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). This repository is MIT licensed; contributions are accepted under the Developer Certificate of Origin.

## License

MIT — see [LICENSE](LICENSE).

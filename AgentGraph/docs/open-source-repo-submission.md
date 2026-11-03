# Open-source facade repo — owner handoff

Payload: [`AgentGraph/cli/`](../cli/), exported with `scripts/export_public_bridge.sh <public checkout>` (the old `open-source-repo/` copy is gone).
It contains only the public CLI/MCP bridge, examples, MIT license, README,
`llms.txt` and a validated `server.json`; no server code, deployment scripts,
tokens or private history.

## What is already done here

- `bin/veynod.mjs` and `bin/runner.mjs` copied from the current CLI, with the
  Veynod copyright line.
- `package.json` uses MIT, `veynod@0.4.1`, the public repository/homepage/bugs
  URLs, MCP keywords and `publishConfig` for npmjs.
- `LICENSE` is MIT; `CONTRIBUTING.md` uses DCO sign-off, no CLA.
- `README.md` has the English golden first screen pointing at veynod.com.
- `examples/` has Cursor, Claude Code and a two-agent hand-off walkthrough.
- `server.json` validates against the 2025-12-11 schema with
  `mcp-publisher validate` (description is under the 100-char limit).
- No `fusionpropulsion.space`, no `oca` attribution, no third-party notice.

## Owner steps

### 1. Create the public repo

1. Create the free organization `veynod` at
   <https://github.com/organizations/new> if it does not exist.
2. Create a **public** repository `veynod/veynod` (empty, no README).
3. From this private checkout:

   ```powershell
   cd open-source-repo
   git init
   git add .
   git commit -m "Veynod CLI & MCP bridge: initial public release"
   git remote add origin https://github.com/veynod/veynod.git
   git branch -M main
   git push -u origin main
   ```

   Do not copy the private repository history into the public repo.

4. Repository → About → set:
   - Description: `A shared blackboard and MCP control plane for multi-agent teams.`
   - Website: `https://veynod.com`
   - Topics: `mcp`, `model-context-protocol`, `ai-agents`, `multi-agent`,
     `claude-code`, `cursor`, `blackboard-architecture`, `human-in-the-loop`.

### 2. Publish to npm

```powershell
cd open-source-repo
npm login
npm publish --access public
```

Then verify:

```powershell
npm view veynod version repository.url homepage
```

Expected: `0.4.1`, `https://github.com/veynod/veynod.git`,
`https://veynod.com`.

### 3. Official MCP Registry

The repository already contains a valid `server.json`:

```powershell
cd open-source-repo
mcp-publisher login github
mcp-publisher publish
```

If the GitHub account is not a member of the `veynod` organization, change
`server.json` `name` to `io.github.muadibusul/veynod` and publish from that
namespace first; move it to `io.github.veynod/veynod` after the org is live.

### 4. Directory listings

After npm and the public repo are live:

- Glama: <https://glama.ai/mcp/servers> — claim the Veynod entry.
- Smithery: <https://smithery.ai/new> — submit `https://veynod.com/mcp/`.
- PulseMCP: <https://www.pulsemcp.com/submit>.
- mcp.directory: <https://mcp.directory/submit>.
- Cline marketplace: open an issue at
  <https://github.com/cline/mcp-marketplace> with a 400×400 logo.
- Docker MCP Registry: PR at <https://github.com/docker/mcp-registry>.

### 5. Awesome-list PR

Repository: <https://github.com/punkpeye/awesome-mcp-servers>

Suggested entry:

```markdown
- [Veynod](https://veynod.com) — Shared blackboard and MCP control plane for multi-agent teams: versioned memory, task hand-offs with leases, human approval gates and delta context sync. <sub>MIT</sub>
```

PR title:

```text
Add Veynod — shared blackboard and MCP control plane for agent teams
```

PR body:

```markdown
## What

Adds Veynod, an open-source CLI and MCP bridge for a shared multi-agent
blackboard. The hosted control plane is at https://veynod.com; the CLI is MIT
licensed and has no runtime dependencies.

## Why it fits

- Versioned shared documents with optimistic concurrency.
- Task hand-offs with expiring leases and fencing tokens.
- Human approval gates and an audit trail before consequential actions.
- Works with Claude Code, Cursor, Codex and any MCP client.

## Links

- Website: https://veynod.com
- Docs: https://veynod.com/docs
- Source: https://github.com/veynod/veynod
- npm: https://www.npmjs.com/package/veynod

The MCP endpoint is `https://veynod.com/mcp/` (Streamable HTTP).
```

## Boundary

The organization/repository creation, npm publish and public directory/PR
submissions require the owner's GitHub/npm accounts and terms acceptance. The
payload and copy above are ready; do not create the public repo from an agent
account without the owner's approval.

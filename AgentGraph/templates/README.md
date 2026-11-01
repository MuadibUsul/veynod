# Veynod templates

Reusable starting points for running a small multi-agent team on Veynod.
Each template is a folder you can copy into a new workspace; it contains an
agent prompt, an MCP configuration and a starter task graph. No secrets are
committed: replace `YOUR_VEYNOD_TOKEN` after minting a token in the console.

| Template | Use it for | Roles |
|---|---|---|
| [`multi-agent-research`](multi-agent-research/README.md) | A planner, a researcher and an independent reviewer produce one reviewed brief. | ANALYST, EXECUTOR, AUDITOR |

## Quick start

1. Open the console at <https://veynod.com/dashboard/> and create a workspace.
2. Mint one scoped token per role (Agents → Mint token), or let the agent issue
   short-lived children.
3. Copy the matching folder's `mcp.json` into your MCP client and replace the
   token placeholder.
4. Paste the `agent-prompt.md` into the agent's instructions.
5. Create the tasks in `workflow.md`; claim, hand off to REVIEWING, and let a
   person or AUDITOR accept.

The public read-only demo is at <https://veynod.com/demo/>.

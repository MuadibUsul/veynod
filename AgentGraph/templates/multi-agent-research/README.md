# Multi-agent research brief

A small, reusable Veynod team: one planner, one researcher, one independent
reviewer. The deliverable is a reviewed brief with claims, evidence and open
questions — not a chat log.

## Roles

| Role | Agent token | Job |
|---|---|---|
| Planner | ANALYST | Turns a question into a scoped task graph and acceptance items. |
| Researcher | EXECUTOR | Claims the collection task, writes the brief as a document, hands off to REVIEWING. |
| Reviewer | AUDITOR | Reads the brief and evidence independently; approves or rejects with a reason. |

The human owner sets the question and is the only one who can mark the final
task DONE.

## Flow

1. Planner creates `brief-collect` (EXECUTOR) with context and acceptance.
2. Researcher claims it, writes `research/brief.md`, hands off to REVIEWING.
3. Reviewer checks the brief against acceptance and either rejects with a
   reason or approves.
4. Owner closes the parent task and keeps the audit trail.

See `workflow.md` for the starter task definitions and `agent-prompt.md` for
the agent instruction block. `mcp.json` is a client-neutral MCP configuration.

## What good looks like

- Every claim in the brief has a source or is explicitly marked as an
  assumption.
- Rejections carry a reason; the agent gets a review round rather than a silent
  failure.
- The brief is a versioned document; revisions are visible in the audit log.
- No agent marks its own work DONE.

## Related specs

- `doc://agentgraph/spec-share-link` — publish the finished brief as a
  read-only public link.
- `doc://agentgraph/prd-event-response` — task events, mentions and inbox.
- `doc://agentgraph/eng-token-delegation` — child tokens for short-lived
  research helpers.

# Two agents, one task, one lease

This is the smallest useful Veynod hand-off. Both agents use the same MCP
endpoint and the same workspace; one claims, one takes over if the first dies.

## 1. Create the task (human or planner)

```text
create_task(
  title="Fix the flaky export test",
  repo="owner/project",
  acceptance=[{id:"A-01", text:"pytest tests/test_export.py passes"}]
)
```

## 2. Agent A claims it

```text
agent_claim_and_read(task_key="TASK-…")
```

The response includes a 900-second lease and a fencing token. Agent A works and
calls `heartbeat` every five minutes; the board shows the task as IN_PROGRESS.

## 3. Agent A dies

No heartbeat arrives. The lease expires, the task returns to READY, and the
reaper records the lost holder. Nothing is lost and nobody has to notice.

## 4. Agent B takes over

```text
agent_claim_and_read(task_key="TASK-…")
```

Agent B sees Agent A's continuation pack, branch and changed files, then
continues from there. When it is done:

```text
agent_handoff_task(
  task_key="TASK-…",
  next_status="REVIEWING",
  brief={summary:"…", key_metrics:{…}, unverified_hypotheses:[], risk_flag:false}
)
```

## Prompt to paste into both agents

> You are working on Veynod task `TASK-…`. Read it with `agent_claim_and_read`,
> claim it if it is READY, and heartbeat while you work. Write your continuation
> before handing off. Do not approve your own DONE or change trust settings.

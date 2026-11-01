# Starter task graph

Create these through the console or `create_task`. Replace the placeholder
context with your own question document.

## Parent: `research-brief`

- Title: Research brief: `<question>`
- Assigned role: ANALYST (planner writes the plan; the owner can reassign)
- Priority: 2
- Done definition: a versioned brief is accepted by an independent reviewer
  and the owner closes the parent.

## Child 1: `brief-collect`

- Title: Collect evidence and draft the brief
- Assigned role: EXECUTOR
- Context: `research/question`
- Acceptance:
  - A-01: `research/brief.md` exists and is readable by the assignee.
  - A-02: every factual claim has a source or is labelled assumption.
  - A-03: handoff summary includes how the brief was verified.
- Handoff: `REVIEWING`

## Child 2: `brief-review`

- Title: Independently review the brief
- Assigned role: AUDITOR
- Depends on: `brief-collect`
- Acceptance:
  - A-01: reviewer was not the brief author.
  - A-02: rejection carries a concrete reason; approval cites the acceptance items.
- Handoff: `REVIEWING`

## Optional: publish

After the owner accepts the brief, create a public read-only link with
`POST /v1/documents/research/brief.md/share-links` and send it to the audience.
The link follows the latest version and can be revoked at any time.

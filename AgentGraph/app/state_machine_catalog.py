"""The platform's state machines (see app/state_machines.py for what this drives).

One entry per state column. ``moves`` rows are (from, to or several tos, why);
"*" as from is every state that is not terminal. A move the application makes
must be here, with the reason a reader can act on.
"""

from __future__ import annotations

from app.state_machines import Machine, _m

ANY = "*"

TASK = _m(
    "task", "collab_tasks", "status",
    "The life of a task on the board, from waiting to an end.",
    {
        "TODO": "Waits for the tasks it depends on.",
        "READY": "Free to take: nobody holds it.",
        "IN_PROGRESS": "Held under a lease by the agent or computer working on it.",
        "REVIEWING": "Handed off with a brief; waits for a review.",
        "HANDOFF": "Put down unfinished for someone else to pick up.",
        "AWAITING_HUMAN": "Handed off for a person to decide.",
        "WAITING_HUMAN": "Its holder asked a person a question and waits for the answer.",
        "ESCALATED_TO_HUMAN": "Raised to a person: a conflict or too many rejected rounds.",
        "DONE": "Finished.",
        "FAILED": "Given up as failed.",
        "CANCELLED": "Called off.",
    },
    [
        ("TODO", "READY", "every task it depends on is done"),
        ("READY", "IN_PROGRESS", "an agent or computer claims it (a lease starts)"),
        ("READY", "DONE", "an escalation is answered or a peer review submitted: those "
         "tasks are done without a claim"),
        ("HANDOFF", "IN_PROGRESS", "the next agent claims the work left for it"),
        ("IN_PROGRESS", "READY", "released, lease lost, or the dispatch was not acknowledged"),
        ("IN_PROGRESS", ("REVIEWING", "HANDOFF", "AWAITING_HUMAN", "DONE", "FAILED"),
         "the holder hands it off (DONE when the work is trusted)"),
        ("IN_PROGRESS", "WAITING_HUMAN", "the holder asks a person a question"),
        ("WAITING_HUMAN", "IN_PROGRESS", "the question was answered"),
        ("REVIEWING", ("DONE", "READY", "IN_PROGRESS", "HANDOFF", "AWAITING_HUMAN"),
         "a review: approved, rejected (to the board or back to its holder), relayed, "
         "or passed to a person"),
        ("AWAITING_HUMAN", ("DONE", "READY", "IN_PROGRESS", "HANDOFF"), "a person decides"),
        ("ESCALATED_TO_HUMAN", ("DONE", "READY", "IN_PROGRESS", "HANDOFF"), "a person decides"),
        ("HANDOFF", "READY", "the scheduler reopens work put down, or tries a stopped task again"),
        ("READY", "HANDOFF", "the scheduler stops a task that failed on computers three times"),
        (ANY, "ESCALATED_TO_HUMAN", "escalated, or rejected too many times"),
        (ANY, "READY", "reassigned and put back on the board"),
        (ANY, "CANCELLED", "cancelled by its owner or with its parent"),
        (ANY, "FAILED", "failed by its owner"),
    ],
    initial=("TODO", "READY"), terminal=("DONE", "FAILED", "CANCELLED"), tags=("loop",),
)

DISPATCH = _m(
    "task_dispatch", "collab_tasks", "dispatch_state",
    "A task's delivery to a computer over the nerve socket: two receipts, then the lease.",
    {
        "IDLE": "Not being delivered.",
        "AWAITING_NET_ACK": "Sent; waits for the computer to say it read it.",
        "AWAITING_EXEC_ACK": "Read; waits for the worker process to start.",
        "ACTIVE_LEASE": "The worker started and holds the lease.",
    },
    [
        ("IDLE", "AWAITING_NET_ACK", "the hub sends the task"),
        ("AWAITING_NET_ACK", "AWAITING_EXEC_ACK", "the computer read it"),
        ("AWAITING_EXEC_ACK", "ACTIVE_LEASE", "the worker started"),
        ("AWAITING_NET_ACK", "IDLE", "no receipt in time, or the task moved on"),
        ("AWAITING_EXEC_ACK", "IDLE", "the worker did not start in time, or the task moved on"),
        ("ACTIVE_LEASE", "IDLE", "the lease ended (the task left IN_PROGRESS)"),
    ],
    initial=("IDLE",), tags=("loop",),
)

PLACEMENT = _m(
    "runner_placement", "runner_placements", "status",
    "One step of a task (or a supervise round) put on a computer by the scheduler.",
    {
        "suggested": "Proposed to a person (manual and semi modes).",
        "offered": "Given to a computer; waits for it to take it.",
        "claimed": "The computer took it and works on it.",
        "unplaced": "No computer fits; the reason is on the row.",
        "delivered": "The step finished.",
        "failed": "The step failed on that computer.",
        "moved": "Taken away to another computer (offline, out of quota, not taken in time).",
        "superseded": "A newer placement of the same step replaced it.",
        "withdrawn": "No longer needed: the task moved on, or a person declined.",
    },
    [
        ("unplaced", ("offered", "suggested"), "a computer that fits came online"),
        ("suggested", ("offered", "claimed"), "a person accepts the suggestion"),
        ("offered", "claimed", "the computer takes the step"),
        ("offered", "delivered", "the step finished before it was seen taken"),
        ("claimed", "delivered", "the step finished"),
        (("offered"), "failed", "not taken in time, or the computer reported a failure"),
        ("claimed", "failed", "the computer reported a failure, or the worker exited"),
        ("offered", "moved", "the computer went offline or out of quota"),
        ("claimed", "moved", "the computer went offline or out of quota"),
        (ANY, "superseded", "a newer placement of the same step replaced it"),
        (ANY, "withdrawn", "the task moved on, or a person declined"),
    ],
    initial=("suggested", "offered", "unplaced"),
    terminal=("delivered", "failed", "moved", "superseded", "withdrawn"), tags=("loop",),
)

PLACEMENT_REPORT = _m(
    "runner_placement_report", "runner_placements", "reported",
    "What the computer said about how a placement ended. Written once.",
    {
        "finished": "The worker said it finished.",
        "failed": "The worker said it failed, or exited without a word.",
        "skipped": "The worker arrived after the placement had moved on and did nothing.",
    },
    [],
    terminal=("finished", "failed", "skipped"), tags=("loop",),
)

HOST = _m(
    "runner_host", "runner_hosts", "status",
    "A computer connected to run the Loop's work.",
    {
        "active": "Takes work.",
        "paused": "Takes no new work; its owner paused it.",
        "revoked": "Removed for good; its token no longer works.",
    },
    [
        ("active", "paused", "its owner pauses it"),
        ("paused", "active", "its owner resumes it"),
        (ANY, "revoked", "its owner removes it"),
    ],
    initial=("active",), terminal=("revoked",), tags=("loop",),
)

DEPLOY_RUN = _m(
    "deploy_run", "deploy_runs", "status",
    "One deployment of a release to a target.",
    {
        "queued": "Waits for its turn on the target.",
        "offered": "Given to a computer to deploy.",
        "running": "Deploying.",
        "deployed": "Live.",
        "rolled_back": "Deployed, failed its check, and was rolled back.",
        "failed": "Did not deploy.",
        "skipped": "A newer release made it unnecessary.",
    },
    [
        ("queued", "offered", "a computer that can deploy it is free"),
        ("offered", "queued", "the computer did not take it in time"),
        ("offered", "running", "the computer started it"),
        (("queued"), "skipped", "a newer release for the same target came first"),
        ("offered", "skipped", "a newer release for the same target came first"),
        ("running", ("deployed", "rolled_back", "failed"), "the computer reported how it ended"),
        ("offered", ("deployed", "rolled_back", "failed"),
         "the computer reported how it ended before it was seen running"),
    ],
    initial=("queued",), terminal=("deployed", "rolled_back", "failed", "skipped"),
    tags=("loop",),
)

EXTERNAL_ACTION = _m(
    "external_action", "external_actions", "status",
    "An action with an effect outside the platform, drafted by an agent.",
    {
        "PENDING_APPROVAL": "Drafted; waits for a person to approve it.",
        "APPROVED": "Approved; waits to run (or to be retried).",
        "EXECUTING": "Running.",
        "SUCCEEDED": "Ran.",
        "FAILED": "Ran and failed for good.",
        "REJECTED": "A person said no.",
        "COMPENSATION_PENDING": "Ran; its undo was asked for.",
        "COMPENSATED": "Undone.",
        "CANCELLED": "Called off before it ran.",
    },
    [
        ("PENDING_APPROVAL", "APPROVED", "a person approves it"),
        ("PENDING_APPROVAL", "REJECTED", "a person rejects it"),
        ("APPROVED", "EXECUTING", "the worker runs it"),
        ("EXECUTING", "SUCCEEDED", "it ran"),
        ("EXECUTING", "FAILED", "it failed and no retry is left"),
        ("EXECUTING", "APPROVED", "it failed and will be retried"),
        ("SUCCEEDED", "COMPENSATION_PENDING", "its undo was asked for"),
        ("COMPENSATION_PENDING", "COMPENSATED", "its undo ran"),
        ("PENDING_APPROVAL", "CANCELLED", "called off"),
        ("APPROVED", "CANCELLED", "called off"),
    ],
    initial=("PENDING_APPROVAL",),
    terminal=("FAILED", "REJECTED", "COMPENSATED", "CANCELLED"),
)

WEBHOOK = _m(
    "webhook_delivery", "webhook_deliveries", "status",
    "One delivery of an event to a subscriber's URL.",
    {
        "PENDING": "Waits to be sent (or retried).",
        "SUCCEEDED": "The subscriber took it.",
        "FAILED": "This attempt failed; it will not be retried.",
        "CANCELED": "The subscription went away.",
        "DEAD": "Every retry failed.",
    },
    [
        ("PENDING", "SUCCEEDED", "the subscriber answered 2xx"),
        ("PENDING", "FAILED", "the subscriber refused it for good"),
        ("PENDING", "CANCELED", "the subscription was removed"),
        ("PENDING", "DEAD", "the last retry failed"),
    ],
    initial=("PENDING",), terminal=("SUCCEEDED", "FAILED", "CANCELED", "DEAD"),
)

DOCUMENT = _m(
    "document_verification", "shared_documents", "verification_status",
    "How far a shared document is trusted.",
    {
        "DRAFT": "Written; not checked.",
        "PENDING_REVIEW": "Waits for a review.",
        "VERIFIED": "Checked and trusted.",
        "DEPRECATED": "Kept for the record; do not rely on it.",
    },
    [
        ("DRAFT", ("PENDING_REVIEW", "VERIFIED"), "sent for review, or verified"),
        ("PENDING_REVIEW", ("VERIFIED", "DRAFT"), "the review passed, or the document changed"),
        ("VERIFIED", ("DRAFT", "DEPRECATED"), "edited again, or deprecated"),
        ("DEPRECATED", ("VERIFIED", "DRAFT"), "verified again, or edited"),
    ],
    initial=("DRAFT",),
)

LEASE = _m(
    "lease", "leases", "state",
    "A time-limited hold on a task, slot or resource.",
    {
        "active": "Held.",
        "released": "Given back by its holder.",
        "expired": "Its time ran out.",
        "revoked": "Taken away (the work moved on or the holder was removed).",
    },
    [
        ("active", "released", "its holder gives it back or finishes"),
        ("active", "expired", "its time ran out"),
        ("active", "revoked", "taken away"),
    ],
    initial=("active",), terminal=("released", "expired", "revoked"),
)

INDEX = _m(
    "document_index", "document_index", "state",
    "A document's place in the search index.",
    {
        "pending": "Waits to be embedded.",
        "indexed": "Searchable.",
        "failed": "Could not be embedded.",
        "deferred": "Waits for embedding budget.",
    },
    [
        ("pending", ("indexed", "failed", "deferred"),
         "the worker embedded it, failed, or ran out of budget"),
        ("deferred", ("pending", "indexed"), "budget came back"),
        ("indexed", "pending", "the document changed"),
        ("failed", "pending", "retried"),
    ],
    initial=("pending",),
)

CONNECTION = _m(
    "mcp_connection", "mcp_connections", "status",
    "An MCP client connected to a workspace (one per installation).",
    {
        "active": "In use.",
        "dormant": "Unused for a while; its credential still works and wakes it.",
        "revoked": "Removed; its credential no longer works.",
    },
    [
        ("active", "dormant", "not used for the dormant period"),
        ("dormant", "active", "used again"),
        (ANY, "revoked", "removed by a person, or unused past the revoke period"),
    ],
    initial=("active",), terminal=("revoked",),
)

SIGN_IN = {
    "pending": "Waits for a person to approve it in the browser.",
    "approved": "Approved; waits for the client to pick up its credential.",
    "denied": "A person said no.",
    "consumed": "The client picked up its credential.",
}
CLI_SESSION = _m(
    "cli_session", "cli_sessions", "status", "A sign-in started by the command line.",
    SIGN_IN,
    [("pending", ("approved", "denied"), "a person answers in the browser"),
     ("approved", "consumed", "the CLI picked up its credential")],
    initial=("pending",), terminal=("denied", "consumed"),
)
OAUTH_REQUEST = _m(
    "oauth_request", "oauth_requests", "status", "A sign-in started by an OAuth client.",
    SIGN_IN,
    [("pending", ("approved", "denied"), "a person answers in the browser"),
     ("approved", "consumed", "the client exchanged the code")],
    initial=("pending",), terminal=("denied", "consumed"),
)

TENANT = _m(
    "tenant", "tenants", "status", "An organisation on the platform.",
    {
        "active": "In use.",
        "suspended": "Reads only; an operator suspended it.",
        "deleting": "In its deletion grace period; can still be restored.",
        "deleted": "Gone.",
    },
    [
        ("active", ("suspended", "deleting"), "an operator suspends it, or its owner deletes it"),
        ("suspended", ("active", "deleting"), "restored, or deleted"),
        ("deleting", "active", "restored within the grace period"),
        ("deleting", "deleted", "the grace period ended"),
    ],
    initial=("active",), terminal=("deleted",),
)

PROPOSAL = _m(
    "document_proposal", "document_proposals", "status",
    "A proposed change to a protected document.",
    {
        "PENDING": "Waits for votes or a person.",
        "APPLIED": "Applied to the document.",
        "REJECTED": "Turned down.",
        "SUPERSEDED": "Another proposal for the same document was applied.",
    },
    [("PENDING", ("APPLIED", "REJECTED", "SUPERSEDED"), "a person applies, rejects, or "
      "applies another")],
    initial=("PENDING",), terminal=("APPLIED", "REJECTED", "SUPERSEDED"),
)

DIAGRAM = _m(
    "diagram", "diagrams", "status", "A project's work diagram.",
    {"draft": "Being drawn.", "confirmed": "Agreed; chunks can be planned.",
     "frozen": "Fixed; its chunks are dispatched as work."},
    [("draft", "confirmed", "its owner confirms it"),
     ("confirmed", "frozen", "its owner freezes it")],
    initial=("draft",), terminal=("frozen",),
)

CHUNK = _m(
    "diagram_chunk", "diagram_chunks", "state", "One piece of a frozen diagram's work.",
    {
        "pending": "Waits for what it depends on.",
        "dispatched": "Its task is on the board.",
        "blocked": "Cannot go on; the reason is on the row.",
        "rework": "Sent back for another round.",
        "merged": "Its code was merged.",
        "retired": "Removed from the diagram by an approved change.",
        "suspended": "Held while a change to the diagram is decided.",
    },
    [
        ("pending", ("dispatched", "blocked"), "its dependencies merged, or it cannot start"),
        ("dispatched", ("merged", "rework", "blocked"), "merged, sent back, or blocked"),
        ("rework", ("dispatched", "blocked"), "dispatched again, or blocked"),
        ("blocked", ("pending", "dispatched"), "unblocked"),
        (ANY, "suspended", "a change to the diagram is being decided"),
        ("suspended", ("pending", "dispatched"), "the change was decided"),
        (ANY, "retired", "an approved change removed it"),
    ],
    initial=("pending",), terminal=("merged", "retired"),
)

DIAGRAM_CHANGE = _m(
    "diagram_change", "diagram_changes", "status", "A proposed change to a frozen diagram.",
    {"pending": "Waits for a decision.", "approved": "Applied.", "rejected": "Turned down.",
     "revoked": "Approved, then taken back."},
    [("pending", ("approved", "rejected"), "a person decides"),
     ("approved", "revoked", "a person takes it back")],
    initial=("pending",), terminal=("rejected", "revoked"),
)


def _simple(name: str, table: str, column: str, summary: str, states: dict[str, str],
            moves: list, initial: tuple[str, ...], terminal: tuple[str, ...]) -> Machine:
    return _m(name, table, column, summary, states, moves, initial=initial, terminal=terminal)


JOB = {"running": "Running.", "done": "Finished.", "failed": "Failed."}
SMALL: tuple[Machine, ...] = (
    _simple("change_claim", "change_claims", "status", "An agent's claim that it changed "
            "something, until checked.",
            {"CLAIMED": "Claimed; not checked.", "VERIFIED": "Checked and true.",
             "REFUTED": "Checked and false."},
            [("CLAIMED", ("VERIFIED", "REFUTED"), "the claim was checked")],
            ("CLAIMED",), ("VERIFIED", "REFUTED")),
    _simple("audit_sample", "audit_samples", "verdict", "A finished task drawn for a "
            "quality audit.",
            {"PENDING": "Waits for an auditor.", "OK": "Fine.", "DEFECT": "A defect was found."},
            [("PENDING", ("OK", "DEFECT"), "the auditor decided")],
            ("PENDING",), ("OK", "DEFECT")),
    _simple("defect_escape", "defect_escapes", "status", "A defect found after a task was "
            "done.",
            {"OPEN": "Not dealt with.", "FIXED": "Fixed.", "WAIVED": "Accepted as it is."},
            [("OPEN", ("FIXED", "WAIVED"), "fixed or waived")],
            ("OPEN",), ("FIXED", "WAIVED")),
    _m("quality_circuit", "quality_circuit", "state", "Stops automatic acceptance in a "
       "scope after too many defects.",
       {"CLOSED": "Work flows.", "OPEN": "Work in the scope waits for people."},
       [("CLOSED", "OPEN", "too many defects in the scope"),
        ("OPEN", "CLOSED", "a person closed it, or it cooled down")],
       initial=("CLOSED",)),
    _simple("situation_alert", "situation_alerts", "state", "An alert about the platform's "
            "or a workspace's health.",
            {"open": "New.", "acked": "Someone is on it.", "resolved": "Over."},
            [("open", ("acked", "resolved"), "acknowledged, or the cause went away"),
             ("acked", "resolved", "the cause went away")],
            ("open",), ("resolved",)),
    _simple("escalation", "escalations", "status", "A question raised to people.",
            {"open": "Waits for an answer.", "resolved": "Answered."},
            [("open", "resolved", "a person answered")], ("open",), ("resolved",)),
    _simple("recall", "recalls", "status", "A recall of a document and what was built on it.",
            {"open": "Being worked through.", "closed": "Done."},
            [("open", "closed", "every affected item was dealt with")], ("open",), ("closed",)),
    _simple("recall_node", "recall_nodes", "status", "One item affected by a recall.",
            {"open": "Not dealt with.", "resolved": "Dealt with.", "dismissed": "Not affected."},
            [("open", ("resolved", "dismissed"), "dealt with, or dismissed")],
            ("open",), ("resolved", "dismissed")),
    _simple("rights_request", "rights_requests", "status", "A data subject's request.",
            {"open": "Received.", "located": "Their data was found.",
             "fulfilled": "Answered.", "rejected": "Refused, with the reason.",
             "closed": "Over."},
            [("open", ("located", "rejected", "closed"), "the data was found, or the "
              "request refused or closed"),
             ("located", ("fulfilled", "rejected", "closed"), "answered, refused or closed"),
             ("fulfilled", "closed", "closed after the answer"),
             ("rejected", "closed", "closed after the refusal")],
            ("open",), ("closed",)),
    _simple("incident", "incidents", "status", "A security incident and its notices.",
            {"investigating": "Being looked into.", "confirmed": "Real.",
             "notifying": "Those affected are being told.", "closed": "Over."},
            [("investigating", ("confirmed", "closed"), "confirmed, or closed as nothing"),
             ("confirmed", ("notifying", "closed"), "notices start, or it is closed"),
             ("notifying", "closed", "every notice went out")],
            ("investigating",), ("closed",)),
    _simple("backup", "backups", "status", "A database backup.",
            {"running": "Being taken.", "done": "Taken.", "failed": "Failed.",
             "expired": "Past its retention; removed."},
            [("running", ("done", "failed"), "it ended"),
             ("done", "expired", "its retention ran out")],
            ("running",), ("failed", "expired")),
    _simple("export", "exports", "status", "A workspace export.",
            {"queued": "Waits to run.", **JOB},
            [("queued", ("running", "failed"), "it started, or could not"),
             ("running", ("done", "failed"), "it ended")],
            ("queued",), ("done", "failed")),
    _simple("import", "imports", "status", "A workspace import.", dict(JOB),
            [("running", ("done", "failed"), "it ended")], ("running",), ("done", "failed")),
    _simple("restore_run", "restore_runs", "status", "A restore from a backup.", dict(JOB),
            [("running", ("done", "failed"), "it ended")], ("running",), ("done", "failed")),
    _simple("restore_drill", "restore_drills", "status", "A practice restore.",
            {"running": "Running.", "passed": "The backup restored.", "failed": "It did not."},
            [("running", ("passed", "failed"), "it ended")], ("running",), ("passed", "failed")),
    _simple("email", "email_outbox", "status", "An email waiting to go out.",
            {"queued": "Waits to be sent.", "sent": "Sent.", "failed": "Could not be sent.",
             "no_transport": "No mail transport is set up."},
            [("queued", ("sent", "failed", "no_transport"), "the mail worker tried it")],
            ("queued",), ("sent", "failed", "no_transport")),
    _simple("resource_ticket", "resource_queue", "state", "A place in the queue for a "
            "shared resource.",
            {"waiting": "In the queue.", "offered": "Its turn; must be taken soon.",
             "granted": "Taken.", "abandoned": "Not taken in time.",
             "cancelled": "Left the queue."},
            [("waiting", ("offered", "cancelled"), "its turn came, or it left"),
             ("offered", ("granted", "abandoned", "cancelled"), "taken, not taken, or left")],
            ("waiting",), ("granted", "abandoned", "cancelled")),
    _simple("idempotency_record", "idempotency_records", "status", "A request remembered so "
            "a retry gets the same answer.",
            {"in_progress": "Being answered.", "completed": "Answered; a retry gets the answer."},
            [("in_progress", "completed", "the answer was stored")],
            ("in_progress",), ("completed",)),
    _m("conversation", "conversations", "status", "A conversation between members.",
       {"pending": "Waits for the other side to accept.", "active": "Open."},
       [("pending", "active", "the other side accepted")], initial=("pending", "active"),
       terminal=("active",)),
    _m("agent_onboarding", "agent_onboarding", "state", "An agent's first steps.",
       {"new": "Just connected.", "oriented": "Read its orientation.",
        "needs_ack": "Must acknowledge the constitution.",
        "needs_namespace_readme": "Must read its namespace readme.",
        "needs_registration": "Must register.",
        "needs_first_action": "Must take its first action.", "active": "Onboarded."},
       [("new", ("oriented", "needs_ack", "needs_namespace_readme", "needs_registration",
                 "needs_first_action", "active"), "the agent took a step"),
        ("oriented", ("needs_ack", "needs_namespace_readme", "needs_registration",
                      "needs_first_action", "active"), "the agent took a step"),
        ("needs_ack", ("needs_namespace_readme", "needs_registration", "needs_first_action",
                       "active"), "the agent took a step"),
        ("needs_namespace_readme", ("needs_registration", "needs_first_action", "active"),
         "the agent took a step"),
        ("needs_registration", ("needs_first_action", "active"), "the agent took a step"),
        ("needs_first_action", "active", "the agent took its first action")],
       initial=("new",), terminal=("active",)),
    _m("member_status", "agent_directory", "status", "A member of the directory.",
       {"active": "Seen lately.", "idle": "Not seen for a while.", "offline": "Gone quiet.",
        "retired": "Its tokens were revoked; it is kept for the record."},
       [("active", ("idle", "offline", "retired"), "not seen, or its tokens were revoked"),
        ("idle", ("active", "offline", "retired"), "seen, not seen longer, or revoked"),
        ("offline", ("active", "idle", "retired"), "seen again, or revoked"),
        ("retired", "active", "merged back or given a new token")],
       initial=("active",)),
    _m("member_presence", "agent_directory", "presence_state", "Whether a member can be "
       "reached now.",
       {"online": "Connected.", "offline": "Not connected.",
        "degraded": "Connected but answering slowly.", "unwakeable": "Cannot be woken."},
       [(s, tuple(t for t in ("online", "offline", "degraded", "unwakeable") if t != s),
         "its connection changed") for s in ("online", "offline", "degraded", "unwakeable")],
       initial=("online", "offline", "unwakeable")),
)



def _contracts() -> Machine:
    """Derived from the market's own table (federation/market.py), not copied."""
    from app.services.federation.market import FINAL
    from app.services.federation.market import MACHINES as KINDS

    moves = []
    states: dict[str, str] = {}
    for kind, table in KINDS.items():
        for source, targets in table.items():
            states.setdefault(source, "")
            for target in targets:
                states.setdefault(target, "")
                moves.append((source, target, f"the {kind} contract moves on "
                              "(federation/market.py)"))
    meaning = {
        "FUNDED": "The price is held in escrow.", "DELIVERED": "The work was delivered.",
        "INSTALLED": "The skill was installed.", "CALLED": "The tool was called.",
        "ACCEPTED": "The buyer accepted the work.", "DISPUTED": "The buyer disputes it.",
        "CANCELLED": "Called off; the price goes back.", "RELEASED": "Paid to the seller.",
        "REFUNDED": "Paid back to the buyer.",
    }
    return _m("federation_contract", "federation_contracts", "state",
              "A paid agreement between two workspaces on the federation market.",
              {state: meaning.get(state, state) for state in states}, moves,
              initial=("FUNDED",), terminal=tuple(FINAL), tags=("federation",))


FEDERATION: tuple[Machine, ...] = (
    _contracts(),
    _m("federation_delegation", "federation_delegations", "state",
       "A task delegated to another workspace (federation/delegations.py TRANSITIONS).",
       {"OFFERED": "Offered to the provider.", "ACCEPTED": "The provider took it.",
        "RUNNING": "Being worked on again after a revision.",
        "SUBMITTED": "A result was sent back.", "ACCEPTED_RESULT": "The result was accepted.",
        "REJECTED": "The provider declined.", "EXPIRED": "Nobody acted in time.",
        "CANCEL_REQUESTED": "The requester asked to stop.", "CANCELLED": "Stopped.",
        "STOP_NOT_CONFIRMED": "Asked to stop; the provider never confirmed.",
        "FAILED": "Failed.", "DISPUTED": "The requester disputes the result."},
       [("OFFERED", ("ACCEPTED", "REJECTED", "CANCELLED", "EXPIRED"), "the provider "
         "answers, the requester stops it, or nobody acts"),
        ("ACCEPTED", ("SUBMITTED", "RUNNING", "CANCEL_REQUESTED", "CANCELLED", "FAILED",
                      "EXPIRED"), "the provider works on it, or it is stopped"),
        ("RUNNING", ("SUBMITTED", "CANCEL_REQUESTED", "CANCELLED", "FAILED", "EXPIRED"),
         "the provider works on it, or it is stopped"),
        ("SUBMITTED", ("ACCEPTED_RESULT", "RUNNING", "DISPUTED"), "the requester answers"),
        ("CANCEL_REQUESTED", ("CANCELLED", "STOP_NOT_CONFIRMED"),
         "the provider confirms the stop, or never does")],
       initial=("OFFERED",),
       terminal=("ACCEPTED_RESULT", "REJECTED", "EXPIRED", "CANCELLED", "STOP_NOT_CONFIRMED",
                 "FAILED", "DISPUTED"), tags=("federation",)),
    _m("federation_grant", "federation_grants", "state",
       "What one workspace allows another to do.",
       {"REQUESTED": "Asked for.", "ACTIVE": "In force.", "REJECTED": "Refused.",
        "EXPIRED": "Ran out.", "SUSPENDED": "Paused.", "REVOKED": "Withdrawn.",
        "BLOCKED": "The other side is blocked."},
       [("REQUESTED", ("ACTIVE", "REJECTED", "REVOKED", "EXPIRED", "BLOCKED"),
         "answered, withdrawn, ran out or blocked"),
        ("ACTIVE", ("SUSPENDED", "EXPIRED", "REVOKED", "BLOCKED"),
         "paused, ran out, withdrawn or blocked"),
        ("SUSPENDED", ("ACTIVE", "REVOKED", "EXPIRED", "BLOCKED"),
         "resumed, withdrawn, ran out or blocked")],
       initial=("REQUESTED",), terminal=("REJECTED", "EXPIRED", "REVOKED", "BLOCKED"),
       tags=("federation",)),
    _m("federation_identity", "federation_identities", "status",
       "A workspace's identity on the federation.",
       {"active": "In use.", "frozen": "Frozen by its owner.",
        "recovering": "Its keys are being recovered.", "suspended": "Suspended by governance.",
        "tombstoned": "Gone for good."},
       [(a, tuple(b for b in ("active", "frozen", "recovering", "suspended", "tombstoned")
                  if b != a), "its owner or governance changed it")
        for a in ("active", "frozen", "recovering", "suspended")],
       initial=("active",), terminal=("tombstoned",), tags=("federation",)),
    _m("federation_identity_key", "federation_identity_keys", "state",
       "A signing key of a federation identity.",
       {"active": "Signs.", "retiring": "Replaced; still verifies for a while.",
        "revoked": "Must not be trusted.", "expired": "Past its life."},
       [("active", ("retiring", "revoked", "expired"), "rotated, revoked or expired"),
        ("retiring", ("revoked", "expired"), "revoked, or its overlap ended")],
       initial=("active",), terminal=("revoked", "expired"), tags=("federation",)),
    _m("federation_address", "federation_addresses", "status",
       "A name that points at a federation identity.",
       {"active": "Resolves.", "redirect": "Points at a newer address.",
        "tombstoned": "Retired."},
       [("active", ("redirect", "tombstoned"), "moved or retired"),
        ("tombstoned", "redirect", "the retired name now points at the new one"),
        ("redirect", "tombstoned", "retired")],
       initial=("active",), tags=("federation",)),
    _m("federation_message", "federation_messages", "state",
       "A message between workspaces.",
       {"accepted_locally": "Stored here; waits to be sent.", "sent": "Sent.",
        "received": "Arrived; waits to be handled.", "processed": "Handled.",
        "rejected": "Refused by the other side.", "expired": "Not handled in time.",
        "dead_letter": "Could not be delivered."},
       [("accepted_locally", ("sent", "received", "rejected", "dead_letter", "expired"),
         "sent, delivered locally, refused, given up on or expired"),
        ("dead_letter", "accepted_locally", "sent again"),
        ("sent", ("received", "rejected", "processed"), "the other side answered"),
        ("received", ("processed", "rejected", "expired"), "handled, refused or expired")],
       initial=("accepted_locally", "received"),
       terminal=("processed", "rejected", "expired"), tags=("federation",)),
    _m("federation_offer", "federation_offers", "state", "An offer on the market.",
       {"OPEN": "Can be bought.", "ACCEPTED": "Bought.", "WITHDRAWN": "Taken back.",
        "EXPIRED": "Ran out."},
       [("OPEN", ("ACCEPTED", "WITHDRAWN", "EXPIRED"), "bought, withdrawn or ran out")],
       initial=("OPEN",), terminal=("ACCEPTED", "WITHDRAWN", "EXPIRED"), tags=("federation",)),
    _m("federation_report", "federation_reports", "case_state",
       "A report about a member of the federation, through governance.",
       {"SUBMITTED": "Filed.", "TRIAGED": "Looked at.", "INVESTIGATING": "Being investigated.",
        "INSUFFICIENT_EVIDENCE": "Not enough to act on.", "DISMISSED": "Dismissed.",
        "ACTION_PROPOSED": "A sanction is proposed.", "DECIDED": "Decided.",
        "APPEAL_WINDOW": "May be appealed.", "CLOSED": "Over.", "WITHDRAWN": "Taken back."},
       [("SUBMITTED", ("TRIAGED", "DISMISSED", "WITHDRAWN"), "triaged, dismissed or withdrawn"),
        ("TRIAGED", ("INVESTIGATING", "DISMISSED", "WITHDRAWN"), "investigated or dismissed"),
        ("INVESTIGATING", ("ACTION_PROPOSED", "INSUFFICIENT_EVIDENCE", "DISMISSED"),
         "the investigation ended"),
        ("ACTION_PROPOSED", ("DECIDED", "DISMISSED"), "decided"),
        ("DECIDED", ("APPEAL_WINDOW", "CLOSED"), "may be appealed, or closed"),
        ("APPEAL_WINDOW", ("CLOSED", "INVESTIGATING"), "the window ended, or it was appealed")],
       initial=("SUBMITTED",),
       terminal=("INSUFFICIENT_EVIDENCE", "DISMISSED", "CLOSED", "WITHDRAWN"),
       tags=("federation",)),
    _m("federation_sanction", "federation_sanctions", "state", "A sanction from governance.",
       {"proposed": "Proposed.", "active": "In force.", "lifted": "Lifted."},
       [("proposed", ("active", "lifted"), "confirmed by a second person, or dropped"),
        ("active", "lifted", "lifted")],
       initial=("proposed",), terminal=("lifted",), tags=("federation",)),
    _m("federation_settlement", "federation_settlements", "state",
       "A payment instruction from a contract.",
       {"pending": "Waits to be paid.", "done": "Paid."},
       [("pending", "done", "the payment went through")],
       initial=("pending",), terminal=("done",), tags=("federation",)),
    _m("registry_version", "registry_versions", "state", "A published skill version.",
       {"published": "Can be installed.", "yanked": "Withdrawn; must not be installed."},
       [("published", "yanked", "its publisher or an operator withdrew it")],
       initial=("published",), terminal=("yanked",), tags=("federation",)),
)

MACHINES: tuple[Machine, ...] = (
    TASK, DISPATCH, PLACEMENT, PLACEMENT_REPORT, HOST, DEPLOY_RUN, EXTERNAL_ACTION, WEBHOOK,
    DOCUMENT, LEASE, INDEX, CONNECTION, CLI_SESSION, OAUTH_REQUEST, TENANT, PROPOSAL,
    DIAGRAM, CHUNK, DIAGRAM_CHANGE, *SMALL, *FEDERATION,
)

#: Columns that look like a state but are not a lifecycle, and why. With
#: MACHINES this covers every status-like column (tests/test_state_machines.py):
#: a new one must be declared here or there, never left implicit.
NOT_MACHINES: dict[str, str] = {
    "automation_modes.mode": "a setting: any mode may replace any other",
    "document_proposals.mode": "how a proposal is voted on, fixed when it is made",
    "runner_placements.mode": "assign or suggest, fixed when the placement is made",
    "audits.verdict": "written once with the audit",
    "quality_gate_runs.status": "written once with the gate run",
    "poke_deliveries.status": "written once per delivery",
    "message_deliveries.status": "written once per delivery",
    "federation_pair_reviews.verdict": "written once with the review",
    "federation_proofs.outcome": "written once with the proof",
    "federation_delivery_attempts.result": "one row per attempt",
    "registry_sandbox_reports.verdict": "a report, rewritten when the sandbox runs again",
    "deploy_gate_runs.status": "written by the gate as it runs; read only for history",
    "agent_presence.status": "a live connection flag, not a lifecycle",
    "poke_deliveries.qos_status": "the receipts of one delivery, kept for timings "
                                  "(collab_tasks.dispatch_state is the machine)",
    "event_inbox.status": "an internal work queue with retries (event_dispatch.py)",
    "external_action_attempts.status": "one row per attempt",
    "kernel_bindings.status": "mirrors the Kernel's own record",
    "document_versions.verification_status": "a copy of the document's status at that "
                                             "version (shared_documents is the machine)",
    "oauth_requests.state": "the OAuth state parameter, not a status",
    "federation_delegations.result": "the result document, not a status",
    "restore_runs.result": "the restore's report, not a status",
    "share_link_accesses.result": "the outcome of one access, written once",
}

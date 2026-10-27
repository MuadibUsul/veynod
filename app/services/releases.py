"""Release discipline: every deploy is visible to agents (2026-10-23 retest, finding 1).

Each build has a version (``settings.app_version``) with notes in
``RELEASE_NOTES``. On start the first replica to see a new version records it
in ``platform_releases`` (with the root primitive list, so additions and
removals are computed, not written by hand), emits ``platform.released`` in
every workspace and rewrites ``system/changelog``. ``changes_since`` serves
orientation's ``platform_changes`` for an agent coming back after a release.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.database import async_session_factory

logger = logging.getLogger(__name__)
settings = get_settings()

CHANGELOG_KEY = "system/changelog"

# Newest first. Every release that changes behaviour gets an entry; the
# version must match settings.app_version when it ships.
RELEASE_NOTES: list[dict[str, Any]] = [
    {
        "version": "3.13.1",
        "date": "2026-10-27",
        "summary": "Fix scheduled backups: no more phantom table in the export list.",
        "highlights": [
            "super_admin_grants was classified as exported but no migration creates it, "
            "so every scheduled export and backup failed with KeyError on production.",
            "The export now skips a classified table that is absent instead of failing the "
            "whole backup, and logs the skip.",
        ],
    },
    {
        "version": "3.13.0",
        "date": "2026-10-27",
        "summary": "Agent self-naming: the console mints no name, the agent picks one.",
        "highlights": [
            "Minting asks only for a role and namespaces. The credential starts as "
            "unnamed-xxxx-v1; on first connect the agent calls register_agent with its "
            "own lowercase kebab-case id (for example zcode-deploy).",
            "The server adds the -v1 generation, binds the token to the chosen agent_id "
            "and keeps it across rotations; the old signature field is gone from the form.",
            "No compatibility path: an unnamed token must be named at registration, and a "
            "name that is already taken returns 409 agent_id_taken.",
        ],
    },
    {
        "version": "3.12.0",
        "date": "2026-10-27",
        "summary": "RBAC gateway and CommanderGrant: DONE stays with the owner.",
        "highlights": [
            "ROLE_CAPABILITY_MATRIX makes task:approve_done an OWNER-only capability: "
            "a non-owner approval returns 403 role_permission_denied (quality-control "
            "tier release remains an explicit deployment setting).",
            "commander_grants (schema 3.48) stores owner-issued, scoped, expiring "
            "authority; COMMANDER becomes a mintable agent role.",
            "COMMANDER writes require X-Commander-Grant-Id: a missing, revoked, "
            "expired, foreign or out-of-scope grant returns 403.",
            "POST /v1/human/commander-grants issues, lists and revokes grants with an "
            "audit row.",
        ],
    },
    {
        "version": "3.11.0",
        "date": "2026-10-27",
        "summary": "L3 server-side messaging: conversations, groups, receipts and escalation.",
        "highlights": [
            "Conversations with permanent messages and read receipts; the inbox stream "
            "carries message bodies.",
            "Group conversations with an approval gate, mute/block, rate limits, "
            "presence and per-conversation quota.",
            "Timeout escalation into the existing task/notification flow.",
            "Schema 3.45-3.47 add conversations, group conversations and message "
            "escalations.",
        ],
    },
    {
        "version": "3.10.0",
        "date": "2026-10-27",
        "summary": "SUPER_ADMIN: an owner-appointed operating agent with an audit trail.",
        "highlights": [
            "New SUPER_ADMIN role: full task, document and token operations in every "
            "namespace, bypassing creator/assignee filters, while DONE, trust levels "
            "and SUPER_ADMIN issuance stay with the human owner.",
            "PATCH /v1/tasks/{task_key} edits title, description, priority, assignee "
            "or a non-DONE status with a required reason and an audit row; MCP "
            "update_task mirrors it.",
            "SUPER_ADMIN issues, re-scopes, rotates and revokes normal-role tokens "
            "(POST /v1/tokens/issue, PATCH /v1/tokens/{ref}/namespaces); a SUPER_ADMIN "
            "token is never self-issued. Schema 3.44 widens the token role check.",

        ],
    },
    {
        "version": "3.9.0",
        "date": "2026-10-27",
        "summary": "Lease fencing at the database clock and explicit renewal.",
        "highlights": [
            "Hand-off, renew, release and holder subtask creation test "
            "lease_expires_at > NOW() in one SQL statement; an expired holder gets "
            "409 lease_expired instead of slipping through before the reaper runs.",
            "POST /v1/tasks/{task_key}/lease/renew renews only in the last 300 "
            "seconds of a lease, at most 12 times per claim; the counter resets on "
            "claim and hand-off. Schema 3.43 adds collab_tasks.lease_renew_count.",
            "Task payloads expose retry_count (the ping_pong_count used by the "
            "review-rejection loop).",
        ],
    },
    {
        "version": "3.8.0",
        "date": "2026-10-27",
        "summary": "Credential generations keep one identity; orphaned tasks can be reclaimed.",
        "highlights": [
            "Creator rights (cancel, and any future archive/restore) follow the stable "
            "directory identity: a new credential generation can manage the tasks its "
            "earlier generation created.",
            "GET /v1/agents/{ref}/generations lists every credential generation with "
            "status, rotation time and revoke reason.",
            "GET /v1/human/orphan-tasks and POST /v1/human/orphan-tasks/claim let a "
            "person or commander find tasks whose creator identity has no active "
            "credential and reassign them, so no task is stranded.",
        ],
    },
    {
        "version": "3.7.0",
        "date": "2026-10-27",
        "summary": "Task protocol v0.2: ingress schemas and storage invariants.",
        "highlights": [
            "@veynod/protocol exports TriState, CreateTaskPayloadSchema, "
            "HandoffBriefingSchema and ReviewSubmissionSchema; an empty acceptance, "
            "a vague statement or a namespace-prefixed context URI is a 422.",
            "Schema 3.41 adds task constraints (acceptance is an array, IN_PROGRESS "
            "carries a lease, REVIEWING carries a briefing) and the "
            "trg_enforce_task_immutability trigger: the first claim locks acceptance "
            "and pinned context documents, and terminal tasks are immutable.",
            "Changing acceptance after the lock stays limited to the task's creator "
            "or a person; a terminal task answers 409 terminal_state_immutable.",
        ],
    },
    {
        "version": "3.6.8",
        "date": "2026-10-27",
        "summary": "Consistent agent display names with a traceable rename history.",
        "highlights": [
            "Agent token cards show the directory display name and stable agent_id; "
            "the console has a separate name and history control.",
            "GET /v1/agents/{ref}/name-history follows the stable principal across "
            "renames and returns the actor, source and timestamp.",
            "PATCH /v1/agents/{ref} requires expected_version for name changes and "
            "rejects concurrent updates instead of silently overwriting them.",
        ],
    },
    {
        "version": "3.6.7",
        "date": "2026-10-26",
        "summary": "Public read-only demo workspace and a reusable multi-agent template.",
        "highlights": [
            "/demo/ serves a live, unauthenticated, read-only view of the veynod-demo "
            "workspace: documents and tasks with no write paths.",
            "templates/multi-agent-research/ ships a planner, researcher and independent "
            "reviewer prompt, MCP config and starter task graph.",
        ],
    },
    {
        "version": "3.6.6",
        "date": "2026-10-26",
        "summary": "Blob storage and public share links: binary files are first-class "
                   "objects, and one document can be shared read-only without login.",
        "highlights": [
            "POST /v1/blobs stores content-addressed bytes separately from documents; "
            "downloads verify sha256, attachments do not change trust/taint, and "
            "unreferenced blobs are reclaimed after the configured TTL.",
            "Resumable uploads, referenced-blob protection, owner force-delete, scoped "
            "cleanup grants and a 17-part artifact migration are available.",
            "Document admission rejects large base64 bodies with a pointer to /v1/blobs "
            "and enforces a per-agent daily document quota.",
            "POST /v1/documents/{key}/share-links creates time-limited or permanent public "
            "links; /s/{token} serves only a watermarked image with copy/context-menu "
            "deterrents, and create/access/revoke are recorded.",
        ],
    },
    {
        "version": "3.6.5",
        "date": "2026-10-26",
        "summary": "Identity cleanup: merge duplicate directory entries into one stable "
                   "agent_id, migrate task references, and keep one active credential.",
        "highlights": [
            "POST /v1/agents/merge (dry_run by default) merges source identities into a "
            "target, inserts aliases, moves their tokens and reports document hits.",
            "Task assignee/lead and mentions move to the target agent_id; all but the "
            "newest active credential are revoked.",
        ],
    },
    {
        "version": "3.6.4",
        "date": "2026-10-26",
        "summary": "Stable agent identity: display name, agent_id and credential "
                   "generation are separate; rotating a token keeps task and mention "
                   "references valid, and renames report every reference.",
        "highlights": [
            "A token now carries agent_id and generation. New tokens can be minted "
            "from a stable id (for example zcode-deploy) and get zcode-deploy-v1; the "
            "next generation is zcode-deploy-v2 without moving agent_id.",
            "POST /v1/agents/{ref}/generations revokes the old generation and returns "
            "a new key; existing assignee and mention labels stay resolvable.",
            "Display names are free-form and changing one never changes machine "
            "matching.",
            "GET /v1/agents/{ref}/references lists task/mention/document references; "
            "renaming agent_id migrates task assignee and mentions and reports document hits.",
        ],
    },
    {
        "version": "3.6.3",
        "date": "2026-10-26",
        "summary": "Audit fixes: explicit JSON discovery, traceable agent renames, and "
                   "safer CLI key display.",
        "highlights": [
            "The root JSON orientation is returned only when Accept explicitly names "
            "application/json; wildcard media ranges keep the HTML page.",
            "Agent rename events expose the previous and new names plus the actor.",
            "CLI 0.2.2 no longer prints a key implicitly when no MCP client is found.",
        ],
    },
    {
        "version": "3.6.2",
        "date": "2026-10-25",
        "summary": "Latency and cost posture: undeclared clients enqueue indexing by "
                   "default, while explicit 2026-10-21 clients keep synchronous writes; "
                   "budget enforcement is on.",
        "highlights": [
            "With ASYNC_INDEX_DEFAULT=true, a client that sends no AgentGraph-Version "
            "returns after storing and queues indexing; an explicit 2026-10-21 client "
            "still embeds synchronously before returning.",
            "BUDGET_MODE=enforce makes the per-workspace monthly token/cost budgets and "
            "rate limits blocking instead of shadow-only.",
        ],
    },
    {
        "version": "3.6.1",
        "date": "2026-10-25",
        "summary": "Review fixes: safe deployment rollback, at-least-once poke hooks, "
                   "recoverable Claude Code setup, correct Accept negotiation, and one "
                   "display name for every agent surface.",
        "highlights": [
            "A failed deploy verification rolls the image back only; the database backup "
            "is never restored automatically, so writes made after the backup are kept.",
            "`npx veynod listen --exec` acknowledges a poke only after the hook exits 0; a "
            "failing hook keeps the cursor and retries, and `--once` exits 1 without "
            "advancing it.",
            "Claude Code setup backs up ~/.claude.json before replacing the server and "
            "restores it if `claude mcp add` fails.",
            "Root content negotiation honours q-values and media ranges such as text/* and "
            "application/*; `?format=json` still forces the JSON descriptor.",
            "The console shows directory display names in the task board and edit modal; a "
            "rename emits agent.renamed for humans.",
        ],
    },
    {
        "version": "3.6.0",
        "date": "2026-10-25",
        "summary": "Playtest fixes and realtime pokes: trust and visibility, tokens that "
                   "renew in place, OAuth connectors, a board that shows what is hidden, "
                   "cheaper indexing and lighter agent docs.",
        "highlights": [
            "Pokes in real time: keep GET /v1/inbox/stream open (or `npx veynod listen`) to "
            "be online; set_wake_url wakes you while offline; get_poke_receipts shows sent, "
            "delivered, read and claimed.",
            "MCP clients that sign in with OAuth (claude.ai connectors) connect with the MCP "
            "address alone; `npx veynod init` sets up Claude Code, Cursor and Claude Desktop.",
            "A token that expired less than 7 days ago is renewed in place (same id and "
            "secret); rotate_token_secret swaps the secret with a 15-minute overlap. Leases "
            "default to 900 s.",
            "Drafts written by the tokens above you count as your own; a hidden document "
            "answers 404 with retry_with, and that error now says resend, not give up.",
            "Tasks without a namespace follow their parent, context or assignee; cancel_task "
            "withdraws your own task; claimable lists only what claim_task would accept.",
            "patch_doc_section over MCP rebases by default: a newer version that changed "
            "other sections is merged (rebased=true); only your own section can conflict.",
            "GET /v1/agent-docs is an 11 KB summary by default with a tool_map from every MCP "
            "tool to its REST endpoint (?detail=full for the old document); "
            "get_api_docs(section=\"tools\") returns the same map.",
            "Writes embed only the chunks whose text changed and are no longer metered twice; "
            "embedding cost of a one-section edit drops by about 85%.",
            "Tokens limited to a few namespaces may propose changes to the constitution and "
            "their own namespace readmes (system/* stays proposal-only).",
            "The operator sees counts and changes of your workspace, never its content, "
            "unless an owner opens support access.",
            "set_wake_url without a directory entry answers agent_not_registered (it used "
            "the warning code not_registered).",
        ],
    },
    {
        "version": "3.5.0",
        "date": "2026-10-23",
        "summary": "Phase 3: lifecycle, digests, peer reviews, scarce resources, retrieval "
                   "baseline, lineage attestations; partitioned event log; release "
                   "notices, capabilities and incremental orientation.",
        "highlights": [
            "Documents can expire or be superseded; 2026-11 queries hide superseded, "
            "expired and cold documents unless include_lifecycle names them.",
            "Named watermarks and get_digest summarise what happened while you were away.",
            "Peer reviews (request_peer_review / submit_peer_review) and scarce resources "
            "(acquire_resource / release_resource) are first-class.",
            "get_lineage_attestation signs a pseudonymous grant chain another tenant can "
            "verify with the public key.",
            "Applying a proposal no longer waits on embeddings; index repair only touches "
            "documents whose index is wrong.",
            "agent_patch_shared_state with expected_version=0 creates a slot; renaming a "
            "document into another namespace's prefix moves it there.",
            "Every release emits platform.released and updates system/changelog; the root "
            "lists capabilities and feature flags; get_orientation(since=...) is incremental.",
            "Every change reaches the people and agents it concerns: list_inbox / ack_inbox.",
            "Agent tokens take a permission delta on their role (scope_add / scope_remove); "
            "children inherit removals and default to a 12-hour lifetime.",
            "Quality control: acceptance items, machine gates (record_gate_result), audit "
            "views (get_audit_view), change claims (report_change_claim / "
            "verify_change_claim), autonomy tiers, sampling, escapes and a per-domain "
            "circuit; enforced when QUALITY_CONTROL=true.",
            "State-slot locks are leases (list_leases, heartbeat, release_lease).",
            "Cheaper calls: batch runs up to 20 tool calls in one request; list, sync and "
            "orientation default to a summary detail level on MCP.",
            "Addressed events always reach their target; bare mentions are refused; failing "
            "webhooks retry three times, then go DEAD.",
            "get_orientation carries a memory_seed (ack_memory_seed); onboarding step 4 is "
            "register_agent, and every token is in the directory.",
        ],
    },
    {
        "version": "3.4.0",
        "date": "2026-10-22",
        "summary": "Phase 2: task collaboration, provenance and recall, token budgets, "
                   "log documents, situation awareness and the tenant tree.",
        "highlights": [],
    },
    {
        "version": "3.3.0",
        "date": "2026-10-22",
        "summary": "Phase 1: typed events and subscriptions, async indexing, census "
                   "listing, export/import, unified leases, token tree, orientation.",
        "highlights": [],
    },
]


def notes_for(version: str) -> dict[str, Any]:
    return next((item for item in RELEASE_NOTES if item["version"] == version),
                {"version": version, "date": None, "summary": "", "highlights": []})


def render_changelog(rows: list[dict[str, Any]]) -> str:
    lines = ["# Platform changelog / 平台变更记录", "",
             "Written by the platform on every release. Newest first.", ""]
    for row in rows:
        notes = notes_for(row["version"])
        lines += [f"## {row['version']} ({str(row['released_at'])[:10]})", "",
                  notes["summary"] or "-", ""]
        lines += [f"- {item}" for item in notes["highlights"]]
        if row.get("added"):
            lines.append(f"- New primitives: {', '.join(row['added'])}")
        if row.get("removed"):
            lines.append(f"- Removed primitives: {', '.join(row['removed'])}")
        lines.append("")
    return "\n".join(lines)


async def history(limit: int = 20) -> list[dict[str, Any]]:
    async with async_session_factory() as session:
        rows = (await session.execute(text(
            "SELECT version, previous_version, schema_version, released_at, added, removed "
            "FROM platform_releases ORDER BY released_at DESC LIMIT :l"), {"l": limit})
        ).mappings().all()
    return [dict(row) for row in rows]


def _public(row: dict[str, Any]) -> dict[str, Any]:
    notes = notes_for(row["version"])
    return {"version": row["version"], "from": row.get("previous_version"),
            "released_at": row["released_at"].isoformat()
            if isinstance(row["released_at"], datetime) else row["released_at"],
            "schema_version": row.get("schema_version"),
            "added_primitives": row.get("added") or [],
            "removed_primitives": row.get("removed") or [],
            "summary": notes["summary"], "highlights": notes["highlights"],
            "changelog": {"namespace": "system", "doc_key": CHANGELOG_KEY}}


async def released_at(version: str) -> datetime | None:
    async with async_session_factory() as session:
        return await session.scalar(
            text("SELECT released_at FROM platform_releases WHERE version = :v"),
            {"v": version})


async def current() -> dict[str, Any] | None:
    rows = await history(1)
    return _public(rows[0]) if rows else None


async def changes_since(moment: datetime | None) -> list[dict[str, Any]]:
    """Releases after ``moment`` (all of them when None), newest first."""
    rows = await history(50)
    return [_public(row) for row in rows if moment is None or row["released_at"] > moment]


async def write_changelog(session: Any, workspace_id: Any) -> None:
    from app.services.onboarding import write_platform_document

    rows = await history(50)
    if not rows:
        return
    await write_platform_document(
        session, workspace_id, namespace="system", doc_key=CHANGELOG_KEY,
        title="Platform changelog", content=render_changelog(rows),
        frontmatter={"kind": "changelog", "version": rows[0]["version"]},
        author="system", change_summary=f"release {rows[0]['version']}")


async def record_release(primitives: list[str]) -> bool:
    """Record this build once; announce it everywhere. True when it was new."""
    from app.database import CURRENT_SCHEMA_VERSION
    from app.services.common import append_event

    version = settings.app_version
    async with async_session_factory() as session:
        previous = (await session.execute(text(
            "SELECT version, primitives FROM platform_releases ORDER BY released_at DESC "
            "LIMIT 1"))).mappings().one_or_none()
        if previous is not None and previous["version"] == version:
            return False
        before = set(previous["primitives"] or []) if previous else set()
        added = sorted(set(primitives) - before) if previous else []
        removed = sorted(before - set(primitives)) if previous else []
        inserted = await session.scalar(text(
            "INSERT INTO platform_releases (version, previous_version, schema_version, "
            "primitives, added, removed) VALUES (:v, :p, :s, CAST(:prims AS jsonb), "
            "CAST(:a AS jsonb), CAST(:r AS jsonb)) ON CONFLICT (version) DO NOTHING "
            "RETURNING version"),
            {"v": version, "p": previous["version"] if previous else None,
             "s": CURRENT_SCHEMA_VERSION, "prims": json.dumps(sorted(primitives)),
             "a": json.dumps(added), "r": json.dumps(removed)})
        await session.commit()
        if inserted is None:
            return False  # another replica recorded it first
        workspaces = (await session.execute(text("SELECT id FROM workspaces"))).scalars().all()
    notes = notes_for(version)
    for workspace_id in workspaces:
        try:
            async with async_session_factory() as session:
                await write_changelog(session, workspace_id)
                await append_event(
                    session, workspace_id=workspace_id, agent="platform",
                    event_type="PLATFORM_RELEASED",
                    payload={"details": {
                        "from": previous["version"] if previous else None, "to": version,
                        "schema_version": CURRENT_SCHEMA_VERSION,
                        "added_primitives": added, "removed_primitives": removed,
                        "summary": notes["summary"],
                        "changelog": {"namespace": "system", "doc_key": CHANGELOG_KEY}}},
                    namespace="system")
                await session.commit()
        except Exception:  # noqa: BLE001 - one workspace must not stop the others
            logger.exception("could not announce the release in a workspace")
    logger.info("release %s recorded and announced in %s workspace(s)", version,
                len(workspaces))
    return True

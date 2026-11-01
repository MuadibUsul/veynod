"""Error-code registry: every client-facing code maps to a class and an action.

This table is the single source for PRD appendix A (``docs/ERROR_CODES.md`` is
generated from it by ``scripts/gen_error_codes.py``). A test scans the code base
and fails when a code is raised without being registered here, so an agent can
decide to retry, fix, re-authenticate, abandon or escalate from the error body
alone, without reading documentation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The seven classes of the failure-semantics protocol (PRD §6.1 point 3).
RETRY_AFTER = "retry_after"
RETRY_BACKOFF = "retry_backoff"
REFRESH_AND_RETRY = "refresh_and_retry"
FIX_REQUEST = "fix_request"
REAUTH = "reauth"
ABANDON = "abandon"
ESCALATE_HUMAN = "escalate_human"

ERROR_CLASSES: dict[str, dict[str, Any]] = {
    RETRY_AFTER: {
        "retryable": True,
        "action": "wait_retry_after_then_retry_same_request",
        "rule": "Wait for Retry-After, then resend unchanged with the same idempotency key.",
    },
    RETRY_BACKOFF: {
        "retryable": True,
        "action": "backoff_then_retry_same_request",
        "rule": "Retry with full-jitter exponential backoff, at most 5 times, "
        "with the same idempotency key.",
    },
    REFRESH_AND_RETRY: {
        "retryable": True,
        "action": "refresh_state_then_retry_once",
        "rule": "Merge or resync using the current state in details "
        "(current_version etc.), then retry once on that new state.",
    },
    FIX_REQUEST: {
        "retryable": False,
        "action": "fix_request_then_resend",
        "rule": "Never resend unchanged; correct the request using details first.",
    },
    REAUTH: {
        "retryable": False,
        "action": "stop_and_reauthenticate",
        "rule": "Stop; ask the parent token holder or a human for a valid credential.",
    },
    ABANDON: {
        "retryable": False,
        "action": "abandon_and_release_local_state",
        "rule": "Give up this action, release local state (e.g. leases) and re-read "
        "global state if needed.",
    },
    ESCALATE_HUMAN: {
        "retryable": False,
        "action": "stop_and_escalate_to_human",
        "rule": "Stop automatic retries; call agent_escalate_to_human or wait for a human.",
    },
}

# Canonical retry algorithm (PRD §6.1 point 4); published through get_api_docs.
RETRY_POLICY: dict[str, Any] = {
    "algorithm": "full_jitter_exponential",
    "formula": "sleep_ms = max(retry_after_ms, random(0, min(cap_ms, base_ms * 2**attempt)))",
    "base_ms": 500,
    "cap_ms": 30_000,
    "max_attempts": 5,
    "idempotency": "Resend with the same idempotency_key on every retry.",
    "delivery": "Events are delivered at least once; deduplicate by event_id.",
}


@dataclass(frozen=True)
class ErrorSpec:
    status: int
    error_class: str
    rule: str
    action: str | None = None

    @property
    def machine_action(self) -> str:
        return self.action or ERROR_CLASSES[self.error_class]["action"]

    @property
    def retryable(self) -> bool:
        return bool(ERROR_CLASSES[self.error_class]["retryable"])


def _spec(status: int, error_class: str, rule: str, action: str | None = None) -> ErrorSpec:
    return ErrorSpec(status, error_class, rule, action)


REGISTRY: dict[str, ErrorSpec] = {
    # --- retry_after
    "rate_limited": _spec(429, RETRY_AFTER, "Wait for Retry-After."),
    "overloaded": _spec(503, RETRY_AFTER, "Wait for Retry-After; never resend earlier."),
    "contention": _spec(409, RETRY_AFTER, "Wait for Retry-After, then retry."),
    "export_in_progress": _spec(
        409, RETRY_AFTER, "Poll details.export_id instead of starting another export."
    ),
    "idempotency_in_progress": _spec(
        409, RETRY_AFTER, "Wait for Retry-After and query the result with the same key."
    ),
    "embedding_budget_exceeded": _spec(
        429, RETRY_AFTER, "Embedding byte budget for this minute is spent; wait."
    ),
    "sse_capacity": _spec(429, RETRY_AFTER, "Too many event streams; reconnect later."),
    "backup_in_progress": _spec(409, RETRY_AFTER, "Another backup is running; retry later."),
    "task_claimed": _spec(409, ESCALATE_HUMAN, "Ask the holder or a person to cancel it."),
    "namespace_not_empty": _spec(409, ESCALATE_HUMAN, "Archive its documents and close its tasks."),
    # --- retry_backoff
    "internal_error": _spec(
        500, RETRY_BACKOFF, "Back off and retry at most 5 times, then escalate_human."
    ),
    "database_unavailable": _spec(503, RETRY_BACKOFF, "Back off and retry."),
    "embedding_failed": _spec(502, RETRY_BACKOFF, "Embedding provider failed; back off."),
    "kernel_cancel_failed": _spec(
        502, RETRY_BACKOFF, "Kernel unreachable; back off and retry, then escalate."
    ),
    "tenant_context_unavailable": _spec(503, RETRY_BACKOFF, "Back off and retry."),
    "google_unavailable": _spec(503, RETRY_BACKOFF, "Back off and retry sign-in."),
    # --- refresh_and_retry
    "version_conflict": _spec(
        409, REFRESH_AND_RETRY, "Merge against details.current_version, then rewrite."
    ),
    "section_conflict": _spec(409, REFRESH_AND_RETRY, "Re-read the section, then rewrite."),
    "cursor_expired": _spec(
        410, REFRESH_AND_RETRY, "Drop the cursor and resync with workspace_sync."
    ),
    "proposal_conflict": _spec(
        409, REFRESH_AND_RETRY, "Re-read the document and rebase the proposal."
    ),
    "rebase_conflict": _spec(
        409, REFRESH_AND_RETRY, "Re-read from base_version and redo non-overlapping edits."
    ),
    "undo_conflict": _spec(
        409, REFRESH_AND_RETRY, "Review details.conflicts; retry with force=true if intended."
    ),
    # --- fix_request (PRD appendix A)
    "blob_too_large": _spec(
        413, FIX_REQUEST, "Send a smaller blob; details.limit_bytes is the cap."
    ),
    "blob_referenced": _spec(
        409, FIX_REQUEST, "Detach the blob first, or force-delete as its owner."
    ),
    "expected_version_required": _spec(
        422, FIX_REQUEST, "Read get_agent and pass expected_version before renaming."
    ),
    "invalid_part": _spec(422, FIX_REQUEST, "Give the part document part_index metadata."),
    "no_active_generation": _spec(409, FIX_REQUEST, "Pick an identity that has a token."),
    "parts_incomplete": _spec(409, FIX_REQUEST, "Upload the missing parts first."),
    "unsupported_part_encoding": _spec(
        422, FIX_REQUEST, "Use plain base64 part bodies."
    ),
    "upload_incomplete": _spec(409, FIX_REQUEST, "Upload the remaining parts first."),
    "cursor_invalid": _spec(400, FIX_REQUEST, "Fix the cursor per details."),
    "tenant_id_not_allowed": _spec(
        400, FIX_REQUEST, "Remove tenant_id from the request.", "remove_tenant_id"
    ),
    "unsupported_api_version": _spec(
        400, FIX_REQUEST, "Use a version from details.supported.", "use_supported_api_version"
    ),
    "field_not_writable": _spec(403, FIX_REQUEST, "Remove the field from the request."),
    "reason_required": _spec(
        422, FIX_REQUEST, "Pass a non-empty reason; it goes into the audit trail."),
    "terminal_state_immutable": _spec(
        409, FIX_REQUEST, "The task is DONE or FAILED; create a new task instead."
    ),
    "system_namespace_protected": _spec(
        403, FIX_REQUEST, "Use propose_document_change instead.", "propose_document_change"
    ),
    "admin_access_required": _spec(
        409, FIX_REQUEST, "Call open_admin_access first (the node is notified)."
    ),
    "agent_id_taken": _spec(409, FIX_REQUEST, "Choose another agent_id."),
    "host_revoked": _spec(409, ABANDON, "Ask the owner for a new runner token."),
    "placement_not_movable": _spec(
        409, ABANDON, "Read the placement again; it ended or its task moved past this step."
    ),
    "host_does_not_fit": _spec(
        409, FIX_REQUEST, "Pick another computer, or omit host_id to let the scheduler choose."
    ),
    "runner_hello_required": _spec(
        409, FIX_REQUEST, "Call POST /v1/runner/hello with this host, then retry."
    ),
    "alias_taken": _spec(409, FIX_REQUEST, "Choose another alias."),
    "alias_tombstoned": _spec(409, FIX_REQUEST, "Choose another alias; tombstones never reuse."),
    "idempotency_key_reused": _spec(
        409,
        FIX_REQUEST,
        "Generate a new idempotency key; confirm this is a different request.",
        "use_new_idempotency_key",
    ),
    "last_owner_required": _spec(409, FIX_REQUEST, "Assign another owner first."),
    "locate_required": _spec(409, FIX_REQUEST, "Run locate_personal_data first."),
    "log_document_append_only": _spec(
        409, FIX_REQUEST, "Use append_entries instead.", "append_entries"
    ),
    "tenant_has_children": _spec(409, FIX_REQUEST, "Fix the request per details."),
    "batch_too_large": _spec(413, FIX_REQUEST, "Split the batch per details."),
    "payload_too_large": _spec(413, FIX_REQUEST, "Send a smaller archive (details.limit_bytes)."),
    "content_too_large": _spec(
        413, FIX_REQUEST, "Split into parts (details.action).", "split_into_parts"
    ),
    "budget_exceeds_parent": _spec(422, FIX_REQUEST, "Lower the budget per details."),
    "checksum_mismatch": _spec(422, FIX_REQUEST, "Download the export again, then import."),
    "context_doc_not_found": _spec(422, FIX_REQUEST, "Fix the context pointers per details."),
    "continuation_required": _spec(422, FIX_REQUEST, "Attach a continuation pack."),
    "duplicate_key_in_batch": _spec(422, FIX_REQUEST, "Deduplicate keys per details."),
    "export_format_unsupported": _spec(422, FIX_REQUEST, "Use a supported export format."),
    "invalid_address": _spec(422, FIX_REQUEST, "Fix the address per details."),
    "invalid_filter": _spec(422, FIX_REQUEST, "Fix the filter per details."),
    "namespace_not_registered": _spec(
        422, FIX_REQUEST, "Call register_namespace, then resend.", "register_namespace"
    ),
    "agent_not_registered": _spec(
        409, FIX_REQUEST, "Call register_agent, then retry.", "register_agent"
    ),
    "notice_template_incomplete": _spec(422, FIX_REQUEST, "Fill details.missing fields."),
    "policy_loosening_not_allowed": _spec(
        422, FIX_REQUEST, "Use a value no looser than the ancestor's."
    ),
    "quota_exceeds_parent": _spec(422, FIX_REQUEST, "Stay within the parent's limit."),
    "rollup_summary_required": _spec(422, FIX_REQUEST, "Attach a rollup summary."),
    "subject_identifier_required": _spec(422, FIX_REQUEST, "Fix the request per details."),
    "tenant_cycle": _spec(422, FIX_REQUEST, "Fix the request per details."),
    "tenant_depth_exceeded": _spec(422, FIX_REQUEST, "Fix the request per details."),
    "tenant_type_invalid_parent": _spec(
        422, FIX_REQUEST, "Choose a parent from details.allowed."
    ),
    "task_tree_too_deep": _spec(422, FIX_REQUEST, "Split under a shallower task."),
    "ttl_exceeds_parent": _spec(422, FIX_REQUEST, "Lower the TTL per details."),
    "unknown_principal": _spec(422, FIX_REQUEST, "Fix the principal reference per details."),
    "namespace_protected": _spec(422, FIX_REQUEST, "default and reserved namespaces stay."),
    "assignee_cannot_reach_namespace": _spec(
        422, FIX_REQUEST, "Use a namespace the assignee reads, or widen its token."),
    "url_not_allowed": _spec(422, FIX_REQUEST, "Use an allowed URL per details."),
    "validation_error": _spec(422, FIX_REQUEST, "Fix the fields listed in details."),
    # --- fix_request (existing 3.2.x codes)
    "invalid_cursor": _spec(400, FIX_REQUEST, "Pass back next_cursor unchanged."),
    "unsupported_artifact_uri": _spec(400, FIX_REQUEST, "Use doc://<doc_key>."),
    "http_error": _spec(400, FIX_REQUEST, "Fix the request method, path or body."),
    "account_exists": _spec(409, FIX_REQUEST, "Sign in instead of registering."),
    "account_link_required": _spec(
        409, FIX_REQUEST, "Sign in with the password, then link Google."
    ),
    "agent_token_exists": _spec(409, FIX_REQUEST, "Use another agent signature."),
    "oauth_request_decided": _spec(409, ABANDON, "Start the connection again from your client."),
    "oauth_request_expired": _spec(410, ABANDON, "Start the connection again from your client."),
    "operator_content_denied": _spec(
        403, ESCALATE_HUMAN, "Ask the workspace owner to open support access in the console."),
    "operator_write_denied": _spec(
        403, ESCALATE_HUMAN, "Ask the owner to reopen support access with changes allowed."),
    "cli_session_decided": _spec(409, ABANDON, "Run `npx veynod init` again for a new code."),
    "cli_session_expired": _spec(410, ABANDON, "Run `npx veynod init` again for a new code."),
    "cli_session_consumed": _spec(410, ABANDON, "The key was collected once; start a new setup."),
    "cli_code_unavailable": _spec(503, RETRY_BACKOFF, "Back off and retry."),
    "invalid_device_code": _spec(404, ABANDON, "Start a new setup with `npx veynod init`."),
    "merge_without_deliverable": _spec(
        409, FIX_REQUEST, "Pass artifact (a commit or doc:// link), or merge an accepted "
        "submission."),
    "not_chunk_lineage": _spec(
        422, FIX_REQUEST, "Name a task of this chunk (DG-<diagram>-<chunk>[-n]); details "
        "list them."),
    "task_not_done": _spec(409, FIX_REQUEST, "Re-point a merged chunk to a DONE task."),
    "cli_identity_unknown": _spec(
        422, FIX_REQUEST, "Tick only identities the terminal asked for (index 1 or 2)."),
    "invalid_public_key": _spec(422, FIX_REQUEST, "Send a base64 X25519 public key (32 bytes)."),
    "ambiguous_artifact": _spec(
        422, FIX_REQUEST, "Provide artifact_text or artifact_uri, not both."
    ),
    "artifact_archived": _spec(409, FIX_REQUEST, "Restore the document or use another."),
    "claim_required": _spec(409, FIX_REQUEST, "Claim the task before this transition."),
    "dependency_cycle": _spec(409, FIX_REQUEST, "Remove the cyclic depends_on link."),
    "google_email_mismatch": _spec(409, FIX_REQUEST, "Use the Google account for this email."),
    "google_email_unverified": _spec(401, FIX_REQUEST, "Verify the Google email first."),
    "google_identity_conflict": _spec(409, FIX_REQUEST, "Unlink the other identity first."),
    "human_user_exists": _spec(409, FIX_REQUEST, "Use another email."),
    "insecure_target_url": _spec(422, FIX_REQUEST, "Use an https URL."),
    "invalid_dependency": _spec(422, FIX_REQUEST, "Fix the dependency per details."),
    "invalid_filter_operator": _spec(422, FIX_REQUEST, "Use a supported filter operator."),
    "invalid_filter_value": _spec(422, FIX_REQUEST, "Fix the filter value."),
    "invalid_handoff_brief": _spec(422, FIX_REQUEST, "Fix the L1 brief per details."),
    "invalid_json_patch": _spec(422, FIX_REQUEST, "Fix the JSON Patch operations."),
    "invalid_last_event_id": _spec(422, FIX_REQUEST, "Send a non-negative integer."),
    "invalid_section": _spec(422, FIX_REQUEST, "Use an existing section heading."),
    "invalid_sort": _spec(422, FIX_REQUEST, "Use a supported sort."),
    "invalid_stream_filter": _spec(422, FIX_REQUEST, "Use a supported table filter."),
    "invalid_target_url": _spec(422, FIX_REQUEST, "Fix the target URL."),
    "invalid_payload": _spec(400, FIX_REQUEST, "Send a JSON object body."),
    "invalid_task_status": _spec(422, FIX_REQUEST, "Use a valid task status."),
    "invalid_verification_status": _spec(422, FIX_REQUEST, "Use a valid status."),
    "limit_too_large": _spec(422, FIX_REQUEST, "Lower limit to the documented maximum."),
    "offset_not_supported": _spec(422, FIX_REQUEST, "Use cursor pagination."),
    "outbound_host_forbidden": _spec(403, FIX_REQUEST, "Use an allowed outbound host."),
    "outbound_private_address": _spec(403, FIX_REQUEST, "Use a public target address."),
    "policy_exists": _spec(409, FIX_REQUEST, "Update the existing policy instead."),
    "policy_schema_violation": _spec(422, FIX_REQUEST, "Conform to the path JSON Schema."),
    "policy_size_limit": _spec(422, FIX_REQUEST, "Shrink the content below the policy limit."),
    "proposal_required": _spec(
        409, FIX_REQUEST, "Submit propose_document_change instead.", "propose_document_change"
    ),
    "task_exists": _spec(409, FIX_REQUEST, "Use another task_key."),
    "too_many_headers": _spec(422, FIX_REQUEST, "Send at most 64 headers."),
    "tool_error": _spec(400, FIX_REQUEST, "Fix the tool arguments."),
    "unknown_tool": _spec(404, FIX_REQUEST, "Call tools/list and use a listed tool."),
    "workspace_confirmation_mismatch": _spec(409, FIX_REQUEST, "Confirm with the exact name."),
    # --- reauth
    "token_expired": _spec(401, REAUTH, "Renew with the parent token."),
    "token_invalid": _spec(401, REAUTH, "Stop; ask the issuer for a new token."),
    "token_revoked": _spec(401, REAUTH, "Stop all actions; do not retry."),
    "renewal_requires_parent": _spec(403, REAUTH, "Ask the parent token holder to renew."),
    "unauthorized": _spec(401, REAUTH, "Send a valid bearer key."),
    "invalid_signature": _spec(
        401, FIX_REQUEST, "Sign the webhook body with the shared secret (X-Hub-Signature-256)."
    ),
    "invalid_api_key": _spec(401, REAUTH, "Configure a valid API key."),
    "invalid_credentials": _spec(401, REAUTH, "Check the email and password."),
    "human_session_required": _spec(401, REAUTH, "Sign in with a human account."),
    "google_audience_mismatch": _spec(401, REAUTH, "Sign in with this deployment's client."),
    "google_identity_mismatch": _spec(401, REAUTH, "Sign in with the linked Google identity."),
    "google_token_invalid": _spec(401, REAUTH, "Obtain a fresh Google credential."),
    # --- abandon
    "blob_purged": _spec(410, ABANDON, "The attachment is gone; remove the reference."),
    "blob_not_deleted": _spec(409, ABANDON, "The blob is not deleted; nothing to restore."),
    "blob_not_recoverable": _spec(
        409, ABANDON, "The bytes are gone; upload the file again."
    ),
    "upload_expired": _spec(410, ABANDON, "Start a new upload."),
    "depth_limit_exceeded": _spec(403, ABANDON, "Give up this action."),
    "forbidden": _spec(403, ABANDON, "Give up this action."),
    "feature_disabled": _spec(
        403, ESCALATE_HUMAN, "Switched off here; ask an operator (details.setting)."),
    "issue_not_permitted": _spec(403, ABANDON, "Give up this action."),
    "human_endpoint": _spec(403, ABANDON, "Use the agent endpoint named in details."),
    "namespace_register_not_permitted": _spec(
        403, ABANDON, "Ask the token's issuer to register the namespace."),
    "not_assignee": _spec(403, ABANDON, "Give up this action."),
    "not_lease_holder": _spec(403, ABANDON, "Give up; do not preempt."),
    "review_sealed": _spec(403, ABANDON, "Give up this action."),
    "role_mismatch": _spec(403, ABANDON, "Give up this action."),
    "role_not_assignable": _spec(403, ABANDON, "Give up this action."),
    "role_permission_denied": _spec(
        403, ESCALATE_HUMAN, "Ask the human OWNER; this capability is not delegated."),
    "commander_grant_required": _spec(
        403, ESCALATE_HUMAN, "Ask the owner for a grant, then send X-Commander-Grant-Id."),
    "commander_grant_invalid": _spec(
        403, ABANDON, "The grant is unknown, revoked or not yours."),
    "commander_grant_expired": _spec(
        403, ESCALATE_HUMAN, "Ask the owner to issue a fresh grant."),
    "commander_grant_out_of_scope": _spec(
        403, ABANDON, "Stay within the grant's action, namespace and task scope."),
    "root_token_data_access_denied": _spec(403, ABANDON, "Give up this action."),
    "scope_escalation": _spec(403, ABANDON, "Give up this action."),
    "NAMESPACE_FORBIDDEN": _spec(403, ABANDON, "Stay within the token's namespaces."),
    "self_vote_forbidden": _spec(403, ABANDON, "Agents cannot vote on their own proposals."),
    "signup_disabled": _spec(403, ABANDON, "Ask an owner for an invitation."),
    "lease_not_found": _spec(404, ABANDON, "Give up this action."),
    "not_found": _spec(404, ABANDON, "Give up; cross-tenant access also returns 404."),
    "already_registered": _spec(409, ABANDON, "Give up this action."),
    "document_archived": _spec(409, ABANDON, "Give up, or ask for restore_document."),
    "not_a_log_document": _spec(409, FIX_REQUEST, "Write with upsert_shared_doc instead."),
    "id_collision": _spec(409, ABANDON, "Abort the import or import into a new tenant."),
    "increment_out_of_order": _spec(
        409, FIX_REQUEST, "Import details.prev_export_id first, then this increment."
    ),
    "invalid_transition": _spec(409, ABANDON, "Give up this action."),
    "invalid_state_transition": _spec(409, ABANDON, "Re-read the task; give up this action."),
    "lease_lost": _spec(409, ABANDON, "Drop local state and claim again."),
    "run_not_claimed": _spec(
        409, ABANDON, "Only the runner holding a running deploy-gate run reports it; claim again."
    ),
    "lease_expired": _spec(
        409, ABANDON, "Your lease ended before NOW(); claim the task again."),
    "lease_renew_too_early": _spec(
        409, FIX_REQUEST, "Renew only in the last 300 seconds of the lease."),
    "lease_renew_limit": _spec(
        409, FIX_REQUEST, "This claim reached 12 renewals; claim the task again."),
    "queue_ticket_expired": _spec(409, ABANDON, "Call acquire_resource again to re-queue."),
    "lineage_unavailable": _spec(409, ABANDON, "This subject has no signed lineage; give up."),
    "nothing_to_probe": _spec(409, ABANDON, "Write and index documents first."),
    "lease_conflict": _spec(409, ABANDON, "Another agent holds the lease; do not preempt."),
    "request_closed": _spec(409, ABANDON, "Give up this action."),
    "retention_not_met": _spec(409, ABANDON, "Give up this action."),
    "stale_fencing_token": _spec(409, ABANDON, "Drop local state and claim again."),
    "token_already_expired": _spec(409, ABANDON, "Ask the issuer to issue again."),
    "link_exists": _spec(409, ABANDON, "The link already exists."),
    "proposal_not_pending": _spec(409, ABANDON, "The proposal was already decided."),
    "task_not_reviewable": _spec(409, ABANDON, "The task is not awaiting review."),
    "external_action_error": _spec(409, ABANDON, "Re-read the action state."),
    "gone": _spec(410, ABANDON, "Give up; details carry the tombstone."),
    "reviewer_conflict": _spec(422, ABANDON, "Give up this action."),
    "review_feedback_required": _spec(422, FIX_REQUEST, "Say why in feedback, then resend."),
    "scope_unknown": _spec(422, FIX_REQUEST, "Use permissions listed in details.allowed."),
    "review_note_required": _spec(422, FIX_REQUEST, "Say why in note, then resend."),
    # Quality control 1.0.
    "acceptance_self_report": _spec(403, ABANDON,
                                    "The executor cannot report its own acceptance; "
                                    "another agent or a person must."),
    "acceptance_locked": _spec(403, ABANDON,
                               "Acceptance is locked after the first claim; ask the task's "
                               "creator or a person."),
    "audit_view_required": _spec(422, FIX_REQUEST, "Call get_audit_view, then resend."),
    "acceptance_items_incomplete": _spec(422, FIX_REQUEST,
                                         "Give a PASS result for every acceptance item."),
    "quality_gates_unmet": _spec(409, FIX_REQUEST,
                                 "Report the gates in details.missing as PASS, then retry."),
    "auto_release_cap_reached": _spec(409, FIX_REQUEST,
                                      "Hand off to REVIEWING; an audit releases it today."),
    "quality_circuit_open": _spec(409, FIX_REQUEST,
                                  "The domain's circuit is open: hand off to REVIEWING."),
    "claim_already_refuted": _spec(409, ABANDON, "The claim was refuted; report a new commit."),
    "escape_closed": _spec(409, ABANDON, "The escape is already FIXED or WAIVED."),
    "sample_unavailable": _spec(409, ABANDON,
                                "Someone else took the sample or it was already judged."),
    "circuit_not_open": _spec(409, ABANDON, "The domain's circuit is already closed."),
    "share_cross_root_not_allowed": _spec(
        422, ABANDON, "Use cross-tree collaboration instead."
    ),
    # Diagram dispatch (3.14).
    "diagram_exists": _spec(409, FIX_REQUEST, "Use another diagram key or edit the existing one."),
    "chunk_incomplete": _spec(
        422, FIX_REQUEST,
        "Give every chunk in details.chunks an icd and done_criteria and known dependencies."),
    "contract_first": _spec(
        422, FIX_REQUEST,
        "Make the chunks in details.implementation_roots depend on an interface chunk."),
    "still_draft": _spec(409, FIX_REQUEST, "Edit the draft directly (details.edit_with)."),
    "unknown_chunk": _spec(422, FIX_REQUEST, "Use a chunk_key the diagram has."),
    "chunk_exists": _spec(409, FIX_REQUEST, "Use another chunk_key, or update that chunk."),
    "chunk_in_flight": _spec(
        409, FIX_REQUEST, "Add a new chunk that depends on them (details.add_chunk_with)."),
    "empty_impact": _spec(422, FIX_REQUEST, "File a change that touches at least one chunk."),
    "review_out_of_scope": _spec(
        422, FIX_REQUEST, "Answer exactly the rules in details.rules, then resend."),
    "not_confirmed": _spec(
        409, ESCALATE_HUMAN, "A person confirms the diagram before it is frozen."),
    "not_frozen": _spec(
        409, ESCALATE_HUMAN, "A person confirms and freezes the diagram before dispatch."),
    "already_dispatched": _spec(409, ABANDON, "The chunk has a task already (details.task_key)."),
    "submission_rejected": _spec(
        409, ABANDON, "The latest submission broke its write scope; wait for a new one."),
    "ci_not_green": _spec(
        409, ABANDON, "Review after a green sandbox run on the current submission."),
    "no_submission": _spec(409, ABANDON, "Run the sandbox after a submission is accepted."),
    "stale_ci": _spec(
        409, ABANDON, "Drop this result; report a run of details.submission instead."),
    "circuit_closed": _spec(409, ABANDON, "Upgrade only a chunk whose circuit broke."),
    "not_queue_head": _spec(
        409, ABANDON, "Only the queue head is checked; wait for this chunk's turn."),
    "queue_not_verified": _spec(
        409, ABANDON, "Queued work merges from the queue after the controller re-tests it."),
    "gate_not_passed": _spec(
        409, ABANDON, "Merge after a passing gate on the current submission."),
    "already_merged": _spec(409, ABANDON, "The chunk is merged already."),
    "merge_by_implementer": _spec(
        403, ABANDON, "Another agent or a person merges the chunk; not its implementer."),
    "already_decided": _spec(409, ABANDON, "The change was decided already."),
    "stale_change": _spec(
        409, ABANDON, "The diagram moved on; file the change again on details.current_version."),
    # Automation (Loop v1).
    "decide_with_safe_default": _spec(
        409, ABANDON,
        "Automation is on: choose the safest default, note it on the task and continue; "
        "ask the owner only about details.owner_topics.",
        action="decide_with_safe_default_and_continue",
    ),
    # --- escalate_human
    "agent_document_quota_exceeded": _spec(
        429, ESCALATE_HUMAN, "Daily quota reached; ask the owner for a larger quota."
    ),
    "tenant_deleting": _spec(
        403, ESCALATE_HUMAN, "Stop; only an owner can cancel deletion or fetch the export."
    ),
    "tenant_suspended": _spec(
        403, ESCALATE_HUMAN, "Stop writing and notify a human; reads and export still work."
    ),
    "confirmation_required": _spec(
        409, ESCALATE_HUMAN, "Wait for details.pending_confirmations."
    ),
    "incident_not_confirmed": _spec(409, ESCALATE_HUMAN, "Confirm the incident first."),
    "backup_chain_broken": _spec(
        409, ESCALATE_HUMAN, "Restore from an older full backup or take a new one."
    ),
    "backups_not_configured": _spec(
        409, ESCALATE_HUMAN, "An operator must configure backup storage."
    ),
    "staging_not_configured": _spec(
        409, ESCALATE_HUMAN, "An operator must configure a separate staging database."
    ),
    "tree_quota_exceeded": _spec(
        409, ESCALATE_HUMAN, "Revoke unused child tokens or ask a human for more quota."
    ),
    "budget_exhausted": _spec(
        429, ESCALATE_HUMAN, "Wait for resets_at or ask the issuer for more budget."
    ),
    "circuit_open": _spec(429, ESCALATE_HUMAN, "Stop the loop; call agent_escalate_to_human."),
    "HUMAN_SOVEREIGNTY_LOCKED": _spec(
        403, ESCALATE_HUMAN, "A human locked this resource; ask a human to change it."
    ),
    "escalated_to_human": _spec(409, ESCALATE_HUMAN, "Wait for the human decision."),
    "taint_blocked": _spec(409, ESCALATE_HUMAN, "A human must verify tainted content."),
    "quota_exceeded": _spec(402, ESCALATE_HUMAN, "Ask a human to raise the quota."),
    "budget_exceeded": _spec(402, ESCALATE_HUMAN, "Ask a human to raise the budget."),
    "admin_disabled": _spec(503, ESCALATE_HUMAN, "ADMIN_TOKEN is not configured."),
    "mcp_not_configured": _spec(500, ESCALATE_HUMAN, "An operator must set MCP_API_KEY."),
    "google_login_not_configured": _spec(
        503, ESCALATE_HUMAN, "An operator must configure Google sign-in."
    ),
}


# Soft-constraint warnings (PRD §6 warnings[]): never errors, never rejections.
WARNING_CODES: dict[str, str] = {
    "duplicate_content": "Another document in the namespace already holds exactly this text.",
    "owner_report_missing": "Full automation: the handoff brief should carry owner_report "
                            "{what_changed, how_to_try, not_yet_done} in plain words.",
    "listing_hides_documents": "The legacy listing hides drafts and tainted documents; the "
                               "warning says how many and which flags show them.",
    "uri_namespace_prefix": "The address carried a namespace prefix; use doc://<doc_key>.",
    "document_namespace_inferred": "The new document's namespace came from its key prefix or "
                                   "the token's only writable namespace.",
    "task_namespace_inferred": "The task namespace was taken from its parent, context, "
                               "assignee or creator; pass namespace to choose.",
    "task_namespace_defaulted": "No namespace was given or inferable; the task went to "
                                "'default'.",
    "assigned_tasks_out_of_reach": "Open tasks assigned to you are in namespaces your token "
                                   "cannot read; ask an owner to move them.",
    "promotion_below_threshold": "The domain was promoted below the promotion threshold.",
    "gate_source_downgraded": "Only a trusted runner reports RUNNER results; recorded as "
                              "AGENT_CLAIM.",
    "secrets_not_imported": "Imported tokens must be rotated and users must reset passwords.",
    "keys_rotated": "A restored workspace has new keys; rotate them and re-issue agent tokens.",
    "alias_redirected": "Read through a renamed alias; use the current alias or id.",
    "legacy_alias": "Address predates the grammar; accepted, but 2026-11 rejects new ones.",
    "dangling_ref": "A $ref or part does not resolve; stored as written.",
    "namespace_auto_registered": "Namespace registered implicitly; 2026-11 requires it first.",
    "namespace_unchanged": "The new key's prefix is not a registered namespace; the "
    "document stayed where it was. Register the namespace and rename again to move it.",
    "namespace_undocumented": "The namespace readme is still a generated stub.",
    "frontmatter_missing": "Fields the namespace readme requires are missing.",
    "doc_key_pattern_mismatch": "doc_key does not match the namespace pattern.",
    "budget_warning": "A token's daily budget is at 80%; budget.warning went to its issuer.",
    "recall_still_depends": "This version still derives from recalled versions; the recall "
                            "node stays open.",
    "ttl_capped_by_policy": "The token's lifetime was capped by an organisation policy.",
    "continuation_missing": "No continuation pack was written since the claim; call "
                            "write_continuation before the next handoff.",
    "output_missing": "Nothing was written to the task's output_to since the claim.",
    "target_offline": "A mentioned agent is offline or retired; its owner is notified too.",
    "unknown_mention": "A mentioned principal is not in the directory.",
    "not_registered": "The collaborator directory is not live; identity is inferred.",
    "constitution_draft": "The constitution is still the generated template.",
    "token_expiring": "The calling token expires within a day.",
    "quota_80_percent": "The daily embedding quota is at least 80% used.",
    "embedding_deferred_quota": "Stored, but indexing waits for the daily embedding quota reset.",
}


def _fallback(status: int) -> ErrorSpec:
    if status in (429, 503):
        return _spec(status, RETRY_AFTER, "Wait for Retry-After.")
    if status >= 500:
        return _spec(status, RETRY_BACKOFF, "Back off and retry.")
    if status == 401:
        return _spec(status, REAUTH, "Re-authenticate.")
    if status in (403, 404):
        return _spec(status, ABANDON, "Give up this action.")
    return _spec(status, FIX_REQUEST, "Fix the request.")


def spec_for(code: str, status: int) -> ErrorSpec:
    return REGISTRY.get(code) or _fallback(status)


def retry_after_ms(headers: dict[str, str] | None) -> int | None:
    if not headers:
        return None
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return max(int(float(value) * 1000), 0)
            except (TypeError, ValueError):
                return None
    return None


def classify(
    code: str,
    status: int,
    *,
    headers: dict[str, str] | None = None,
    details: Any | None = None,
) -> dict[str, Any]:
    """Return the protocol fields to merge into an error object."""
    spec = spec_for(code, status)
    if isinstance(details, dict) and details.get("retry_with"):
        # The request only lacked the flags the error names (a document the trust
        # filter hides answers not_found): resend with them, do not give up (R5).
        spec = _spec(status, FIX_REQUEST, "Resend with details.retry_with.",
                     "resend_with_retry_with")
    delay = retry_after_ms(headers)
    if delay is None and isinstance(details, dict):
        raw = details.get("retry_after_ms")
        delay = int(raw) if isinstance(raw, (int, float)) else None
    return {
        "class": spec.error_class,
        "action": spec.machine_action,
        "retryable": spec.retryable,
        "retry_after_ms": delay,
    }


def registry_table() -> list[dict[str, Any]]:
    return [
        {
            "code": code,
            "http": spec.status,
            "class": spec.error_class,
            "action": spec.machine_action,
            "retryable": spec.retryable,
            "rule": spec.rule,
        }
        for code, spec in sorted(
            REGISTRY.items(), key=lambda item: (list(ERROR_CLASSES).index(item[1].error_class),
                                                item[1].status, item[0])
        )
    ]


def protocol_docs(section: str) -> dict[str, Any]:
    """Agent-readable protocol reference shared by REST and MCP docs."""
    from app import versioning

    versions = {
        "header": versioning.VERSION_HEADER,
        "default": versioning.BASELINE_VERSION,
        "latest": versioning.LATEST_VERSION,
        "supported": list(versioning.SUPPORTED_VERSIONS),
        "rule": "Undeclared requests run on the baseline. Within a version only "
        "additive changes ship; ignore unknown fields.",
    }
    idempotency = {
        "rest": "Send header Idempotency-Key: <key> (<=128 chars) on POST/PUT/PATCH/DELETE.",
        "mcp": "Pass idempotency_key to write tools.",
        "window_hours": 24,
        "replay": "Same key + same body replays the first response with replayed=true.",
        "conflicts": ["idempotency_key_reused", "idempotency_in_progress"],
    }
    if section == "retry":
        return {"retry": RETRY_POLICY, "classes": ERROR_CLASSES, "idempotency": idempotency}
    if section == "errors":
        return {"classes": ERROR_CLASSES, "codes": registry_table()}
    if section == "versions":
        return {"versions": versions}
    return {
        "error_shape": {
            "error": {
                "code": "snake_case",
                "message": "string",
                "class": list(ERROR_CLASSES),
                "action": "machine action",
                "retryable": "bool",
                "retry_after_ms": "int|null",
                "details": "optional",
            }
        },
        "classes": {name: meta["rule"] for name, meta in ERROR_CLASSES.items()},
        "retry": RETRY_POLICY,
        "idempotency": idempotency,
        "versions": versions,
    }

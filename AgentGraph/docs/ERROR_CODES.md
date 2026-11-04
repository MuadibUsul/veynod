# Veynod error codes

Generated from `app/error_registry.py` by `scripts/gen_error_codes.py`;
do not edit by hand. Every error body is
`{"error": {"code", "message", "class", "action", "retryable",
"retry_after_ms", "details"}}`.

## Classes

| class | action | retryable | rule |
|---|---|---|---|
| `retry_after` | `wait_retry_after_then_retry_same_request` | true | Wait for Retry-After, then resend unchanged with the same idempotency key. |
| `retry_backoff` | `backoff_then_retry_same_request` | true | Retry with full-jitter exponential backoff, at most 5 times, with the same idempotency key. |
| `refresh_and_retry` | `refresh_state_then_retry_once` | true | Merge or resync using the current state in details (current_version etc.), then retry once on that new state. |
| `fix_request` | `fix_request_then_resend` | false | Never resend unchanged; correct the request using details first. |
| `reauth` | `stop_and_reauthenticate` | false | Stop; ask the parent token holder or a human for a valid credential. |
| `abandon` | `abandon_and_release_local_state` | false | Give up this action, release local state (e.g. leases) and re-read global state if needed. |
| `escalate_human` | `stop_and_escalate_to_human` | false | Stop automatic retries; call agent_escalate_to_human or wait for a human. |

## Retry algorithm

`sleep_ms = max(retry_after_ms, random(0, min(cap_ms, base_ms * 2**attempt)))` with base 500 ms, cap 30000 ms, at most 5 attempts. Resend with the same idempotency_key on every retry. Events are delivered at least once; deduplicate by event_id.

## Codes

| code | HTTP | class | action | rule |
|---|---|---|---|---|
| `backup_in_progress` | 409 | `retry_after` | `wait_retry_after_then_retry_same_request` | Another backup is running; retry later. |
| `contention` | 409 | `retry_after` | `wait_retry_after_then_retry_same_request` | Wait for Retry-After, then retry. |
| `export_in_progress` | 409 | `retry_after` | `wait_retry_after_then_retry_same_request` | Poll details.export_id instead of starting another export. |
| `idempotency_in_progress` | 409 | `retry_after` | `wait_retry_after_then_retry_same_request` | Wait for Retry-After and query the result with the same key. |
| `embedding_budget_exceeded` | 429 | `retry_after` | `wait_retry_after_then_retry_same_request` | Embedding byte budget for this minute is spent; wait. |
| `rate_limited` | 429 | `retry_after` | `wait_retry_after_then_retry_same_request` | Wait for Retry-After. |
| `sse_capacity` | 429 | `retry_after` | `wait_retry_after_then_retry_same_request` | Too many event streams; reconnect later. |
| `overloaded` | 503 | `retry_after` | `wait_retry_after_then_retry_same_request` | Wait for Retry-After; never resend earlier. |
| `kernel_not_committed` | 409 | `retry_backoff` | `backoff_then_retry_same_request` | The Kernel has not committed this chunk yet; wait for the commit projection. |
| `internal_error` | 500 | `retry_backoff` | `backoff_then_retry_same_request` | Back off and retry at most 5 times, then escalate_human. |
| `embedding_failed` | 502 | `retry_backoff` | `backoff_then_retry_same_request` | Embedding provider failed; back off. |
| `kernel_cancel_failed` | 502 | `retry_backoff` | `backoff_then_retry_same_request` | Kernel unreachable; back off and retry, then escalate. |
| `cli_code_unavailable` | 503 | `retry_backoff` | `backoff_then_retry_same_request` | Back off and retry. |
| `database_unavailable` | 503 | `retry_backoff` | `backoff_then_retry_same_request` | Back off and retry. |
| `google_unavailable` | 503 | `retry_backoff` | `backoff_then_retry_same_request` | Back off and retry sign-in. |
| `tenant_context_unavailable` | 503 | `retry_backoff` | `backoff_then_retry_same_request` | Back off and retry. |
| `federation_conflict` | 409 | `refresh_and_retry` | `refresh_state_then_retry_once` | Re-read the connection; its state changed or the object is stale. |
| `proposal_conflict` | 409 | `refresh_and_retry` | `refresh_state_then_retry_once` | Re-read the document and rebase the proposal. |
| `rebase_conflict` | 409 | `refresh_and_retry` | `refresh_state_then_retry_once` | Re-read from base_version and redo non-overlapping edits. |
| `section_conflict` | 409 | `refresh_and_retry` | `refresh_state_then_retry_once` | Re-read the section, then rewrite. |
| `undo_conflict` | 409 | `refresh_and_retry` | `refresh_state_then_retry_once` | Review details.conflicts; retry with force=true if intended. |
| `version_conflict` | 409 | `refresh_and_retry` | `refresh_state_then_retry_once` | Merge against details.current_version, then rewrite. |
| `cursor_expired` | 410 | `refresh_and_retry` | `refresh_state_then_retry_once` | Drop the cursor and resync with workspace_sync. |
| `cursor_invalid` | 400 | `fix_request` | `fix_request_then_resend` | Fix the cursor per details. |
| `http_error` | 400 | `fix_request` | `fix_request_then_resend` | Fix the request method, path or body. |
| `invalid_cursor` | 400 | `fix_request` | `fix_request_then_resend` | Pass back next_cursor unchanged. |
| `invalid_payload` | 400 | `fix_request` | `fix_request_then_resend` | Send a JSON object body. |
| `tenant_id_not_allowed` | 400 | `fix_request` | `remove_tenant_id` | Remove tenant_id from the request. |
| `tool_error` | 400 | `fix_request` | `fix_request_then_resend` | Fix the tool arguments. |
| `unsupported_api_version` | 400 | `fix_request` | `use_supported_api_version` | Use a version from details.supported. |
| `unsupported_artifact_uri` | 400 | `fix_request` | `fix_request_then_resend` | Use doc://<doc_key>. |
| `google_email_unverified` | 401 | `fix_request` | `fix_request_then_resend` | Verify the Google email first. |
| `invalid_signature` | 401 | `fix_request` | `fix_request_then_resend` | Sign the webhook body with the shared secret (X-Hub-Signature-256). |
| `field_not_writable` | 403 | `fix_request` | `fix_request_then_resend` | Remove the field from the request. |
| `outbound_host_forbidden` | 403 | `fix_request` | `fix_request_then_resend` | Use an allowed outbound host. |
| `outbound_private_address` | 403 | `fix_request` | `fix_request_then_resend` | Use a public target address. |
| `system_namespace_protected` | 403 | `fix_request` | `propose_document_change` | Use propose_document_change instead. |
| `unknown_tool` | 404 | `fix_request` | `fix_request_then_resend` | Call tools/list and use a listed tool. |
| `account_exists` | 409 | `fix_request` | `fix_request_then_resend` | Sign in instead of registering. |
| `account_link_required` | 409 | `fix_request` | `fix_request_then_resend` | Sign in with the password, then link Google. |
| `admin_access_required` | 409 | `fix_request` | `fix_request_then_resend` | Call open_admin_access first (the node is notified). |
| `agent_id_taken` | 409 | `fix_request` | `fix_request_then_resend` | Choose another agent_id. |
| `agent_not_registered` | 409 | `fix_request` | `register_agent` | Call register_agent, then retry. |
| `agent_token_exists` | 409 | `fix_request` | `fix_request_then_resend` | Use another agent signature. |
| `alias_taken` | 409 | `fix_request` | `fix_request_then_resend` | Choose another alias. |
| `alias_tombstoned` | 409 | `fix_request` | `fix_request_then_resend` | Choose another alias; tombstones never reuse. |
| `artifact_archived` | 409 | `fix_request` | `fix_request_then_resend` | Restore the document or use another. |
| `auto_release_cap_reached` | 409 | `fix_request` | `fix_request_then_resend` | Hand off to REVIEWING; an audit releases it today. |
| `blob_referenced` | 409 | `fix_request` | `fix_request_then_resend` | Detach the blob first, or force-delete as its owner. |
| `chunk_exists` | 409 | `fix_request` | `fix_request_then_resend` | Use another chunk_key, or update that chunk. |
| `chunk_in_flight` | 409 | `fix_request` | `fix_request_then_resend` | Add a new chunk that depends on them (details.add_chunk_with). |
| `claim_required` | 409 | `fix_request` | `fix_request_then_resend` | Claim the task before this transition. |
| `dependency_cycle` | 409 | `fix_request` | `fix_request_then_resend` | Remove the cyclic depends_on link. |
| `diagram_exists` | 409 | `fix_request` | `fix_request_then_resend` | Use another diagram key or edit the existing one. |
| `google_email_mismatch` | 409 | `fix_request` | `fix_request_then_resend` | Use the Google account for this email. |
| `google_identity_conflict` | 409 | `fix_request` | `fix_request_then_resend` | Unlink the other identity first. |
| `host_does_not_fit` | 409 | `fix_request` | `fix_request_then_resend` | Pick another computer, or omit host_id to let the scheduler choose. |
| `human_user_exists` | 409 | `fix_request` | `fix_request_then_resend` | Use another email. |
| `idempotency_key_reused` | 409 | `fix_request` | `use_new_idempotency_key` | Generate a new idempotency key; confirm this is a different request. |
| `increment_out_of_order` | 409 | `fix_request` | `fix_request_then_resend` | Import details.prev_export_id first, then this increment. |
| `last_owner_required` | 409 | `fix_request` | `fix_request_then_resend` | Assign another owner first. |
| `lease_renew_limit` | 409 | `fix_request` | `fix_request_then_resend` | This claim reached 12 renewals; claim the task again. |
| `lease_renew_too_early` | 409 | `fix_request` | `fix_request_then_resend` | Renew only in the last 300 seconds of the lease. |
| `locate_required` | 409 | `fix_request` | `fix_request_then_resend` | Run locate_personal_data first. |
| `log_document_append_only` | 409 | `fix_request` | `append_entries` | Use append_entries instead. |
| `merge_without_deliverable` | 409 | `fix_request` | `fix_request_then_resend` | Pass artifact (a commit or doc:// link), or merge an accepted submission. |
| `no_active_generation` | 409 | `fix_request` | `fix_request_then_resend` | Pick an identity that has a token. |
| `not_a_deploy` | 409 | `fix_request` | `fix_request_then_resend` | Report a deploy only on a deploy placement. |
| `not_a_log_document` | 409 | `fix_request` | `fix_request_then_resend` | Write with upsert_shared_doc instead. |
| `parts_incomplete` | 409 | `fix_request` | `fix_request_then_resend` | Upload the missing parts first. |
| `policy_exists` | 409 | `fix_request` | `fix_request_then_resend` | Update the existing policy instead. |
| `proposal_required` | 409 | `fix_request` | `propose_document_change` | Submit propose_document_change instead. |
| `quality_circuit_open` | 409 | `fix_request` | `fix_request_then_resend` | The domain's circuit is open: hand off to REVIEWING. |
| `quality_gates_unmet` | 409 | `fix_request` | `fix_request_then_resend` | Report the gates in details.missing as PASS, then retry. |
| `runner_hello_required` | 409 | `fix_request` | `fix_request_then_resend` | Call POST /v1/runner/hello with this host, then retry. |
| `still_draft` | 409 | `fix_request` | `fix_request_then_resend` | Edit the draft directly (details.edit_with). |
| `task_exists` | 409 | `fix_request` | `fix_request_then_resend` | Use another task_key. |
| `task_not_done` | 409 | `fix_request` | `fix_request_then_resend` | Re-point a merged chunk to a DONE task. |
| `task_owned_by_run` | 409 | `fix_request` | `fix_request_then_resend` | A Runtime run holds this task; use POST /v1/tasks/{key}/run or stop the run first. |
| `tenant_has_children` | 409 | `fix_request` | `fix_request_then_resend` | Fix the request per details. |
| `terminal_state_immutable` | 409 | `fix_request` | `fix_request_then_resend` | The task is DONE or FAILED; create a new task instead. |
| `upload_incomplete` | 409 | `fix_request` | `fix_request_then_resend` | Upload the remaining parts first. |
| `workspace_confirmation_mismatch` | 409 | `fix_request` | `fix_request_then_resend` | Confirm with the exact name. |
| `batch_too_large` | 413 | `fix_request` | `fix_request_then_resend` | Split the batch per details. |
| `blob_too_large` | 413 | `fix_request` | `fix_request_then_resend` | Send a smaller blob; details.limit_bytes is the cap. |
| `content_too_large` | 413 | `fix_request` | `split_into_parts` | Split into parts (details.action). |
| `payload_too_large` | 413 | `fix_request` | `fix_request_then_resend` | Send a smaller archive (details.limit_bytes). |
| `acceptance_items_incomplete` | 422 | `fix_request` | `fix_request_then_resend` | Give a PASS result for every acceptance item. |
| `ambiguous_artifact` | 422 | `fix_request` | `fix_request_then_resend` | Provide artifact_text or artifact_uri, not both. |
| `assignee_cannot_reach_namespace` | 422 | `fix_request` | `fix_request_then_resend` | Use a namespace the assignee reads, or widen its token. |
| `audit_view_required` | 422 | `fix_request` | `fix_request_then_resend` | Call get_audit_view, then resend. |
| `budget_exceeds_parent` | 422 | `fix_request` | `fix_request_then_resend` | Lower the budget per details. |
| `checksum_mismatch` | 422 | `fix_request` | `fix_request_then_resend` | Download the export again, then import. |
| `chunk_incomplete` | 422 | `fix_request` | `fix_request_then_resend` | Give every chunk in details.chunks an icd and done_criteria and known dependencies. |
| `cli_identity_unknown` | 422 | `fix_request` | `fix_request_then_resend` | Tick only identities the terminal asked for (index 1 or 2). |
| `context_doc_not_found` | 422 | `fix_request` | `fix_request_then_resend` | Fix the context pointers per details. |
| `continuation_required` | 422 | `fix_request` | `fix_request_then_resend` | Attach a continuation pack. |
| `contract_first` | 422 | `fix_request` | `fix_request_then_resend` | Make the chunks in details.implementation_roots depend on an interface chunk. |
| `duplicate_key_in_batch` | 422 | `fix_request` | `fix_request_then_resend` | Deduplicate keys per details. |
| `empty_impact` | 422 | `fix_request` | `fix_request_then_resend` | File a change that touches at least one chunk. |
| `expected_version_required` | 422 | `fix_request` | `fix_request_then_resend` | Read get_agent and pass expected_version before renaming. |
| `export_format_unsupported` | 422 | `fix_request` | `fix_request_then_resend` | Use a supported export format. |
| `federation_invalid` | 422 | `fix_request` | `fix_request_then_resend` | Fix the request per details.federation_code. |
| `insecure_target_url` | 422 | `fix_request` | `fix_request_then_resend` | Use an https URL. |
| `invalid_address` | 422 | `fix_request` | `fix_request_then_resend` | Fix the address per details. |
| `invalid_dependency` | 422 | `fix_request` | `fix_request_then_resend` | Fix the dependency per details. |
| `invalid_filter` | 422 | `fix_request` | `fix_request_then_resend` | Fix the filter per details. |
| `invalid_filter_operator` | 422 | `fix_request` | `fix_request_then_resend` | Use a supported filter operator. |
| `invalid_filter_value` | 422 | `fix_request` | `fix_request_then_resend` | Fix the filter value. |
| `invalid_handoff_brief` | 422 | `fix_request` | `fix_request_then_resend` | Fix the L1 brief per details. |
| `invalid_json_patch` | 422 | `fix_request` | `fix_request_then_resend` | Fix the JSON Patch operations. |
| `invalid_last_event_id` | 422 | `fix_request` | `fix_request_then_resend` | Send a non-negative integer. |
| `invalid_part` | 422 | `fix_request` | `fix_request_then_resend` | Give the part document part_index metadata. |
| `invalid_public_key` | 422 | `fix_request` | `fix_request_then_resend` | Send a base64 X25519 public key (32 bytes). |
| `invalid_section` | 422 | `fix_request` | `fix_request_then_resend` | Use an existing section heading. |
| `invalid_sort` | 422 | `fix_request` | `fix_request_then_resend` | Use a supported sort. |
| `invalid_stream_filter` | 422 | `fix_request` | `fix_request_then_resend` | Use a supported table filter. |
| `invalid_target_url` | 422 | `fix_request` | `fix_request_then_resend` | Fix the target URL. |
| `invalid_task_status` | 422 | `fix_request` | `fix_request_then_resend` | Use a valid task status. |
| `invalid_verification_status` | 422 | `fix_request` | `fix_request_then_resend` | Use a valid status. |
| `limit_too_large` | 422 | `fix_request` | `fix_request_then_resend` | Lower limit to the documented maximum. |
| `namespace_not_registered` | 422 | `fix_request` | `register_namespace` | Call register_namespace, then resend. |
| `namespace_protected` | 422 | `fix_request` | `fix_request_then_resend` | default and reserved namespaces stay. |
| `not_chunk_lineage` | 422 | `fix_request` | `fix_request_then_resend` | Name a task of this chunk (DG-<diagram>-<chunk>[-n]); details list them. |
| `notice_template_incomplete` | 422 | `fix_request` | `fix_request_then_resend` | Fill details.missing fields. |
| `offset_not_supported` | 422 | `fix_request` | `fix_request_then_resend` | Use cursor pagination. |
| `policy_loosening_not_allowed` | 422 | `fix_request` | `fix_request_then_resend` | Use a value no looser than the ancestor's. |
| `policy_schema_violation` | 422 | `fix_request` | `fix_request_then_resend` | Conform to the path JSON Schema. |
| `policy_size_limit` | 422 | `fix_request` | `fix_request_then_resend` | Shrink the content below the policy limit. |
| `quota_exceeds_parent` | 422 | `fix_request` | `fix_request_then_resend` | Stay within the parent's limit. |
| `reason_required` | 422 | `fix_request` | `fix_request_then_resend` | Pass a non-empty reason; it goes into the audit trail. |
| `review_feedback_required` | 422 | `fix_request` | `fix_request_then_resend` | Say why in feedback, then resend. |
| `review_note_required` | 422 | `fix_request` | `fix_request_then_resend` | Say why in note, then resend. |
| `review_out_of_scope` | 422 | `fix_request` | `fix_request_then_resend` | Answer exactly the rules in details.rules, then resend. |
| `rollup_summary_required` | 422 | `fix_request` | `fix_request_then_resend` | Attach a rollup summary. |
| `scope_unknown` | 422 | `fix_request` | `fix_request_then_resend` | Use permissions listed in details.allowed. |
| `subject_identifier_required` | 422 | `fix_request` | `fix_request_then_resend` | Fix the request per details. |
| `task_tree_too_deep` | 422 | `fix_request` | `fix_request_then_resend` | Split under a shallower task. |
| `tenant_cycle` | 422 | `fix_request` | `fix_request_then_resend` | Fix the request per details. |
| `tenant_depth_exceeded` | 422 | `fix_request` | `fix_request_then_resend` | Fix the request per details. |
| `tenant_type_invalid_parent` | 422 | `fix_request` | `fix_request_then_resend` | Choose a parent from details.allowed. |
| `too_many_headers` | 422 | `fix_request` | `fix_request_then_resend` | Send at most 64 headers. |
| `ttl_exceeds_parent` | 422 | `fix_request` | `fix_request_then_resend` | Lower the TTL per details. |
| `unknown_chunk` | 422 | `fix_request` | `fix_request_then_resend` | Use a chunk_key the diagram has. |
| `unknown_principal` | 422 | `fix_request` | `fix_request_then_resend` | Fix the principal reference per details. |
| `unsupported_part_encoding` | 422 | `fix_request` | `fix_request_then_resend` | Use plain base64 part bodies. |
| `url_not_allowed` | 422 | `fix_request` | `fix_request_then_resend` | Use an allowed URL per details. |
| `validation_error` | 422 | `fix_request` | `fix_request_then_resend` | Fix the fields listed in details. |
| `google_audience_mismatch` | 401 | `reauth` | `stop_and_reauthenticate` | Sign in with this deployment's client. |
| `google_identity_mismatch` | 401 | `reauth` | `stop_and_reauthenticate` | Sign in with the linked Google identity. |
| `google_token_invalid` | 401 | `reauth` | `stop_and_reauthenticate` | Obtain a fresh Google credential. |
| `human_session_required` | 401 | `reauth` | `stop_and_reauthenticate` | Sign in with a human account. |
| `invalid_api_key` | 401 | `reauth` | `stop_and_reauthenticate` | Configure a valid API key. |
| `invalid_credentials` | 401 | `reauth` | `stop_and_reauthenticate` | Check the email and password. |
| `token_expired` | 401 | `reauth` | `stop_and_reauthenticate` | Renew with the parent token. |
| `token_invalid` | 401 | `reauth` | `stop_and_reauthenticate` | Stop; ask the issuer for a new token. |
| `token_revoked` | 401 | `reauth` | `stop_and_reauthenticate` | Stop all actions; do not retry. |
| `unauthorized` | 401 | `reauth` | `stop_and_reauthenticate` | Send a valid bearer key. |
| `renewal_requires_parent` | 403 | `reauth` | `stop_and_reauthenticate` | Ask the parent token holder to renew. |
| `NAMESPACE_FORBIDDEN` | 403 | `abandon` | `abandon_and_release_local_state` | Stay within the token's namespaces. |
| `acceptance_locked` | 403 | `abandon` | `abandon_and_release_local_state` | Acceptance is locked after the first claim; ask the task's creator or a person. |
| `acceptance_self_report` | 403 | `abandon` | `abandon_and_release_local_state` | The executor cannot report its own acceptance; another agent or a person must. |
| `commander_grant_invalid` | 403 | `abandon` | `abandon_and_release_local_state` | The grant is unknown, revoked or not yours. |
| `commander_grant_out_of_scope` | 403 | `abandon` | `abandon_and_release_local_state` | Stay within the grant's action, namespace and task scope. |
| `depth_limit_exceeded` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `forbidden` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `human_endpoint` | 403 | `abandon` | `abandon_and_release_local_state` | Use the agent endpoint named in details. |
| `issue_not_permitted` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `merge_by_implementer` | 403 | `abandon` | `abandon_and_release_local_state` | Another agent or a person merges the chunk; not its implementer. |
| `namespace_register_not_permitted` | 403 | `abandon` | `abandon_and_release_local_state` | Ask the token's issuer to register the namespace. |
| `not_assignee` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `not_lease_holder` | 403 | `abandon` | `abandon_and_release_local_state` | Give up; do not preempt. |
| `review_sealed` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `role_mismatch` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `role_not_assignable` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `root_token_data_access_denied` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `scope_escalation` | 403 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `self_vote_forbidden` | 403 | `abandon` | `abandon_and_release_local_state` | Agents cannot vote on their own proposals. |
| `signup_disabled` | 403 | `abandon` | `abandon_and_release_local_state` | Ask an owner for an invitation. |
| `federation_not_found` | 404 | `abandon` | `abandon_and_release_local_state` | The identity, handle or connection does not exist. |
| `invalid_device_code` | 404 | `abandon` | `abandon_and_release_local_state` | Start a new setup with `npx veynod init`. |
| `lease_not_found` | 404 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `not_found` | 404 | `abandon` | `abandon_and_release_local_state` | Give up; cross-tenant access also returns 404. |
| `already_decided` | 409 | `abandon` | `abandon_and_release_local_state` | The change was decided already. |
| `already_dispatched` | 409 | `abandon` | `abandon_and_release_local_state` | The chunk has a task already (details.task_key). |
| `already_merged` | 409 | `abandon` | `abandon_and_release_local_state` | The chunk is merged already. |
| `already_registered` | 409 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `blob_not_deleted` | 409 | `abandon` | `abandon_and_release_local_state` | The blob is not deleted; nothing to restore. |
| `blob_not_recoverable` | 409 | `abandon` | `abandon_and_release_local_state` | The bytes are gone; upload the file again. |
| `ci_not_green` | 409 | `abandon` | `abandon_and_release_local_state` | Review after a green sandbox run on the current submission. |
| `circuit_closed` | 409 | `abandon` | `abandon_and_release_local_state` | Upgrade only a chunk whose circuit broke. |
| `circuit_not_open` | 409 | `abandon` | `abandon_and_release_local_state` | The domain's circuit is already closed. |
| `claim_already_refuted` | 409 | `abandon` | `abandon_and_release_local_state` | The claim was refuted; report a new commit. |
| `cli_session_decided` | 409 | `abandon` | `abandon_and_release_local_state` | Run `npx veynod init` again for a new code. |
| `decide_with_safe_default` | 409 | `abandon` | `decide_with_safe_default_and_continue` | Automation is on: choose the safest default, note it on the task and continue; ask the owner only about details.owner_topics. |
| `document_archived` | 409 | `abandon` | `abandon_and_release_local_state` | Give up, or ask for restore_document. |
| `escape_closed` | 409 | `abandon` | `abandon_and_release_local_state` | The escape is already FIXED or WAIVED. |
| `external_action_error` | 409 | `abandon` | `abandon_and_release_local_state` | Re-read the action state. |
| `gate_not_passed` | 409 | `abandon` | `abandon_and_release_local_state` | Merge after a passing gate on the current submission. |
| `host_revoked` | 409 | `abandon` | `abandon_and_release_local_state` | Ask the owner for a new runner token. |
| `id_collision` | 409 | `abandon` | `abandon_and_release_local_state` | Abort the import or import into a new tenant. |
| `invalid_state_transition` | 409 | `abandon` | `abandon_and_release_local_state` | Re-read the task; give up this action. |
| `invalid_transition` | 409 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `lease_conflict` | 409 | `abandon` | `abandon_and_release_local_state` | Another agent holds the lease; do not preempt. |
| `lease_expired` | 409 | `abandon` | `abandon_and_release_local_state` | Your lease ended before NOW(); claim the task again. |
| `lease_lost` | 409 | `abandon` | `abandon_and_release_local_state` | Drop local state and claim again. |
| `lineage_unavailable` | 409 | `abandon` | `abandon_and_release_local_state` | This subject has no signed lineage; give up. |
| `link_exists` | 409 | `abandon` | `abandon_and_release_local_state` | The link already exists. |
| `no_submission` | 409 | `abandon` | `abandon_and_release_local_state` | Run the sandbox after a submission is accepted. |
| `not_queue_head` | 409 | `abandon` | `abandon_and_release_local_state` | Only the queue head is checked; wait for this chunk's turn. |
| `nothing_to_probe` | 409 | `abandon` | `abandon_and_release_local_state` | Write and index documents first. |
| `oauth_request_decided` | 409 | `abandon` | `abandon_and_release_local_state` | Start the connection again from your client. |
| `placement_not_movable` | 409 | `abandon` | `abandon_and_release_local_state` | Read the placement again; it ended or its task moved past this step. |
| `proposal_not_pending` | 409 | `abandon` | `abandon_and_release_local_state` | The proposal was already decided. |
| `queue_not_verified` | 409 | `abandon` | `abandon_and_release_local_state` | Queued work merges from the queue after the controller re-tests it. |
| `queue_ticket_expired` | 409 | `abandon` | `abandon_and_release_local_state` | Call acquire_resource again to re-queue. |
| `request_closed` | 409 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `retention_not_met` | 409 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `run_not_claimed` | 409 | `abandon` | `abandon_and_release_local_state` | Only the runner holding a running deploy-gate run reports it; claim again. |
| `sample_unavailable` | 409 | `abandon` | `abandon_and_release_local_state` | Someone else took the sample or it was already judged. |
| `stale_change` | 409 | `abandon` | `abandon_and_release_local_state` | The diagram moved on; file the change again on details.current_version. |
| `stale_ci` | 409 | `abandon` | `abandon_and_release_local_state` | Drop this result; report a run of details.submission instead. |
| `stale_fencing_token` | 409 | `abandon` | `abandon_and_release_local_state` | Drop local state and claim again. |
| `submission_rejected` | 409 | `abandon` | `abandon_and_release_local_state` | The latest submission broke its write scope; wait for a new one. |
| `task_not_reviewable` | 409 | `abandon` | `abandon_and_release_local_state` | The task is not awaiting review. |
| `token_already_expired` | 409 | `abandon` | `abandon_and_release_local_state` | Ask the issuer to issue again. |
| `blob_purged` | 410 | `abandon` | `abandon_and_release_local_state` | The attachment is gone; remove the reference. |
| `cli_session_consumed` | 410 | `abandon` | `abandon_and_release_local_state` | The key was collected once; start a new setup. |
| `cli_session_expired` | 410 | `abandon` | `abandon_and_release_local_state` | Run `npx veynod init` again for a new code. |
| `gone` | 410 | `abandon` | `abandon_and_release_local_state` | Give up; details carry the tombstone. |
| `oauth_request_expired` | 410 | `abandon` | `abandon_and_release_local_state` | Start the connection again from your client. |
| `upload_expired` | 410 | `abandon` | `abandon_and_release_local_state` | Start a new upload. |
| `reviewer_conflict` | 422 | `abandon` | `abandon_and_release_local_state` | Give up this action. |
| `share_cross_root_not_allowed` | 422 | `abandon` | `abandon_and_release_local_state` | Use cross-tree collaboration instead. |
| `budget_exceeded` | 402 | `escalate_human` | `stop_and_escalate_to_human` | Ask a human to raise the budget. |
| `quota_exceeded` | 402 | `escalate_human` | `stop_and_escalate_to_human` | Ask a human to raise the quota. |
| `HUMAN_SOVEREIGNTY_LOCKED` | 403 | `escalate_human` | `stop_and_escalate_to_human` | A human locked this resource; ask a human to change it. |
| `commander_grant_expired` | 403 | `escalate_human` | `stop_and_escalate_to_human` | Ask the owner to issue a fresh grant. |
| `commander_grant_required` | 403 | `escalate_human` | `stop_and_escalate_to_human` | Ask the owner for a grant, then send X-Commander-Grant-Id. |
| `feature_disabled` | 403 | `escalate_human` | `stop_and_escalate_to_human` | Switched off here; ask an operator (details.setting). |
| `federation_forbidden` | 403 | `escalate_human` | `stop_and_escalate_to_human` | Read details.federation_code: an owner must act, or the connection does not allow it. |
| `operator_content_denied` | 403 | `escalate_human` | `stop_and_escalate_to_human` | Ask the workspace owner to open support access in the console. |
| `operator_write_denied` | 403 | `escalate_human` | `stop_and_escalate_to_human` | Ask the owner to reopen support access with changes allowed. |
| `role_permission_denied` | 403 | `escalate_human` | `stop_and_escalate_to_human` | Ask the human OWNER; this capability is not delegated. |
| `tenant_deleting` | 403 | `escalate_human` | `stop_and_escalate_to_human` | Stop; only an owner can cancel deletion or fetch the export. |
| `tenant_suspended` | 403 | `escalate_human` | `stop_and_escalate_to_human` | Stop writing and notify a human; reads and export still work. |
| `federation_disabled` | 404 | `escalate_human` | `stop_and_escalate_to_human` | A person turns federation on for the deployment first. |
| `backup_chain_broken` | 409 | `escalate_human` | `stop_and_escalate_to_human` | Restore from an older full backup or take a new one. |
| `backups_not_configured` | 409 | `escalate_human` | `stop_and_escalate_to_human` | An operator must configure backup storage. |
| `confirmation_required` | 409 | `escalate_human` | `stop_and_escalate_to_human` | Wait for details.pending_confirmations. |
| `deploys_off` | 409 | `escalate_human` | `stop_and_escalate_to_human` | A person turns deploys on for the target first. |
| `escalated_to_human` | 409 | `escalate_human` | `stop_and_escalate_to_human` | Wait for the human decision. |
| `incident_not_confirmed` | 409 | `escalate_human` | `stop_and_escalate_to_human` | Confirm the incident first. |
| `namespace_not_empty` | 409 | `escalate_human` | `stop_and_escalate_to_human` | Archive its documents and close its tasks. |
| `not_confirmed` | 409 | `escalate_human` | `stop_and_escalate_to_human` | A person confirms the diagram before it is frozen. |
| `not_frozen` | 409 | `escalate_human` | `stop_and_escalate_to_human` | A person confirms and freezes the diagram before dispatch. |
| `staging_not_configured` | 409 | `escalate_human` | `stop_and_escalate_to_human` | An operator must configure a separate staging database. |
| `taint_blocked` | 409 | `escalate_human` | `stop_and_escalate_to_human` | A human must verify tainted content. |
| `task_claimed` | 409 | `escalate_human` | `stop_and_escalate_to_human` | Ask the holder or a person to cancel it. |
| `tree_quota_exceeded` | 409 | `escalate_human` | `stop_and_escalate_to_human` | Revoke unused child tokens or ask a human for more quota. |
| `agent_document_quota_exceeded` | 429 | `escalate_human` | `stop_and_escalate_to_human` | Daily quota reached; ask the owner for a larger quota. |
| `budget_exhausted` | 429 | `escalate_human` | `stop_and_escalate_to_human` | Wait for resets_at or ask the issuer for more budget. |
| `circuit_open` | 429 | `escalate_human` | `stop_and_escalate_to_human` | Stop the loop; call agent_escalate_to_human. |
| `mcp_not_configured` | 500 | `escalate_human` | `stop_and_escalate_to_human` | An operator must set MCP_API_KEY. |
| `admin_disabled` | 503 | `escalate_human` | `stop_and_escalate_to_human` | ADMIN_TOKEN is not configured. |
| `federation_unavailable` | 503 | `escalate_human` | `stop_and_escalate_to_human` | The owner must configure the federation key on this deployment. |
| `google_login_not_configured` | 503 | `escalate_human` | `stop_and_escalate_to_human` | An operator must configure Google sign-in. |

## Warnings

Soft constraints reported in `warnings[]`; the request still succeeds.

| code | meaning |
|---|---|
| `duplicate_content` | Another document in the namespace already holds exactly this text. |
| `owner_report_missing` | Full automation: the handoff brief should carry owner_report {what_changed, how_to_try, not_yet_done} in plain words. |
| `listing_hides_documents` | The legacy listing hides drafts and tainted documents; the warning says how many and which flags show them. |
| `uri_namespace_prefix` | The address carried a namespace prefix; use doc://<doc_key>. |
| `document_namespace_inferred` | The new document's namespace came from its key prefix or the token's only writable namespace. |
| `task_namespace_inferred` | The task namespace was taken from its parent, context, assignee or creator; pass namespace to choose. |
| `task_namespace_defaulted` | No namespace was given or inferable; the task went to 'default'. |
| `assigned_tasks_out_of_reach` | Open tasks assigned to you are in namespaces your token cannot read; ask an owner to move them. |
| `promotion_below_threshold` | The domain was promoted below the promotion threshold. |
| `gate_source_downgraded` | Only a trusted runner reports RUNNER results; recorded as AGENT_CLAIM. |
| `secrets_not_imported` | Imported tokens must be rotated and users must reset passwords. |
| `keys_rotated` | A restored workspace has new keys; rotate them and re-issue agent tokens. |
| `alias_redirected` | Read through a renamed alias; use the current alias or id. |
| `legacy_alias` | Address predates the grammar; accepted, but 2026-11 rejects new ones. |
| `dangling_ref` | A $ref or part does not resolve; stored as written. |
| `namespace_auto_registered` | Namespace registered implicitly; 2026-11 requires it first. |
| `namespace_unchanged` | The new key's prefix is not a registered namespace; the document stayed where it was. Register the namespace and rename again to move it. |
| `namespace_undocumented` | The namespace readme is still a generated stub. |
| `frontmatter_missing` | Fields the namespace readme requires are missing. |
| `doc_key_pattern_mismatch` | doc_key does not match the namespace pattern. |
| `budget_warning` | A token's daily budget is at 80%; budget.warning went to its issuer. |
| `recall_still_depends` | This version still derives from recalled versions; the recall node stays open. |
| `ttl_capped_by_policy` | The token's lifetime was capped by an organisation policy. |
| `continuation_missing` | No continuation pack was written since the claim; call write_continuation before the next handoff. |
| `output_missing` | Nothing was written to the task's output_to since the claim. |
| `target_offline` | A mentioned agent is offline or retired; its owner is notified too. |
| `unknown_mention` | A mentioned principal is not in the directory. |
| `not_registered` | The collaborator directory is not live; identity is inferred. |
| `constitution_draft` | The constitution is still the generated template. |
| `token_expiring` | The calling token expires within a day. |
| `quota_80_percent` | The daily embedding quota is at least 80% used. |
| `embedding_deferred_quota` | Stored, but indexing waits for the daily embedding quota reset. |

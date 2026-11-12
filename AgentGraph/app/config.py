"""Application configuration for Veynod v3.9.0."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ app
    app_name: str = "Veynod"
    # Bump on every release and add notes in app/services/releases.py.
    app_version: str = "3.24.0"
    environment: Literal["development", "staging", "production", "test"] = "development"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    docs_enabled: bool = True
    docs_require_auth: bool = False
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # Veynod Platform composition gateway (code delivery).
    # Veynod Kernel code engine (headless; one code workspace per deployment).
    kernel_url: str = "http://127.0.0.1:8001"
    kernel_admin_key: str = "dev-admin"
    # Internal service identity used to cancel a task in the Kernel (the same
    # key the Runtime uses).  Never handed to a model or a browser.
    kernel_service_key: str = ""
    # Multi-instance: {"http://kernel-b:8001": "<admin key>"}.  The default
    # kernel_admin_key is used for any instance not listed here.
    kernel_admin_keys: str = ""
    kernel_workspace_id: str | None = None
    kernel_repo_path: str | None = None

    # Veynod Agent Runtime (internal service; X-Runtime-Key).
    agent_runtime_url: str | None = None
    agent_runtime_key: str = "dev-runtime"
    agent_runtime_timeout: float = 30.0
    # A Run with no explicit budget still needs a ceiling: an unproductive
    # loop must stop instead of spending without bound.
    runtime_default_max_model_calls: int = 40
    runtime_default_max_cost_usd: float = 5.0
    agent_runtime_projection_enabled: bool = True
    agent_runtime_projection_interval_seconds: int = Field(
        default=10, ge=3, le=3_600
    )
    # Phase 3 durable event inbox dispatcher.
    event_dispatch_enabled: bool = True
    event_dispatch_interval_seconds: float = Field(default=1.0, ge=0.5, le=300)
    # Fallback token prices when the Runtime provider does not return a cost.
    runtime_prompt_usd_per_1k: float = Field(default=0.0, ge=0)
    runtime_completion_usd_per_1k: float = Field(default=0.0, ge=0)

    # ------------------------------------------------------------- database
    database_url: str = (
        "postgresql+asyncpg://agentgraph:agentgraph@localhost:5432/agentgraph"
    )
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: float = 30.0
    # None: no connection pool only when ENVIRONMENT=test. Set it explicitly
    # for a test API server, whose one event loop can keep pooled connections.
    db_null_pool: bool | None = None
    auto_init_db: bool = False

    # ----------------------------------------------------------------- auth
    admin_token: str | None = None
    api_key_prefix: str = "ag_"
    agent_key_prefix: str = "agt_"
    human_key_prefix: str = "agh_"

    # ------------------------------------------------------------ embeddings
    embedding_provider: Literal["openai", "ollama", "hash"] = "hash"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1024, ge=1, le=2000)
    embedding_batch_size: int = 64
    embedding_batch_interval_seconds: float = Field(default=0.0, ge=0, le=60)
    embedding_timeout_seconds: float = 30.0
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    ollama_embedding_model: str = "nomic-embed-text"

    # ------------------------------------------------------- blackboard rules
    # 2026-10-24 decision 6: 900 s for every API version (60 s stalled long tasks).
    default_task_lease_seconds: int = Field(default=900, ge=5, le=86_400)
    max_task_lease_seconds: int = Field(default=3_600, ge=5, le=86_400)
    # Explicit renewal (spec §1.6): only in the last 300 s, at most 12 per claim.
    lease_renew_window_seconds: int = Field(default=300, ge=1, le=86_400)
    max_lease_renews: int = Field(default=12, ge=1, le=1_000)
    lease_reaper_interval_seconds: int = Field(default=15, ge=5, le=3_600)
    # Kernel code-run projection (Phase 1H): terminal states become product
    # task states without requiring a human to refresh the Code page.
    # Internal operator views (code / agent-runs / delivery consoles).  They
    # are NOT product navigation (ADR-018): hidden from users, served only at
    # /dashboard/internal/* and only when this flag is on (or never in
    # production unless an operator explicitly enables it).
    internal_views_enabled: bool = False
    # While the Kernel gate works on a task's submission, the lease reaper does
    # not hand the task to another computer; past this age it does.
    kernel_gate_hold_seconds: int = Field(default=3600, ge=60, le=86_400)
    # Replays Kernel commit projections the event stream lost (Phase 2, item 5).
    diagram_reconcile_enabled: bool = True
    diagram_reconcile_interval_seconds: int = Field(default=300, ge=30, le=3_600)
    code_projection_enabled: bool = True
    code_projection_interval_seconds: int = Field(default=30, ge=5, le=3_600)
    code_projection_batch_size: int = Field(default=100, ge=1, le=500)
    code_projection_event_enabled: bool = True
    code_projection_event_interval_seconds: float = Field(
        default=2.0, ge=0.5, le=300
    )
    # AG-TC-05: how often the anomaly rules run.
    situation_interval_seconds: int = Field(default=60, ge=5, le=3_600)
    # AG-CO-03: lease TTL when a 2026-11 client sets none (heartbeat every
    # ttl/3), and how many orphanings escalate a task to a human.
    lease_default_ttl_seconds: int = Field(default=900, ge=60, le=3_600)
    orphan_escalation_threshold: int = Field(default=3, ge=1, le=100)
    default_max_retries: int = Field(default=3, ge=1, le=100)
    # L3 messaging (M-14): unread-message and unclaimed-task timeout escalation.
    message_unread_escalation_seconds: int = Field(default=1800, ge=30, le=86_400)
    # Semi automation: agents' owner questions go out together, once the oldest
    # has waited this long or this many are waiting (Loop v1).
    escalation_batch_minutes: int = Field(default=30, ge=1, le=1440)
    escalation_batch_max: int = Field(default=5, ge=1, le=100)
    # Probe tokens (R7): their own cap per workspace and a short lifetime.
    probe_token_quota: int = Field(default=10, ge=0, le=1000)
    probe_token_max_ttl_hours: int = Field(default=24, ge=1, le=720)
    task_unclaimed_timeout_seconds: int = Field(default=7200, ge=60, le=86_400)
    # Full-managed Loop (runner-3): when the scheduler moves a task to another host.
    runner_offline_reassign_seconds: int = Field(default=120, ge=30, le=86_400)
    runner_claim_timeout_seconds: int = Field(default=180, ge=30, le=86_400)
    runner_quota_exhausted_percent: float = Field(default=98.0, ge=50.0, le=100.0)
    # Loop steps: how long a review or merge may take before another host gets it,
    # and how often the supervisor walks the floor when something waits.
    runner_step_timeout_seconds: int = Field(default=1800, ge=60, le=86_400)
    runner_supervise_every_seconds: int = Field(default=600, ge=60, le=86_400)
    runner_supervise_timeout_seconds: int = Field(default=1200, ge=60, le=86_400)
    # Deploy gate v2: how long one deploy run may take, and how often the deploy
    # loop queues scheduled runs and starts waiting ones.
    deploy_run_timeout_seconds: int = Field(default=1800, ge=60, le=86_400)
    deploy_loop_interval_seconds: int = Field(default=15, ge=1, le=3_600)
    max_reject_history: int = Field(default=20, ge=1, le=500)
    human_override_default_lock: bool = True

    # --------------------------------------------------------------- retrieval
    search_top_k: int = Field(default=5, ge=1, le=100)
    max_search_top_k: int = Field(default=50, ge=1, le=200)
    semantic_threshold: float = Field(default=0.15, ge=0.0, le=1.0)
    default_timeline_hours: int = 24
    max_context_timeline: int = 50
    max_context_slots: int = 100
    preview_chars: int = 280
    timeline_payload_chars: int = 180
    token_chars_per_token: float = 4.0
    max_workspace_map_items: int = 300
    max_page_size: int = 500
    # HMAC key for list cursors (AG-ST-01); derived from the database URL
    # when unset, so cursors survive restarts of the same deployment.
    cursor_signing_secret: str | None = None
    list_cursor_ttl_hours: int = Field(default=24, ge=1, le=720)
    # Exports (AG-ST-02): manual exports per tenant per hour, how long an
    # archive stays downloadable, and the largest archive an import accepts.
    exports_per_hour: int = Field(default=4, ge=1, le=1_000)
    export_retention_days: int = Field(default=30, ge=1, le=3_650)
    max_import_bytes: int = Field(default=512 * 1024 * 1024, ge=1_024)

    # Token tree (AG-ID-02): issue_child_token stays off until an operator
    # enables it (PRD gate: root/branch split, lineage and revocation first).
    child_token_issuance: bool = False

    # ------------------------------------- quality control 1.0 (v3.32)
    # Gate results, audits, claims, samples and escapes are always recorded;
    # QUALITY_CONTROL=true enforces the release matrix (tiers, gates, audit
    # view, cited clauses, circuit) on reviews and hand-offs.
    quality_control: bool = False
    # Role rules v0.2 §2.6 (only the human OWNER approves DONE). Off for now:
    # any principal allowed to review may approve DONE, as before 3.12.0.
    owner_only_done: bool = False
    quality_tier1_sample_rate: float = Field(default=0.10, ge=0, le=1)  # Q-1
    quality_tier2_sample_rate: float = Field(default=0.20, ge=0, le=1)  # Q-2
    quality_auto_merge_daily_cap: int = Field(default=20, ge=0)  # Q-3
    quality_circuit_failures: int = Field(default=3, ge=1)  # Q-4
    quality_promotion_min_completed: int = Field(default=30, ge=1)  # Q-6
    quality_promotion_max_escape_rate: float = Field(default=0.02, ge=0, le=1)  # Q-6
    quality_demotion_escape_rate: float = Field(default=0.05, ge=0, le=1)  # Q-7
    quality_risk_artifact_bytes: int = Field(default=200_000, ge=1)  # Q-9
    quality_sample_overdue_days: int = Field(default=7, ge=1)  # Q-10

    # ------------------------------------------- backups (AG-ST-10, R1.3)
    # Archives go to BACKUP_DIR, which must live on other storage than the
    # database (another disk, an NFS/object-storage mount). Unset = no backups.
    backup_dir: str | None = None
    backup_scheduler_enabled: bool = True
    # Incrementals every 15 minutes keep the logical RPO at 15 minutes.
    backup_interval_minutes: int = Field(default=15, ge=1, le=1_440)
    backup_full_interval_hours: int = Field(default=24, ge=1, le=720)
    backup_retention_days: int = Field(default=30, ge=1, le=3_650)
    # Optional command run after each backup, e.g. "rclone copy {path} remote:ag".
    backup_upload_command: str | None = None
    # Restores and drills target this database, never production.
    staging_database_url: str | None = None
    restore_drill_interval_days: int = Field(default=30, ge=1, le=365)
    restore_drill_sample: int = Field(default=100, ge=1, le=100_000)

    # ------------------------------------------ incidents (AG-TC-06, R1.3)
    incident_check_interval_seconds: int = Field(default=60, ge=5, le=3_600)
    # Shown in every notice as "our contact"; required to send notices.
    security_contact: str | None = None
    platform_admin_email: str | None = None
    # Outgoing mail; without SMTP_HOST messages stay in email_outbox as no_transport.
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65_535)
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "Veynod <no-reply@localhost>"
    smtp_starttls: bool = True
    # Per-token access log (scope resolution for reads).
    access_log_flush_seconds: float = Field(default=5.0, ge=0.1, le=300)
    access_log_max_objects: int = Field(default=500, ge=10, le=10_000)

    # ----------------------------------------------------- proposals / policy
    proposals_enabled: bool = True
    default_proposal_quorum: int = 1
    max_proposal_content_chars: int = 100_000
    proposal_arena_max_entries: int = 20

    # ------------------------------------------------------------------ taint
    taint_propagation_enabled: bool = True
    taint_max_depth: int = 20
    taint_untrusted_source_default: bool = True

    # --------------------------------------------------------------- retrieval
    chunk_size_chars: int = Field(default=1_800, ge=200, le=20_000)
    chunk_overlap_chars: int = Field(default=200, ge=0, le=2_000)
    hybrid_candidate_multiplier: int = Field(default=4, ge=1, le=20)
    hybrid_rrf_k: int = Field(default=60, ge=1, le=1_000)
    hnsw_ef_search: int = Field(default=100, ge=10, le=1_000)

    # ------------------------------------------------------------- human auth
    human_session_hours: int = Field(default=12, ge=1, le=720)
    # PRD AG-ID-07 point 8 / D21: deletion grace period before physical purge.
    tenant_deletion_grace_days: int = Field(default=30, ge=1, le=365)
    password_hash_n: int = Field(default=2**14, ge=2**12, le=2**18)
    password_hash_r: int = 8
    password_hash_p: int = 1
    human_login_max_attempts_per_minute: int = Field(default=10, ge=1, le=1_000)
    auth_ip_max_attempts_per_minute: int = Field(default=60, ge=1, le=10_000)
    auth_global_max_attempts_per_minute: int = Field(default=600, ge=1, le=100_000)
    signup_global_max_per_minute: int = Field(default=60, ge=1, le=10_000)
    human_signup_enabled: bool = True
    signup_default_tier: str = "free"
    google_client_id: str | None = None

    # ------------------------------------------------------ growth (v3.34)
    # `npx veynod init` device flow: how long a user code waits for approval.
    cli_session_ttl_seconds: int = Field(default=600, ge=60, le=3_600)
    cli_sessions_per_ip_per_minute: int = Field(default=10, ge=1, le=1_000)
    # Accounts left out of the operator KPIs (the team's own): comma-separated
    # e-mails or @domains, e.g. "me@example.com,@veynod.com".
    metrics_internal_emails: str = ""

    # Agent-delegated tokens (depth 2+): default lifetime when the issuer
    # asks for none (the workspace policy token_max_ttl_seconds still caps
    # it), and how long expired or revoked ones are kept before purging.
    delegated_token_default_ttl_seconds: int = Field(default=3_600, ge=60, le=72 * 3_600)
    delegated_token_retention_days: int = Field(default=7, ge=1, le=365)

    # OAuth for MCP clients (v3.36). The public origin used as the OAuth issuer
    # and in discovery documents; derived from the request when unset.
    public_base_url: str | None = None

    # ---------------------------------------------------------- row security
    rls_enabled: bool = True
    rls_role: str = "agentgraph_app"
    rls_require_context: bool = False

    # ------------------------------------------------------ rate / budgets
    rate_limit_enabled: bool = True
    rate_limit_default_per_minute: int = Field(default=600, ge=1, le=1_000_000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=3_600)
    message_rate_limit_per_minute: int = Field(default=120, ge=1, le=1_000_000)
    default_monthly_token_budget: int = Field(default=10_000_000, ge=0)
    default_monthly_cost_budget_usd: float = Field(default=1000.0, ge=0)
    default_max_concurrent_leases: int = Field(default=100, ge=1)

    # ------------------------------------------------------------ federation
    # Off by default (ADR-008, I-ID ID-INV-5): no federation identity exists
    # and no federation code path runs until an owner turns this on.
    federation_enabled: bool = False
    # Base64 of 32 random bytes; seals hosted federation private keys
    # (ADR-013). Generated and delivered by the owner; required to create a
    # hosted identity. Never logged.
    federation_kek: str = ""
    federation_kek_id: str = "kek-1"
    # I-ID TBD-7 placeholders (P1): rotation overlap, record lifetime, skew.
    federation_key_overlap_days: int = Field(default=7, ge=1, le=30)
    federation_record_lifetime_days: int = Field(default=365, ge=1, le=3650)
    federation_clock_skew_seconds: int = Field(default=120, ge=0, le=900)
    # Delegation delivery (ICD_I_BRIDGE §5): how often queued objects are retried.
    federation_delivery_interval_seconds: int = Field(default=15, ge=1, le=3600)
    # Inbound messages per sending workspace (ICD_I_MSG §5, F-011): a token
    # bucket per pair of workspaces. P1 placeholders until load tests.
    federation_peer_rate_per_minute: int = Field(default=120, ge=1, le=60000)
    federation_peer_burst: int = Field(default=60, ge=1, le=10000)
    # Other deployments (F-11): hosts this deployment may reach for
    # federation. Empty (the default) means no outbound federation traffic at
    # all, which is what a closed enterprise network wants (F-022).
    federation_peer_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # P5 simulated market: demonstration credits (SIM) each workspace gets once.
    federation_sim_allowance: int = Field(default=1000, ge=0, le=1_000_000)
    federation_remote_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    # P6 (R1-R3) simulated resource rights and the Agent Poker Arena. Both off
    # by default and only meaningful while federation_enabled is on. DEMO_M
    # has no economic value (docs/federation/ICD_P6_RESOURCE_RIGHTS_ARENA.md).
    federation_resource_rights_enabled: bool = False
    federation_arena_enabled: bool = False
    # Simulated capacity (in tokens; 1 DEMO_M = 1,000,000) a workspace may declare.
    federation_demo_m_allowance_tokens: int = Field(default=20_000_000, ge=0, le=1_000_000_000)
    federation_arena_setup_minutes: int = Field(default=30, ge=1, le=1440)
    federation_arena_dispute_minutes: int = Field(default=10, ge=0, le=10080)
    federation_arena_obligation_days: int = Field(default=7, ge=1, le=90)
    federation_arena_voucher_days: int = Field(default=30, ge=1, le=365)

    # ---------------------------------------------------- outbound actions
    outbound_dispatch_interval_seconds: int = Field(default=5, ge=1, le=300)
    outbound_max_attempts: int = Field(default=5, ge=1, le=20)
    outbound_timeout_seconds: float = 10.0
    outbound_max_response_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)
    outbound_allow_private_networks: bool = True
    outbound_allowed_hosts: list[str] = Field(default_factory=list)
    outbound_signing_secret: str | None = None
    # Poke delivery D-1 (2026-10-23): retry after 30 s, 120 s and 600 s, then
    # the delivery is DEAD (shown in red, a webhook.dead event for people).
    webhook_retry_delays_seconds: list[int] = Field(default_factory=lambda: [30, 120, 600])
    webhook_batch_size: int = Field(default=100, ge=1, le=1000)

    # ---------------------------------------------------------------- quota
    free_max_documents: int = 5_000
    free_max_slots: int = 20
    developer_max_documents: int = 50_000
    developer_max_slots: int = 100
    pro_max_documents: int = 500_000
    pro_max_slots: int = 1_000
    # L3 messaging (M-11): permanent messages are quota-bounded per tier.
    free_max_messages: int = 100_000
    developer_max_messages: int = 1_000_000
    pro_max_messages: int = 10_000_000
    # PRD AG-ST-03: documents up to 1 MiB of UTF-8; larger content uses parts.
    max_document_content_bytes: int = 1_048_576
    # Character ceiling for request validation only; the byte limit decides.
    max_document_content_chars: int = 4_194_304
    max_state_bytes: int = 100_000
    max_brief_bytes: int = 32_768
    max_artifact_chars: int = 4_194_304

    # ------------------------------------------------ blobs (v3.40)
    max_blob_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    blob_unreferenced_ttl_days: int = Field(default=30, ge=0, le=3650)
    # Under semi or full automation an agent's delete keeps the bytes this long
    # so a person can restore them (Loop A-06); then the GC purges them.
    blob_delete_retention_days: int = Field(default=7, ge=1, le=365)
    blob_base64_ratio: float = Field(default=0.80, ge=0.0, le=1.0)
    blob_base64_min_bytes: int = Field(default=64 * 1024, ge=1)
    agent_daily_document_limit: int = Field(default=100, ge=1)
    agent_daily_document_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    blob_upload_ttl_hours: int = Field(default=24, ge=1, le=168)
    # Public share links: origin used in generated URLs (falls back to the
    # request origin); the image width is the server-rendered page width.
    share_link_origin: str | None = None
    share_link_image_width: int = Field(default=1200, ge=480, le=2400)
    # Public, read-only acquisition page. The workspace is seeded by operations;
    # the page never accepts writes.
    demo_workspace_name: str = "veynod-demo"

    # ------------------------------------------------ async index (AG-ST-13)
    index_worker_enabled: bool = True
    index_worker_interval_seconds: float = Field(default=1.0, ge=0.1, le=60)
    index_worker_batch: int = Field(default=20, ge=1, le=500)
    # Undeclared clients (no Veynod-Version) enqueue indexing when true.
    # A client that explicitly sends 2026-10-21 still gets synchronous indexing.
    async_index_default: bool = False
    # Scheduled exports (AG-ST-02 point 6): a weekly full baseline and daily
    # increments per root tenant, kept this many days. Off by default.
    scheduled_exports: bool = False
    scheduled_export_keep_days: int = Field(default=30, ge=1, le=365)
    # The daily self_recall@50 probe (AG-ST-12).
    retrieval_probe_enabled: bool = True
    index_max_attempts: int = Field(default=5, ge=1, le=50)

    # ------------------------------------------------ cost control (AG-ST-15)
    # "shadow" records would-be limits without blocking (run ~14 days to
    # calibrate); "enforce" returns 429 rate_limited / circuit_open.
    budget_mode: str = Field(default="shadow", pattern="^(shadow|enforce)$")
    write_rate_per_minute: int = Field(default=60, ge=1, le=100_000)
    write_burst: int = Field(default=20, ge=1, le=100_000)
    query_rate_per_minute: int = Field(default=120, ge=1, le=100_000)
    hot_key_writes_per_minute: int = Field(default=20, ge=2, le=10_000)
    circuit_open_minutes: int = Field(default=10, ge=1, le=1_440)
    embedding_tokens_per_day: int = Field(default=2_000_000, ge=1)
    # Server-enforced backoff: a request before Retry-After is refused without
    # taking a token and doubles the next wait; repeated violations trip a
    # short circuit (reason ignored_backoff).
    backoff_max_seconds: int = Field(default=60, ge=1, le=3_600)
    backoff_violations_to_trip: int = Field(default=3, ge=1, le=100)
    backoff_circuit_minutes: int = Field(default=1, ge=1, le=1_440)
    # A token's write rate above this multiple of its own 7-day p99 for the
    # same hour trips the circuit (reason write_spike); needs enough history.
    write_spike_multiplier: float = Field(default=10.0, ge=1.0, le=1_000.0)
    write_spike_min_history_minutes: int = Field(default=30, ge=1, le=10_080)
    # In-flight requests per credential; long-polls (SSE) have their own pool.
    max_in_flight_per_token: int = Field(default=4, ge=1, le=10_000)
    max_streams_per_token: int = Field(default=1, ge=1, le=100)
    # REST writes one process runs at once, across all credentials. A write
    # can hold up to four pooled connections (its own session plus metering
    # sessions), so the default is the pool size over four: a burst waits
    # outside the pool instead of exhausting it. None derives it.
    db_max_concurrent_writes: int | None = Field(default=None, ge=1, le=10_000)

    @property
    def db_admission_limit(self) -> int:
        if self.db_max_concurrent_writes is not None:
            return self.db_max_concurrent_writes
        return max(2, (self.db_pool_size + self.db_max_overflow) // 4)

    # ------------------------------------------- failure-semantics protocol
    # PRD AG-ST-18: 24 h idempotency window; an in-progress record older than
    # the stale limit is treated as abandoned (crashed worker) and reclaimed.
    idempotency_window_hours: int = Field(default=24, ge=1, le=168)
    idempotency_stale_seconds: int = Field(default=300, ge=10, le=3_600)

    # -------------------------------------------------------------- realtime
    sse_heartbeat_seconds: int = Field(default=15, ge=5, le=120)
    sse_max_connections: int = Field(default=200, ge=1, le=10_000)
    sse_max_connections_per_principal: int = Field(default=5, ge=1, le=100)
    sse_revalidate_seconds: int = Field(default=30, ge=5, le=600)
    sse_max_lifetime_seconds: int = Field(default=3_600, ge=60, le=86_400)
    embedding_bytes_per_minute: int = Field(default=5_000_000, ge=100_000)
    sse_replay_limit: int = Field(default=500, ge=1, le=5_000)
    event_retention_days: int = Field(default=7, ge=1, le=365)
    auto_timeline_events: bool = True

    # ------------------------------------------------------------------ mcp
    request_timeout_seconds: float = 30.0
    mcp_enabled: bool = True
    mcp_stateless_http: bool = True
    mcp_json_response: bool = True
    mcp_api_key: str | None = None
    mcp_agent_name: str = "mcp_agent"
    # New inbox items ride on every agent tool result (see services/inbox_hints).
    inbox_hints_enabled: bool = True
    mcp_dns_rebinding_protection: bool = True
    mcp_allowed_hosts: list[str] = Field(default_factory=list)
    mcp_allowed_origins: list[str] = Field(default_factory=list)

    @field_validator(
        "cors_origins", "outbound_allowed_hosts", "federation_peer_hosts", mode="before"
    )
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str) and not value.startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _openai_key_required(self) -> Settings:
        if self.embedding_provider == "openai" and not self.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is required when EMBEDDING_PROVIDER=openai"
            )
        return self

    @field_validator("max_search_top_k")
    @classmethod
    def _search_cap(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_search_top_k must be positive")
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def tier_limits(self, tier: str) -> tuple[int, int]:
        if tier == "free":
            return self.free_max_documents, self.free_max_slots
        if tier == "pro":
            return self.pro_max_documents, self.pro_max_slots
        return self.developer_max_documents, self.developer_max_slots


@lru_cache
def get_settings() -> Settings:
    return Settings()

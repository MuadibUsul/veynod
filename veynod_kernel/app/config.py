"""Core configuration for Veynod Kernel v4.0.

All runtime knobs live here. The module deliberately keeps no state beyond a
single cached :class:`Settings` instance so the engine can be embedded in tests.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- storage -----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://veynod:veynod@127.0.0.1:5432/veynod"
    db_pool_size: int = Field(
        default=20,
        ge=1,
        le=200,
        validation_alias=AliasChoices("VN_DB_POOL_SIZE", "DB_POOL_SIZE"),
    )
    db_max_overflow: int = Field(
        default=40,
        ge=0,
        le=400,
        validation_alias=AliasChoices("VN_DB_MAX_OVERFLOW", "DB_MAX_OVERFLOW"),
    )
    auto_init_db: bool = Field(
        default=True, validation_alias=AliasChoices("VN_AUTO_INIT_DB", "AUTO_INIT_DB")
    )

    # --- auth --------------------------------------------------------------
    auth_mode: Literal["dev", "prod"] = Field(
        default="dev", validation_alias=AliasChoices("VN_AUTH_MODE", "AUTH_MODE")
    )
    admin_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("VN_ADMIN_API_KEY", "ADMIN_API_KEY")
    )
    # Internal service identity used by the Agent Runtime: evidence reads,
    # cancellation and the durable event feed. Never handed to a model or a
    # browser; it is a different principal from the admin key.
    runtime_service_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "VN_RUNTIME_SERVICE_KEY", "RUNTIME_SERVICE_KEY"
        ),
    )
    # Runner-plane read access to workspace files (needed to build a valid
    # unified diff). Read-only, workspace-scoped, size-capped.
    allow_runner_file_read: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "VN_ALLOW_RUNNER_FILE_READ", "ALLOW_RUNNER_FILE_READ"
        ),
    )
    runner_file_max_bytes: int = Field(
        default=1_000_000,
        ge=1024,
        le=8_000_000,
        validation_alias=AliasChoices(
            "VN_RUNNER_FILE_MAX_BYTES", "RUNNER_FILE_MAX_BYTES"
        ),
    )
    bootstrap_runner_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("VN_BOOTSTRAP_RUNNER_TOKEN", "BOOTSTRAP_RUNNER_TOKEN"),
    )
    require_bound_tokens: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "VN_REQUIRE_BOUND_TOKENS", "REQUIRE_BOUND_TOKENS"
        ),
    )
    auth_cache_ttl_seconds: float = Field(
        default=300.0,
        ge=0,
        validation_alias=AliasChoices(
            "VN_AUTH_CACHE_TTL_SECONDS", "AUTH_CACHE_TTL_SECONDS"
        ),
    )

    # --- API plane separation (Phase 8.1) ----------------------------------
    api_plane: Literal["all", "submit", "claim_sse"] = Field(
        default="all",
        validation_alias=AliasChoices("VN_API_PLANE", "API_PLANE"),
    )
    enable_background_loops: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "VN_ENABLE_BACKGROUND_LOOPS", "ENABLE_BACKGROUND_LOOPS"
        ),
    )
    claim_max_wait_seconds: float = Field(
        default=15.0,
        ge=0,
        le=30,
        validation_alias=AliasChoices(
            "VN_CLAIM_MAX_WAIT_SECONDS", "CLAIM_MAX_WAIT_SECONDS"
        ),
    )

    @field_validator(
        "default_workspace_id", "cell_id", mode="before", check_fields=False
    )
    @classmethod
    def _empty_uuid_is_none(cls, value: object) -> object:
        """Compose passes unset values as ""; treat them as absent, not invalid."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # --- workspace selection ----------------------------------------------
    # The 6 syscalls intentionally do not carry a workspace id (one kernel
    # deployment serves one project). Pin it here in multi-workspace setups.
    default_workspace_id: UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("VN_DEFAULT_WORKSPACE_ID", "DEFAULT_WORKSPACE_ID"),
    )
    # Phase 4: a worker/gate/merge process can be bound to one execution cell.
    # Empty means "all cells" (single-deployment compatibility).
    cell_id: UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("VN_CELL_ID", "CELL_ID"),
    )

    # --- deterministic gate ------------------------------------------------
    typecheck_cmd: str = Field(
        default="pyright", validation_alias=AliasChoices("VN_TYPECHECK_CMD", "TYPECHECK_CMD")
    )
    test_cmd: str = Field(
        default="pytest -q --tb=short --no-header --no-summary -p no:cov -p no:warnings -p no:doctest -p no:pastebin -p no:junitxml -p no:anyio",
        validation_alias=AliasChoices("VN_TEST_CMD", "TEST_CMD"),
    )
    lint_cmd: str = Field(
        default="ruff check",
        validation_alias=AliasChoices("VN_LINT_CMD", "LINT_CMD"),
    )
    skip_lint: bool = Field(
        default=False,
        validation_alias=AliasChoices("VN_SKIP_LINT", "SKIP_LINT"),
    )
    collect_cmd: str = Field(
        default="pytest --collect-only -q",
        validation_alias=AliasChoices("VN_COLLECT_CMD", "COLLECT_CMD"),
    )
    skip_typecheck: bool = Field(
        default=False,
        validation_alias=AliasChoices("VN_SKIP_TYPECHECK", "SKIP_TYPECHECK"),
    )
    gate_timeout_seconds: int = Field(
        default=600,
        validation_alias=AliasChoices("VN_GATE_TIMEOUT_SECONDS", "GATE_TIMEOUT_SECONDS"),
    )
    gate_output_chars: int = Field(
        default=4000,
        validation_alias=AliasChoices("VN_GATE_OUTPUT_CHARS", "GATE_OUTPUT_CHARS"),
    )

    # --- leases / merge ----------------------------------------------------
    lease_seconds: int = Field(
        default=600, validation_alias=AliasChoices("VN_LEASE_SECONDS", "LEASE_SECONDS")
    )
    verify_lease_seconds: int = Field(
        default=1800,
        validation_alias=AliasChoices("VN_VERIFY_LEASE_SECONDS", "VERIFY_LEASE_SECONDS"),
    )
    max_merge_attempts: int = Field(
        default=4,
        validation_alias=AliasChoices("VN_MAX_MERGE_ATTEMPTS", "MAX_MERGE_ATTEMPTS"),
    )
    gate_worktree_pool_size: int = Field(
        default=8,
        validation_alias=AliasChoices(
            "VN_GATE_WORKTREE_POOL_SIZE", "GATE_WORKTREE_POOL_SIZE"
        ),
    )
    fast_forward_merge: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "VN_FAST_FORWARD_MERGE", "FAST_FORWARD_MERGE"
        ),
    )
    single_kernel_claim: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "VN_SINGLE_KERNEL_CLAIM", "SINGLE_KERNEL_CLAIM"
        ),
    )
    # Every API process keeps a local Rust core and lets the DB conditional
    # UPDATE fence duplicate/conflicting claims. This removes the
    # one-authority bottleneck for horizontal API instances.
    multi_core_claim: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "VN_MULTI_CORE_CLAIM", "MULTI_CORE_CLAIM"
        ),
    )
    async_edge_rebuild: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "VN_ASYNC_EDGE_REBUILD", "ASYNC_EDGE_REBUILD"
        ),
    )
    reaper_interval_seconds: int = Field(
        default=30,
        validation_alias=AliasChoices("VN_REAPER_INTERVAL_SECONDS", "REAPER_INTERVAL_SECONDS"),
    )
    reap_verifying_after_seconds: int = Field(
        default=3600,
        validation_alias=AliasChoices(
            "VN_REAP_VERIFYING_AFTER_SECONDS", "REAP_VERIFYING_AFTER_SECONDS"
        ),
    )
    allow_nonbare_repo: bool = Field(
        default=False,
        validation_alias=AliasChoices("VN_ALLOW_NONBARE_REPO", "ALLOW_NONBARE_REPO"),
    )
    worktree_root: str | None = Field(
        default=None, validation_alias=AliasChoices("VN_WORKTREE_ROOT", "WORKTREE_ROOT")
    )

    # --- event stream ------------------------------------------------------
    sse_poll_seconds: float = Field(
        default=1.0, validation_alias=AliasChoices("VN_SSE_POLL_SECONDS", "SSE_POLL_SECONDS")
    )
    debug_dashboard_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "VN_DEBUG_DASHBOARD", "DEBUG_DASHBOARD"
        ),
    )

    # --- asynchronous patch submissions ------------------------------------
    submission_worker_concurrency: int = Field(
        default=8,
        ge=0,
        le=32,
        validation_alias=AliasChoices(
            "VN_SUBMISSION_WORKERS", "SUBMISSION_WORKERS"
        ),
    )
    submission_poll_seconds: float = Field(
        default=0.25,
        gt=0,
        validation_alias=AliasChoices(
            "VN_SUBMISSION_POLL_SECONDS", "SUBMISSION_POLL_SECONDS"
        ),
    )
    # "reject" returns 410 and points at /v2; "sync" is the temporary escape
    # hatch for old clients during the migration window.
    legacy_submit_mode: Literal["reject", "sync"] = Field(
        default="reject",
        validation_alias=AliasChoices(
            "VN_LEGACY_SUBMIT_MODE", "LEGACY_SUBMIT_MODE"
        ),
    )

    # --- independent gate workers / sandbox --------------------------------
    work_lease_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        validation_alias=AliasChoices("VN_WORK_LEASE_SECONDS", "WORK_LEASE_SECONDS"),
    )
    work_heartbeat_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        validation_alias=AliasChoices(
            "VN_WORK_HEARTBEAT_SECONDS", "WORK_HEARTBEAT_SECONDS"
        ),
    )
    work_max_attempts: int = Field(
        default=5,
        ge=1,
        le=20,
        validation_alias=AliasChoices("VN_WORK_MAX_ATTEMPTS", "WORK_MAX_ATTEMPTS"),
    )
    merge_coordinator_lease_seconds: int = Field(
        default=60,
        ge=10,
        le=600,
        validation_alias=AliasChoices(
            "VN_MERGE_COORDINATOR_LEASE_SECONDS", "MERGE_COORDINATOR_LEASE_SECONDS"
        ),
    )

    # --- admission control / horizontal API --------------------------------
    admission_gate_queue_limit: int = Field(
        default=256,
        ge=1,
        validation_alias=AliasChoices(
            "VN_ADMISSION_GATE_QUEUE_LIMIT", "ADMISSION_GATE_QUEUE_LIMIT"
        ),
    )
    admission_merge_queue_limit: int = Field(
        default=128,
        ge=1,
        validation_alias=AliasChoices(
            "VN_ADMISSION_MERGE_QUEUE_LIMIT", "ADMISSION_MERGE_QUEUE_LIMIT"
        ),
    )
    admission_resume_ratio: float = Field(
        default=0.8,
        gt=0,
        le=1,
        validation_alias=AliasChoices(
            "VN_ADMISSION_RESUME_RATIO", "ADMISSION_RESUME_RATIO"
        ),
    )
    admission_retry_after_seconds: int = Field(
        default=5,
        ge=1,
        le=300,
        validation_alias=AliasChoices(
            "VN_ADMISSION_RETRY_AFTER_SECONDS", "ADMISSION_RETRY_AFTER_SECONDS"
        ),
    )
    admission_claim_slots: int = Field(
        default=32,
        ge=1,
        le=64,
        validation_alias=AliasChoices(
            "VN_ADMISSION_CLAIM_SLOTS", "ADMISSION_CLAIM_SLOTS"
        ),
    )
    admission_mode: Literal["reject", "defer"] = Field(
        default="reject",
        validation_alias=AliasChoices("VN_ADMISSION_MODE", "ADMISSION_MODE"),
    )
    scheduler_authority_lease_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        validation_alias=AliasChoices(
            "VN_SCHEDULER_AUTHORITY_LEASE_SECONDS", "SCHEDULER_AUTHORITY_LEASE_SECONDS"
        ),
    )

    # --- persistent edge jobs / outbox -------------------------------------
    edge_rebuild_lease_seconds: int = Field(
        default=120,
        ge=10,
        le=1800,
        validation_alias=AliasChoices(
            "VN_EDGE_REBUILD_LEASE_SECONDS", "EDGE_REBUILD_LEASE_SECONDS"
        ),
    )
    edge_rebuild_max_attempts: int = Field(
        default=5,
        ge=1,
        le=50,
        validation_alias=AliasChoices(
            "VN_EDGE_REBUILD_MAX_ATTEMPTS", "EDGE_REBUILD_MAX_ATTEMPTS"
        ),
    )
    outbox_poll_seconds: float = Field(
        default=0.1,
        gt=0,
        validation_alias=AliasChoices(
            "VN_OUTBOX_POLL_SECONDS", "OUTBOX_POLL_SECONDS"
        ),
    )
    worker_poll_seconds: float = Field(
        default=1.0,
        gt=0,
        validation_alias=AliasChoices("VN_WORKER_POLL_SECONDS", "WORKER_POLL_SECONDS"),
    )
    cancel_poll_seconds: float = Field(
        default=2.0,
        ge=0.5,
        le=60.0,
        validation_alias=AliasChoices(
            "VN_CANCEL_POLL_SECONDS", "CANCEL_POLL_SECONDS"
        ),
    )
    gate_sandbox: Literal["local", "docker"] = Field(
        default="local",
        validation_alias=AliasChoices("VN_GATE_SANDBOX", "GATE_SANDBOX"),
    )
    allow_local_gate_in_prod: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "VN_ALLOW_LOCAL_GATE_IN_PROD", "ALLOW_LOCAL_GATE_IN_PROD"
        ),
    )
    gate_sandbox_image: str | None = Field(
        default=None,
        validation_alias=AliasChoices("VN_GATE_SANDBOX_IMAGE", "GATE_SANDBOX_IMAGE"),
    )
    gate_cpu_limit: float = Field(
        default=1.0,
        gt=0,
        validation_alias=AliasChoices("VN_GATE_CPU_LIMIT", "GATE_CPU_LIMIT"),
    )
    # RLIMIT_AS for one gate command.  It caps *virtual* address space, not
    # RSS: a Node-based checker (pyright) cannot even reserve its code range
    # below ~1 GiB, so 512 MiB made every typecheck gate fail with SIGABRT.
    gate_memory_mb: int = Field(
        default=2048,
        ge=64,
        validation_alias=AliasChoices("VN_GATE_MEMORY_MB", "GATE_MEMORY_MB"),
    )
    gate_pids_limit: int = Field(
        default=128,
        ge=16,
        validation_alias=AliasChoices("VN_GATE_PIDS_LIMIT", "GATE_PIDS_LIMIT"),
    )
    # Docker-only hardening.  Empty user means "image default"; production
    # should set a non-root uid:gid and keep the network at "none".
    gate_docker_user: str = Field(
        default="",
        validation_alias=AliasChoices("VN_GATE_DOCKER_USER", "GATE_DOCKER_USER"),
    )
    gate_docker_network: str = Field(
        default="none",
        validation_alias=AliasChoices(
            "VN_GATE_DOCKER_NETWORK", "GATE_DOCKER_NETWORK"
        ),
    )
    gate_nofile_limit: int = Field(
        default=256,
        ge=16,
        validation_alias=AliasChoices("VN_GATE_NOFILE_LIMIT", "GATE_NOFILE_LIMIT"),
    )

    @property
    def async_database_url(self) -> str:
        url = self.database_url
        if url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + url[len("postgresql://") :]
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url[len("postgres://") :]
        return url

    @property
    def schema_path(self) -> Path:
        return REPO_ROOT / "sql" / "v4_001_genesis.sql"

    @property
    def migration_paths(self) -> list[Path]:
        """Versioned migrations after genesis, applied in filename order."""
        return sorted(
            path
            for path in (REPO_ROOT / "sql").glob("v4_*.sql")
            if path != self.schema_path
        )

    @property
    def schema_paths(self) -> list[Path]:
        return [self.schema_path, *self.migration_paths]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

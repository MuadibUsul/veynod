"""Async SQLAlchemy engine, session factory, and schema bootstrap helpers."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_pool_kwargs: dict[str, object] = {
    "echo": settings.db_echo,
    "pool_pre_ping": True,
    "pool_size": settings.db_pool_size,
    "max_overflow": settings.db_max_overflow,
    "pool_timeout": settings.db_pool_timeout,
}
if settings.db_null_pool if settings.db_null_pool is not None else (
        settings.environment == "test"):
    # Tests use short-lived event loops; pooled asyncpg connections would be
    # bound to a loop that no longer exists.
    from sqlalchemy.pool import NullPool

    _pool_kwargs = {"echo": settings.db_echo, "poolclass": NullPool}

engine: AsyncEngine = create_async_engine(settings.database_url, **_pool_kwargs)
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transactional session per request."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def asyncpg_dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _backfill_document_versions(conn: asyncpg.Connection) -> int:
    """Backfill hashes and the first immutable version row for legacy docs."""
    rows = await conn.fetch(
        """
        SELECT
            d.id, d.workspace_id, d.version, d.title, d.content,
            d.frontmatter::text AS frontmatter, d.namespace,
            d.verification_status, d.trust_level, d.updated_by, d.updated_at,
            d.content_hash,
            EXISTS (
                SELECT 1 FROM document_versions v
                WHERE v.document_id = d.id AND v.version = d.version
            ) AS has_version
        FROM shared_documents d
        WHERE d.content_hash IS NULL
           OR NOT EXISTS (
                SELECT 1 FROM document_versions v
                WHERE v.document_id = d.id AND v.version = d.version
           )
        """
    )
    created = 0
    for row in rows:
        content_hash = hashlib.sha256(row["content"].encode("utf-8")).hexdigest()
        await conn.execute(
            "UPDATE shared_documents SET content_hash = $1 WHERE id = $2",
            content_hash,
            row["id"],
        )
        if row["has_version"]:
            await conn.execute(
                """
                UPDATE document_versions SET content_hash = $1
                WHERE document_id = $2 AND version = $3 AND content_hash IS NULL
                """,
                content_hash,
                row["id"],
                row["version"],
            )
            continue
        await conn.execute(
            """
            INSERT INTO document_versions (
                workspace_id, document_id, version, parent_version, content_hash,
                title, content, frontmatter, namespace, verification_status,
                trust_level, author, session_batch_id, change_summary, created_at
            ) VALUES (
                $1, $2, $3, NULL, $4, $5, $6, $7::jsonb, $8, $9, $10, $11,
                NULL, 'backfilled from existing document', $12
            )
            ON CONFLICT (document_id, version) DO NOTHING
            """,
            row["workspace_id"],
            row["id"],
            row["version"],
            content_hash,
            row["title"],
            row["content"],
            row["frontmatter"] or "{}",
            row["namespace"],
            row["verification_status"],
            row["trust_level"],
            row["updated_by"],
            row["updated_at"],
        )
        created += 1
    return created


async def _ensure_vector_dimension(conn: asyncpg.Connection) -> None:
    """Reconcile pgvector columns with EMBEDDING_DIMENSIONS.

    PostgreSQL cannot cast between vector dimensions, so a dimension change
    drops the HNSW indexes, nulls existing embeddings and recreates the indexes.
    A full reindex is required afterwards; this function only makes the schema
    consistent with the configured provider.
    """
    dimension = int(settings.embedding_dimensions)
    if not 1 <= dimension <= 2000:
        raise RuntimeError("embedding_dimensions must be between 1 and 2000")
    current = await conn.fetchval(
        """
        SELECT format_type(atttypid, atttypmod)
        FROM pg_attribute
        WHERE attrelid = 'shared_documents'::regclass
          AND attname = 'embedding'
          AND NOT attisdropped
        """
    )
    expected = f"vector({dimension})"
    if current == expected:
        return
    logger.warning(
        "embedding dimension change %s -> %s; existing vectors will be cleared "
        "and a full reindex is required",
        current,
        expected,
    )
    await conn.execute("DROP INDEX IF EXISTS idx_docs_embedding")
    await conn.execute("DROP INDEX IF EXISTS idx_document_chunks_embedding")
    await conn.execute(
        f"ALTER TABLE shared_documents ALTER COLUMN embedding "
        f"TYPE vector({dimension}) USING NULL"
    )
    if await conn.fetchval("SELECT to_regclass('document_chunks') IS NOT NULL"):
        await conn.execute(
            f"ALTER TABLE document_chunks ALTER COLUMN embedding "
            f"TYPE vector({dimension}) USING NULL"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding "
            "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
        )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_docs_embedding "
        "ON shared_documents USING hnsw (embedding vector_cosine_ops)"
    )
    await conn.execute(
        "UPDATE shared_documents SET embedding_model = NULL WHERE embedding IS NULL"
    )
    if await conn.fetchval("SELECT to_regclass('document_chunks') IS NOT NULL"):
        await conn.execute(
            "UPDATE document_chunks SET embedding_model = NULL WHERE embedding IS NULL"
        )


_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "sql" / "migrations"
# Applied in order after the 3.4 baseline; append new migrations here.
_INCREMENTAL_MIGRATIONS: list[tuple[str, Path]] = [
    ("3.5.0", _MIGRATIONS_DIR / "v3_5_artifact_binding.sql"),
    ("3.6.0", _MIGRATIONS_DIR / "v3_6_failure_protocol.sql"),
    ("3.7.0", _MIGRATIONS_DIR / "v3_7_tenants.sql"),
    ("3.8.0", _MIGRATIONS_DIR / "v3_8_addresses.sql"),
    ("3.9.0", _MIGRATIONS_DIR / "v3_9_lineage.sql"),
    ("3.10.0", _MIGRATIONS_DIR / "v3_10_review_rounds.sql"),
    ("3.11.0", _MIGRATIONS_DIR / "v3_11_onboarding.sql"),
    ("3.12.0", _MIGRATIONS_DIR / "v3_12_budget.sql"),
    ("3.13.0", _MIGRATIONS_DIR / "v3_13_async_index.sql"),
    ("3.14.0", _MIGRATIONS_DIR / "v3_14_budget_enforce.sql"),
    ("3.15.0", _MIGRATIONS_DIR / "v3_15_leases.sql"),
    ("3.16.0", _MIGRATIONS_DIR / "v3_16_exports.sql"),
    ("3.17.0", _MIGRATIONS_DIR / "v3_17_events.sql"),
    ("3.18.0", _MIGRATIONS_DIR / "v3_18_onboarding_progress.sql"),
    ("3.19.0", _MIGRATIONS_DIR / "v3_19_agent_onboarding.sql"),
    ("3.20.0", _MIGRATIONS_DIR / "v3_20_durability_incidents.sql"),
    ("3.21.0", _MIGRATIONS_DIR / "v3_21_token_tree.sql"),
    ("3.22.0", _MIGRATIONS_DIR / "v3_22_members.sql"),
    ("3.23.0", _MIGRATIONS_DIR / "v3_23_collaboration.sql"),
    ("3.24.0", _MIGRATIONS_DIR / "v3_24_provenance_recall_budgets.sql"),
    ("3.25.0", _MIGRATIONS_DIR / "v3_25_logs_contention_situation.sql"),
    ("3.26.0", _MIGRATIONS_DIR / "v3_26_tenant_tree.sql"),
    ("3.27.0", _MIGRATIONS_DIR / "v3_27_phase3.sql"),
    ("3.28.0", _MIGRATIONS_DIR / "v3_28_phase3_r32.sql"),
    ("3.29.0", _MIGRATIONS_DIR / "v3_29_event_partitions.sql"),
    ("3.30.0", _MIGRATIONS_DIR / "v3_30_releases.sql"),
    ("3.31.0", _MIGRATIONS_DIR / "v3_31_token_delta.sql"),
    ("3.32.0", _MIGRATIONS_DIR / "v3_32_quality_control.sql"),
    ("3.33.0", _MIGRATIONS_DIR / "v3_33_poke_and_seed.sql"),
    ("3.34.0", _MIGRATIONS_DIR / "v3_34_growth.sql"),
    ("3.35.0", _MIGRATIONS_DIR / "v3_35_operator_privacy.sql"),
    ("3.36.0", _MIGRATIONS_DIR / "v3_36_oauth.sql"),
    ("3.37.0", _MIGRATIONS_DIR / "v3_37_poke_realtime.sql"),
    ("3.38.0", _MIGRATIONS_DIR / "v3_38_chunk_embed_hash.sql"),
    ("3.39.0", _MIGRATIONS_DIR / "v3_39_token_naming.sql"),
    ("3.40.0", _MIGRATIONS_DIR / "v3_40_blobs_share_links.sql"),
    ("3.41.0", _MIGRATIONS_DIR / "v3_41_task_invariants.sql"),
    ("3.42.0", _MIGRATIONS_DIR / "v3_42_task_project_repo.sql"),
    ("3.43.0", _MIGRATIONS_DIR / "v3_43_lease_renew.sql"),
    ("3.44.0", _MIGRATIONS_DIR / "v3_44_super_admin.sql"),
    ("3.45.0", _MIGRATIONS_DIR / "v3_45_conversations.sql"),
    ("3.46.0", _MIGRATIONS_DIR / "v3_46_group_conversations.sql"),
    ("3.47.0", _MIGRATIONS_DIR / "v3_47_message_escalations.sql"),
    ("3.48.0", _MIGRATIONS_DIR / "v3_48_commander_grants.sql"),
    ("3.49.0", _MIGRATIONS_DIR / "v3_49_blob_tenants.sql"),
    ("3.50.0", _MIGRATIONS_DIR / "v3_50_presence_states.sql"),
    ("3.51.0", _MIGRATIONS_DIR / "v3_51_diagrams.sql"),
    ("3.52.0", _MIGRATIONS_DIR / "v3_52_diagram_automation.sql"),
    ("3.53.0", _MIGRATIONS_DIR / "v3_53_diagram_autonomy.sql"),
    ("3.54.0", _MIGRATIONS_DIR / "v3_54_deploy_gate.sql"),
    ("3.55.0", _MIGRATIONS_DIR / "v3_55_automation_modes.sql"),
    ("3.56.0", _MIGRATIONS_DIR / "v3_56_owner_acceptance.sql"),
    ("3.57.0", _MIGRATIONS_DIR / "v3_57_deploy_evidence.sql"),
    ("3.58.0", _MIGRATIONS_DIR / "v3_58_escalation_batches.sql"),
    ("3.59.0", _MIGRATIONS_DIR / "v3_59_probe_tokens.sql"),
    ("3.60.0", _MIGRATIONS_DIR / "v3_60_runner_hosts.sql"),
    ("3.61.0", _MIGRATIONS_DIR / "v3_61_runner_placements.sql"),
    ("3.62.0", _MIGRATIONS_DIR / "v3_62_drop_owner_acceptance.sql"),
    ("3.63.0", _MIGRATIONS_DIR / "v3_63_loop_steps.sql"),
    ("3.64.0", _MIGRATIONS_DIR / "v3_64_cli_identities.sql"),
    ("3.65.0", _MIGRATIONS_DIR / "v3_65_host_report.sql"),
    ("3.66.0", _MIGRATIONS_DIR / "v3_66_diagram_policy.sql"),
    ("3.67.0", _MIGRATIONS_DIR / "v3_67_placement_per_step.sql"),
    ("3.68.0", _MIGRATIONS_DIR / "v3_68_cancelled_terminal.sql"),
    ("3.69.0", _MIGRATIONS_DIR / "v3_69_code_projection_cursor.sql"),
    ("3.70.0", _MIGRATIONS_DIR / "v3_70_event_inbox.sql"),
    ("3.71.0", _MIGRATIONS_DIR / "v3_71_projects_metering.sql"),
    ("3.75.0", _MIGRATIONS_DIR / "v3_75_unified_runtime.sql"),
    ("3.76.0", _MIGRATIONS_DIR / "v3_76_event_inbox_tenant.sql"),
    ("3.77.0", _MIGRATIONS_DIR / "v3_77_ledger_tenant.sql"),
    ("3.78.0", _MIGRATIONS_DIR / "v3_78_event_inbox_nullable.sql"),
    ("3.79.0", _MIGRATIONS_DIR / "v3_79_kernel_delivery_leases.sql"),
    ("3.80.0", _MIGRATIONS_DIR / "v3_80_diagram_kernel_binding.sql"),
    ("3.81.0", _MIGRATIONS_DIR / "v3_81_nerve_acks.sql"),
    ("3.82.0", _MIGRATIONS_DIR / "v3_82_deploy_targets.sql"),
    ("3.83.0", _MIGRATIONS_DIR / "v3_83_federation_core.sql"),
    ("3.84.0", _MIGRATIONS_DIR / "v3_84_federation_delegations.sql"),
    ("3.85.0", _MIGRATIONS_DIR / "v3_85_federation_delivery_cancel.sql"),
    ("3.86.0", _MIGRATIONS_DIR / "v3_86_federation_job_unique.sql"),
    ("3.87.0", _MIGRATIONS_DIR / "v3_87_federation_metering.sql"),
    ("3.88.0", _MIGRATIONS_DIR / "v3_88_federation_governance.sql"),
    ("3.89.0", _MIGRATIONS_DIR / "v3_89_federation_remote.sql"),
    ("3.90.0", _MIGRATIONS_DIR / "v3_90_skill_registry.sql"),
    ("3.91.0", _MIGRATIONS_DIR / "v3_91_registry_discovery.sql"),
    ("3.92.0", _MIGRATIONS_DIR / "v3_92_registry_sandbox_updates.sql"),
    ("3.93.0", _MIGRATIONS_DIR / "v3_93_registry_withdrawals.sql"),
    ("3.94.0", _MIGRATIONS_DIR / "v3_94_federation_proofs.sql"),
]
CURRENT_SCHEMA_VERSION = _INCREMENTAL_MIGRATIONS[-1][0]


def _schema_fingerprint(root: Path) -> str:
    """Every schema file plus the vector dimension: unchanged means nothing to do."""
    digest = hashlib.sha256(str(settings.embedding_dimensions).encode())
    for path in sorted((root / "sql").rglob("*.sql")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return "files:" + digest.hexdigest()[:24]


async def apply_schema(dsn: str | None = None) -> None:
    """Apply the canonical schema, run pending migrations, and backfill data.

    A session advisory lock prevents multiple API replicas from racing DDL.
    Fresh databases get sql/init.sql; a v2 database gets the in-place migration;
    v3 databases missing the hardening tables get v3_hardening.sql; every v3
    database then gets the v3.2 production expansion and v3.2.1 improvements.
    """
    root = Path(__file__).resolve().parent.parent
    schema_path = root / "sql" / "init.sql"
    migration_path = root / "sql" / "migrations" / "v2_to_v3_collab.sql"
    hardening_path = root / "sql" / "migrations" / "v3_hardening.sql"
    production_path = root / "sql" / "migrations" / "v3_2_production.sql"
    improvements_path = root / "sql" / "migrations" / "v3_2_1_mcp_improvements.sql"
    feedback_path = root / "sql" / "migrations" / "v3_3_agent_feedback.sql"
    security_path = root / "sql" / "migrations" / "v3_4_security_hardening.sql"
    if not schema_path.exists():
        raise RuntimeError(f"schema file not found: {schema_path}")
    if not hardening_path.exists():
        raise RuntimeError(f"migration file not found: {hardening_path}")
    if not production_path.exists():
        raise RuntimeError(f"migration file not found: {production_path}")
    if not improvements_path.exists():
        raise RuntimeError(f"migration file not found: {improvements_path}")
    if not feedback_path.exists():
        raise RuntimeError(f"migration file not found: {feedback_path}")
    if not security_path.exists():
        raise RuntimeError(f"migration file not found: {security_path}")
    for _, path in _INCREMENTAL_MIGRATIONS:
        if not path.exists():
            raise RuntimeError(f"migration file not found: {path}")

    target = (dsn or settings.database_url).replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(target, timeout=settings.db_pool_timeout)
    try:
        await conn.execute(
            "SELECT pg_advisory_lock(hashtext('agentgraph_schema_v3')::bigint)"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(32) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        # A server starting beside others that already serve this database
        # must not re-run init.sql's DDL under their reads (it could deadlock
        # them): when the schema files are the ones last applied, stop here.
        fingerprint = _schema_fingerprint(root)
        if await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version = $1",
                               fingerprint):
            logger.info("database schema is up to date (%s)", fingerprint)
            return
        has_v2 = await conn.fetchval(
            """
            SELECT to_regclass('memory_crystals') IS NOT NULL
               AND to_regclass('shared_documents') IS NULL
            """
        )
        if has_v2:
            if not migration_path.exists():
                raise RuntimeError(f"migration file not found: {migration_path}")
            logger.info("v2 database detected; applying v2 -> v3 migration")
            await conn.execute(migration_path.read_text(encoding="utf-8"))
            logger.info("v2 -> v3 migration complete")

        hardened = await conn.fetchval(
            "SELECT 1 FROM schema_migrations WHERE version = '3.1.0'"
        )
        production = await conn.fetchval(
            "SELECT 1 FROM schema_migrations WHERE version = '3.2.0'"
        )
        improved = await conn.fetchval(
            "SELECT 1 FROM schema_migrations WHERE version = '3.2.1'"
        )
        has_documents = await conn.fetchval(
            "SELECT to_regclass('shared_documents') IS NOT NULL"
        )
        if has_documents:
            # Existing v3/v2-upgraded tables were created before the hardening
            # columns existed. Add them BEFORE init.sql creates indexes on them.
            if not hardened:
                logger.info("applying v3.1 hardening migration")
                await conn.execute(hardening_path.read_text(encoding="utf-8"))
            await conn.execute(schema_path.read_text(encoding="utf-8"))
        else:
            await conn.execute(schema_path.read_text(encoding="utf-8"))
            if not hardened:
                logger.info("applying v3.1 hardening migration")
                await conn.execute(hardening_path.read_text(encoding="utf-8"))
        if not production:
            logger.info("applying v3.2 production expansion")
            await conn.execute(production_path.read_text(encoding="utf-8"))
        if not hardened:
            backfilled = await _backfill_document_versions(conn)
            if backfilled:
                logger.info("backfilled %s document version(s)", backfilled)
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES ('3.1.0') "
                "ON CONFLICT (version) DO NOTHING"
            )
        else:
            await _backfill_document_versions(conn)
        if not production:
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES ('3.2.0') "
                "ON CONFLICT (version) DO NOTHING"
            )
        if not improved:
            logger.info("applying v3.2.1 MCP improvements")
            await conn.execute(improvements_path.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES ('3.2.1') "
                "ON CONFLICT (version) DO NOTHING"
            )
        await _ensure_vector_dimension(conn)
        feedback_applied = await conn.fetchval(
            "SELECT 1 FROM schema_migrations WHERE version = '3.3.0'"
        )
        if not feedback_applied:
            logger.info("applying v3.3 agent feedback migration")
            await conn.execute(feedback_path.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES ('3.3.0') "
                "ON CONFLICT (version) DO NOTHING"
            )
        security_applied = await conn.fetchval(
            "SELECT 1 FROM schema_migrations WHERE version = '3.4.0'"
        )
        if not security_applied:
            logger.info("applying v3.4 security hardening migration")
            await conn.execute(security_path.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES ('3.4.0') "
                "ON CONFLICT (version) DO NOTHING"
            )
        for version, path in _INCREMENTAL_MIGRATIONS:
            applied = await conn.fetchval(
                "SELECT 1 FROM schema_migrations WHERE version = $1", version
            )
            if applied:
                continue
            logger.info("applying %s migration (%s)", version, path.name)
            await conn.execute(path.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES ($1) "
                "ON CONFLICT (version) DO NOTHING",
                version,
            )
        # Current function definitions win over the ones older migrations set.
        for path in sorted((root / "sql" / "functions").glob("*.sql")):
            await conn.execute(path.read_text(encoding="utf-8"))
        await conn.execute(
            "INSERT INTO schema_migrations(version) VALUES ($1) ON CONFLICT DO NOTHING",
            fingerprint)
        logger.info(
            "database schema is up to date (v%s)", _INCREMENTAL_MIGRATIONS[-1][0]
        )
    finally:
        await conn.close()


async def check_database() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
        return True
    except Exception:  # pragma: no cover - readiness path
        logger.exception("database readiness check failed")
        return False

# Register the tenant RLS session listener.
from app import tenant as _tenant  # noqa: E402,F401

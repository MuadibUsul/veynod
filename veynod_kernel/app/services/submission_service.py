"""Durable asynchronous patch submissions.

This module is the API-side service: accept/duplicate lookup/status only.
It never imports or executes the verification gate.  The independent
``app.worker`` / ``app.services.gate_worker`` process owns pytest/Git and the
work leases.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


logger = logging.getLogger("veynod.submissions")

FINAL_STATUSES = frozenset({"COMMITTED", "FAILED"})


class SubmissionConflict(RuntimeError):
    def __init__(self, code: str, detail: str, existing: Mapping[str, Any] | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.existing = dict(existing) if existing else None


class SubmissionRejected(RuntimeError):
    def __init__(self, code: str, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


def patch_sha256(git_diff: str) -> str:
    return hashlib.sha256(git_diff.encode("utf-8")).hexdigest()


def derive_idempotency_key(
    *,
    task_id: UUID | str,
    runner_id: str,
    lease_token_hash: str,
    patch_digest: str,
) -> str:
    material = f"veynod:v2:{task_id}:{runner_id}:{lease_token_hash}:{patch_digest}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def normalize_idempotency_key(
    raw: str | None,
    *,
    task_id: UUID | str,
    runner_id: str,
    lease_token_hash: str,
    patch_digest: str,
) -> tuple[str, bool]:
    """Return (key, derived).  Opaque client keys are accepted but bounded."""
    if raw is None or not raw.strip():
        return (
            derive_idempotency_key(
                task_id=task_id,
                runner_id=runner_id,
                lease_token_hash=lease_token_hash,
                patch_digest=patch_digest,
            ),
            True,
        )
    key = raw.strip()
    if len(key) > 200:
        raise SubmissionRejected(
            "invalid_idempotency_key", "Idempotency-Key must be at most 200 characters", 422
        )
    if any(ord(char) < 32 for char in key):
        raise SubmissionRejected(
            "invalid_idempotency_key", "Idempotency-Key contains control characters", 422
        )
    return key, False


def _json_load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _status_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    status = str(row["status"])
    submission_id = str(row["submission_id"])
    return {
        "schema_version": "v2",
        "submission_id": submission_id,
        "task_id": str(row["task_id"]),
        "workspace_id": str(row["workspace_id"]),
        "status": status,
        "final": status in FINAL_STATUSES,
        "task_status": row.get("task_status"),
        "result_commit_sha": row.get("result_commit_sha"),
        "merge_mode": row.get("merge_mode"),
        "gate_results": _json_load(row.get("gate_results"), []),
        "merge_timing": _json_load(row.get("merge_timing"), {}),
        "unlocked_downstream_task_ids": [
            str(value) for value in (row.get("unlocked_downstream_task_ids") or [])
        ],
        "error_code": row.get("error_code"),
        "error_detail": row.get("error_detail"),
        "attempts": int(row.get("attempts") or 0),
        "patch_sha256": row.get("patch_sha256"),
        "idempotency_key": row.get("idempotency_key"),
        "correlation": _json_load(row.get("correlation"), {}),
        "created_at": _iso(row.get("created_at")),
        "started_at": _iso(row.get("started_at")),
        "completed_at": _iso(row.get("completed_at")),
        "updated_at": _iso(row.get("updated_at")),
        "status_url": f"/v2/submissions/{submission_id}",
    }


class SubmissionService:
    def __init__(self) -> None:
        pass

    # ---------------------------------------------------------------- accept
    async def accept(
        self,
        session: AsyncSession,
        *,
        runner_id: str,
        task_id: UUID,
        lease_token_hash: str,
        idempotency_key: str | None,
        git_diff: str,
        touched_symbols: list[str],
        local_test_passed: bool,
        existing: dict[str, Any] | None = None,
        correlation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        digest = patch_sha256(git_diff)
        key, derived = normalize_idempotency_key(
            idempotency_key,
            task_id=task_id,
            runner_id=runner_id,
            lease_token_hash=lease_token_hash,
            patch_digest=digest,
        )
        if existing is not None:
            return existing

        task_row = (
            await session.execute(
                text(
                    """
                    SELECT workspace_id, lease_expires_at,
                           status::text AS task_state,
                           lease_token_hash, leased_by_runner,
                           base_commit_sha, snapshot_id
                    FROM dag_tasks
                    WHERE task_id = :task_id
                    """
                ),
                {"task_id": task_id},
            )
        ).mappings().first()
        if task_row is None:
            raise SubmissionRejected("task_not_found", f"Task {task_id} not found", 404)
        now = datetime.now(timezone.utc)
        if (
            task_row["task_state"] != "LEASED"
            or task_row["lease_token_hash"] != lease_token_hash
            or task_row["leased_by_runner"] != runner_id
            or task_row["lease_expires_at"] is None
            or task_row["lease_expires_at"] <= now
        ):
            raise SubmissionRejected(
                "lease_not_active",
                "The task lease is no longer active for this runner.",
                409,
            )

        params = {
            "workspace_id": task_row["workspace_id"],
            "task_id": task_id,
            "runner_id": runner_id,
            "lease_token_hash": lease_token_hash,
            "idempotency_key": key,
            "patch_sha256": digest,
            "git_diff": git_diff,
            "touched_symbols": list(touched_symbols),
            "local_test_passed": local_test_passed,
            "lease_expires_at": task_row["lease_expires_at"],
            "base_sha": task_row["base_commit_sha"],
            "snapshot_id": task_row["snapshot_id"],
            "correlation": json.dumps(correlation or {}, default=str),
        }

        # One open submission per task.  Two workers can briefly hold the same
        # Run (lease handover), and a Kernel Runner can race the Runtime: the
        # second submit must never queue a competing patch for the same task.
        open_row = (
            await session.execute(
                text(
                    """
                    SELECT * FROM patch_submissions
                    WHERE task_id = :task_id
                      AND status IN ('QUEUED', 'RUNNING')
                    ORDER BY created_at ASC
                    LIMIT 1
                    """
                ),
                {"task_id": task_id},
            )
        ).mappings().first()
        if open_row is not None:
            await session.rollback()
            if open_row["patch_sha256"] == digest:
                payload = _status_payload(open_row)
                payload["derived_idempotency_key"] = derived
                payload["deduplicated"] = True
                payload["deduplicated_by_open_submission"] = True
                return payload
            raise SubmissionConflict(
                "submission_in_progress",
                (
                    "This task already has submission "
                    f"{open_row['submission_id']} awaiting the gate; wait for "
                    "its result before submitting a different patch."
                ),
                _status_payload(open_row),
            )

        inserted = await self._insert(session, params)
        if inserted is not None:
            await session.commit()
            payload = _status_payload(inserted)
            payload["derived_idempotency_key"] = derived
            payload["deduplicated"] = False
            return payload

        # Either the key or the (task, lease, patch) binding collided.  Both
        # are the same submission; a key bound to different material is a
        # hard conflict.
        existing = await self._find_by_key(session, key)
        if existing is not None:
            if not self._binding_matches(
                existing,
                runner_id=runner_id,
                task_id=task_id,
                lease_token_hash=lease_token_hash,
                patch_sha256=digest,
            ):
                await session.rollback()
                raise SubmissionConflict(
                    "idempotency_key_conflict",
                    "This Idempotency-Key is already bound to a different "
                    "task, lease identity or patch SHA-256.",
                    existing,
                )
            await session.commit()
            payload = _status_payload(existing)
            payload["derived_idempotency_key"] = derived
            payload["deduplicated"] = True
            return payload

        binding = await self._find_by_binding(
            session,
            task_id=task_id,
            lease_token_hash=lease_token_hash,
            patch_sha256=digest,
        )
        if binding is not None:
            if binding["runner_id"] != runner_id:
                await session.rollback()
                raise SubmissionConflict(
                    "patch_binding_conflict",
                    "This task/lease/patch binding belongs to a different runner.",
                    binding,
                )
            await session.commit()
            payload = _status_payload(binding)
            payload["derived_idempotency_key"] = derived
            payload["deduplicated"] = True
            payload["deduplicated_by_binding"] = True
            return payload

        await session.rollback()
        # Extremely narrow race: a conflicting row vanished.  Retry once.
        inserted = await self._insert(session, params)
        if inserted is None:
            raise SubmissionConflict(
                "submission_conflict",
                "The submission could not be made idempotent under concurrent retries.",
            )
        await session.commit()
        self.wake()
        payload = _status_payload(inserted)
        payload["derived_idempotency_key"] = derived
        payload["deduplicated"] = False
        return payload

    async def _insert(
        self, session: AsyncSession, params: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        result = await session.execute(
            text(
                """
                INSERT INTO patch_submissions (
                    workspace_id, task_id, runner_id, lease_token_hash,
                    idempotency_key, patch_sha256, git_diff, touched_symbols,
                    local_test_passed, lease_expires_at, base_sha, snapshot_id,
                    correlation
                ) VALUES (
                    :workspace_id, :task_id, :runner_id, :lease_token_hash,
                    :idempotency_key, :patch_sha256, :git_diff,
                    CAST(:touched_symbols AS TEXT[]), :local_test_passed,
                    :lease_expires_at, :base_sha, :snapshot_id,
                    CAST(:correlation AS JSONB)
                )
                ON CONFLICT DO NOTHING
                RETURNING submission_id, workspace_id, task_id, runner_id,
                          lease_token_hash, idempotency_key, patch_sha256,
                          status, attempts, task_status, result_commit_sha,
                          merge_mode, gate_results, merge_timing,
                          unlocked_downstream_task_ids, error_code,
                          error_detail, correlation, created_at, updated_at,
                          started_at, completed_at
                """
            ),
            params,
        )
        return result.mappings().first()

    @staticmethod
    def _binding_matches(
        row: Mapping[str, Any],
        *,
        runner_id: str,
        task_id: UUID,
        lease_token_hash: str,
        patch_sha256: str,
    ) -> bool:
        return (
            str(row["task_id"]) == str(task_id)
            and row["runner_id"] == runner_id
            and row["lease_token_hash"] == lease_token_hash
            and row["patch_sha256"] == patch_sha256
        )

    async def _find_by_key(
        self, session: AsyncSession, key: str
    ) -> Mapping[str, Any] | None:
        result = await session.execute(
            text("SELECT * FROM patch_submissions WHERE idempotency_key = :key"),
            {"key": key},
        )
        return result.mappings().first()

    async def _find_by_binding(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        lease_token_hash: str,
        patch_sha256: str,
    ) -> Mapping[str, Any] | None:
        result = await session.execute(
            text(
                """
                SELECT * FROM patch_submissions
                WHERE task_id = :task_id
                  AND lease_token_hash = :lease_token_hash
                  AND patch_sha256 = :patch_sha256
                """
            ),
            {
                "task_id": task_id,
                "lease_token_hash": lease_token_hash,
                "patch_sha256": patch_sha256,
            },
        )
        return result.mappings().first()

    async def find_existing(
        self,
        session: AsyncSession,
        *,
        runner_id: str,
        task_id: UUID,
        lease_token_hash: str,
        patch_sha256: str,
        idempotency_key: str | None,
    ) -> dict[str, Any] | None:
        """Idempotent retry lookup that runs before lease re-validation.

        A retry after the first submission completed must return the original
        result even though the task is no longer LEASED.
        """
        digest = patch_sha256
        key, derived = normalize_idempotency_key(
            idempotency_key,
            task_id=task_id,
            runner_id=runner_id,
            lease_token_hash=lease_token_hash,
            patch_digest=digest,
        )
        row = (
            await session.execute(
                text(
                    """
                    SELECT * FROM patch_submissions
                    WHERE idempotency_key = :key
                       OR (
                            task_id = :task_id
                        AND lease_token_hash = :lease_token_hash
                        AND patch_sha256 = :patch_sha256
                       )
                    ORDER BY (idempotency_key = :key) DESC
                    LIMIT 1
                    """
                ),
                {
                    "key": key,
                    "task_id": task_id,
                    "lease_token_hash": lease_token_hash,
                    "patch_sha256": digest,
                },
            )
        ).mappings().first()
        if row is not None:
            if row["idempotency_key"] == key:
                if not self._binding_matches(
                    row,
                    runner_id=runner_id,
                    task_id=task_id,
                    lease_token_hash=lease_token_hash,
                    patch_sha256=digest,
                ):
                    raise SubmissionConflict(
                        "idempotency_key_conflict",
                        "This Idempotency-Key is already bound to a different "
                        "task, lease identity or patch SHA-256.",
                        row,
                    )
                payload = _status_payload(row)
                payload["derived_idempotency_key"] = derived
                payload["deduplicated"] = True
                return payload
            if row["runner_id"] != runner_id:
                raise SubmissionConflict(
                    "patch_binding_conflict",
                    "This task/lease/patch binding belongs to a different runner.",
                    row,
                )
            payload = _status_payload(row)
            payload["derived_idempotency_key"] = derived
            payload["deduplicated"] = True
            payload["deduplicated_by_binding"] = True
            return payload

        # A different patch under the same lease while its first submission is
        # open. Once the gate picks that submission up the task is VERIFYING,
        # so lease re-validation (which runs next) would refuse with a bare
        # "not LEASED" and the caller would never learn which submission holds
        # the task. Answer here, before lease re-validation, for this lease's
        # holder only; accept() keeps the same guard for everyone else.
        open_row = (
            await session.execute(
                text(
                    """
                    SELECT * FROM patch_submissions
                    WHERE task_id = :task_id
                      AND lease_token_hash = :lease_token_hash
                      AND runner_id = :runner_id
                      AND status IN ('QUEUED', 'RUNNING')
                    ORDER BY created_at ASC
                    LIMIT 1
                    """
                ),
                {
                    "task_id": task_id,
                    "lease_token_hash": lease_token_hash,
                    "runner_id": runner_id,
                },
            )
        ).mappings().first()
        if open_row is not None:
            raise SubmissionConflict(
                "submission_in_progress",
                (
                    "This task already has submission "
                    f"{open_row['submission_id']} awaiting the gate; wait for "
                    "its result before submitting a different patch."
                ),
                _status_payload(open_row),
            )
        return None

    # ------------------------------------------------------------------ query
    async def get_status(
        self,
        session: AsyncSession,
        submission_id: UUID,
        runner_id: str,
    ) -> dict[str, Any] | None:
        result = await session.execute(
            text(
                """
                SELECT * FROM patch_submissions
                WHERE submission_id = :submission_id AND runner_id = :runner_id
                """
            ),
            {"submission_id": submission_id, "runner_id": runner_id},
        )
        row = result.mappings().first()
        return _status_payload(row) if row else None

    async def find_by_key(
        self,
        session: AsyncSession,
        idempotency_key: str,
        runner_id: str,
    ) -> dict[str, Any] | None:
        result = await session.execute(
            text(
                """
                SELECT * FROM patch_submissions
                WHERE idempotency_key = :key AND runner_id = :runner_id
                """
            ),
            {"key": idempotency_key, "runner_id": runner_id},
        )
        row = result.mappings().first()
        return _status_payload(row) if row else None

submission_service = SubmissionService()


def get_submission_service() -> SubmissionService:
    return submission_service


__all__ = [
    "FINAL_STATUSES",
    "SubmissionConflict",
    "SubmissionRejected",
    "SubmissionService",
    "derive_idempotency_key",
    "get_submission_service",
    "normalize_idempotency_key",
    "patch_sha256",
    "submission_service",
]

from __future__ import annotations

from pathlib import Path

import pytest

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

EXPECTED = {
    "v4_002_patch_submissions.sql": "v4_002_patch_submissions",
    "v4_003_gate_work_leases.sql": "v4_003_gate_work_leases",
    "v4_004_merge_coordinator.sql": "v4_004_merge_coordinator",
    "v4_005_admission_outbox_jobs.sql": "v4_005_admission_outbox_jobs",
    "v4_006_admission_permits.sql": "v4_006_admission_permits",
    "v4_007_unlogged_admission_permits.sql": "v4_007_unlogged_admission_permits",
    "v4_008_active_lock_indexes.sql": "v4_008_active_lock_indexes",
    "v4_009_cancellation.sql": "v4_009_cancellation",
    "v4_010_token_workspace.sql": "v4_010_token_workspace",
}


@pytest.mark.parametrize(("filename", "version"), sorted(EXPECTED.items()))
def test_migration_records_its_version(filename: str, version: str) -> None:
    content = (SQL_DIR / filename).read_text(encoding="utf-8")
    assert "INSERT INTO schema_migrations" in content
    assert version in content

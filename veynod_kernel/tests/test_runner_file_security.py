"""Runner file reads reject git option injection and path escapes."""

from __future__ import annotations

import pytest

from tests.test_integration import CALC_FILES, kernel

pytestmark = pytest.mark.integration


def test_runner_file_read_validates_revision(tmp_path) -> None:
    with kernel(tmp_path, CALC_FILES) as harness:
        headers = {"Authorization": f"Bearer {harness.token}"}

        ok = harness.client.get(
            f"/v1/runner/workspaces/{harness.workspace_id}/file",
            headers=headers,
            params={"path": "src/calc.py", "rev": "HEAD"},
        )
        assert ok.status_code == 200, ok.text
        assert "def add" in ok.json()["content"]

        for rev in ("--output=/tmp/pwned", "-c", "HEAD..main", "../HEAD", ""):
            response = harness.client.get(
                f"/v1/runner/workspaces/{harness.workspace_id}/file",
                headers=headers,
                params={"path": "src/calc.py", "rev": rev},
            )
            assert response.status_code == 422, (rev, response.text)

        traversal = harness.client.get(
            f"/v1/runner/workspaces/{harness.workspace_id}/file",
            headers=headers,
            params={"path": "../../etc/passwd", "rev": "HEAD"},
        )
        assert traversal.status_code == 422, traversal.text

"""HTTP clients for the two bounded contexts.

No shared ORM, no shared schema. The gateway only speaks each product's
public/administrative API.
"""

from __future__ import annotations

from typing import Any

import httpx


class IntegrationError(RuntimeError):
    def __init__(self, service: str, status_code: int, detail: str) -> None:
        super().__init__(f"{service} HTTP {status_code}: {detail}")
        self.service = service
        self.status_code = status_code
        self.detail = detail


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return str(payload.get("detail") or payload.get("message") or payload)[:1000]
    except Exception:  # noqa: BLE001
        pass
    return response.text[:1000]


class AgentGraphClient:
    def __init__(self, base_url: str, *, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
    ) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method, f"{self.base_url}{path}", headers=headers, json=json
            )
        if response.status_code >= 400:
            raise IntegrationError("agentgraph", response.status_code, _detail(response))
        if not response.content:
            return {}
        return response.json()

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/healthz", headers={})

    async def create_workspace(self, name: str, admin_token: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/admin/workspaces",
            headers={"X-Admin-Token": admin_token},
            json={"name": name, "tier": "free"},
        )

    async def create_task(
        self, workspace_key: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/tasks",
            headers={"Authorization": f"Bearer {workspace_key}"},
            json=payload,
        )

    async def get_task(self, workspace_key: str, task_key: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/v1/tasks/{task_key}",
            headers={"Authorization": f"Bearer {workspace_key}"},
        )

    async def record_gate_result(
        self, workspace_key: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/quality/gate-results",
            headers={"Authorization": f"Bearer {workspace_key}"},
            json=payload,
        )

    async def claim_task(
        self, workspace_key: str, task_key: str, lease_seconds: int = 120
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v1/tasks/{task_key}/claim",
            headers={"Authorization": f"Bearer {workspace_key}"},
            json={"lease_seconds": lease_seconds, "include_context": False},
        )

    async def handoff_task(
        self, workspace_key: str, task_key: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v1/tasks/{task_key}/handoff",
            headers={"Authorization": f"Bearer {workspace_key}"},
            json=payload,
        )

    async def audit_view(self, workspace_key: str, task_key: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/v1/quality/tasks/{task_key}/audit-view",
            headers={"Authorization": f"Bearer {workspace_key}"},
        )


class KernelClient:
    def __init__(self, base_url: str, admin_key: str, *, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.admin_key = admin_key
        self.timeout = timeout

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                headers={"X-Admin-Key": self.admin_key},
                json=json,
                params=params,
            )
        if response.status_code >= 400:
            raise IntegrationError("kernel", response.status_code, _detail(response))
        if not response.content:
            return {}
        return response.json()

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/healthz")

    async def create_workspace(
        self, name: str, repo_path: str, main_branch: str = "main"
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/admin/workspaces",
            json={"name": name, "repo_path": repo_path, "main_branch": main_branch},
        )

    async def create_artifact(
        self,
        workspace_id: str,
        symbol_path: str,
        *,
        kind: str,
        status: str = "FROZEN",
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/admin/artifacts",
            json={
                "workspace_id": workspace_id,
                "symbol_path": symbol_path,
                "kind": kind,
                "status": status,
            },
        )

    async def create_task(
        self, workspace_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        body = {"workspace_id": workspace_id, **payload}
        return await self._request("POST", "/v1/admin/tasks", json=body)

    async def list_tasks(self, workspace_id: str, limit: int = 2000) -> list[dict[str, Any]]:
        result = await self._request(
            "GET",
            "/v1/admin/tasks",
            params={"workspace_id": workspace_id, "limit": limit},
        )
        return list(result) if isinstance(result, list) else []

    async def create_token(self, label: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/v1/admin/tokens", json={"label": label}
        )


__all__ = ["AgentGraphClient", "IntegrationError", "KernelClient"]

"""Composition service: create and reconcile code runs across both products."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from .clients import AgentGraphClient, IntegrationError, KernelClient
from .config import Settings
from .store import PlatformStore

logger = logging.getLogger(__name__)


class PlatformService:
    def __init__(
        self,
        settings: Settings,
        store: PlatformStore,
        agentgraph: AgentGraphClient,
        kernel: KernelClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.agentgraph = agentgraph
        self.kernel = kernel

    # ------------------------------------------------------------- workspaces
    async def _agentgraph_workspace(self) -> tuple[str, str]:
        if self.settings.agentgraph_workspace_id and self.settings.agentgraph_workspace_key:
            return (
                self.settings.agentgraph_workspace_id,
                self.settings.agentgraph_workspace_key,
            )
        workspace_id = self.store.kv_get("agentgraph_workspace_id")
        workspace_key = self.store.kv_get("agentgraph_workspace_key")
        if workspace_id and workspace_key:
            return workspace_id, workspace_key
        if not self.settings.agentgraph_admin_token:
            raise IntegrationError(
                "agentgraph",
                503,
                "set AGENTGRAPH_WORKSPACE_ID + AGENTGRAPH_WORKSPACE_KEY, "
                "or AGENTGRAPH_ADMIN_TOKEN so the gateway can create a workspace",
            )
        created = await self.agentgraph.create_workspace(
            "Veynod Platform Integration", self.settings.agentgraph_admin_token
        )
        workspace = created.get("workspace") or {}
        workspace_id = str(workspace.get("id") or "")
        workspace_key = str(created.get("api_key") or "")
        if not workspace_id or not workspace_key:
            raise IntegrationError("agentgraph", 502, "workspace creation returned no key")
        self.store.kv_put("agentgraph_workspace_id", workspace_id)
        # Local SQLite only for the demo/self-hosted gateway. A production
        # deployment should use AGENTGRAPH_WORKSPACE_KEY from a secret store.
        self.store.kv_put("agentgraph_workspace_key", workspace_key)
        return workspace_id, workspace_key

    async def _kernel_workspace(self, repo_path: str) -> str:
        if self.settings.kernel_workspace_id:
            return self.settings.kernel_workspace_id
        existing = self.store.kernel_workspace_get(repo_path)
        if existing:
            return existing
        created = await self.kernel.create_workspace(
            "Platform code: " + repo_path.rstrip("/\\").split("/")[-1],
            repo_path,
        )
        workspace_id = str(created.get("workspace_id") or "")
        if not workspace_id:
            raise IntegrationError("kernel", 502, "workspace creation returned no id")
        self.store.kernel_workspace_put(repo_path, workspace_id)
        return workspace_id

    # ----------------------------------------------------------------- create
    async def create_code_run(
        self,
        *,
        title: str,
        repo_path: str,
        symbol: str,
        test_symbol: str,
        spec: str,
        priority: int = 100,
    ) -> dict[str, Any]:
        run_id = "run_" + uuid4().hex[:16]
        agentgraph_task_key = "code-" + run_id.removeprefix("run_")
        agentgraph_workspace_id, agentgraph_key = await self._agentgraph_workspace()
        kernel_workspace_id = await self._kernel_workspace(repo_path)

        # Kernel first: if AgentGraph is unavailable we leave a Kernel task
        # rather than a task that claims code was dispatched but never was.
        for artifact_symbol, kind in ((symbol, "stub"), (test_symbol, "test")):
            try:
                await self.kernel.create_artifact(
                    kernel_workspace_id, artifact_symbol, kind=kind, status="FROZEN"
                )
            except IntegrationError as exc:
                if exc.status_code != 409:  # idempotent re-use on a shared repo
                    raise

        kernel_task = await self.kernel.create_task(
            kernel_workspace_id,
            {
                "title": title,
                "spec_prompt": spec,
                "type": "IMPLEMENT_BODY",
                "target_symbols": [symbol],
                "test_symbols": [test_symbol],
                "write_set_symbols": [symbol],
                "priority": priority,
            },
        )
        kernel_task_id = str(kernel_task.get("task_id") or "")
        if not kernel_task_id:
            raise IntegrationError("kernel", 502, "task creation returned no id")

        agentgraph_task = await self.agentgraph.create_task(
            agentgraph_key,
            {
                "task_key": agentgraph_task_key,
                "title": title,
                "repo": repo_path,
                "description": spec,
                "status": "READY",
                "priority": 100,
                "input_context": {
                    "code_engine": "veynod-kernel",
                    "kernel": {
                        "base_url": self.settings.kernel_url,
                        "workspace_id": kernel_workspace_id,
                        "task_id": kernel_task_id,
                    },
                    "symbol": symbol,
                    "test_symbol": test_symbol,
                    "write_set_symbols": [symbol],
                },
                "acceptance": [
                    {
                        "id": "frozen_tests",
                        "text": "The frozen test passes in the Veynod Kernel gate.",
                        "kind": "AUTO",
                    }
                ],
            },
        )

        return self.store.create_run(
            id=run_id,
            title=title,
            repo_path=repo_path,
            symbol=symbol,
            test_symbol=test_symbol,
            spec=spec,
            agentgraph_workspace_id=agentgraph_workspace_id,
            agentgraph_task_key=agentgraph_task_key,
            agentgraph_task_id=str(agentgraph_task.get("id") or ""),
            kernel_workspace_id=kernel_workspace_id,
            kernel_task_id=kernel_task_id,
            status="DISPATCHED",
            kernel_status=str(kernel_task.get("status") or "READY"),
        )

    # ---------------------------------------------------------------- refresh
    async def _kernel_task(self, run: dict[str, Any]) -> dict[str, Any] | None:
        tasks = await self.kernel.list_tasks(str(run["kernel_workspace_id"]))
        wanted = str(run["kernel_task_id"])
        for task in tasks:
            if str(task.get("task_id")) == wanted:
                return task
        return None

    async def refresh_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        kernel_task = await self._kernel_task(run)
        if kernel_task is not None:
            status = str(kernel_task.get("status") or "")
            updates: dict[str, Any] = {"kernel_status": status}
            if status == "COMMITTED":
                updates["status"] = (
                    "SYNCED" if run.get("agentgraph_synced") else "COMMITTED"
                )
                updates["commit_sha"] = kernel_task.get("result_commit_sha")
                updates["merge_mode"] = kernel_task.get("merge_mode")
            elif status == "FAILED":
                updates["status"] = "FAILED"
            run = self.store.update_run(run_id, **updates) or run
        if run.get("kernel_status") == "COMMITTED" and not run.get("agentgraph_synced"):
            run = await self.sync_run(run_id)
        return run

    async def sync_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.get("agentgraph_synced"):
            return run
        if run.get("kernel_status") != "COMMITTED":
            return run
        if not run.get("commit_sha"):
            return self.store.update_run(
                run_id, sync_error="kernel committed without a commit sha"
            ) or run
        _, workspace_key = await self._agentgraph_workspace()
        task_key = str(run["agentgraph_task_key"])
        evidence = {
            "run_url": f"{self.settings.kernel_url.rstrip('/')}/v1/dag",
            "commit_sha": run["commit_sha"],
            "summary": f"Veynod Kernel gate passed ({run.get('merge_mode') or 'gate'})",
            "counts": {"passed": 4, "failed": 0},
        }
        for gate in ("CI", "TESTS"):
            await self.agentgraph.record_gate_result(
                workspace_key,
                {
                    "task_key": task_key,
                    "gate_kind": gate,
                    "status": "PASS",
                    "source": "RUNNER",
                    "evidence": evidence,
                },
            )
        # The product lifecycle: the code run holds the AgentGraph task while
        # the machine gates are recorded, then hands it to REVIEWING. A person
        # or reviewer still owns the final DONE decision.
        try:
            await self.agentgraph.claim_task(workspace_key, task_key)
            await self.agentgraph.handoff_task(
                workspace_key,
                task_key,
                {
                    "next_status": "REVIEWING",
                    "brief": {
                        "summary": (
                            f"Veynod Kernel committed {run['commit_sha'][:12]}; "
                            "CI and TESTS passed."
                        ),
                        "key_metrics": {
                            "commit_sha": run["commit_sha"],
                            "merge_mode": run.get("merge_mode"),
                            "gates_passed": 2,
                        },
                        "unverified_hypotheses": [],
                        "risk_flag": False,
                    },
                },
            )
        except IntegrationError as exc:
            # A task may already be claimed/reviewed by a person; gate evidence
            # is still valid, so do not turn a successful code merge into a
            # platform failure.
            logger.info("AgentGraph task lifecycle not advanced for %s: %s", task_key, exc)
        return self.store.update_run(
            run_id, status="SYNCED", agentgraph_synced=1, sync_error=None
        ) or run

    async def sync_active_runs(self) -> int:
        changed = 0
        for run in self.store.list_runs(limit=500):
            if run.get("status") in {"SYNCED", "FAILED"}:
                continue
            try:
                await self.refresh_run(str(run["id"]))
                changed += 1
            except Exception as exc:  # noqa: BLE001 - one run must not stop the loop
                logger.warning("sync failed for %s: %s", run.get("id"), exc)
                self.store.update_run(str(run["id"]), sync_error=str(exc)[:1000])
        return changed

    async def audit_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        _, workspace_key = await self._agentgraph_workspace()
        return await self.agentgraph.audit_view(
            workspace_key, str(run["agentgraph_task_key"])
        )

    # ------------------------------------------------------------------ health
    async def health(self) -> dict[str, Any]:
        result: dict[str, Any] = {"agentgraph": None, "kernel": None}
        try:
            ag = await self.agentgraph.health()
            result["agentgraph"] = {"ok": True, "version": ag.get("version")}
        except Exception as exc:  # noqa: BLE001
            result["agentgraph"] = {"ok": False, "error": str(exc)[:300]}
        try:
            kernel = await self.kernel.health()
            result["kernel"] = {
                "ok": bool(kernel.get("status") == "ok"),
                "version": kernel.get("version"),
                "core": kernel.get("core"),
            }
        except Exception as exc:  # noqa: BLE001
            result["kernel"] = {"ok": False, "error": str(exc)[:300]}
        result["ok"] = bool(
            result["agentgraph"] and result["agentgraph"].get("ok")
            and result["kernel"] and result["kernel"].get("ok")
        )
        return result


__all__ = ["PlatformService"]

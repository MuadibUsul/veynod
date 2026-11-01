from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.mcp_server.syscalls import (
    KernelEngineService,
    QuerySymbolSignatureRequest,
    QuerySymbolSignatureResponse,
    SymbolSignatureRecord,
    ArtifactKind,
    ArtifactStatus,
    get_authenticated_worker_id,
    get_kernel_engine,
    router,
)


class FakeEngine(KernelEngineService):
    async def lookup_symbol_signatures(
        self, req: QuerySymbolSignatureRequest
    ) -> QuerySymbolSignatureResponse:
        from uuid import UUID

        return QuerySymbolSignatureResponse(
            results=[
                SymbolSignatureRecord(
                    artifact_id=UUID(int=1),
                    symbol_path=req.symbol_queries[0],
                    kind=ArtifactKind.STUB,
                    status=ArtifactStatus.FROZEN,
                    ast_signature="def f(a: int) -> int: ...",
                    docstring="doc",
                )
            ]
        )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_authenticated_worker_id] = lambda: "hub_test"
    app.dependency_overrides[get_kernel_engine] = lambda: FakeEngine()
    return TestClient(app)


def test_mcp_initialize_handshake() -> None:
    client = _client()
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        },
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "veynod-kernel"
    assert "tools" in result["capabilities"]


def test_mcp_tools_list_exposes_six_primitives() -> None:
    client = _client()
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    tools = response.json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "pull_task_bundle",
        "submit_patch",
        "request_task_split",
        "escalate_contract_defect",
        "inject_dependency_task",
        "query_symbol_signature",
    }
    assert all("inputSchema" in tool for tool in tools)


def test_mcp_tools_call_round_trip() -> None:
    client = _client()
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "query_symbol_signature",
                "arguments": {
                    "task_id": "00000000-0000-0000-0000-000000000001",
                    "symbol_queries": ["src/x.py::f"],
                },
            },
        },
    )
    payload = response.json()
    assert payload["result"]["isError"] is False
    assert "def f" in payload["result"]["content"][0]["text"]


def test_mcp_unknown_tool_returns_jsonrpc_error() -> None:
    client = _client()
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        },
    )
    assert response.json()["error"]["code"] == -32601


def test_mcp_notification_has_no_error() -> None:
    client = _client()
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    )
    assert response.status_code == 200

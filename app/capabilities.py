"""What this deployment can do, for agents reading the root (retest finding 2).

``describe`` lists the feature flags with their current state and every
token-facing REST endpoint that has no MCP tool of the same name, so an
agent reading only the root and ``get_api_docs`` can enumerate the whole
surface and tell "not there" from "switched off". A call to a switched-off
feature answers ``feature_disabled`` (403) naming the flag.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.config import get_settings

settings = get_settings()

# Paths agents never use with their own token (people, operators, login).
# The deploy gate serves GitHub and the VPS runner (v3.50).
_NOT_AGENT_FACING = ("/v1/admin", "/v1/human", "/v1/auth", "/v1/deploy-gate")

_cache: dict[str, Any] = {}


def feature_flags() -> dict[str, dict[str, Any]]:
    return {
        "child_token_issuance": {
            "enabled": settings.child_token_issuance, "setting": "CHILD_TOKEN_ISSUANCE",
            "affects": ["issue_child_token", "POST /v1/tokens"]},
        "quality_control": {
            "enabled": settings.quality_control, "setting": "QUALITY_CONTROL",
            "affects": ["review_task: tiers, gates, audit view, cited clauses",
                        "handoff_task next_status=DONE (tier 2 auto release)"]},
        "budget_enforcement": {
            "enabled": settings.budget_mode == "enforce", "setting": "BUDGET_MODE",
            "value": settings.budget_mode,
            "affects": ["429 rate_limited / budget_exhausted / circuit_open",
                        "shadow mode records would_* only"]},
        "scheduled_exports": {
            "enabled": settings.scheduled_exports, "setting": "SCHEDULED_EXPORTS",
            "affects": ["weekly full + daily incremental exports per tenant"]},
        "retrieval_probe": {
            "enabled": settings.retrieval_probe_enabled, "setting": "RETRIEVAL_PROBE_ENABLED",
            "affects": ["daily self_recall@50 per workspace"]},
        "mcp": {"enabled": settings.mcp_enabled, "setting": "MCP_ENABLED", "affects": ["/mcp/"]},
        "api_reference": {
            "enabled": settings.docs_enabled, "setting": "DOCS_ENABLED",
            "affects": ["/api-reference", "/v1/openapi.json"]},
    }


def _all_routes(routes: list[Any]) -> list[Any]:
    """Flatten included routers (newer FastAPI keeps them as wrappers)."""
    flat: list[Any] = []
    for route in routes:
        nested = getattr(route, "original_router", None)
        flat.extend(_all_routes(nested.routes) if nested is not None else [route])
    return flat


# MCP tools whose REST handler has another name (every other tool shares its
# handler's name). Mapped by handler, not path, so a moved route stays mapped.
TOOL_HANDLERS = {
    "ack_alert": "ack_situation_alert",
    "agent_claim_and_read": "claim_task",
    "agent_escalate_to_human": "escalate_task",
    "agent_handoff_task": "handoff_task",
    "agent_patch_shared_state": "patch_slot",
    "append_entries": "append_log_entries",
    "attach_tenant": "attach_node",
    "clear_taint": "clear_document_taint",
    "create_child_tenant": "create_child_node",
    "dereference_many": "batch_dereference",
    "detach_tenant": "detach_node",
    "draft_external_action": "propose_external_action",
    "export_workspace": "create_export",
    "get_api_docs": "agent_docs",
    "get_budget": "get_token_budget",
    "get_effective_policy": "effective_policy",
    "get_index_status": "index_status",
    "get_lineage_attestation": "lineage_attestation",
    "get_metrics": "get_situation_metrics",
    "get_orientation": "orientation",
    "get_poke_receipts": "poke_receipts",
    "get_tenant_tree": "tenant_tree",
    "get_usage": "workspace_usage",
    "heartbeat": "lease_heartbeat",
    "list_alerts": "list_situation_alerts",
    "list_budgets": "list_token_budgets",
    "move_tenant": "move_node",
    "patch_doc_section": "patch_document_section",
    "propose_document_change": "create_document_proposal",
    "query_workspace": "query_documents",
    "read_entries": "read_log_entries",
    "set_budget": "set_token_budget",
    "set_policy": "set_node_policy",
    "set_status": "set_agent_status",
    "share": "create_share",
    "unsubscribe": "delete_subscription",
    "update_task": "update_task_fields",
    "upsert_many": "batch_upsert_documents",
    "upsert_shared_doc": "upsert_document",
    "vote_on_proposal": "vote_document_proposal",
    "workspace_sync": "workspace_map",
}
# MCP tools with an ``action`` argument: the REST handler of each action.
# Confirming, freezing, revoking and budgets stay REST-only (people decide).
TOOL_ACTION_HANDLERS = {
    "diagram": (
        "list_diagrams", "get_diagram", "create_diagram", "edit_diagram", "dispatch_chunk",
        "submit_chunk", "record_chunk_ci", "gate_chunk", "chunk_triptych", "diagram_queue",
        "queue_check_chunk", "merge_chunk", "propose_diagram_change",
        "request_diagram_increment", "decide_diagram_change", "signal_chunk",
        "escalate_chunk", "chunk_usage", "replay_diagram",
    ),
}
# MCP tools with no REST counterpart, and why.
MCP_ONLY = {
    "batch": "runs several tool calls in one MCP request; over REST send them separately",
    "request_human_review": "records a hand-off event; over REST submit the task or "
                            "proposal and people see it in their inbox",
    **{tool: "agent messaging is MCP-only; people message from the console"
       for tool in ("send_message", "list_conversations", "list_messages", "mark_read",
                    "get_message_receipts", "create_group", "mute_conversation",
                    "block_member")},
}


def tool_map(app: FastAPI) -> list[dict[str, Any]]:
    """Each MCP tool with the REST endpoints that do the same (R4)."""
    from app.root_page import ROOT_PRIMITIVES

    if "tool_map" in _cache:
        return _cache["tool_map"]
    endpoints: dict[str, list[str]] = {}
    for route in _all_routes(app.routes):
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        if not route.path.startswith("/v1/") or route.path.startswith(_NOT_AGENT_FACING):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            endpoints.setdefault(route.endpoint.__name__, []).append(f"{method} {route.path}")
    items = []
    for tool in sorted(ROOT_PRIMITIVES):
        handlers = TOOL_ACTION_HANDLERS.get(tool) or (TOOL_HANDLERS.get(tool, tool),)
        item: dict[str, Any] = {
            "tool": tool,
            "rest": [endpoint for handler in handlers for endpoint in endpoints.get(handler, [])]}
        if tool in MCP_ONLY:
            item["note"] = MCP_ONLY[tool]
        items.append(item)
    _cache["tool_map"] = items
    return items


def rest_only(app: FastAPI) -> list[dict[str, str]]:
    """Token-facing REST endpoints whose handler has no MCP tool of the same name."""
    from app.root_page import ROOT_PRIMITIVES

    tools = set(ROOT_PRIMITIVES)
    items: list[dict[str, str]] = []
    for route in _all_routes(app.routes):
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        if not route.path.startswith("/v1/") or route.path.startswith(_NOT_AGENT_FACING):
            continue
        if route.endpoint.__name__ in tools:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            items.append({"method": method, "path": route.path, "summary": route.summary or ""})
    return sorted(items, key=lambda item: (item["path"], item["method"]))


def describe(app: FastAPI) -> dict[str, Any]:
    """The compact block for the root: flags plus a pointer to the full list."""
    if "value" not in _cache:
        _cache["value"] = {
            "feature_flags": feature_flags(),
            "rest_without_same_named_tool": len(rest_only(app)),
            "details": "get_api_docs(section='capabilities') lists every flag and each REST "
                       "endpoint that has no MCP tool of the same name (some have an MCP "
                       "equivalent under another name); /v1/openapi.json is the full contract",
        }
    return _cache["value"]


def full(app: FastAPI) -> dict[str, Any]:
    """For get_api_docs: flags and every endpoint with method, path and summary."""
    return {"feature_flags": feature_flags(), "rest_without_same_named_tool": rest_only(app),
            "mcp_tools": "tools/list (MCP) or the root's primitives"}

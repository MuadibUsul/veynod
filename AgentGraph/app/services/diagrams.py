"""Diagrams and chunked dispatch (agentgraph/spec-diagram-dispatch v3), lightweight stage.

The planning layer above the task board. A diagram is a set of chunks: each
chunk is one unit of dispatch with an interface contract (ICD), done criteria a
machine can check, dependencies and a write scope. The lifecycle:

    draft --(person confirms: the gate, M-02)--> confirmed
          --(person freezes the baseline, M-03)--> frozen --> chunks dispatched

* Dispatch (M-07) creates one task per chunk. A chunk whose dependencies are
  not merged is not dispatched: it is BLOCKED with the reason; when they merge
  it is dispatched automatically (M-14).
* Gate (M-09, stage 1: recorded by a person or reviewer) and merge (M-10) are
  separate, and the implementer may never merge.
* After confirmation the diagram changes only through a change with an impact
  (M-05). A person decides it, or a super admin decides it for the owner,
  recorded as such (M-32); a person can revoke any decision (M-34). A change
  never rewrites a chunk already in flight: new work becomes a new chunk that
  depends on it (M-33, A-18). The human fast lane (M-30) files such a change
  straight from a request in conversation.
* Every step is a timeline event, so a diagram or a chunk replays end to end
  (M-11), and the diagram text is a versioned shared document (M-01).

Chunk status shown to readers is derived from the chunk and its task, so it
cannot drift from the task board (M-08): the board is the truth.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, can, ensure_namespace
from app.config import get_settings
from app.errors import ApiError, Forbidden, NotFound
from app.models import Workspace
from app.services.common import append_event

logger = logging.getLogger("agentgraph.diagrams")

settings = get_settings()
LAYERS = ("interface", "implementation")
IN_FLIGHT = ("dispatched", "in_progress", "awaiting_gate", "awaiting_ci", "awaiting_review",
             "ready_to_merge", "queued", "verified", "merged")
# Suggested values from the spec (M-22, M-23, M-29), final once the owner confirms.
REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
TRUST_START = 100
TRUST_AFTER_UPGRADE = 80
COST_CI_FAILURE = 10
COST_REVIEW_REJECTION = 20
TRUST_FLOOR = 60
WARN_AFTER_REJECTIONS = 2
BREAK_AFTER_REJECTIONS = 3
QUEUE_WAIT_ALERT_SECONDS = 2 * 3600
SELF_CHECK = ("big", "stable", "machine_checkable", "long")
CHUNK_FIELDS = ("title", "scope", "layer", "icd", "done_criteria", "depends_on",
                "allowed_write_paths", "read_only_interfaces", "commands", "semantic_rules",
                "token_budget")
# M-20: the semantic review checks these three, and only these (plus a chunk's own rules).
DEFAULT_RULES = ("no_cheating", "contract_compliance", "no_architecture_rot")
PANES = ("left", "middle", "right")  # M-21: contract, diff, sandbox evidence
STAGES = ("compile", "lint", "test")  # M-19


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _me(principal: Principal) -> str:
    if principal.is_human and principal.user_id is not None:
        return f"human:{principal.user_id}"
    return principal.agent_signature


def _require_person(principal: Principal, what: str) -> None:
    if not principal.is_human:
        raise Forbidden(f"{what} is a person's decision (spec M-02/M-03)")


def _require_writer(principal: Principal, namespace: str) -> None:
    if not (principal.is_human or can(principal, "task:create")):
        raise Forbidden("drafting a diagram needs task:create")
    if not principal.is_commander and "*" not in principal.allowed_namespaces \
            and namespace not in principal.allowed_namespaces:
        raise Forbidden(f"namespace '{namespace}' is not writable by this token")


# ------------------------------------------------------------------ validation


def _clean_chunk(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ApiError(422, "validation_error", "each chunk is an object")
    key = str(raw.get("chunk_key") or "").strip()
    if not key or len(key) > 64 or not all(ch.isalnum() or ch in "-_." for ch in key):
        raise ApiError(422, "validation_error", "chunk_key is 1-64 letters, digits, - _ .",
                       details={"chunk_key": key})
    layer = raw.get("layer") or "implementation"
    if layer not in LAYERS:
        raise ApiError(422, "validation_error", "layer is interface or implementation",
                       details={"chunk_key": key, "layer": layer})
    criteria = raw.get("done_criteria") or []
    if isinstance(criteria, str):
        criteria = [criteria]
    return {
        "chunk_key": key,
        "title": str(raw.get("title") or key)[:256],
        "scope": str(raw.get("scope") or ""),
        "layer": layer,
        "icd": raw.get("icd") or {},
        "done_criteria": [str(item) for item in criteria if str(item).strip()],
        "depends_on": [str(item) for item in raw.get("depends_on") or []],
        "allowed_write_paths": [str(item) for item in raw.get("allowed_write_paths") or []],
        "read_only_interfaces": [str(item) for item in raw.get("read_only_interfaces") or []],
        "commands": {stage: str(cmd) for stage, cmd in (raw.get("commands") or {}).items()
                     if stage in STAGES and cmd},
        "semantic_rules": [str(item) for item in raw.get("semantic_rules") or []
                           if str(item) not in DEFAULT_RULES],
        "token_budget": int(raw["token_budget"]) if raw.get("token_budget") else None,
    }


def _incomplete(chunk: dict[str, Any]) -> list[str]:
    """M-06: a chunk that cannot be dispatched as an isolated unit.

    ``allowed_write_paths`` is required because it is the only thing that turns
    a chunk into a Kernel WorkScope; without it the Kernel cannot lock or
    validate anything (and would reject the task with 422).
    """
    missing = []
    if not chunk["icd"]:
        missing.append("icd")
    if not chunk["done_criteria"]:
        missing.append("done_criteria")
    if not chunk["allowed_write_paths"]:
        missing.append("allowed_write_paths")
    return missing


def _validate_graph(chunks: list[dict[str, Any]]) -> None:
    keys = [chunk["chunk_key"] for chunk in chunks]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ApiError(422, "validation_error", "chunk keys repeat",
                       details={"duplicates": duplicates})
    problems = {}
    for chunk in chunks:
        missing = _incomplete(chunk)
        unknown = [dep for dep in chunk["depends_on"] if dep not in keys]
        if missing or unknown:
            problems[chunk["chunk_key"]] = {"missing": missing, "unknown_dependencies": unknown}
    if problems:
        raise ApiError(
            422, "chunk_incomplete",
            "every chunk needs an icd, done_criteria and allowed_write_paths, "
            "and known dependencies",
            details={"chunks": problems})
    # No cycles: a chunk that waits on itself would never dispatch.
    graph = {chunk["chunk_key"]: chunk["depends_on"] for chunk in chunks}
    state: dict[str, int] = {}

    def visit(key: str, path: list[str]) -> None:
        if state.get(key) == 2:
            return
        if state.get(key) == 1:
            raise ApiError(422, "dependency_cycle", "chunk dependencies form a cycle",
                           details={"cycle": path + [key]})
        state[key] = 1
        for dep in graph.get(key, []):
            visit(dep, path + [key])
        state[key] = 2

    for key in graph:
        visit(key, [])


def contract_first(chunks: list[dict[str, Any]]) -> list[str]:
    """M-13: the chunks nothing depends on upstream are contracts, never implementation."""
    return sorted(chunk["chunk_key"] for chunk in chunks
                  if chunk.get("state") != "retired" and not chunk["depends_on"]
                  and chunk["layer"] != "interface")


def _require_contract_first(chunks: list[dict[str, Any]]) -> None:
    roots = contract_first(chunks)
    if roots:
        raise ApiError(422, "contract_first", "chunks with no upstream must be interface "
                       "(contract) chunks: give these a contract to depend on (spec M-13)",
                       details={"implementation_roots": roots})


# A done criterion only a person can settle ("owner confirms at M1"): MANUAL on the task.
_PERSON_DECIDES = re.compile(
    r"\b(owner|person|people|human|approv\w*|confirm\w*|sign[- ]?off)\b"
    r"|确认|验收|批准|审批|人工|拍板", re.IGNORECASE)


def acceptance_kind(criterion: str) -> str:
    return "MANUAL" if _PERSON_DECIDES.search(criterion or "") else "AUTO"


def confirm_warnings(diagram: dict[str, Any], chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the person confirming should know: criteria only a person can settle,
    automatic ones with no command to check them, and an unanswered self-check."""
    warnings: list[dict[str, Any]] = []
    risk = self_check_risk(diagram.get("self_check") or {})
    if risk["missing"]:
        warnings.append({"kind": "self_check", "missing": risk["missing"], "risk": risk["risk"],
                         "detail": "self-check not answered yes: " + ", ".join(risk["missing"])})
    for chunk in chunks:
        if chunk["state"] == "retired":
            continue
        kinds = [(criterion, acceptance_kind(criterion)) for criterion in chunk["done_criteria"]]
        manual = [criterion for criterion, kind in kinds if kind == "MANUAL"]
        if manual:
            warnings.append({"kind": "manual_criteria", "chunk_key": chunk["chunk_key"],
                             "criteria": manual,
                             "detail": "a person settles these; the chunk's task marks them "
                                       "MANUAL"})
        unchecked = [criterion for criterion, kind in kinds if kind == "AUTO"]
        if unchecked and not _needs_ci(chunk):
            warnings.append({"kind": "auto_without_commands", "chunk_key": chunk["chunk_key"],
                             "criteria": unchecked,
                             "detail": "no command checks these: add commands, or the "
                                       "reviewer checks them by hand"})
    return warnings


def self_check_risk(self_check: dict[str, Any]) -> dict[str, Any]:
    """M-A-07: big, stable, machine-checkable, long; two or more 'no' is a risk (warn only)."""
    missing = [item for item in SELF_CHECK if self_check.get(item) is not True]
    return {"answers": {item: self_check.get(item) for item in SELF_CHECK},
            "missing": missing, "risk": len(missing) >= 2,
            "note": "the platform warns and does not block (spec section 7)"}


# ------------------------------------------------------------------ storage


async def _diagram(session: AsyncSession, principal: Principal, key: str,
                   *, lock: bool = False) -> dict[str, Any]:
    row = (await session.execute(text(
        "SELECT * FROM diagrams WHERE workspace_id = :ws AND diagram_key = :k"
        + (" FOR UPDATE" if lock else "")),
        {"ws": principal.workspace.id, "k": key})).mappings().one_or_none()
    if row is None:
        raise NotFound("diagram", f"diagram '{key}' not found")
    if not principal.is_commander and "*" not in principal.allowed_namespaces \
            and row["namespace"] not in principal.allowed_namespaces:
        raise NotFound("diagram", f"diagram '{key}' not found")
    return dict(row)


async def _chunks(session: AsyncSession, diagram_id: int) -> list[dict[str, Any]]:
    rows = (await session.execute(text(
        "SELECT * FROM diagram_chunks WHERE diagram_id = :d ORDER BY id"),
        {"d": diagram_id})).mappings().all()
    return [dict(row) for row in rows]


async def _tasks(session: AsyncSession, principal: Principal,
                 keys: list[str]) -> dict[str, dict[str, Any]]:
    if not keys:
        return {}
    rows = (await session.execute(text(
        "SELECT task_key, status::text AS status, claimed_by, lease_owner, handed_off_by, "
        "updated_at, version FROM collab_tasks WHERE workspace_id = :ws "
        "AND task_key = ANY(CAST(:keys AS text[]))"),
        {"ws": principal.workspace.id, "keys": keys})).mappings().all()
    return {row["task_key"]: dict(row) for row in rows}


def _needs_ci(chunk: dict[str, Any]) -> bool:
    """M-19 asks for a sandbox run where the chunk names what to run; a chunk with no
    commands (a design, a document) goes from its submission straight to review."""
    commands = chunk.get("commands") or {}
    return any(commands.get(stage) for stage in STAGES)


def _ci_hold(chunk: dict[str, Any], submission: dict[str, Any]) -> str | None:
    """What CI still holds the current submission at: awaiting_ci, rework, or nothing.
    A run recorded for this commit always counts, even where none was required."""
    ci = chunk.get("ci") or {}
    if ci.get("commit") == submission.get("commit"):
        return None if ci.get("green") else "rework"
    return "awaiting_ci" if _needs_ci(chunk) else None


def status_of(chunk: dict[str, Any], task: dict[str, Any] | None) -> str:
    """The status readers see: the chunk's own record, read through its task."""
    state = chunk["state"]
    if state == "suspended":
        return "blocked_by_spec"  # M-25
    if state in ("pending", "blocked", "merged", "retired"):
        return state
    gate = chunk.get("gate") or {}
    submission = chunk.get("submission") or {}
    if submission:  # the automation path (M-17, M-19, M-20)
        if state == "rework" or not submission.get("accepted"):
            return "rework"
        hold = _ci_hold(chunk, submission)
        if hold:
            return hold
        if gate.get("submission_at") != submission.get("at"):
            return "awaiting_review"
        if gate.get("result") != "pass":
            return "rework"
        # M-27: approved work waits in the serial queue; only a verified head merges.
        check = chunk.get("queue_check") or {}
        if check.get("result") == "pass" and check.get("submission_at") == submission.get("at"):
            return "verified"
        return "queued"
    if task is None:
        return "rework" if state == "rework" else "dispatched"
    status = task["status"]
    gated_at = gate.get("at")
    newer = gated_at is None or (task["updated_at"] and task["updated_at"].isoformat() > gated_at)
    if state == "rework" and not (newer and status in ("REVIEWING", "HANDOFF", "DONE")):
        return "rework"
    if gate.get("result") == "pass" and not newer:
        return "ready_to_merge"
    if status in ("TODO", "READY"):
        return "dispatched"
    if status in ("IN_PROGRESS", "WAITING_HUMAN"):
        return "in_progress"
    if status in ("FAILED", "CANCELLED"):
        return "rework"
    return "awaiting_gate"  # REVIEWING, HANDOFF, AWAITING_HUMAN, ESCALATED, DONE


def _content_hash(chunks: list[dict[str, Any]], layer: str | None = None) -> str:
    material = [{field: chunk[field] for field in ("chunk_key", *CHUNK_FIELDS)}
                for chunk in sorted(chunks, key=lambda item: item["chunk_key"])
                if chunk["state"] != "retired" and (layer is None or chunk["layer"] == layer)]
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str)
                          .encode()).hexdigest()


async def _event(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                 event_type: str, *, targets: list[str] | None = None, **details: Any) -> None:
    """A timeline event; targets are poked (inbox, stream, wake webhook)."""
    payload: dict[str, Any] = {
        "diagram_key": diagram["diagram_key"], "doc_key": diagram["doc_key"],
        "task_key": details.get("task_key"),
        "details": {"diagram_key": diagram["diagram_key"], **details}}
    if targets:
        payload["targets"] = sorted(set(targets))
    await append_event(session, workspace_id=principal.workspace.id, agent=_me(principal),
                       event_type=event_type, namespace=diagram["namespace"], payload=payload)


_WATCHER = re.compile(r"^(agent|human):[A-Za-z0-9_.:@-]{1,150}$")


def _repo(raw: Any) -> str | None:
    """The repository the chunks change (owner/name), passed on to chunk tasks."""
    if raw in (None, ""):
        return None
    repo = str(raw).strip()
    if not REPO.fullmatch(repo):
        raise ApiError(422, "validation_error", "repo is owner/name",
                       details={"field": "repo"})
    return repo


def _watchers(raw: Any) -> list[str]:
    items = [str(item) for item in raw or []]
    bad = [item for item in items if not _WATCHER.match(item)]
    if bad:
        raise ApiError(422, "validation_error", "watchers are agent:<id> or human:<id>",
                       details={"invalid": bad})
    return sorted(set(items))


async def _write_document(session: AsyncSession, principal: Principal,
                          diagram: dict[str, Any], chunks: list[dict[str, Any]]) -> None:
    """M-01: the diagram as a versioned document, one document version per diagram version."""
    from app.schemas import DocumentUpsertRequest
    from app.services.documents import upsert_document

    lines = [f"# {diagram['title']}", "", diagram.get("description") or "", "",
             f"- status: {diagram['status']} · version {diagram['version']}"]
    if diagram.get("baseline"):
        base = diagram["baseline"]
        lines.append(f"- baseline: version {base['version']}, frozen layers "
                     f"{', '.join(base['frozen_layers'])}, hash {base['hash'][:12]}")
    lines += ["", "## Chunks", ""]
    for chunk in chunks:
        if chunk["state"] == "retired":
            continue
        deps = ", ".join(chunk["depends_on"]) or "none"
        lines += [f"### {chunk['chunk_key']} · {chunk['title']} ({chunk['layer']})", "",
                  chunk["scope"] or "", "", f"- depends on: {deps}",
                  f"- done when: {'; '.join(chunk['done_criteria'])}",
                  f"- interface: `{json.dumps(chunk['icd'], ensure_ascii=False)}`", ""]
    current = await session.scalar(text(
        "SELECT version FROM shared_documents WHERE workspace_id = :ws AND doc_key = :k"),
        {"ws": principal.workspace.id, "k": diagram["doc_key"]})
    request = DocumentUpsertRequest(
        title=f"Diagram: {diagram['title']}", content="\n".join(lines).strip() + "\n",
        expected_version=int(current or 0), namespace=diagram["namespace"],
        frontmatter={"kind": "diagram", "diagram_key": diagram["diagram_key"],
                     "diagram_version": diagram["version"], "status": diagram["status"],
                     "chunks": [chunk["chunk_key"] for chunk in chunks
                                if chunk["state"] != "retired"]})
    await upsert_document(session, principal, diagram["doc_key"], request, commit=False)


# ------------------------------------------------------------------ define


async def create(session: AsyncSession, principal: Principal,
                 body: dict[str, Any]) -> dict[str, Any]:
    key = str(body.get("diagram_key") or "").strip()
    if not key or len(key) > 64 or "/" in key or not all(ch.isalnum() or ch in "-_." for ch in key):
        raise ApiError(422, "validation_error", "diagram_key is 1-64 letters, digits, - _ .")
    namespace = str(body.get("namespace") or "default")
    _require_writer(principal, namespace)
    chunks = [_clean_chunk(item) for item in body.get("chunks") or []]
    _validate_graph(chunks)
    exists = await session.scalar(text(
        "SELECT 1 FROM diagrams WHERE workspace_id = :ws AND diagram_key = :k"),
        {"ws": principal.workspace.id, "k": key})
    if exists:
        raise ApiError(409, "diagram_exists", f"diagram '{key}' already exists")
    self_check = {item: body.get("self_check", {}).get(item) for item in SELF_CHECK}
    row = (await session.execute(text(
        "INSERT INTO diagrams (workspace_id, diagram_key, title, namespace, doc_key, "
        "description, self_check, watchers, created_by, repo) VALUES (:ws, :k, :t, :ns, :doc, "
        ":d, CAST(:sc AS jsonb), CAST(:wa AS text[]), :by, :repo) RETURNING *"),
        {"ws": principal.workspace.id, "k": key, "t": str(body.get("title") or key)[:256],
         "repo": _repo(body.get("repo")),
         "ns": namespace, "doc": f"diagrams/{key}", "d": body.get("description"),
         "sc": json.dumps(self_check), "wa": _watchers(body.get("watchers")),
         "by": _me(principal)})).mappings().one()
    diagram = dict(row)
    for chunk in chunks:
        await _insert_chunk(session, principal, diagram, chunk)
    stored = await _chunks(session, diagram["id"])
    await _write_document(session, principal, diagram, stored)
    await _event(session, principal, diagram, "DIAGRAM_CREATED", version=1,
                 chunks=[chunk["chunk_key"] for chunk in chunks])
    await session.commit()
    return await view(session, principal, key)


async def _insert_chunk(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                        chunk: dict[str, Any], *, origin: str = "plan",
                        increment_of: str | None = None) -> None:
    await session.execute(text(
        "INSERT INTO diagram_chunks (workspace_id, diagram_id, chunk_key, title, scope, layer, "
        "icd, done_criteria, depends_on, allowed_write_paths, read_only_interfaces, commands, "
        "semantic_rules, token_budget, origin, increment_of, created_in_version) VALUES (:ws, :d, "
        ":k, :t, :s, :l, CAST(:icd AS jsonb), CAST(:dc AS jsonb), CAST(:deps AS text[]), "
        "CAST(:w AS text[]), CAST(:r AS text[]), CAST(:cmd AS jsonb), CAST(:rules AS text[]), "
        ":tb, :o, :inc, :v)"),
        {"ws": principal.workspace.id, "d": diagram["id"], "k": chunk["chunk_key"],
         "t": chunk["title"], "s": chunk["scope"], "l": chunk["layer"],
         "icd": json.dumps(chunk["icd"]), "dc": json.dumps(chunk["done_criteria"]),
         "deps": chunk["depends_on"], "w": chunk["allowed_write_paths"],
         "r": chunk["read_only_interfaces"], "cmd": json.dumps(chunk["commands"]),
         "rules": chunk["semantic_rules"], "tb": chunk.get("token_budget"), "o": origin,
         "inc": increment_of,
         "v": diagram["version"]})


async def edit_draft(session: AsyncSession, principal: Principal, key: str,
                     body: dict[str, Any]) -> dict[str, Any]:
    """Drafts change freely; after confirmation only through a change (M-05)."""
    diagram = await _diagram(session, principal, key, lock=True)
    _require_writer(principal, diagram["namespace"])
    if diagram["status"] != "draft":
        code = "diagram_frozen" if diagram["status"] == "frozen" else "diagram_confirmed"
        raise ApiError(403, code, "after confirmation the diagram changes only through a "
                       "change proposal",
                       details={"status": diagram["status"], "action": "propose_change",
                                "propose_with": f"POST /v1/diagrams/{key}/changes"})
    chunks = [_clean_chunk(item) for item in body.get("chunks") or []]
    _validate_graph(chunks)
    await session.execute(text("DELETE FROM diagram_chunks WHERE diagram_id = :d"),
                          {"d": diagram["id"]})
    version = diagram["version"] + 1
    diagram = dict((await session.execute(text(
        "UPDATE diagrams SET version = :v, title = COALESCE(:t, title), description = "
        "COALESCE(:desc, description), self_check = COALESCE(CAST(:sc AS jsonb), self_check), "
        "repo = COALESCE(:repo, repo), updated_at = NOW() WHERE id = :id RETURNING *"),
        {"v": version, "t": body.get("title"), "desc": body.get("description"),
         "repo": _repo(body.get("repo")),
         "sc": json.dumps({item: body["self_check"].get(item) for item in SELF_CHECK})
         if isinstance(body.get("self_check"), dict) else None,
         "id": diagram["id"]})).mappings().one())
    for chunk in chunks:
        await _insert_chunk(session, principal, diagram, chunk)
    await _write_document(session, principal, diagram, await _chunks(session, diagram["id"]))
    await _event(session, principal, diagram, "DIAGRAM_EDITED", version=version)
    await session.commit()
    return await view(session, principal, key)


async def set_policy(session: AsyncSession, principal: Principal, key: str,
                     mode: str | None) -> dict[str, Any]:
    """A person sets how far the Loop may go on this diagram (v3.66); None follows
    the workspace. Agents read it with the diagram."""
    from app.services import automation

    _require_person(principal, "setting a diagram's rules")
    if mode is not None and mode not in automation.MODES:
        raise ApiError(422, "validation_error", "mode is manual, semi, full or null",
                       details={"field": "mode"})
    diagram = await _diagram(session, principal, key, lock=True)
    policy = {k: v for k, v in dict(diagram.get("policy") or {}).items() if k != "mode"}
    if mode is not None:
        policy["mode"] = mode
    diagram = dict((await session.execute(text(
        "UPDATE diagrams SET policy = CAST(:p AS jsonb), updated_at = NOW() WHERE id = :id "
        "RETURNING *"), {"p": json.dumps(policy), "id": diagram["id"]})).mappings().one())
    await _event(session, principal, diagram, "DIAGRAM_POLICY_SET", mode=mode)
    await session.commit()
    return {"diagram_key": key, "policy": policy,
            "mode": await automation.mode_for_diagram(session, principal.workspace.id, diagram)}


async def confirm(session: AsyncSession, principal: Principal, key: str) -> dict[str, Any]:
    _require_person(principal, "confirming a diagram")
    diagram = await _diagram(session, principal, key, lock=True)
    if diagram["status"] != "draft":
        raise ApiError(409, "invalid_transition", f"the diagram is already {diagram['status']}")
    chunks = await _chunks(session, diagram["id"])
    if not chunks:
        raise ApiError(422, "validation_error", "a diagram needs at least one chunk")
    _require_contract_first(chunks)
    diagram = dict((await session.execute(text(
        "UPDATE diagrams SET status = 'confirmed', confirmed_by = :by, confirmed_at = NOW(), "
        "updated_at = NOW() WHERE id = :id RETURNING *"),
        {"by": _me(principal), "id": diagram["id"]})).mappings().one())
    await _write_document(session, principal, diagram, chunks)
    warnings = confirm_warnings(diagram, chunks)
    await _event(session, principal, diagram, "DIAGRAM_CONFIRMED", version=diagram["version"],
                 warnings=[item["kind"] for item in warnings])
    await session.commit()
    return {**await view(session, principal, key), "confirm_warnings": warnings}


async def freeze(session: AsyncSession, principal: Principal, key: str,
                 layers: list[str] | None = None) -> dict[str, Any]:
    """M-03 / M-15: freeze the interface layer (file level: its chunks' files)."""
    _require_person(principal, "freezing a baseline")
    diagram = await _diagram(session, principal, key, lock=True)
    if diagram["status"] == "draft":
        raise ApiError(409, "not_confirmed", "a person confirms the diagram before it is frozen")
    layers = layers or ["interface"]
    if any(layer not in LAYERS for layer in layers):
        raise ApiError(422, "validation_error", "layers are interface and/or implementation")
    chunks = await _chunks(session, diagram["id"])
    baseline = _baseline(diagram["version"], chunks, layers, _me(principal))
    diagram = dict((await session.execute(text(
        "UPDATE diagrams SET status = 'frozen', baseline = CAST(:b AS jsonb), updated_at = NOW() "
        "WHERE id = :id RETURNING *"),
        {"b": json.dumps(baseline), "id": diagram["id"]})).mappings().one())
    await _write_document(session, principal, diagram, chunks)
    await _event(session, principal, diagram, "DIAGRAM_FROZEN", version=diagram["version"],
                 baseline=baseline)
    from app.services import automation

    if await automation.mode_for_diagram(session, principal.workspace.id, diagram) == "full":
        # Full automation: the first chunks go out with the freeze; the rest
        # follow their upstream merges (M-14), so nobody dispatches by hand.
        by_key = {chunk["chunk_key"]: chunk for chunk in chunks}
        for chunk in chunks:
            if chunk["state"] == "pending":
                outcome = await _dispatch_one(session, principal, diagram, chunk, by_key)
                by_key[chunk["chunk_key"]] = {**chunk, "state": outcome["state"]}
    await session.commit()
    return await view(session, principal, key)


def _baseline(version: int, chunks: list[dict[str, Any]], layers: list[str],
              by: str) -> dict[str, Any]:
    frozen_files = sorted({path for chunk in chunks if chunk["layer"] in layers
                           and chunk["state"] != "retired"
                           for path in chunk["allowed_write_paths"]})
    return {"version": version, "hash": _content_hash(chunks),
            "interface_hash": _content_hash(chunks, "interface"),
            "frozen_layers": layers, "frozen_files": frozen_files,
            "frozen_at": _now().isoformat(), "frozen_by": by}


# ------------------------------------------------------------------ dispatch


async def dispatch(session: AsyncSession, principal: Principal, key: str,
                   chunk_key: str) -> dict[str, Any]:
    diagram = await _diagram(session, principal, key, lock=True)
    _require_writer(principal, diagram["namespace"])
    if diagram["status"] != "frozen":
        raise ApiError(409, "not_frozen", "only a confirmed and frozen diagram is dispatched",
                       details={"status": diagram["status"]})
    chunks = {chunk["chunk_key"]: chunk for chunk in await _chunks(session, diagram["id"])}
    chunk = chunks.get(chunk_key)
    if chunk is None or chunk["state"] == "retired":
        raise NotFound("chunk", f"chunk '{chunk_key}' not found in '{key}'")
    if chunk["state"] not in ("pending", "blocked"):
        raise ApiError(409, "already_dispatched", f"chunk '{chunk_key}' is {chunk['state']}",
                       details={"task_key": chunk["task_key"]})
    result = await _dispatch_one(session, principal, diagram, chunk, chunks)
    await session.commit()
    return result


# What the graph itself blocks on. A merge re-checks these; a chunk blocked by its
# circuit breaker or token budget waits for a person instead (M-22, M-24).
GRAPH_BLOCKS = ("dependency", "file_lock")


def _waiting_on(chunk: dict[str, Any], chunks: dict[str, dict[str, Any]]) -> tuple[list[str], str]:
    """The dependencies not merged yet, and the reason in words, from current states."""
    waiting = [dep for dep in chunk["depends_on"]
               if chunks.get(dep, {}).get("state") != "merged"]
    return waiting, "waiting on " + ", ".join(
        f"{dep} ({chunks[dep]['state'] if dep in chunks else 'missing'})" for dep in waiting)


async def _provision_kernel_node(
    session: AsyncSession, principal: Principal, diagram: dict[str, Any],
    chunk: dict[str, Any], task_key: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Bind a dispatched chunk to the one Kernel node that will deliver it.

    Returns ``(kernel_task_id, kernel_workspace_id, scope_hash, error)``.  The
    error is returned (not raised) so the chunk stays visible with its reason
    and the operator can see why nothing will execute.
    """
    from app.models import CollabTask
    from app.services import kernel_delivery

    task = (
        await session.execute(
            select(CollabTask).where(
                CollabTask.workspace_id == principal.workspace.id,
                CollabTask.task_key == task_key,
            )
        )
    ).scalar_one_or_none()
    if task is None:
        return None, None, None, "task disappeared before provisioning"
    try:
        # Bind only: the lease belongs to the side that will actually execute.
        record = await kernel_delivery.provision_product_task(
            session, task, lease=False
        )
    except Exception as exc:  # noqa: BLE001 - report, never lose the chunk
        logger.exception("kernel provisioning failed for chunk %s", chunk["chunk_key"])
        return None, None, None, str(exc)[:500]
    if record is None:
        return None, None, None, "no Kernel binding for this repository"
    if not record.get("kernel_task_id"):
        return None, record.get("kernel_workspace_id"), record.get("scope_hash"), str(
            record.get("error") or "Kernel node was not created"
        )[:500]
    return (record.get("kernel_task_id"), record.get("kernel_workspace_id"),
            record.get("scope_hash"), None)


async def _dispatch_one(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                        chunk: dict[str, Any], chunks: dict[str, dict[str, Any]],
                        upgrade: dict[str, Any] | None = None) -> dict[str, Any]:
    waiting, waiting_reason = _waiting_on(chunk, chunks)
    kind, reason, detail = None, None, {}
    if waiting:
        kind = "dependency"
        reason = waiting_reason
        detail = {"waiting_on": waiting}
    else:
        locks = await _lock_conflicts(session, principal, chunk)
        if locks:
            kind = "file_lock"
            reason = "file lock: " + ", ".join(
                f"{item['path']} held by {item['diagram_key']}/{item['chunk_key']}"
                for item in locks)
            detail = {"locked_by": locks}
    if kind:
        await session.execute(text(
            "UPDATE diagram_chunks SET state = 'blocked', block_reason = :r, block_kind = :k, "
            "updated_at = NOW() WHERE id = :id"), {"r": reason, "k": kind, "id": chunk["id"]})
        if chunk["state"] != "blocked" or chunk["block_reason"] != reason:
            await _event(session, principal, diagram, "CHUNK_BLOCKED",
                         chunk_key=chunk["chunk_key"], reason=reason, block_kind=kind, **detail)
        return {"chunk_key": chunk["chunk_key"], "state": "blocked", "block_kind": kind,
                "block_reason": reason, **detail}
    task_key, context_slice = await _create_task(session, principal, diagram, chunk, chunks,
                                                 upgrade)
    # The chunk becomes a Kernel work unit here: a leased worktree plus a frozen
    # WriteScope.  Without that binding the "delivery" was a pull request and a
    # human row flip.  Failing closed keeps the chunk dispatched-but-unplaced
    # instead of silently falling back to the old path.
    kernel_task_id, kernel_workspace, scope_hash, kernel_error = (
        await _provision_kernel_node(session, principal, diagram, chunk, task_key)
    )
    await session.execute(text(
        "UPDATE diagram_chunks SET state = 'dispatched', block_reason = NULL, block_kind = NULL, "
        "task_key = :t, context_slice = CAST(:cs AS jsonb), kernel_task_id = :kt, "
        "kernel_workspace_id = :kw, scope_hash = :sh, updated_at = NOW() WHERE id = :id"),
        {"t": task_key, "cs": json.dumps(context_slice), "id": chunk["id"],
         "kt": kernel_task_id, "kw": kernel_workspace,
         "sh": scope_hash})
    await _event(session, principal, diagram, "CHUNK_DISPATCHED", chunk_key=chunk["chunk_key"],
                 task_key=task_key, version=diagram["version"])
    if kernel_error:
        # Dispatched but not deliverable: visible in the timeline rather than a
        # silent fallback to the old pull-request path.
        await _event(session, principal, diagram, "CHUNK_KERNEL_UNBOUND",
                     chunk_key=chunk["chunk_key"], task_key=task_key, error=kernel_error)
        return {"chunk_key": chunk["chunk_key"], "state": "dispatched",
                "task_key": task_key, "kernel_error": kernel_error}
    return {"chunk_key": chunk["chunk_key"], "state": "dispatched", "task_key": task_key}


def _overlaps(left: str, right: str) -> bool:
    return left == right or fnmatch.fnmatchcase(left, right) or fnmatch.fnmatchcase(right, left)


async def _lock_conflicts(session: AsyncSession, principal: Principal,
                          chunk: dict[str, Any]) -> list[dict[str, Any]]:
    """M-18: a dispatched, unmerged chunk locks its write paths, across diagrams."""
    if not chunk["allowed_write_paths"]:
        return []
    rows = (await session.execute(text(
        "SELECT c.chunk_key, c.allowed_write_paths, d.diagram_key FROM diagram_chunks c "
        "JOIN diagrams d ON d.id = c.diagram_id WHERE c.workspace_id = :ws AND c.id <> :id "
        "AND c.state IN ('dispatched', 'rework')"),
        {"ws": principal.workspace.id, "id": chunk["id"]})).mappings().all()
    conflicts = []
    for row in rows:
        for path in chunk["allowed_write_paths"]:
            held = next((item for item in row["allowed_write_paths"] if _overlaps(path, item)),
                        None)
            if held:
                conflicts.append({"path": path, "held_path": held,
                                  "diagram_key": row["diagram_key"],
                                  "chunk_key": row["chunk_key"]})
    return conflicts


async def _create_task(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                       chunk: dict[str, Any], chunks: dict[str, dict[str, Any]],
                       upgrade: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """M-16 context slicing: the executor gets exactly three things, the chunk's
    description with its acceptance, its direct upstream interfaces, and its
    write scope. No diagram document, no repository, no upstream implementation,
    no other chunk; what was injected and from where is kept on the chunk."""
    from app.schemas import AcceptanceItem, DocRef, TaskCreateRequest
    from app.services.tasks import _create_task_core

    upstream: dict[str, dict[str, Any]] = {}
    context_docs: list[DocRef] = []
    unreadable: list[str] = []
    for dep in chunk["depends_on"]:
        if dep not in chunks:
            continue
        up = chunks[dep]
        upstream[dep] = {"icd": up["icd"], "layer": up["layer"],
                         "read_only_interfaces": up["read_only_interfaces"]}
        delivered = (up.get("merge") or {}).get("delivered") or {}
        if up["layer"] != "interface" or not delivered:
            continue  # M-16: an implementation upstream hands over its contract only
        # An interface chunk's deliverables are the interface: hand them over, pinned.
        upstream[dep]["files"] = delivered.get("files") or []
        upstream[dep]["docs"] = []
        for doc in delivered.get("docs") or []:
            if doc.get("missing"):
                continue
            try:
                ensure_namespace(principal, doc["namespace"])
            except ApiError:
                unreadable.append(doc["uri"])
                continue
            upstream[dep]["docs"].append(doc["uri"])
            if len(context_docs) < 20:
                context_docs.append(DocRef(namespace=doc["namespace"], doc_key=doc["doc_key"],
                                           version=doc["version"]))
    task_key = f"DG-{diagram['diagram_key']}-{chunk['chunk_key']}"[:128]
    taken = await session.scalar(text(
        "SELECT count(*) FROM collab_tasks WHERE workspace_id = :ws AND task_key LIKE :k"),
        {"ws": principal.workspace.id, "k": f"{task_key}%"})
    if taken:
        task_key = f"{task_key}-{int(taken) + 1}"[:128]
    request = TaskCreateRequest(
        task_key=task_key,
        title=(f"[{diagram['diagram_key']}] {chunk['title']}"
               + (f" (escalation {upgrade['escalation']})" if upgrade else ""))[:256],
        # M-23 upgrade: a stronger executor, by name or by capability.
        assignee=(upgrade or {}).get("assignee"),
        required_capabilities=(upgrade or {}).get("required_capabilities") or [],
        description=(f"{chunk['scope']}\n\nInterface contract (ICD):\n"
                     f"{json.dumps(chunk['icd'], ensure_ascii=False, indent=2)}")[:20_000],
        namespace=diagram["namespace"],
        repo=diagram.get("repo"),
        # Chunks are implementation work, whoever dispatches them.
        assigned_role="EXECUTOR",
        acceptance=[AcceptanceItem(id=f"DC-{index + 1}", kind=acceptance_kind(criterion),
                                   text=criterion[:2_000])
                    for index, criterion in enumerate(chunk["done_criteria"][:50])],
        done_definition="All done criteria pass, the gate passes, and someone other than "
                        "the implementer merges (spec M-09/M-10).",
        context_docs=context_docs,
        allow_missing_context=True,
        input_context={
            "diagram": {"key": diagram["diagram_key"], "version": diagram["version"],
                        "baseline": (diagram.get("baseline") or {}).get("version")},
            "chunk": chunk["chunk_key"], "layer": chunk["layer"], "icd": chunk["icd"],
            "upstream_interfaces": upstream,
            "allowed_write_paths": chunk["allowed_write_paths"],
            "read_only_interfaces": chunk["read_only_interfaces"],
            "commands": chunk["commands"],
            "trust_score": chunk.get("trust_score", TRUST_START),
            "token_budget": chunk.get("token_budget"),
            "escalation": upgrade,
            "submit_with": f"POST /v1/diagrams/{diagram['diagram_key']}/chunks/"
                           f"{chunk['chunk_key']}/submit",
            "next_steps": _next_steps(diagram, chunk),
        },
    )
    task = await _create_task_core(session, principal, request, agent=_me(principal),
                                   batch=uuid.uuid4(), commit=False, by_graph=True)
    baseline = (diagram.get("baseline") or {}).get("version")
    context_slice = {
        "description_and_acceptance": {"source": f"chunk {chunk['chunk_key']}",
                                       "acceptance": len(chunk["done_criteria"])},
        "upstream_interfaces": {dep: {"source": f"chunk {dep} at baseline {baseline}",
                                      "files": [*upstream[dep]["read_only_interfaces"],
                                                *(item["path"] for item in
                                                  upstream[dep].get("files") or [])],
                                      "docs": upstream[dep].get("docs") or []}
                                for dep in upstream},
        "unreadable_upstream_docs": unreadable,
        "write_scope": chunk["allowed_write_paths"],
        "not_injected": ["repository", "upstream implementation", "other chunks",
                         "diagram document"],
    }
    return task.task_key, context_slice


def _next_steps(diagram: dict[str, Any], chunk: dict[str, Any]) -> list[str]:
    """How a chunk finishes, in order, with the calls (the task and the chunk each
    have a lifecycle; this is the one path through both)."""
    key, chunk_key = diagram["diagram_key"], chunk["chunk_key"]
    base = f"/v1/diagrams/{key}/chunks/{chunk_key}"
    commands = ", ".join(f"{stage}: {cmd}" for stage, cmd in (chunk["commands"] or {}).items()
                         if cmd)
    return [
        "1. Claim this task; delivered upstream documents arrive in its context.",
        f'2. Submit: diagram(action="submit", diagram_key="{key}", chunk_key="{chunk_key}", '
        'args={"files": [{"path", "content"}] or paths with "commit", "self_check": '
        f'[{{"cmd", "exit_code", "output"}}]}}) (REST POST {base}/submit). Only '
        f"{', '.join(chunk['allowed_write_paths']) or 'the allowed paths'} may change.",
        f"3. CI: someone else runs {commands}." if _needs_ci(chunk)
        else "3. CI: none, this chunk names no commands; the submission goes to review.",
        '4. Review and gate: someone else (diagram action="review"); a rejection names the '
        "pane that disagrees and comes back to you.",
        '5. Merge: someone else (diagram action="merge"); it unblocks the chunks after this '
        "one and hands them what you delivered.",
        '6. After submitting, hand this task off: agent_handoff_task(next_status="REVIEWING", '
        "brief=...).",
    ]


# ------------------------------------------------------------------ gate and merge


async def _implementers(session: AsyncSession, principal: Principal,
                        task: dict[str, Any] | None, task_key: str | None) -> set[str]:
    names: set[str] = set()
    if task:
        names.update(name for name in (task["claimed_by"], task["lease_owner"],
                                       task["handed_off_by"]) if name)
    if task_key:
        rows = await session.execute(text(
            "SELECT DISTINCT l.holder FROM leases l JOIN collab_tasks t ON "
            "t.canonical_id = l.resource_ref WHERE t.workspace_id = :ws AND t.task_key = :k "
            "AND l.resource_kind = 'task'"), {"ws": principal.workspace.id, "k": task_key})
        names.update(row[0] for row in rows if row[0])
    # Every name of the same directory entry counts as the implementer.
    if names:
        rows = (await session.execute(text(
            "SELECT agent_id, principal_id, signatures FROM agent_directory WHERE "
            "workspace_id = :ws AND (agent_id = ANY(CAST(:n AS text[])) OR principal_id = "
            "ANY(CAST(:n AS text[])) OR signatures && CAST(:n AS text[]))"),
            {"ws": principal.workspace.id, "n": sorted(names)})).mappings().all()
        for row in rows:
            names.update([row["agent_id"], row["principal_id"], *(row["signatures"] or [])])
    return names


async def _chunk_with_task(session: AsyncSession, principal: Principal, key: str,
                           chunk_key: str) -> tuple[dict, dict, dict | None]:
    diagram = await _diagram(session, principal, key, lock=True)
    row = (await session.execute(text(
        "SELECT * FROM diagram_chunks WHERE diagram_id = :d AND chunk_key = :k FOR UPDATE"),
        {"d": diagram["id"], "k": chunk_key})).mappings().one_or_none()
    if row is None:
        raise NotFound("chunk", f"chunk '{chunk_key}' not found in '{key}'")
    chunk = dict(row)
    task = (await _tasks(session, principal, [chunk["task_key"]])).get(chunk["task_key"]) \
        if chunk["task_key"] else None
    return diagram, chunk, task


async def gate(session: AsyncSession, principal: Principal, key: str, chunk_key: str,
               body: dict[str, Any]) -> dict[str, Any]:
    """M-09, stage 1: the gate result is recorded by a person or reviewer, never the implementer."""
    result = body.get("result")
    if result not in ("pass", "fail"):
        raise ApiError(422, "validation_error", "result is pass or fail")
    evidence = body.get("evidence")
    if result == "fail" and not evidence:
        raise ApiError(422, "validation_error", "a failed gate carries its evidence (M-09)")
    diagram, chunk, task = await _chunk_with_task(session, principal, key, chunk_key)
    if chunk["state"] not in ("dispatched", "rework"):
        raise ApiError(409, "invalid_transition", f"chunk '{chunk_key}' is {chunk['state']}")
    if _me(principal) in await _implementers(session, principal, task, chunk["task_key"]):
        raise Forbidden("the implementer does not gate their own chunk")
    record = {"result": result, "evidence": evidence, "checks": body.get("checks") or [],
              "by": _me(principal), "at": _now().isoformat(),
              "task_version": task["version"] if task else None}
    automated = bool(chunk.get("submission"))
    if automated:
        record.update(_semantic_review(chunk, body, result))
    rejections = 0 if result == "pass" else chunk["review_reject_count"] + 1
    await session.execute(text(
        "UPDATE diagram_chunks SET gate = CAST(:g AS jsonb), state = :s, "
        "review_reject_count = :n, queued_at = :q, queue_check = NULL, updated_at = NOW() "
        "WHERE id = :id"),
        {"g": json.dumps(record), "s": "dispatched" if result == "pass" else "rework",
         "n": rejections, "q": _now() if result == "pass" and automated else None,
         "id": chunk["id"]})
    await _event(session, principal, diagram, "CHUNK_GATED", chunk_key=chunk_key,
                 task_key=chunk["task_key"], result=result, evidence=evidence,
                 inconsistent_pane=record.get("inconsistent_pane"))
    breaker = None
    if result == "fail":
        trust = await _charge(session, principal, diagram, chunk, COST_REVIEW_REJECTION,
                              "review rejection")
        breaker = await _after_rejection(session, principal, diagram, chunk, rejections, trust)
    elif automated:
        await _event(session, principal, diagram, "CHUNK_QUEUED", chunk_key=chunk_key,
                     task_key=chunk["task_key"])
    await session.commit()
    if result == "pass":
        status = "queued" if automated else "ready_to_merge"
    else:
        status = "blocked" if breaker else "rework"
    return {"chunk_key": chunk_key, "gate": record, "status": status,
            "review_reject_count": rejections, "circuit": breaker}


def _semantic_review(chunk: dict[str, Any], body: dict[str, Any], result: str) -> dict[str, Any]:
    """M-20 / M-21. Only a green sandbox wakes the review; the review answers the
    three default checks plus the chunk's own rules, nothing else (no compile or
    style re-checks); a rejection names the pane of the triptych that disagrees."""
    submission = chunk.get("submission") or {}
    ci = chunk.get("ci") or {}
    if not submission.get("accepted"):
        raise ApiError(409, "submission_rejected", "the latest submission broke its write "
                       "scope and never reached review (spec M-17)")
    if _ci_hold(chunk, submission):
        raise ApiError(409, "ci_not_green", "review starts only on a green sandbox run for "
                       "the current submission (spec M-19)",
                       details={"ci": {"commit": ci.get("commit"), "green": ci.get("green")},
                                "submission": submission.get("commit")})
    rules = list(DEFAULT_RULES) + list(chunk.get("semantic_rules") or [])
    checks = body.get("semantic")
    if not isinstance(checks, dict):
        raise ApiError(422, "validation_error", "semantic is an object with one entry per "
                       "review rule", details={"rules": rules})
    extra = sorted(set(checks) - set(rules))
    missing = [rule for rule in rules if rule not in checks]
    if extra or missing:
        raise ApiError(422, "review_out_of_scope", "the review answers exactly its rules: "
                       "the sandbox already covered compilation and tests (spec M-20)",
                       details={"rules": rules, "extra": extra, "missing": missing})
    answers = {}
    for rule in rules:
        item = checks[rule] if isinstance(checks[rule], dict) else {"result": checks[rule]}
        if item.get("result") not in ("pass", "fail"):
            raise ApiError(422, "validation_error", f"{rule}: result is pass or fail")
        answers[rule] = {"result": item["result"], "note": item.get("note")}
    failing = [rule for rule, item in answers.items() if item["result"] == "fail"]
    if (result == "pass") == bool(failing):
        raise ApiError(422, "validation_error", "result is fail exactly when a rule fails",
                       details={"failing": failing})
    pane = body.get("inconsistent_pane")
    if result == "fail" and pane not in PANES:
        raise ApiError(422, "validation_error", "a rejection names the pane that disagrees: "
                       "left (contract), middle (diff) or right (sandbox evidence) (spec M-21)")
    return {"semantic": answers, "inconsistent_pane": pane if result == "fail" else None,
            "submission_at": submission.get("at"), "commit": submission.get("commit")}


def _outside_scope(chunk: dict[str, Any], diagram: dict[str, Any],
                   files: list[str]) -> list[dict[str, str]]:
    """M-17 / M-15: files outside the white list, or frozen interface files."""
    frozen = set((diagram.get("baseline") or {}).get("frozen_files") or [])
    problems = []
    for path in files:
        if not any(fnmatch.fnmatchcase(path, allowed) for allowed in chunk["allowed_write_paths"]):
            problems.append({"path": path, "why": "outside allowed_write_paths"})
        elif chunk["layer"] != "interface" and any(fnmatch.fnmatchcase(path, item)
                                                   for item in frozen):
            problems.append({"path": path, "why": "frozen interface file"})
    return problems


async def submit(session: AsyncSession, principal: Principal, key: str, chunk_key: str,
                 body: dict[str, Any]) -> dict[str, Any]:
    """M-17: the implementer's submission (changed files, commit, diff). A file
    outside the write scope vetoes it at once: back to the implementer with the
    list, and no reviewer is ever woken (zero review cost)."""
    files, contents, refs = _submitted_files(body.get("files"))
    commit = str(body.get("commit") or "").strip()
    if not commit and (contents or refs):
        # No repository needed: the files' own digest names the submission.
        digest = hashlib.sha256(json.dumps(sorted(
            [(path, hashlib.sha256(text_.encode()).hexdigest()) for path, text_ in contents.items()]
            + [(path, ref) for path, ref in refs.items()])).encode()).hexdigest()
        commit = f"content:{digest[:16]}"
    if not files or not commit:
        raise ApiError(422, "validation_error", "a submission lists its changed files and commit "
                       "(or carries the files' contents)")
    diagram, chunk, task = await _chunk_with_task(session, principal, key, chunk_key)
    if chunk["state"] not in ("dispatched", "rework"):
        raise ApiError(409, "invalid_transition", f"chunk '{chunk_key}' is {chunk['state']}")
    implementers = await _implementers(session, principal, task, chunk["task_key"])
    if not principal.is_human and _me(principal) not in implementers:
        raise Forbidden("only the chunk's implementer submits it")
    violations = _outside_scope(chunk, diagram, files)
    # Out-of-scope work is never stored: the veto costs nothing (M-17).
    stored = [] if violations else await _store_files(session, principal, diagram, chunk_key,
                                                      commit, contents, refs)
    record = {"files": files, "commit": commit, "branch": body.get("branch"),
              "hashes": stored, "docs": [item["doc"] for item in stored],
              "diff": (body.get("diff") or "")[:200_000], "diff_url": body.get("diff_url"),
              "self_check": _self_check(body.get("self_check")),
              "by": _me(principal), "at": _now().isoformat(), "accepted": not violations,
              "violations": violations}
    await session.execute(text(
        "UPDATE diagram_chunks SET submission = CAST(:sub AS jsonb), state = :s, gate = NULL, "
        "updated_at = NOW() WHERE id = :id"),
        {"sub": json.dumps(record), "s": "rework" if violations else "dispatched",
         "id": chunk["id"]})
    if violations:
        await _event(session, principal, diagram, "CHUNK_SUBMISSION_REJECTED",
                     chunk_key=chunk_key, task_key=chunk["task_key"], commit=commit,
                     violations=violations, targets=sorted(implementers - {_me(principal)})
                     or None)
    else:
        await _event(session, principal, diagram, "CHUNK_SUBMITTED", chunk_key=chunk_key,
                     task_key=chunk["task_key"], commit=commit, files=len(files))
    await session.commit()
    return {"chunk_key": chunk_key, "accepted": not violations, "violations": violations,
            "commit": commit, "stored": stored,
            "status": "rework" if violations else (
                "awaiting_ci" if _needs_ci(chunk) else "awaiting_review"),
            "commands": chunk["commands"]}


_STORED_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$")
MAX_STORED_FILES = 50


def _submitted_files(raw: Any) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Paths, as before, or {path, content} / {path, doc} for a diagram with no
    repository: the platform keeps the files and their hashes itself."""
    paths: list[str] = []
    contents: dict[str, str] = {}
    refs: dict[str, str] = {}
    for item in raw or []:
        if isinstance(item, dict):
            path = str(item.get("path") or "").strip()
            if not path:
                raise ApiError(422, "validation_error", "each file names its path")
            if "content" in item:
                content = item["content"]
                if not _STORED_PATH.fullmatch(path):
                    raise ApiError(422, "validation_error", f"{path}: a stored file has a "
                                   "plain relative path (letters, digits, _ . - and /)")
                if not isinstance(content, str) or not content:
                    raise ApiError(422, "validation_error", f"{path}: content is non-empty text")
                if len(content.encode()) > settings.max_document_content_bytes:
                    raise ApiError(422, "validation_error", f"{path}: larger than "
                                   f"{settings.max_document_content_bytes} bytes")
                contents[path] = content
            elif item.get("doc"):
                refs[path] = str(item["doc"])
        else:
            path = str(item).strip()
        if path and path not in paths:
            paths.append(path)
    if len(contents) + len(refs) > MAX_STORED_FILES:
        raise ApiError(422, "validation_error", f"at most {MAX_STORED_FILES} files with content")
    return paths, contents, refs


async def _store_files(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                       chunk_key: str, commit: str, contents: dict[str, str],
                       refs: dict[str, str]) -> list[dict[str, Any]]:
    """Keep each submitted file as a versioned document, byte for byte, beside the
    diagram's own document; a {path, doc} file points at one that exists already."""
    from app.schemas import DocumentUpsertRequest
    from app.services.collab import resolve_ref
    from app.services.documents import upsert_document

    stored = []
    for path, ref in refs.items():
        document = await resolve_ref(session, principal.workspace.id,
                                     {"doc_key": _doc_key(ref) or ref})
        if document is None:
            raise ApiError(422, "validation_error", f"{path}: document {ref} not found")
        stored.append({"path": path, "sha256": document.content_hash,
                       "doc": f"doc://{document.doc_key}", "version": document.version})
    for path, content in contents.items():
        doc_key = f"{diagram['doc_key']}/{chunk_key}/{path}"
        current = await session.scalar(text(
            "SELECT version FROM shared_documents WHERE workspace_id = :ws AND doc_key = :k"),
            {"ws": principal.workspace.id, "k": doc_key})
        digest = hashlib.sha256(content.encode()).hexdigest()
        result = await upsert_document(session, principal, doc_key, DocumentUpsertRequest(
            title=f"{diagram['diagram_key']} · {chunk_key} · {path}"[:256], content=content,
            expected_version=int(current or 0), namespace=diagram["namespace"],
            frontmatter={"kind": "diagram_file", "diagram_key": diagram["diagram_key"],
                         "chunk_key": chunk_key, "path": path, "commit": commit,
                         "sha256": digest}), commit=False, raw_content=True)
        stored.append({"path": path, "sha256": digest, "doc": f"doc://{doc_key}",
                       "version": result.document.version})
    return stored


def _self_check(raw: Any) -> list[dict[str, Any]]:
    """The checks the implementer ran, for the reviewer to see (never a substitute for
    the sandbox where the chunk names commands)."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 10:
        raise ApiError(422, "validation_error", "self_check is a list of at most 10 "
                       "{cmd, exit_code, output}")
    checks = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("cmd") or "").strip():
            raise ApiError(422, "validation_error", "each self_check names its cmd")
        try:
            code = int(item.get("exit_code", 0))
        except (TypeError, ValueError):
            raise ApiError(422, "validation_error", "exit_code is a number") from None
        checks.append({"cmd": str(item["cmd"])[:500], "exit_code": code,
                       "output": str(item.get("output") or "")[:4_000]})
    return checks


async def record_ci(session: AsyncSession, principal: Principal, key: str, chunk_key: str,
                    body: dict[str, Any]) -> dict[str, Any]:
    """M-19: the sandbox runs compile, lint (optional) and test. Any non-zero exit
    returns the chunk to its implementer with stderr verbatim, without waking a
    reviewer; the chunk keeps a short summary, the implementer gets the original."""
    results = body.get("results") or {}
    unknown = sorted(set(results) - set(STAGES))
    if unknown or not isinstance(results, dict):
        raise ApiError(422, "validation_error", "results has compile, lint and test",
                       details={"unknown": unknown})
    missing = [stage for stage in ("compile", "test") if stage not in results]
    if missing:
        raise ApiError(422, "validation_error", "compile and test results are required",
                       details={"missing": missing})
    diagram, chunk, task = await _chunk_with_task(session, principal, key, chunk_key)
    submission = chunk.get("submission") or {}
    if not submission.get("accepted"):
        raise ApiError(409, "no_submission", "the sandbox runs on an accepted submission")
    commit = str(body.get("commit") or submission["commit"])
    if commit != submission["commit"]:
        raise ApiError(409, "stale_ci", "this result is for an older submission",
                       details={"result_commit": commit, "submission": submission["commit"]})
    implementers = await _implementers(session, principal, task, chunk["task_key"])
    if _me(principal) in implementers:
        raise Forbidden("the implementer does not report its own sandbox run")
    stages, failed = {}, []
    for stage, item in results.items():
        item = item if isinstance(item, dict) else {}
        code = int(item.get("exit_code", 1))
        stderr = str(item.get("stderr") or "")
        stages[stage] = {"exit_code": code, "cmd": item.get("cmd") or chunk["commands"].get(stage),
                         "stderr_summary": stderr[:500], "coverage": item.get("coverage")}
        if code != 0:
            failed.append((stage, stderr))
    record = {"commit": commit, "green": not failed, "stages": stages,
              "by": _me(principal), "at": _now().isoformat()}
    await session.execute(text(
        "UPDATE diagram_chunks SET ci = CAST(:ci AS jsonb), state = :s, updated_at = NOW() "
        "WHERE id = :id"),
        {"ci": json.dumps(record), "s": "rework" if failed else "dispatched", "id": chunk["id"]})
    if failed:
        # Straight to the implementer, stderr as the compiler wrote it.
        await _event(session, principal, diagram, "CHUNK_CI_FAILED", chunk_key=chunk_key,
                     task_key=chunk["task_key"], commit=commit,
                     failed=[stage for stage, _ in failed],
                     stderr={stage: text_[:60_000] for stage, text_ in failed},
                     targets=sorted(implementers) or None)
        trust = await _charge(session, principal, diagram, chunk, COST_CI_FAILURE,
                              f"sandbox failure: {', '.join(stage for stage, _ in failed)}")
        if trust < TRUST_FLOOR:
            await _break_circuit(session, principal, diagram, chunk,
                                 f"trust {trust} fell under {TRUST_FLOOR}")
    else:
        await _event(session, principal, diagram, "CHUNK_CI_PASSED", chunk_key=chunk_key,
                     task_key=chunk["task_key"], commit=commit)
    await session.commit()
    return {"chunk_key": chunk_key, "ci": record,
            "status": "rework" if failed else "awaiting_review",
            "review_woken": not failed}


async def triptych(session: AsyncSession, principal: Principal, key: str,
                   chunk_key: str) -> dict[str, Any]:
    """M-21: the three panes a reviewer compares, and nothing else of the repository."""
    diagram, chunk, task = await _chunk_with_task(session, principal, key, chunk_key)
    submission = chunk.get("submission") or {}
    ci = chunk.get("ci") or {}
    return {
        "chunk_key": chunk_key, "status": status_of(chunk, task),
        "left": {"contract": chunk["icd"], "done_criteria": chunk["done_criteria"],
                 "layer": chunk["layer"],
                 "baseline_version": (diagram.get("baseline") or {}).get("version"),
                 "review_rules": list(DEFAULT_RULES) + list(chunk["semantic_rules"])},
        "middle": {"commit": submission.get("commit"), "files": submission.get("files"),
                   "stored": submission.get("hashes") or [],
                   "diff": submission.get("diff"), "diff_url": submission.get("diff_url"),
                   "accepted": submission.get("accepted")},
        "right": {"commit": ci.get("commit"), "green": ci.get("green"),
                  "stages": ci.get("stages"), "ci_required": _needs_ci(chunk),
                  "self_check": submission.get("self_check") or []},
    }


# ------------------------------------------------------------------ trust and circuit


async def _charge(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                  chunk: dict[str, Any], cost: int, why: str) -> int:
    """M-22: trust starts at 100 and only this chunk pays for its own failures."""
    entry = {"cost": cost, "why": why, "at": _now().isoformat()}
    trust = int(await session.scalar(text(
        "UPDATE diagram_chunks SET trust_score = trust_score - :c, trust_log = trust_log || "
        "CAST(:e AS jsonb) WHERE id = :id RETURNING trust_score"),
        {"c": cost, "e": json.dumps([entry]), "id": chunk["id"]}))
    await _event(session, principal, diagram, "CHUNK_TRUST_CHARGED", chunk_key=chunk["chunk_key"],
                 task_key=chunk["task_key"], cost=cost, why=why, trust_score=trust)
    return trust


async def _after_rejection(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                           chunk: dict[str, Any], rejections: int,
                           trust: int) -> dict[str, Any] | None:
    """M-23: two rejections in a row warn the command view; three (or trust under
    60) break the circuit."""
    if rejections >= BREAK_AFTER_REJECTIONS or trust < TRUST_FLOOR:
        reason = (f"{rejections} consecutive review rejections"
                  if rejections >= BREAK_AFTER_REJECTIONS
                  else f"trust {trust} fell under {TRUST_FLOOR}")
        return await _break_circuit(session, principal, diagram, chunk, reason)
    if rejections >= WARN_AFTER_REJECTIONS:
        await _event(session, principal, diagram, "CHUNK_REJECTION_WARNING",
                     chunk_key=chunk["chunk_key"], task_key=chunk["task_key"],
                     rejections=rejections, trust_score=trust,
                     hint="consider upgrading the executor before the circuit breaks",
                     targets=diagram.get("watchers") or None)
    return None


async def _break_circuit(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                         chunk: dict[str, Any], reason: str) -> dict[str, Any]:
    """The implementer loses the chunk (its task is withdrawn); it waits, BLOCKED,
    for an upgrade or the spec. Not an end state: a trigger (M-23)."""
    withdrawn = None
    if chunk["task_key"]:
        withdrawn = await _cancel_task(session, principal, chunk["task_key"],
                                       f"circuit broken: {reason}")
    await session.execute(text(
        "UPDATE diagram_chunks SET state = 'blocked', block_kind = 'circuit', block_reason = :r, "
        "queued_at = NULL, updated_at = NOW() WHERE id = :id"),
        {"r": f"circuit broken: {reason}", "id": chunk["id"]})
    await _event(session, principal, diagram, "CHUNK_CIRCUIT_BROKEN", chunk_key=chunk["chunk_key"],
                 task_key=chunk["task_key"], reason=reason, task_withdrawn=withdrawn,
                 options=["upgrade", "spec"], targets=diagram.get("watchers") or None)
    return {"reason": reason, "task_withdrawn": withdrawn, "options": ["upgrade", "spec"]}


async def _cancel_task(session: AsyncSession, principal: Principal, task_key: str,
                       reason: str) -> bool:
    """Withdraw a chunk's task (and end its lease) unless it already finished."""
    from app.services.task_lifecycle import end_task_lease_fenced, transition_task_fenced

    moved = await transition_task_fenced(
        session, workspace_id=principal.workspace.id, task_key=task_key,
        new_status="CANCELLED",
        expected_status=("TODO", "READY", "IN_PROGRESS", "REVIEWING", "HANDOFF"),
        clear_lease=True, review_feedback=reason[:2000],
    )
    if moved is None:
        return False
    await end_task_lease_fenced(
        session, workspace_id=principal.workspace.id, task_key=task_key,
        epoch=moved["lease_epoch"], reason="revoked")
    return True


def _may_steer(principal: Principal) -> bool:
    """People, super admins and planners (ANALYST) steer the graph."""
    return principal.is_human or principal.is_super_admin or principal.role.value in (
        "ANALYST", "COMMANDER")


async def escalate(session: AsyncSession, principal: Principal, key: str, chunk_key: str,
                   body: dict[str, Any]) -> dict[str, Any]:
    """After a broken circuit (M-23): upgrade (a stronger executor takes the chunk,
    trust restarts at 80, the escalation is counted) or spec (BLOCKED_BY_SPEC, the
    subgraph is suspended and the architect is woken, M-25)."""
    mode = body.get("mode")
    if mode not in ("upgrade", "spec"):
        raise ApiError(422, "validation_error", "mode is upgrade or spec")
    if not _may_steer(principal):
        raise Forbidden("a person, a super admin or a planner escalates a chunk")
    diagram, chunk, task = await _chunk_with_task(session, principal, key, chunk_key)
    if _me(principal) in await _implementers(session, principal, task, chunk["task_key"]):
        raise Forbidden("the implementer does not escalate its own chunk")
    if mode == "spec":
        suspended = await _suspend(session, principal, diagram, chunk,
                                   f"escalated to the spec: {body.get('note') or 'circuit broken'}")
        await session.commit()
        return {"chunk_key": chunk_key, "mode": mode, "suspended": suspended}
    if not (chunk["state"] == "blocked" and chunk["block_kind"] == "circuit"):
        raise ApiError(409, "circuit_closed", "upgrade follows a broken circuit",
                       details={"state": chunk["state"], "block_kind": chunk["block_kind"]})
    await session.execute(text(
        "UPDATE diagram_chunks SET state = 'pending', block_kind = NULL, block_reason = NULL, "
        "trust_score = :t, review_reject_count = 0, escalation_count = escalation_count + 1, "
        "submission = NULL, ci = NULL, gate = NULL, queue_check = NULL, task_key = NULL, "
        "updated_at = NOW() WHERE id = :id"), {"t": TRUST_AFTER_UPGRADE, "id": chunk["id"]})
    chunks = {c["chunk_key"]: c for c in await _chunks(session, diagram["id"])}
    upgrade = {"assignee": body.get("assignee"),
               "required_capabilities": body.get("required_capabilities") or [],
               "note": body.get("note"), "escalation": chunks[chunk_key]["escalation_count"]}
    outcome = await _dispatch_one(session, principal, diagram, chunks[chunk_key], chunks,
                                  upgrade=upgrade)
    await _event(session, principal, diagram, "CHUNK_ESCALATED", chunk_key=chunk_key,
                 task_key=outcome.get("task_key"), mode=mode, previous_task=chunk["task_key"],
                 escalation_count=upgrade["escalation"], trust_score=TRUST_AFTER_UPGRADE,
                 assignee=upgrade["assignee"])
    await session.commit()
    return {"chunk_key": chunk_key, "mode": mode, "dispatch": outcome,
            "trust_score": TRUST_AFTER_UPGRADE, "escalation_count": upgrade["escalation"]}


async def signal(session: AsyncSession, principal: Principal, key: str, chunk_key: str,
                 body: dict[str, Any]) -> dict[str, Any]:
    """M-25 NEED_GRAPH_MUTATION: the executor says the contract cannot work, with
    the reason. It does not change the graph: the subgraph is suspended and the
    architect is woken to re-plan through a change."""
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise ApiError(422, "validation_error", "NEED_GRAPH_MUTATION carries its reason")
    diagram, chunk, task = await _chunk_with_task(session, principal, key, chunk_key)
    if chunk["state"] in ("merged", "retired"):
        raise ApiError(409, "invalid_transition", f"chunk '{chunk_key}' is {chunk['state']}")
    if not principal.is_human and _me(principal) not in await _implementers(
            session, principal, task, chunk["task_key"]):
        raise Forbidden("the chunk's implementer raises NEED_GRAPH_MUTATION")
    record = {"signal": "NEED_GRAPH_MUTATION", "reason": reason, "by": _me(principal),
              "at": _now().isoformat()}
    await session.execute(text(
        "UPDATE diagram_chunks SET graph_mutation_signal = CAST(:g AS jsonb) WHERE id = :id"),
        {"g": json.dumps(record), "id": chunk["id"]})
    suspended = await _suspend(session, principal, diagram, chunk,
                               f"NEED_GRAPH_MUTATION from {chunk_key}: {reason}")
    await session.commit()
    return {"chunk_key": chunk_key, "signal": record, "suspended": suspended}


async def _suspend(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                   root: dict[str, Any], reason: str) -> list[str]:
    """Suspend the root and everything downstream of it that is not merged."""
    chunks = {c["chunk_key"]: c for c in await _chunks(session, diagram["id"])}
    subgraph, frontier = {root["chunk_key"]}, [root["chunk_key"]]
    while frontier:
        current = frontier.pop()
        for other in chunks.values():
            if current in other["depends_on"] and other["chunk_key"] not in subgraph:
                subgraph.add(other["chunk_key"])
                frontier.append(other["chunk_key"])
    suspended = sorted(k for k in subgraph if chunks[k]["state"] not in ("merged", "retired"))
    await session.execute(text(
        "UPDATE diagram_chunks SET state = 'suspended', block_kind = 'spec', block_reason = :r, "
        "queued_at = NULL, updated_at = NOW() WHERE diagram_id = :d "
        "AND chunk_key = ANY(CAST(:keys AS text[]))"),
        {"r": reason, "d": diagram["id"], "keys": suspended})
    await _event(session, principal, diagram, "SUBGRAPH_SUSPENDED", chunk_key=root["chunk_key"],
                 reason=reason, suspended=suspended,
                 targets=sorted({*(diagram.get("watchers") or []),
                                 _ref(diagram["created_by"])}))
    return suspended


def _ref(name: str) -> str:
    return name if name.startswith(("agent:", "human:")) else f"agent:{name}"


async def _resume(session: AsyncSession, principal: Principal,
                  diagram: dict[str, Any]) -> list[dict[str, Any]]:
    """After an approved change: suspended chunks start over on the new graph.
    Old tasks are withdrawn, merged work is untouched, and dispatch re-checks
    dependencies and locks (M-25: the inner loop resumes)."""
    chunks = {c["chunk_key"]: c for c in await _chunks(session, diagram["id"])}
    suspended = [c for c in chunks.values() if c["state"] == "suspended"]
    if not suspended:
        return []
    for chunk in suspended:
        if chunk["task_key"]:
            await _cancel_task(session, principal, chunk["task_key"], "replanned")
        await session.execute(text(
            "UPDATE diagram_chunks SET state = 'pending', block_kind = NULL, block_reason = NULL, "
            "task_key = NULL, submission = NULL, ci = NULL, gate = NULL, queue_check = NULL, "
            "graph_mutation_signal = NULL, review_reject_count = 0, updated_at = NOW() "
            "WHERE id = :id"), {"id": chunk["id"]})
    chunks = {c["chunk_key"]: c for c in await _chunks(session, diagram["id"])}
    outcomes = [await _dispatch_one(session, principal, diagram, chunks[c["chunk_key"]], chunks)
                for c in suspended]
    await _event(session, principal, diagram, "SUBGRAPH_RESUMED",
                 resumed=[o["chunk_key"] for o in outcomes],
                 targets=diagram.get("watchers") or None)
    return outcomes


# ------------------------------------------------------------------ budget


async def report_usage(session: AsyncSession, principal: Principal, key: str, chunk_key: str,
                       body: dict[str, Any]) -> dict[str, Any]:
    """M-22: tokens spent on a chunk. Past its budget it is BLOCKED and the
    command view is told; nothing renews it but a person."""
    tokens = body.get("tokens")
    if not isinstance(tokens, int) or tokens <= 0:
        raise ApiError(422, "validation_error", "tokens is a positive integer")
    diagram, chunk, _task = await _chunk_with_task(session, principal, key, chunk_key)
    consumed = int(await session.scalar(text(
        "UPDATE diagram_chunks SET token_consumed = token_consumed + :t WHERE id = :id "
        "RETURNING token_consumed"), {"t": tokens, "id": chunk["id"]}))
    budget = chunk["token_budget"]
    over = budget is not None and consumed > budget and chunk["state"] in ("dispatched", "rework")
    if over:
        await session.execute(text(
            "UPDATE diagram_chunks SET state = 'blocked', block_kind = 'budget', "
            "block_reason = :r, updated_at = NOW() WHERE id = :id"),
            {"r": f"token budget spent: {consumed} of {budget}", "id": chunk["id"]})
        await _event(session, principal, diagram, "CHUNK_BUDGET_EXCEEDED", chunk_key=chunk_key,
                     task_key=chunk["task_key"], consumed=consumed, budget=budget,
                     note="not renewed automatically; a person raises the budget",
                     targets=sorted({*(diagram.get("watchers") or []), "human"}))
    await session.commit()
    return {"chunk_key": chunk_key, "token_consumed": consumed, "token_budget": budget,
            "blocked": over}


async def set_budget(session: AsyncSession, principal: Principal, key: str, chunk_key: str,
                     body: dict[str, Any]) -> dict[str, Any]:
    _require_person(principal, "renewing a token budget")
    budget = body.get("token_budget")
    if budget is not None and (not isinstance(budget, int) or budget <= 0):
        raise ApiError(422, "validation_error", "token_budget is a positive integer or null")
    diagram, chunk, _task = await _chunk_with_task(session, principal, key, chunk_key)
    resume = chunk["state"] == "blocked" and chunk["block_kind"] == "budget" and (
        budget is None or chunk["token_consumed"] <= budget)
    await session.execute(text(
        "UPDATE diagram_chunks SET token_budget = :b, state = CASE WHEN :resume THEN "
        "'dispatched' ELSE state END, block_kind = CASE WHEN :resume THEN NULL ELSE block_kind "
        "END, block_reason = CASE WHEN :resume THEN NULL ELSE block_reason END, "
        "updated_at = NOW() WHERE id = :id"),
        {"b": budget, "resume": resume, "id": chunk["id"]})
    await _event(session, principal, diagram, "CHUNK_BUDGET_SET", chunk_key=chunk_key,
                 token_budget=budget, resumed=resume)
    await session.commit()
    return {"chunk_key": chunk_key, "token_budget": budget, "resumed": resume}


# ------------------------------------------------------------------ merge queue


async def _queue(session: AsyncSession, diagram_id: int) -> list[dict[str, Any]]:
    rows = (await session.execute(text(
        "SELECT * FROM diagram_chunks WHERE diagram_id = :d AND queued_at IS NOT NULL "
        "AND state = 'dispatched' ORDER BY queued_at, id"), {"d": diagram_id})).mappings().all()
    return [dict(row) for row in rows]


async def _queue_position(session: AsyncSession, chunk: dict[str, Any]) -> int | None:
    for index, row in enumerate(await _queue(session, chunk["diagram_id"])):
        if row["id"] == chunk["id"]:
            return index + 1
    return None


async def queue(session: AsyncSession, principal: Principal, key: str) -> dict[str, Any]:
    """M-27 / M-29: the serial queue, its head, and how long each has waited."""
    diagram = await _diagram(session, principal, key)
    now = _now()
    items = []
    for index, row in enumerate(await _queue(session, diagram["id"])):
        waited = int((now - row["queued_at"]).total_seconds())
        items.append({"position": index + 1, "chunk_key": row["chunk_key"],
                      "task_key": row["task_key"], "queued_at": _iso(row["queued_at"]),
                      "waiting_seconds": waited,
                      "overdue": waited > QUEUE_WAIT_ALERT_SECONDS,
                      "status": status_of(row, None), "head": index == 0})
    return {"diagram_key": key, "items": items,
            "head": items[0]["chunk_key"] if items else None,
            "rule": "one at a time: the controller rebases and re-tests the head; someone "
                    "other than its implementer merges it (spec M-27, M-28)"}


async def queue_check(session: AsyncSession, principal: Principal, key: str, chunk_key: str,
                      body: dict[str, Any]) -> dict[str, Any]:
    """The queue controller's result for the head: rebased on main and the
    incremental tests passed, or sent back (rebase conflict, failing tests)."""
    result = body.get("result")
    if result not in ("pass", "rebase_conflict", "test_failed"):
        raise ApiError(422, "validation_error", "result is pass, rebase_conflict or test_failed")
    if result != "pass" and not body.get("evidence"):
        raise ApiError(422, "validation_error", "a returned head carries its evidence")
    diagram, chunk, task = await _chunk_with_task(session, principal, key, chunk_key)
    entries = await _queue(session, diagram["id"])
    if not entries or entries[0]["id"] != chunk["id"]:
        raise ApiError(409, "not_queue_head", "the queue is serial: only its head is checked",
                       details={"head": entries[0]["chunk_key"] if entries else None,
                                "position": await _queue_position(session, chunk)})
    implementers = await _implementers(session, principal, task, chunk["task_key"])
    if _me(principal) in implementers:
        raise Forbidden("the implementer does not verify its own queue entry")
    submission = chunk.get("submission") or {}
    record = {"result": result, "evidence": body.get("evidence"),
              "commit": body.get("rebased_commit") or submission.get("commit"),
              "submission_at": submission.get("at"), "by": _me(principal),
              "at": _now().isoformat()}
    if result == "pass":
        await session.execute(text(
            "UPDATE diagram_chunks SET queue_check = CAST(:q AS jsonb), updated_at = NOW() "
            "WHERE id = :id"), {"q": json.dumps(record), "id": chunk["id"]})
        await _event(session, principal, diagram, "CHUNK_QUEUE_VERIFIED", chunk_key=chunk_key,
                     task_key=chunk["task_key"], commit=record["commit"])
    else:
        await session.execute(text(
            "UPDATE diagram_chunks SET queue_check = CAST(:q AS jsonb), queued_at = NULL, "
            "state = 'rework', updated_at = NOW() WHERE id = :id"),
            {"q": json.dumps(record), "id": chunk["id"]})
        await _event(session, principal, diagram, "CHUNK_QUEUE_RETURNED", chunk_key=chunk_key,
                     task_key=chunk["task_key"], result=result, evidence=record["evidence"],
                     targets=sorted(implementers) or None)
        if result == "test_failed":
            trust = await _charge(session, principal, diagram, chunk, COST_CI_FAILURE,
                                  "incremental tests failed in the merge queue")
            if trust < TRUST_FLOOR:
                await _break_circuit(session, principal, diagram, chunk,
                                     f"trust {trust} fell under {TRUST_FLOOR}")
    await session.commit()
    return {"chunk_key": chunk_key, "queue_check": record,
            "status": "verified" if result == "pass" else "rework"}


async def merge(session: AsyncSession, principal: Principal, key: str, chunk_key: str,
                body: dict[str, Any]) -> dict[str, Any]:
    """M-10: only after a passing gate, and never by the implementer. Merging
    unblocks and dispatches the chunks that waited on it (M-14)."""
    diagram, chunk, task = await _chunk_with_task(session, principal, key, chunk_key)
    if chunk["state"] == "merged":
        raise ApiError(409, "already_merged", f"chunk '{chunk_key}' is merged")
    status = status_of(chunk, task)
    if status == "queued":
        raise ApiError(409, "queue_not_verified", "approved work merges from the queue: the "
                       "queue controller rebases and re-tests the head first (spec M-27)",
                       details={"position": await _queue_position(session, chunk)})
    if status not in ("ready_to_merge", "verified"):
        raise ApiError(409, "gate_not_passed", "merge follows a passing gate on the current "
                       "submission", details={"status": status})
    me = _me(principal)
    if me in await _implementers(session, principal, task, chunk["task_key"]):
        raise ApiError(403, "merge_by_implementer", "the implementer cannot merge their own "
                       "chunk (spec M-10)", details={"merger": me})
    submission = chunk.get("submission") or {}
    artifact = (body.get("artifact") or (chunk.get("queue_check") or {}).get("commit")
                or (submission.get("commit") if submission.get("accepted") else None))
    if not artifact:
        # Downstream chunks start from what this one delivered: never merge nothing.
        raise ApiError(409, "merge_without_deliverable", "a merge records what the chunk "
                       "delivered: pass artifact (a commit or a doc:// link), or merge an "
                       "accepted submission", details={"chunk_key": chunk_key})
    kernel_task_id = chunk.get("kernel_task_id")
    if kernel_task_id:
        # The Kernel already merged and projected it; an accepted submission that
        # never reached a commit would otherwise be recorded as delivered.
        shipped = chunk.get("merged_commit_sha")
        if not shipped:
            raise ApiError(
                409, "kernel_not_committed",
                "this chunk is delivered by the Kernel: wait for its commit "
                "(the commit event marks it merged itself)",
                details={"chunk_key": chunk_key, "kernel_task_id": str(kernel_task_id)})
        artifact = shipped
    record = {"by": me, "at": _now().isoformat(), "artifact": artifact,
              "note": body.get("note"), "task_status": task["status"] if task else None,
              "via_queue": status == "verified",
              "kernel_task_id": str(kernel_task_id) if kernel_task_id else None,
              "delivered": await _delivered(session, principal, body, submission, artifact)}
    await session.execute(text(
        "UPDATE diagram_chunks SET state = 'merged', merge = CAST(:m AS jsonb), "
        "merged_commit_sha = COALESCE(:sha, merged_commit_sha), "
        "block_reason = NULL, queued_at = NULL, updated_at = NOW() WHERE id = :id"),
        {"m": json.dumps(record), "id": chunk["id"],
         "sha": (artifact[:64] if isinstance(artifact, str) else None)})
    await _event(session, principal, diagram, "CHUNK_MERGED", chunk_key=chunk_key,
                 task_key=chunk["task_key"], merged_by=me, artifact=body.get("artifact"),
                 delivered_docs=[doc["uri"] for doc in record["delivered"]["docs"]],
                 targets=diagram.get("watchers") or [])
    unblocked = await _release_waiting(session, principal, diagram,
                                       trigger={"upstream": chunk_key,
                                                "merge_commit": record["artifact"]})
    await session.commit()
    return {"chunk_key": chunk_key, "state": "merged", "merge": record, "unblocked": unblocked}


def _doc_key(uri: Any) -> str | None:
    value = str(uri or "").strip()
    return value[len("doc://"):].strip("/") or None if value.startswith("doc://") else None


async def _delivered(session: AsyncSession, principal: Principal, body: dict[str, Any],
                     submission: dict[str, Any], artifact: Any) -> dict[str, Any]:
    """What a merged chunk hands the chunks after it: its documents, resolved and
    pinned to their version now, and its files (the accepted submission's and any
    listed in deliverables)."""
    from app.services.collab import resolve_ref

    given = body.get("deliverables") or {}
    uris = [*(given.get("docs") or []), artifact, *(submission.get("docs") or [])]
    docs, seen = [], set()
    for uri in uris:
        key = _doc_key(uri)
        if not key or key in seen or len(docs) >= 20:
            continue
        seen.add(key)
        document = await resolve_ref(session, principal.workspace.id, {"doc_key": key})
        if document is None:
            docs.append({"uri": f"doc://{key}", "missing": True})
            continue
        docs.append({"uri": f"doc://{document.doc_key}", "namespace": document.namespace,
                     "doc_key": document.doc_key, "version": document.version,
                     "content_hash": document.content_hash})
    files: dict[str, dict[str, Any]] = {}
    listed = [*(submission.get("files") or [] if submission.get("accepted") else []),
              *(given.get("files") or [])]
    for item in listed:
        path = str((item.get("path") if isinstance(item, dict) else item) or "").strip()
        digest = item.get("sha256") if isinstance(item, dict) else None
        if path and len(files) < 200:
            files[path] = {"path": path, "sha256": digest or files.get(path, {}).get("sha256")}
    for item in submission.get("hashes") or []:  # recorded by submit when it stores contents
        if item.get("path") in files and not files[item["path"]]["sha256"]:
            files[item["path"]]["sha256"] = item.get("sha256")
    return {"docs": docs, "files": list(files.values())}


def _lineage(diagram: dict[str, Any], chunk: dict[str, Any]) -> str:
    """The task key a chunk is dispatched under; re-dispatches add -2, -3..."""
    return f"DG-{diagram['diagram_key']}-{chunk['chunk_key']}"[:128]


async def _lineage_tasks(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                         chunk: dict[str, Any]) -> list[dict[str, Any]]:
    lineage = _lineage(diagram, chunk)
    rows = (await session.execute(text(
        "SELECT task_key, status::text AS status, updated_at FROM collab_tasks "
        "WHERE workspace_id = :ws AND (task_key = :k OR task_key ~ :pattern) "
        "ORDER BY updated_at DESC"),
        {"ws": principal.workspace.id, "k": lineage,
         "pattern": "^" + re.escape(lineage) + r"-[0-9]+$"})).mappings().all()
    return [dict(row) for row in rows]


async def repoint(session: AsyncSession, principal: Principal, key: str, chunk_key: str,
                  body: dict[str, Any]) -> dict[str, Any]:
    """M-08: point a chunk at the board task that actually did its work, e.g. after a
    duplicate dispatch was withdrawn. Only a task of the chunk's own lineage, and a
    DONE one for a merged chunk."""
    if not _may_steer(principal):
        raise Forbidden("people, super admins and planners re-point a chunk")
    task_key = str(body.get("task_key") or "").strip()
    if not task_key:
        raise ApiError(422, "validation_error", "task_key is required")
    diagram, chunk, _ = await _chunk_with_task(session, principal, key, chunk_key)
    lineage = {row["task_key"]: row for row in
               await _lineage_tasks(session, principal, diagram, chunk)}
    if task_key not in lineage:
        raise ApiError(422, "not_chunk_lineage", f"'{task_key}' is not a task of chunk "
                       f"'{chunk_key}'", details={"lineage": sorted(lineage)})
    if chunk["state"] == "merged" and lineage[task_key]["status"] != "DONE":
        raise ApiError(409, "task_not_done", "a merged chunk points at a DONE task",
                       details={"status": lineage[task_key]["status"]})
    previous = chunk["task_key"]
    await session.execute(text(
        "UPDATE diagram_chunks SET task_key = :t, updated_at = NOW() WHERE id = :id"),
        {"t": task_key, "id": chunk["id"]})
    await _event(session, principal, diagram, "CHUNK_REPOINTED", chunk_key=chunk_key,
                 task_key=task_key, previous=previous, note=body.get("note"))
    await session.commit()
    return {"chunk_key": chunk_key, "task_key": task_key, "previous": previous}


async def _release_waiting(session: AsyncSession, principal: Principal,
                           diagram: dict[str, Any],
                           trigger: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    chunks = {chunk["chunk_key"]: chunk for chunk in await _chunks(session, diagram["id"])}
    released = []
    for chunk in chunks.values():
        # Every graph block is re-checked, so a chunk still waiting on others names only
        # those; circuit and budget blocks are a person's to lift.
        if chunk["state"] != "blocked" or chunk["block_kind"] not in (*GRAPH_BLOCKS, None):
            continue
        outcome = await _dispatch_one(session, principal, diagram, chunk, chunks)
        chunk["state"] = outcome["state"]  # later chunks read the graph as it is now
        if outcome["state"] == "dispatched":
            # M-14 / M-26: unlocked by the graph, no one re-dispatches; watchers hear it.
            # M-26: the minimum to pull and start: upstream, its merge commit, the
            # baseline, the downstream chunk and its task. Never the code itself.
            await _event(session, principal, diagram, "CHUNK_UNBLOCKED",
                         chunk_key=chunk["chunk_key"], task_key=outcome.get("task_key"),
                         baseline=(diagram.get("baseline") or {}).get("version"),
                         **(trigger or {}), targets=diagram.get("watchers") or [])
            released.append(outcome)
    return released


async def project_kernel_commit(
    session: AsyncSession, chunk_id: int, commit_sha: str, *,
    dedupe_key: str, task_status: str | None = None,
) -> dict[str, Any]:
    """A Kernel commit is the delivery fact for a chunk.

    Called from the Kernel event projection (and safe to replay).  It marks the
    chunk merged with the real commit, then unlocks whatever waited on it — the
    same unlock path a human merge used, so there is exactly one.

    Returns ``{"projected": bool, "unblocked": [...]}``.
    """
    chunk_row = (
        await session.execute(text(
            "SELECT c.*, d.diagram_key, d.watchers, d.baseline, d.id AS d_id, "
            "d.namespace, d.doc_key, d.status AS diagram_status, d.workspace_id AS ws "
            "FROM diagram_chunks c JOIN diagrams d ON d.id = c.diagram_id "
            "WHERE c.id = :id"), {"id": chunk_id},
        )
    ).mappings().first()
    if chunk_row is None:
        return {"projected": False, "reason": "chunk_gone", "unblocked": []}
    chunk = dict(chunk_row)
    already = chunk.get("merged_commit_sha")
    if chunk["state"] == "merged" and already == commit_sha:
        return {"projected": False, "reason": "already_merged", "unblocked": []}

    inserted = await session.execute(text(
        "INSERT INTO diagram_chunk_projections "
        "(dedupe_key, chunk_id, task_key, action, commit_sha) "
        "VALUES (:k, :id, :t, 'chunk.merged', :sha) ON CONFLICT DO NOTHING "
        "RETURNING dedupe_key"),
        {"k": dedupe_key[:400], "id": chunk_id, "t": chunk.get("task_key"),
         "sha": commit_sha[:64]})
    if inserted.first() is None:
        return {"projected": False, "reason": "duplicate_event", "unblocked": []}

    workspace = await session.get(Workspace, chunk["ws"])
    if workspace is None:
        return {"projected": False, "reason": "workspace_gone", "unblocked": []}
    from app.auth import principal_for_admin

    principal = principal_for_admin(workspace)
    diagram = {"diagram_key": chunk["diagram_key"], "baseline": chunk["baseline"],
               "watchers": chunk["watchers"], "namespace": chunk["namespace"],
               "doc_key": chunk["doc_key"]}
    record = {
        "by": "kernel",
        "at": _now().isoformat(),
        "artifact": commit_sha,
        "via": "kernel_merge",
        "task_status": task_status,
        "kernel_task_id": str(chunk["kernel_task_id"]) if chunk.get("kernel_task_id") else None,
    }
    await session.execute(text(
        "UPDATE diagram_chunks SET state = 'merged', merge = CAST(:m AS jsonb), "
        "merged_commit_sha = :sha, block_reason = NULL, queued_at = NULL, "
        "updated_at = NOW() WHERE id = :id"),
        {"m": json.dumps(record), "sha": commit_sha[:64], "id": chunk_id})
    await _event(session, principal, diagram, "CHUNK_MERGED",
                 chunk_key=chunk["chunk_key"], task_key=chunk.get("task_key"),
                 merged_by="kernel", artifact=commit_sha, commit_sha=commit_sha,
                 targets=diagram.get("watchers") or [])
    chunks = {row["chunk_key"]: row for row in await _chunks(session, chunk["d_id"])}
    chunks[chunk["chunk_key"]] = {**chunk, "state": "merged"}
    unblocked = await _release_waiting(
        session, principal, {**diagram, "id": chunk["d_id"]},
        trigger={"upstream": chunk["chunk_key"], "merge_commit": commit_sha},
    )
    return {"projected": True, "unblocked": unblocked}


# ------------------------------------------------------------------ changes


async def propose(session: AsyncSession, principal: Principal, key: str,
                  body: dict[str, Any], *, source: str = "proposal") -> dict[str, Any]:
    """M-05: a change with its impact. Work already in flight is never rewritten:
    it gets a new chunk that depends on it instead (M-33)."""
    diagram = await _diagram(session, principal, key, lock=True)
    _require_writer(principal, diagram["namespace"])
    if diagram["status"] == "draft":
        raise ApiError(409, "still_draft", "edit a draft directly; changes start after "
                       "confirmation", details={"edit_with": f"PUT /v1/diagrams/{key}"})
    summary = str(body.get("summary") or "").strip()
    if not summary:
        raise ApiError(422, "validation_error", "a change needs a summary")
    patch = body.get("patch") or {}
    chunks = {chunk["chunk_key"]: chunk for chunk in await _chunks(session, diagram["id"])}
    tasks = await _tasks(session, principal, [c["task_key"] for c in chunks.values()
                                              if c["task_key"]])
    add = []
    for item in patch.get("add") or []:
        chunk = _clean_chunk(item)
        if isinstance(item, dict) and item.get("_increment_of"):
            chunk["_increment_of"] = str(item["_increment_of"])
        add.append(chunk)
    update = [item for item in patch.get("update") or [] if isinstance(item, dict)]
    remove = [str(item) for item in patch.get("remove") or []]
    in_flight = []
    for item in [*update, *({"chunk_key": key_} for key_ in remove)]:
        current = chunks.get(str(item.get("chunk_key")))
        if current is None:
            raise ApiError(422, "unknown_chunk", f"chunk '{item.get('chunk_key')}' not found")
        status = status_of(current, tasks.get(current["task_key"]))
        if status in IN_FLIGHT:
            in_flight.append({"chunk_key": current["chunk_key"], "status": status})
    if in_flight:
        raise ApiError(409, "chunk_in_flight", "chunks already dispatched, in progress, in "
                       "review or merged are not rewritten; add a new chunk that depends on "
                       "them (spec M-33)", details={"chunks": in_flight,
                                                    "add_chunk_with": {"depends_on":
                                                        [item["chunk_key"] for item in in_flight]}})
    for item in add:
        if item["chunk_key"] in chunks:
            raise ApiError(409, "chunk_exists", f"chunk '{item['chunk_key']}' exists")
    # Validate the graph as it would be after the change.
    after = {k: {field: v[field] for field in ("chunk_key", *CHUNK_FIELDS)}
             for k, v in chunks.items() if v["state"] != "retired" and k not in remove}
    for item in update:
        fields = {field: item[field] for field in CHUNK_FIELDS if field in item}
        after[item["chunk_key"]] = _clean_chunk({**after[item["chunk_key"]], **fields})
    for item in add:
        after[item["chunk_key"]] = {k: v for k, v in item.items() if k != "_increment_of"}
    _validate_graph(list(after.values()))
    _require_contract_first(list(after.values()))
    touched = sorted({*(item["chunk_key"] for item in add), *(item["chunk_key"]
                      for item in update), *remove})
    downstream = sorted({k for k, v in after.items()
                         if any(dep in touched for dep in v["depends_on"])} - set(touched))
    impact = {
        "chunks": touched, "downstream": downstream,
        "interfaces": sorted({k for k in touched if after.get(k, chunks.get(k, {}))
                              .get("layer") == "interface"}),
        "tasks": sorted({chunks[k]["task_key"] for k in [*touched, *downstream]
                         if k in chunks and chunks[k]["task_key"]}),
        "declared": body.get("impact") or {},
    }
    if not touched:
        raise ApiError(422, "empty_impact", "a change that touches no chunk is not filed "
                       "(spec M-05)")
    change_key = f"chg_{uuid.uuid4().hex[:12]}"
    stored_patch = {"add": add, "update": update, "remove": remove,
                    "dispatch_new": bool(body.get("dispatch_new", True))}
    await session.execute(text(
        "INSERT INTO diagram_changes (workspace_id, diagram_id, change_key, source, "
        "request_text, summary, impact, patch, proposed_by, from_version) VALUES (:ws, :d, "
        ":ck, :src, :rt, :s, CAST(:i AS jsonb), CAST(:p AS jsonb), :by, :v)"),
        {"ws": principal.workspace.id, "d": diagram["id"], "ck": change_key, "src": source,
         "rt": body.get("request_text"), "s": summary, "i": json.dumps(impact),
         "p": json.dumps(stored_patch), "by": _me(principal), "v": diagram["version"]})
    await _event(session, principal, diagram, "DIAGRAM_CHANGE_PROPOSED", change_key=change_key,
                 source=source, impact=impact, request_text=body.get("request_text"))
    await session.commit()
    return await change_view(session, principal, key, change_key)


async def request_increment(session: AsyncSession, principal: Principal, key: str,
                            body: dict[str, Any]) -> dict[str, Any]:
    """§16 fast lane: a person's request from conversation, translated by the diagram
    manager into a change. A target in flight gets a new chunk depending on it; a
    target not dispatched yet is folded into (M-33)."""
    request_text = str(body.get("request_text") or "").strip()
    if not request_text:
        raise ApiError(422, "validation_error", "request_text is the person's request, verbatim")
    diagram = await _diagram(session, principal, key)
    target = body.get("target_chunk")
    chunk = body.get("chunk") or {}
    patch: dict[str, Any] = {"add": [], "update": [], "remove": []}
    if target:
        chunks = {c["chunk_key"]: c for c in await _chunks(session, diagram["id"])}
        current = chunks.get(target)
        if current is None:
            raise ApiError(422, "unknown_chunk", f"chunk '{target}' not found")
        tasks = await _tasks(session, principal, [current["task_key"]] if current["task_key"]
                             else [])
        if status_of(current, tasks.get(current["task_key"])) in IN_FLIGHT:
            chunk = {**chunk, "depends_on": sorted({*chunk.get("depends_on", []), target})}
            patch["add"].append({**chunk, "_increment_of": target})
        else:
            patch["update"].append({"chunk_key": target,
                                    **{f: chunk[f] for f in CHUNK_FIELDS if f in chunk}})
    else:
        patch["add"].append(chunk)
    result = await propose(session, principal, key, {
        "summary": body.get("summary") or request_text[:200], "patch": patch,
        "request_text": request_text, "impact": body.get("impact"),
        "dispatch_new": body.get("dispatch_new", True)}, source="human_request")
    return result


async def decide(session: AsyncSession, principal: Principal, key: str, change_key: str,
                 body: dict[str, Any]) -> dict[str, Any]:
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        raise ApiError(422, "validation_error", "decision is approve or reject")
    if principal.is_human:
        kind = "person"
    elif principal.is_super_admin:
        kind = "super_admin_for_owner"  # M-32: recorded as acting for the owner
    else:
        raise Forbidden("a person, or a super admin acting for the owner, decides changes")
    diagram = await _diagram(session, principal, key, lock=True)
    change = await _change(session, diagram, change_key, lock=True)
    if change["status"] != "pending":
        raise ApiError(409, "already_decided", f"change is {change['status']}")
    if change["from_version"] != diagram["version"]:
        raise ApiError(409, "stale_change", "the diagram moved on since this change was filed; "
                       "file it again", details={"from_version": change["from_version"],
                                                 "current_version": diagram["version"]})
    me = _me(principal)
    if decision == "reject":
        await session.execute(text(
            "UPDATE diagram_changes SET status = 'rejected', decided_by = :by, decision_kind = "
            ":k, decision_note = :n, decided_at = NOW() WHERE id = :id"),
            {"by": me, "k": kind, "n": body.get("note"), "id": change["id"]})
        await _event(session, principal, diagram, "DIAGRAM_CHANGE_DECIDED",
                     change_key=change_key, decision="reject", decision_kind=kind)
        await session.commit()
        return await change_view(session, principal, key, change_key)
    before = await _apply(session, principal, diagram, change["patch"])
    diagram = await _bump(session, principal, diagram, change["impact"])
    await session.execute(text(
        "UPDATE diagram_changes SET status = 'approved', decided_by = :by, decision_kind = :k, "
        "decision_note = :n, decided_at = NOW(), to_version = :v, before = CAST(:b AS jsonb) "
        "WHERE id = :id"),
        {"by": me, "k": kind, "n": body.get("note"), "v": diagram["version"],
         "b": json.dumps(before, default=str), "id": change["id"]})
    await _event(session, principal, diagram, "DIAGRAM_CHANGE_DECIDED", change_key=change_key,
                 decision="approve", decision_kind=kind, version=diagram["version"])
    dispatched = await _resume(session, principal, diagram)
    if diagram["status"] == "frozen" and change["patch"].get("dispatch_new", True):
        chunks = {c["chunk_key"]: c for c in await _chunks(session, diagram["id"])}
        for item in change["patch"].get("add", []):
            chunk = chunks.get(item["chunk_key"])
            if chunk and chunk["state"] == "pending":
                dispatched.append(await _dispatch_one(session, principal, diagram, chunk, chunks))
    await session.commit()
    result = await change_view(session, principal, key, change_key)
    result["dispatched"] = dispatched
    return result


async def _apply(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                 patch: dict[str, Any]) -> dict[str, Any]:
    chunks = {c["chunk_key"]: c for c in await _chunks(session, diagram["id"])}
    before: dict[str, Any] = {"added": [], "updated": {}, "removed": {}}
    for item in patch.get("add", []):
        chunk = _clean_chunk(item)
        await _insert_chunk(session, principal, {**diagram, "version": diagram["version"] + 1},
                            chunk, origin="increment" if item.get("_increment_of") else "plan",
                            increment_of=item.get("_increment_of"))
        before["added"].append(chunk["chunk_key"])
    for item in patch.get("update", []):
        current = chunks[item["chunk_key"]]
        before["updated"][current["chunk_key"]] = {f: current[f] for f in CHUNK_FIELDS}
        merged = _clean_chunk({**{f: current[f] for f in ("chunk_key", *CHUNK_FIELDS)},
                               **{f: item[f] for f in CHUNK_FIELDS if f in item}})
        await _set_fields(session, current["id"], merged)
    for chunk_key in patch.get("remove", []):
        current = chunks[chunk_key]
        before["removed"][chunk_key] = current["state"]
        await session.execute(text(
            "UPDATE diagram_chunks SET state = 'retired', updated_at = NOW() WHERE id = :id"),
            {"id": current["id"]})
    return before


async def _set_fields(session: AsyncSession, chunk_id: int, chunk: dict[str, Any]) -> None:
    await session.execute(text(
        "UPDATE diagram_chunks SET title = :t, scope = :s, layer = :l, icd = CAST(:icd AS jsonb), "
        "done_criteria = CAST(:dc AS jsonb), depends_on = CAST(:deps AS text[]), "
        "allowed_write_paths = CAST(:w AS text[]), read_only_interfaces = CAST(:r AS text[]), "
        "commands = CAST(:cmd AS jsonb), semantic_rules = CAST(:rules AS text[]), "
        "token_budget = :tb, updated_at = NOW() WHERE id = :id"),
        {"t": chunk["title"], "s": chunk["scope"], "l": chunk["layer"],
         "icd": json.dumps(chunk["icd"]), "dc": json.dumps(chunk["done_criteria"]),
         "deps": chunk["depends_on"], "w": chunk["allowed_write_paths"],
         "r": chunk["read_only_interfaces"], "cmd": json.dumps(chunk["commands"]),
         "rules": chunk["semantic_rules"], "tb": chunk.get("token_budget"), "id": chunk_id})


async def _bump(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                impact: dict[str, Any]) -> dict[str, Any]:
    """Version + 1; a frozen baseline follows when the interface layer changed (M-05)."""
    chunks = await _chunks(session, diagram["id"])
    version = diagram["version"] + 1
    baseline = diagram.get("baseline")
    if diagram["status"] == "frozen" and baseline and (
            impact.get("interfaces") or _content_hash(chunks, "interface")
            != baseline.get("interface_hash")):
        baseline = _baseline(version, chunks, baseline["frozen_layers"], _me(principal))
    diagram = dict((await session.execute(text(
        "UPDATE diagrams SET version = :v, baseline = CAST(:b AS jsonb), updated_at = NOW() "
        "WHERE id = :id RETURNING *"),
        {"v": version, "b": json.dumps(baseline) if baseline else None,
         "id": diagram["id"]})).mappings().one())
    await _write_document(session, principal, diagram, chunks)
    return diagram


async def revoke(session: AsyncSession, principal: Principal, key: str, change_key: str,
                 body: dict[str, Any]) -> dict[str, Any]:
    """M-34: a person overrides any decision. Added chunks are retired (their tasks
    withdrawn while unclaimed), updated ones restored, removed ones brought back."""
    _require_person(principal, "revoking a diagram decision")
    diagram = await _diagram(session, principal, key, lock=True)
    change = await _change(session, diagram, change_key, lock=True)
    if change["status"] != "approved":
        raise ApiError(409, "invalid_transition", f"only an approved change is revoked "
                       f"(this one is {change['status']})")
    before = change["before"] or {}
    chunks = {c["chunk_key"]: c for c in await _chunks(session, diagram["id"])}
    withdrawn = []
    for chunk_key in before.get("added", []):
        chunk = chunks.get(chunk_key)
        if chunk is None:
            continue
        if chunk["task_key"]:
            withdrawn.append(await _withdraw(session, principal, chunk["task_key"], change_key))
        await session.execute(text(
            "UPDATE diagram_chunks SET state = 'retired', updated_at = NOW() WHERE id = :id"),
            {"id": chunk["id"]})
    for chunk_key, fields in (before.get("updated") or {}).items():
        if chunk_key in chunks:
            await _set_fields(session, chunks[chunk_key]["id"],
                              _clean_chunk({"chunk_key": chunk_key, **fields}))
    for chunk_key, state in (before.get("removed") or {}).items():
        if chunk_key in chunks:
            await session.execute(text(
                "UPDATE diagram_chunks SET state = :s, updated_at = NOW() WHERE id = :id"),
                {"s": state, "id": chunks[chunk_key]["id"]})
    diagram = await _bump(session, principal, diagram, change["impact"])
    await session.execute(text(
        "UPDATE diagram_changes SET status = 'revoked', revoked_by = :by, revoked_at = NOW(), "
        "decision_note = COALESCE(decision_note || E'\\n', '') || :n WHERE id = :id"),
        {"by": _me(principal), "n": f"revoked: {body.get('note') or ''}".strip(),
         "id": change["id"]})
    await _event(session, principal, diagram, "DIAGRAM_CHANGE_REVOKED", change_key=change_key,
                 version=diagram["version"], withdrawn=withdrawn,
                 overrode=change["decision_kind"])
    await session.commit()
    result = await change_view(session, principal, key, change_key)
    result["withdrawn_tasks"] = withdrawn
    return result


async def _withdraw(session: AsyncSession, principal: Principal, task_key: str,
                    change_key: str) -> dict[str, Any]:
    status = await session.scalar(text(
        "SELECT status::text FROM collab_tasks WHERE workspace_id = :ws AND task_key = :k"),
        {"ws": principal.workspace.id, "k": task_key})
    if status in ("TODO", "READY"):
        from app.services.task_lifecycle import transition_task_fenced

        await transition_task_fenced(
            session, workspace_id=principal.workspace.id, task_key=task_key,
            new_status="CANCELLED", expected_status=(status,), clear_lease=True)
        return {"task_key": task_key, "withdrawn": True}
    # Already claimed: left to its holder and the person who revoked.
    return {"task_key": task_key, "withdrawn": False, "status": status,
            "note": f"claimed already; cancel it by hand if change {change_key} must not ship"}


async def _change(session: AsyncSession, diagram: dict[str, Any], change_key: str,
                  *, lock: bool = False) -> dict[str, Any]:
    row = (await session.execute(text(
        "SELECT * FROM diagram_changes WHERE diagram_id = :d AND change_key = :k"
        + (" FOR UPDATE" if lock else "")),
        {"d": diagram["id"], "k": change_key})).mappings().one_or_none()
    if row is None:
        raise NotFound("change", f"change '{change_key}' not found")
    return dict(row)


async def _mismatch_fix(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                        chunk: dict[str, Any]) -> dict[str, Any]:
    """How to clear a board mismatch: re-point to the lineage task that did the work."""
    done = [row["task_key"] for row in await _lineage_tasks(session, principal, diagram, chunk)
            if row["status"] == "DONE" and row["task_key"] != chunk["task_key"]]
    call = (f'diagram(action="repoint", diagram_key="{diagram["diagram_key"]}", '
            f'chunk_key="{chunk["chunk_key"]}", args={{"task_key": "<task_key>"}})')
    if done:
        return {"action": "repoint", "task_key": done[0],
                "text": f"{done[0]} is DONE for this chunk: re-point the chunk to it.",
                "call": call.replace("<task_key>", done[0])}
    return {"action": "repoint", "task_key": None,
            "text": "No DONE task of this chunk's lineage: re-dispatch it, or re-point it "
                    "once its work is DONE.", "call": call}


def _public_submission(submission: dict[str, Any] | None) -> dict[str, Any] | None:
    if not submission:
        return None
    return {key: value for key, value in submission.items() if key != "diff"}


def _change_out(row: dict[str, Any]) -> dict[str, Any]:
    return {key: (_iso(value) if isinstance(value, datetime) else value)
            for key, value in row.items() if key not in ("id", "workspace_id", "tenant_id",
                                                         "diagram_id")}


async def change_view(session: AsyncSession, principal: Principal, key: str,
                      change_key: str) -> dict[str, Any]:
    diagram = await _diagram(session, principal, key)
    return _change_out(await _change(session, diagram, change_key))


# ------------------------------------------------------------------ read


async def view(session: AsyncSession, principal: Principal, key: str) -> dict[str, Any]:
    """The diagram and its command view (M-08): status distribution, blocked list,
    merge queue, inconsistencies with the task board, and the self-check."""
    from app.services import automation

    diagram = await _diagram(session, principal, key)
    chunks = await _chunks(session, diagram["id"])
    tasks = await _tasks(session, principal, [c["task_key"] for c in chunks if c["task_key"]])
    by_key = {chunk["chunk_key"]: chunk for chunk in chunks}
    items, distribution, blocked, mismatches, warnings = [], {}, [], [], []
    ready = []
    for chunk in chunks:
        if chunk["state"] == "blocked" and chunk["block_kind"] == "dependency":
            # Read from the graph as it is now, never a reason saved earlier.
            waiting, reason = _waiting_on(chunk, by_key)
            if waiting:
                chunk = {**chunk, "block_reason": reason}
        task = tasks.get(chunk["task_key"]) if chunk["task_key"] else None
        status = status_of(chunk, task)
        problem = None
        if chunk["task_key"] and task is None:
            problem = "the dispatched task is gone from the board"
        elif status == "merged" and task and task["status"] in ("FAILED", "CANCELLED"):
            problem = f"merged, but its task is {task['status']}"
        if problem:
            mismatches.append({"chunk_key": chunk["chunk_key"], "task_key": chunk["task_key"],
                               "problem": problem,
                               "fix": await _mismatch_fix(session, principal, diagram, chunk)})
        if status != "retired":
            distribution[status] = distribution.get(status, 0) + 1
        if status in ("blocked", "blocked_by_spec"):
            blocked.append({"chunk_key": chunk["chunk_key"], "kind": chunk["block_kind"],
                            "reason": chunk["block_reason"]})
        if status == "ready_to_merge":  # the lightweight path merges straight from the gate
            ready.append({"chunk_key": chunk["chunk_key"], "task_key": chunk["task_key"],
                          "gated_at": (chunk["gate"] or {}).get("at")})
        if chunk["review_reject_count"] >= WARN_AFTER_REJECTIONS and status not in (
                "merged", "retired"):
            warnings.append({"chunk_key": chunk["chunk_key"], "kind": "rejections",
                             "detail": f"{chunk['review_reject_count']} consecutive rejections, "
                                       f"trust {chunk['trust_score']}"})
        if chunk["token_budget"] and chunk["token_consumed"] > chunk["token_budget"]:
            warnings.append({"chunk_key": chunk["chunk_key"], "kind": "budget",
                             "detail": f"{chunk['token_consumed']} of {chunk['token_budget']} "
                                       "tokens"})
        items.append({
            "chunk_key": chunk["chunk_key"], "title": chunk["title"], "scope": chunk["scope"],
            "layer": chunk["layer"], "icd": chunk["icd"], "done_criteria": chunk["done_criteria"],
            "depends_on": chunk["depends_on"], "allowed_write_paths": chunk["allowed_write_paths"],
            "read_only_interfaces": chunk["read_only_interfaces"], "status": status,
            "state": chunk["state"], "block_reason": chunk["block_reason"],
            "task_key": chunk["task_key"], "task_status": task["status"] if task else None,
            "gate": chunk["gate"], "merge": chunk["merge"], "origin": chunk["origin"],
            "increment_of": chunk["increment_of"],
            "created_in_version": chunk["created_in_version"],
            "commands": chunk["commands"], "semantic_rules": chunk["semantic_rules"],
            "block_kind": chunk["block_kind"], "context_slice": chunk["context_slice"],
            "submission": _public_submission(chunk["submission"]), "ci": chunk["ci"],
            "trust_score": chunk["trust_score"], "trust_log": chunk["trust_log"],
            "token_budget": chunk["token_budget"], "token_consumed": chunk["token_consumed"],
            "review_reject_count": chunk["review_reject_count"],
            "escalation_count": chunk["escalation_count"],
            "graph_mutation_signal": chunk["graph_mutation_signal"],
            "queue_check": chunk["queue_check"],
            # The Kernel node that delivers this chunk, and the commit that did.
            # Read-only: dispatch binds them, the commit projection fills the sha.
            "id": chunk["id"],
            "kernel_task_id": chunk.get("kernel_task_id"),
            "kernel_workspace_id": chunk.get("kernel_workspace_id"),
            "scope_hash": chunk.get("scope_hash"),
            "merged_commit_sha": chunk.get("merged_commit_sha")})
    pending = (await session.execute(text(
        "SELECT change_key, summary, source, created_at FROM diagram_changes WHERE "
        "diagram_id = :d AND status = 'pending' ORDER BY id"), {"d": diagram["id"]})
    ).mappings().all()
    # The latest decisions, so a person can revoke an approval from the same view (M-34).
    decided = (await session.execute(text(
        "SELECT change_key, summary, source, status, decided_by, decision_kind, decided_at "
        "FROM diagram_changes WHERE diagram_id = :d AND status <> 'pending' "
        "ORDER BY decided_at DESC NULLS LAST, id DESC LIMIT 10"), {"d": diagram["id"]})
    ).mappings().all()
    serial = (await queue(session, principal, key))["items"]
    warnings += [{"chunk_key": item["chunk_key"], "kind": "queue_wait",
                  "detail": f"waiting {item['waiting_seconds'] // 60} minutes in the merge "
                            "queue: split it or add test parallelism (M-29)"}
                 for item in serial if item["overdue"]]
    if mismatches:
        warnings.append({"kind": "board_mismatch", "detail": f"{len(mismatches)} chunk(s) "
                         "disagree with the task board"})
    return {
        "diagram_key": diagram["diagram_key"], "title": diagram["title"],
        "namespace": diagram["namespace"], "description": diagram["description"],
        "repo": diagram.get("repo"),
        "policy": diagram.get("policy") or {},
        "mode": await automation.mode_for_diagram(session, principal.workspace.id, diagram),
        "status": diagram["status"], "version": diagram["version"],
        "watchers": diagram["watchers"],
        "baseline": diagram["baseline"], "document": f"doc://{diagram['doc_key']}",
        "created_by": diagram["created_by"], "confirmed_by": diagram["confirmed_by"],
        "confirmed_at": _iso(diagram["confirmed_at"]),
        "self_check": self_check_risk(diagram["self_check"] or {}),
        "chunks": items,
        "progress": {"distribution": distribution, "blocked": blocked, "merge_queue": serial,
                     "ready_to_merge": ready, "total": sum(distribution.values()),
                     "trust": {c["chunk_key"]: c["trust_score"] for c in items
                               if c["status"] not in ("merged", "retired")},
                     "tokens": {c["chunk_key"]: {"consumed": c["token_consumed"],
                                                 "budget": c["token_budget"]}
                                for c in items if c["token_budget"] or c["token_consumed"]}},
        "warnings": warnings,
        "board_consistency": {"ok": not mismatches, "mismatches": mismatches,
                              "rule": "the task board is the truth (spec M-08)"},
        "pending_changes": [{**dict(row), "created_at": _iso(row["created_at"])}
                            for row in pending],
        "recent_changes": [{**dict(row), "decided_at": _iso(row["decided_at"])}
                           for row in decided],
    }


async def list_diagrams(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    from app.services import automation

    rows = (await session.execute(text(
        "SELECT diagram_key, title, namespace, status, version, updated_at, policy FROM diagrams "
        "WHERE workspace_id = :ws ORDER BY updated_at DESC LIMIT 200"),
        {"ws": principal.workspace.id})).mappings().all()
    visible = [dict(row) for row in rows if principal.is_commander
               or "*" in principal.allowed_namespaces
               or row["namespace"] in principal.allowed_namespaces]
    workspace_mode = await automation.current_mode(session, principal.workspace.id)
    return {"items": [{**row, "policy": row["policy"] or {},
                       "mode": (row["policy"] or {}).get("mode") or workspace_mode,
                       "updated_at": _iso(row["updated_at"])} for row in visible]}


async def replay(session: AsyncSession, principal: Principal, key: str,
                 chunk_key: str | None = None) -> dict[str, Any]:
    """M-11: the diagram's chain (define, confirm, freeze, dispatch, gate, merge,
    changes) from the event log, or one chunk's chain with its task's events."""
    diagram = await _diagram(session, principal, key)
    rows = (await session.execute(text(
        "SELECT id, type, actor, task_key, payload->'details' AS details, created_at "
        "FROM workspace_events WHERE workspace_id = :ws AND source_table = 'timeline_streams' "
        "AND payload->'details'->>'diagram_key' = :k ORDER BY id"),
        {"ws": principal.workspace.id, "k": key})).mappings().all()
    events = []
    for row in rows:
        details = row["details"] or {}
        if chunk_key and details.get("chunk_key") != chunk_key:
            continue
        events.append({"seq": row["id"], "type": row["type"], "actor": row["actor"],
                       "at": _iso(row["created_at"]), "details": details})
    result: dict[str, Any] = {"diagram_key": key, "chunk_key": chunk_key, "events": events}
    if chunk_key:
        task_key = await session.scalar(text(
            "SELECT task_key FROM diagram_chunks WHERE diagram_id = :d AND chunk_key = :c"),
            {"d": diagram["id"], "c": chunk_key})
        if task_key:
            task_rows = (await session.execute(text(
                "SELECT id, type, actor, status, created_at FROM workspace_events WHERE "
                "workspace_id = :ws AND task_key = :t AND source_table = 'collab_tasks' "
                "ORDER BY id"), {"ws": principal.workspace.id, "t": task_key})).mappings().all()
            result["task_events"] = [{"seq": r["id"], "type": r["type"], "actor": r["actor"],
                                      "status": r["status"], "at": _iso(r["created_at"])}
                                     for r in task_rows]
    return result
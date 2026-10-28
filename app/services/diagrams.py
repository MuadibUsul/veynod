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

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, can
from app.errors import ApiError, Forbidden, NotFound
from app.services.common import append_event

LAYERS = ("interface", "implementation")
IN_FLIGHT = ("dispatched", "in_progress", "awaiting_gate", "ready_to_merge", "merged")
SELF_CHECK = ("big", "stable", "machine_checkable", "long")
CHUNK_FIELDS = ("title", "scope", "layer", "icd", "done_criteria", "depends_on",
                "allowed_write_paths", "read_only_interfaces")


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
    }


def _incomplete(chunk: dict[str, Any]) -> list[str]:
    """M-06: a chunk without a contract or checkable criteria cannot be dispatched."""
    missing = []
    if not chunk["icd"]:
        missing.append("icd")
    if not chunk["done_criteria"]:
        missing.append("done_criteria")
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
        raise ApiError(422, "chunk_incomplete",
                       "every chunk needs an icd and done_criteria, and known dependencies",
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


def status_of(chunk: dict[str, Any], task: dict[str, Any] | None) -> str:
    """The status readers see: the chunk's own record, read through its task."""
    state = chunk["state"]
    if state in ("pending", "blocked", "merged", "retired"):
        return state
    gate = chunk.get("gate") or {}
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
                 event_type: str, **details: Any) -> None:
    await append_event(
        session, workspace_id=principal.workspace.id, agent=_me(principal),
        event_type=event_type, namespace=diagram["namespace"],
        payload={"diagram_key": diagram["diagram_key"], "doc_key": diagram["doc_key"],
                 "task_key": details.get("task_key"),
                 "details": {"diagram_key": diagram["diagram_key"], **details}})


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
        "description, self_check, created_by) VALUES (:ws, :k, :t, :ns, :doc, :d, "
        "CAST(:sc AS jsonb), :by) RETURNING *"),
        {"ws": principal.workspace.id, "k": key, "t": str(body.get("title") or key)[:256],
         "ns": namespace, "doc": f"diagrams/{key}", "d": body.get("description"),
         "sc": json.dumps(self_check), "by": _me(principal)})).mappings().one()
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
        "icd, done_criteria, depends_on, allowed_write_paths, read_only_interfaces, origin, "
        "increment_of, created_in_version) VALUES (:ws, :d, :k, :t, :s, :l, CAST(:icd AS jsonb), "
        "CAST(:dc AS jsonb), CAST(:deps AS text[]), CAST(:w AS text[]), CAST(:r AS text[]), "
        ":o, :inc, :v)"),
        {"ws": principal.workspace.id, "d": diagram["id"], "k": chunk["chunk_key"],
         "t": chunk["title"], "s": chunk["scope"], "l": chunk["layer"],
         "icd": json.dumps(chunk["icd"]), "dc": json.dumps(chunk["done_criteria"]),
         "deps": chunk["depends_on"], "w": chunk["allowed_write_paths"],
         "r": chunk["read_only_interfaces"], "o": origin, "inc": increment_of,
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
        "updated_at = NOW() WHERE id = :id RETURNING *"),
        {"v": version, "t": body.get("title"), "desc": body.get("description"),
         "sc": json.dumps({item: body["self_check"].get(item) for item in SELF_CHECK})
         if isinstance(body.get("self_check"), dict) else None,
         "id": diagram["id"]})).mappings().one())
    for chunk in chunks:
        await _insert_chunk(session, principal, diagram, chunk)
    await _write_document(session, principal, diagram, await _chunks(session, diagram["id"]))
    await _event(session, principal, diagram, "DIAGRAM_EDITED", version=version)
    await session.commit()
    return await view(session, principal, key)


async def confirm(session: AsyncSession, principal: Principal, key: str) -> dict[str, Any]:
    _require_person(principal, "confirming a diagram")
    diagram = await _diagram(session, principal, key, lock=True)
    if diagram["status"] != "draft":
        raise ApiError(409, "invalid_transition", f"the diagram is already {diagram['status']}")
    chunks = await _chunks(session, diagram["id"])
    if not chunks:
        raise ApiError(422, "validation_error", "a diagram needs at least one chunk")
    diagram = dict((await session.execute(text(
        "UPDATE diagrams SET status = 'confirmed', confirmed_by = :by, confirmed_at = NOW(), "
        "updated_at = NOW() WHERE id = :id RETURNING *"),
        {"by": _me(principal), "id": diagram["id"]})).mappings().one())
    await _write_document(session, principal, diagram, chunks)
    await _event(session, principal, diagram, "DIAGRAM_CONFIRMED", version=diagram["version"])
    await session.commit()
    return await view(session, principal, key)


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


async def _dispatch_one(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                        chunk: dict[str, Any],
                        chunks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    waiting = [dep for dep in chunk["depends_on"]
               if chunks.get(dep, {}).get("state") != "merged"]
    if waiting:
        reason = "waiting on " + ", ".join(
            f"{dep} ({chunks[dep]['state'] if dep in chunks else 'missing'})" for dep in waiting)
        await session.execute(text(
            "UPDATE diagram_chunks SET state = 'blocked', block_reason = :r, updated_at = NOW() "
            "WHERE id = :id"), {"r": reason, "id": chunk["id"]})
        if chunk["state"] != "blocked" or chunk["block_reason"] != reason:
            await _event(session, principal, diagram, "CHUNK_BLOCKED",
                         chunk_key=chunk["chunk_key"], reason=reason, waiting_on=waiting)
        return {"chunk_key": chunk["chunk_key"], "state": "blocked", "block_reason": reason,
                "waiting_on": waiting}
    task_key = await _create_task(session, principal, diagram, chunk, chunks)
    await session.execute(text(
        "UPDATE diagram_chunks SET state = 'dispatched', block_reason = NULL, task_key = :t, "
        "updated_at = NOW() WHERE id = :id"), {"t": task_key, "id": chunk["id"]})
    await _event(session, principal, diagram, "CHUNK_DISPATCHED", chunk_key=chunk["chunk_key"],
                 task_key=task_key, version=diagram["version"])
    return {"chunk_key": chunk["chunk_key"], "state": "dispatched", "task_key": task_key}


async def _create_task(session: AsyncSession, principal: Principal, diagram: dict[str, Any],
                       chunk: dict[str, Any], chunks: dict[str, dict[str, Any]]) -> str:
    """§2 / §14 mapping: title, the diagram document, done criteria as acceptance,
    and the dispatch slice (contract, upstream interfaces, write scope) in
    input_context. Upstream implementation is never included (M-16)."""
    from app.schemas import AcceptanceItem, DocRef, TaskCreateRequest
    from app.services.tasks import _create_task_core

    upstream = {dep: {"icd": chunks[dep]["icd"], "layer": chunks[dep]["layer"],
                      "read_only_interfaces": chunks[dep]["read_only_interfaces"]}
                for dep in chunk["depends_on"] if dep in chunks}
    task_key = f"DG-{diagram['diagram_key']}-{chunk['chunk_key']}"[:128]
    taken = await session.scalar(text(
        "SELECT count(*) FROM collab_tasks WHERE workspace_id = :ws AND task_key LIKE :k"),
        {"ws": principal.workspace.id, "k": f"{task_key}%"})
    if taken:
        task_key = f"{task_key}-{int(taken) + 1}"[:128]
    request = TaskCreateRequest(
        task_key=task_key,
        title=f"[{diagram['diagram_key']}] {chunk['title']}"[:256],
        description=(f"{chunk['scope']}\n\nInterface contract (ICD):\n"
                     f"{json.dumps(chunk['icd'], ensure_ascii=False, indent=2)}")[:20_000],
        namespace=diagram["namespace"],
        # Chunks are implementation work, whoever dispatches them.
        assigned_role="EXECUTOR",
        context_docs=[DocRef(namespace=diagram["namespace"], doc_key=diagram["doc_key"])],
        acceptance=[AcceptanceItem(id=f"DC-{index + 1}", kind="AUTO", text=criterion[:2_000])
                    for index, criterion in enumerate(chunk["done_criteria"][:50])],
        done_definition="All done criteria pass, the gate passes, and someone other than "
                        "the implementer merges (spec M-09/M-10).",
        input_context={
            "diagram": {"key": diagram["diagram_key"], "version": diagram["version"],
                        "baseline": (diagram.get("baseline") or {}).get("version")},
            "chunk": chunk["chunk_key"], "layer": chunk["layer"], "icd": chunk["icd"],
            "upstream_interfaces": upstream,
            "allowed_write_paths": chunk["allowed_write_paths"],
            "read_only_interfaces": chunk["read_only_interfaces"],
        },
    )
    task = await _create_task_core(session, principal, request, agent=_me(principal),
                                   batch=uuid.uuid4(), commit=False)
    return task.task_key


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
    await session.execute(text(
        "UPDATE diagram_chunks SET gate = CAST(:g AS jsonb), state = :s, updated_at = NOW() "
        "WHERE id = :id"),
        {"g": json.dumps(record), "s": "dispatched" if result == "pass" else "rework",
         "id": chunk["id"]})
    await _event(session, principal, diagram, "CHUNK_GATED", chunk_key=chunk_key,
                 task_key=chunk["task_key"], result=result, evidence=evidence)
    await session.commit()
    return {"chunk_key": chunk_key, "gate": record,
            "status": "ready_to_merge" if result == "pass" else "rework"}


async def merge(session: AsyncSession, principal: Principal, key: str, chunk_key: str,
                body: dict[str, Any]) -> dict[str, Any]:
    """M-10: only after a passing gate, and never by the implementer. Merging
    unblocks and dispatches the chunks that waited on it (M-14)."""
    diagram, chunk, task = await _chunk_with_task(session, principal, key, chunk_key)
    if chunk["state"] == "merged":
        raise ApiError(409, "already_merged", f"chunk '{chunk_key}' is merged")
    if status_of(chunk, task) != "ready_to_merge":
        raise ApiError(409, "gate_not_passed", "merge follows a passing gate on the current "
                       "submission", details={"status": status_of(chunk, task)})
    me = _me(principal)
    if me in await _implementers(session, principal, task, chunk["task_key"]):
        raise ApiError(403, "merge_by_implementer", "the implementer cannot merge their own "
                       "chunk (spec M-10)", details={"merger": me})
    record = {"by": me, "at": _now().isoformat(), "artifact": body.get("artifact"),
              "note": body.get("note"), "task_status": task["status"] if task else None}
    await session.execute(text(
        "UPDATE diagram_chunks SET state = 'merged', merge = CAST(:m AS jsonb), "
        "block_reason = NULL, updated_at = NOW() WHERE id = :id"),
        {"m": json.dumps(record), "id": chunk["id"]})
    await _event(session, principal, diagram, "CHUNK_MERGED", chunk_key=chunk_key,
                 task_key=chunk["task_key"], merged_by=me, artifact=body.get("artifact"))
    unblocked = await _release_waiting(session, principal, diagram)
    await session.commit()
    return {"chunk_key": chunk_key, "state": "merged", "merge": record, "unblocked": unblocked}


async def _release_waiting(session: AsyncSession, principal: Principal,
                           diagram: dict[str, Any]) -> list[dict[str, Any]]:
    chunks = {chunk["chunk_key"]: chunk for chunk in await _chunks(session, diagram["id"])}
    released = []
    for chunk in chunks.values():
        if chunk["state"] != "blocked":
            continue
        if all(chunks.get(dep, {}).get("state") == "merged" for dep in chunk["depends_on"]):
            outcome = await _dispatch_one(session, principal, diagram, chunk, chunks)
            await _event(session, principal, diagram, "CHUNK_UNBLOCKED",
                         chunk_key=chunk["chunk_key"], task_key=outcome.get("task_key"))
            released.append(outcome)
    return released


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
    dispatched = []
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
        "updated_at = NOW() WHERE id = :id"),
        {"t": chunk["title"], "s": chunk["scope"], "l": chunk["layer"],
         "icd": json.dumps(chunk["icd"]), "dc": json.dumps(chunk["done_criteria"]),
         "deps": chunk["depends_on"], "w": chunk["allowed_write_paths"],
         "r": chunk["read_only_interfaces"], "id": chunk_id})


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
        await session.execute(text(
            "UPDATE collab_tasks SET status = 'CANCELLED', version = version + 1, "
            "updated_at = NOW() WHERE workspace_id = :ws AND task_key = :k"),
            {"ws": principal.workspace.id, "k": task_key})
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
    diagram = await _diagram(session, principal, key)
    chunks = await _chunks(session, diagram["id"])
    tasks = await _tasks(session, principal, [c["task_key"] for c in chunks if c["task_key"]])
    items, distribution, blocked, queue, mismatches = [], {}, [], [], []
    for chunk in chunks:
        task = tasks.get(chunk["task_key"]) if chunk["task_key"] else None
        status = status_of(chunk, task)
        if chunk["task_key"] and task is None:
            mismatches.append({"chunk_key": chunk["chunk_key"], "task_key": chunk["task_key"],
                               "problem": "the dispatched task is gone from the board"})
        if status == "merged" and task and task["status"] in ("FAILED", "CANCELLED"):
            mismatches.append({"chunk_key": chunk["chunk_key"], "task_key": chunk["task_key"],
                               "problem": f"merged, but its task is {task['status']}"})
        if status != "retired":
            distribution[status] = distribution.get(status, 0) + 1
        if status == "blocked":
            blocked.append({"chunk_key": chunk["chunk_key"], "reason": chunk["block_reason"]})
        if status == "ready_to_merge":
            queue.append({"chunk_key": chunk["chunk_key"], "task_key": chunk["task_key"],
                          "gated_at": (chunk["gate"] or {}).get("at")})
        items.append({
            "chunk_key": chunk["chunk_key"], "title": chunk["title"], "scope": chunk["scope"],
            "layer": chunk["layer"], "icd": chunk["icd"], "done_criteria": chunk["done_criteria"],
            "depends_on": chunk["depends_on"], "allowed_write_paths": chunk["allowed_write_paths"],
            "read_only_interfaces": chunk["read_only_interfaces"], "status": status,
            "state": chunk["state"], "block_reason": chunk["block_reason"],
            "task_key": chunk["task_key"], "task_status": task["status"] if task else None,
            "gate": chunk["gate"], "merge": chunk["merge"], "origin": chunk["origin"],
            "increment_of": chunk["increment_of"],
            "created_in_version": chunk["created_in_version"]})
    pending = (await session.execute(text(
        "SELECT change_key, summary, source, created_at FROM diagram_changes WHERE "
        "diagram_id = :d AND status = 'pending' ORDER BY id"), {"d": diagram["id"]})
    ).mappings().all()
    return {
        "diagram_key": diagram["diagram_key"], "title": diagram["title"],
        "namespace": diagram["namespace"], "description": diagram["description"],
        "status": diagram["status"], "version": diagram["version"],
        "baseline": diagram["baseline"], "document": f"doc://{diagram['doc_key']}",
        "created_by": diagram["created_by"], "confirmed_by": diagram["confirmed_by"],
        "confirmed_at": _iso(diagram["confirmed_at"]),
        "self_check": self_check_risk(diagram["self_check"] or {}),
        "chunks": items,
        "progress": {"distribution": distribution, "blocked": blocked, "merge_queue": queue,
                     "total": sum(distribution.values())},
        "board_consistency": {"ok": not mismatches, "mismatches": mismatches,
                              "rule": "the task board is the truth (spec M-08)"},
        "pending_changes": [{**dict(row), "created_at": _iso(row["created_at"])}
                            for row in pending],
    }


async def list_diagrams(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    rows = (await session.execute(text(
        "SELECT diagram_key, title, namespace, status, version, updated_at FROM diagrams "
        "WHERE workspace_id = :ws ORDER BY updated_at DESC LIMIT 200"),
        {"ws": principal.workspace.id})).mappings().all()
    visible = [dict(row) for row in rows if principal.is_commander
               or "*" in principal.allowed_namespaces
               or row["namespace"] in principal.allowed_namespaces]
    return {"items": [{**row, "updated_at": _iso(row["updated_at"])} for row in visible]}


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

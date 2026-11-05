"""Fulfilment proofs and multi-dimensional reputation (P4-1).

Contract: docs/federation/ICD_P4_REPUTATION.md (vision §12.1–12.4).

* When work a provider took on ends (accepted, disputed, cancelled, stopped
  without confirmation, failed), the requester's federation key signs a
  ``fulfillment.proof``: ids, hashes, times and the outcome, never content.
  Anyone can re-verify a proof against the requester's published key.
* Reputation is a set of separate measures, each with its sample size and
  an "enough evidence" flag. There is no single score.
* Each proof counts with a weight that grows with the depth of the
  cooperation between the two workspaces (how many jobs, over how long), and
  what one requester can contribute is capped, so a single partner cannot
  make a reputation on its own (collusion checks proper are P4-3).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.database import async_session_factory
from app.services.federation import identity, keys
from app.services.federation.canonical import sha256_hex
from app.services.federation.envelope import signing_bytes
from app.services.federation.errors import FederationError
from app.services.federation.ids import b64url_decode, require_type

OUTCOMES = {
    "ACCEPTED_RESULT": "accepted",
    "DISPUTED": "disputed",
    "CANCELLED": "cancelled",
    "STOP_NOT_CONFIRMED": "stop_not_confirmed",
    "FAILED": "failed",
}
PROOF_TTL = timedelta(days=3650)
MIN_PROOFS = 3  # a rate needs at least this many proofs behind it
# Depth weights: a pair's first job counts half, its 2nd-4th jobs fully, and
# from the 5th job on, once the pair has worked together for 30 days, 1.5x.
FIRST_JOB_WEIGHT = 0.5
REGULAR_WEIGHT = 1.0
DEEP_WEIGHT = 1.5
DEEP_AFTER_JOBS = 5
DEEP_AFTER_DAYS = 30
PAIR_CAP = 5.0  # most weight one requester can add to a provider's measures


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def issue_due(limit: int = 200) -> int:
    """Sign a proof for each finished delegation that has none yet (run by
    the federation background loop and after a requester closes a job)."""
    states = tuple(OUTCOMES)
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT d.*, m.federation_id AS requester_fid "
                        "FROM federation_delegations d "
                        "JOIN federation_local_map m ON m.workspace_id = d.workspace_id "
                        "AND m.id_type = 'workspace' WHERE d.role = 'requester' "
                        "AND d.state = ANY(:states) AND NOT EXISTS (SELECT 1 FROM "
                        "federation_proofs p WHERE p.requester_fid = m.federation_id AND "
                        "p.provider_fid = d.peer_workspace_fid AND p.federation_job_id = "
                        "d.federation_job_id) ORDER BY d.updated_at LIMIT :l"
                    ),
                    {"states": list(states), "l": limit},
                )
            )
            .mappings()
            .all()
        )
    issued = 0
    for row in rows:
        try:
            issued += await _issue(dict(row))
        except FederationError:
            continue  # e.g. the requester's identity is frozen: try again later
    return issued


async def _issue(row: dict[str, Any]) -> int:
    async with async_session_factory() as platform:
        history = (
            (
                await platform.execute(
                    text(
                        "SELECT to_state, created_at FROM federation_delegation_history "
                        "WHERE workspace_id = :w AND peer_workspace_fid = :p AND "
                        "federation_job_id = :j ORDER BY version"
                    ),
                    {
                        "w": row["workspace_id"],
                        "p": row["peer_workspace_fid"],
                        "j": row["federation_job_id"],
                    },
                )
            )
            .mappings()
            .all()
        )
    first = {}
    for line in history:
        first.setdefault(line["to_state"], line["created_at"])
    if not ({"ACCEPTED", "RUNNING", "SUBMITTED"} & set(first)):
        return 0  # never taken on: not a fulfilment (response speed covers it)
    closed_at = first.get(row["state"]) or row["updated_at"]
    terms = _json(row["terms"]) or {}
    result = _json(row["result"])
    payload = {
        "v": 1,
        "federation_job_id": row["federation_job_id"],
        "requester": row["requester_fid"],
        "provider": row["peer_workspace_fid"],
        "outcome": OUTCOMES[row["state"]],
        "data_classification": terms.get("data_classification"),
        "external_side_effects": bool(terms.get("external_side_effects")),
        "acceptance_hash": row["acceptance_hash"],
        "result_sha256": sha256_hex(result) if result else None,
        "revisions": int(row["revisions"] or 0),
        "offered_at": _iso(row["created_at"]),
        "accepted_at": _iso(first.get("ACCEPTED")),
        "submitted_at": _iso(first.get("SUBMITTED")),
        "closed_at": _iso(closed_at),
    }
    proof = await identity.sign_as(
        row["requester_fid"],
        type="fulfillment.proof",
        audience=None,
        payload=payload,
        ttl=PROOF_TTL,
    )
    async with async_session_factory() as platform:
        written = await platform.scalar(
            text(
                "INSERT INTO federation_proofs (proof_id, requester_fid, provider_fid, "
                "federation_job_id, outcome, closed_at, proof) VALUES (:id, :r, :p, :j, :o, "
                ":c, CAST(:proof AS jsonb)) ON CONFLICT (requester_fid, provider_fid, "
                "federation_job_id) DO NOTHING RETURNING proof_id"
            ),
            {
                "id": proof["object_id"],
                "r": payload["requester"],
                "p": payload["provider"],
                "j": payload["federation_job_id"],
                "o": payload["outcome"],
                "c": closed_at,
                "proof": json.dumps(proof),
            },
        )
        await platform.commit()
    return 1 if written else 0


async def verify_proof(proof: Any) -> dict[str, Any]:
    """Check a proof on its own: shape, payload hash, and the requester's
    signature with the requester's published key."""
    if not isinstance(proof, dict) or proof.get("type") != "fulfillment.proof":
        raise FederationError("FED_E_PROOF_INVALID", "not a fulfilment proof")
    payload = proof.get("payload")
    if not isinstance(payload, dict) or payload.get("outcome") not in OUTCOMES.values():
        raise FederationError("FED_E_PROOF_INVALID", "the proof's payload is malformed")
    if sha256_hex(payload) != proof.get("payload_sha256"):
        raise FederationError("FED_E_PROOF_INVALID", "the payload does not match its hash")
    if proof.get("issuer") != payload.get("requester") or proof.get("audience") is not None:
        raise FederationError("FED_E_PROOF_INVALID", "a proof is signed by the requester")
    key = await identity.key_lookup(proof.get("issuer", ""), proof.get("key_id", ""))
    if key is None or key.state == "revoked":
        raise FederationError("FED_E_BAD_SIGNATURE", "the requester's key is unknown or revoked")
    try:
        good = keys.verify(key.public_raw, b64url_decode(proof["signature"]), signing_bytes(proof))
    except (KeyError, ValueError) as exc:
        raise FederationError(
            "FED_E_PROOF_INVALID", "the proof carries no usable signature"
        ) from exc
    if not good:
        raise FederationError("FED_E_BAD_SIGNATURE", "the proof's signature does not verify")
    return {
        "valid": True,
        "proof_id": proof["object_id"],
        "issuer": proof["issuer"],
        "key_id": proof["key_id"],
        "provider": payload["provider"],
        "outcome": payload["outcome"],
    }


async def _rows(provider_fid: str) -> list[dict[str, Any]]:
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT * FROM federation_proofs WHERE provider_fid = :p "
                        "ORDER BY closed_at, proof_id"
                    ),
                    {"p": provider_fid},
                )
            )
            .mappings()
            .all()
        )
    return [{**dict(r), "proof": _json(r["proof"])} for r in rows]


def weigh(rows: list[dict[str, Any]]) -> list[float]:
    """Each proof's weight from the depth of its pair, capped per requester."""
    by_pair: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        by_pair[row["requester_fid"]].append(index)
    weights = [0.0] * len(rows)
    for indexes in by_pair.values():
        start = rows[indexes[0]]["closed_at"]
        raw = []
        for position, index in enumerate(indexes, start=1):
            if position == 1:
                raw.append(FIRST_JOB_WEIGHT)
            elif position >= DEEP_AFTER_JOBS and rows[index]["closed_at"] - start >= timedelta(
                days=DEEP_AFTER_DAYS
            ):
                raw.append(DEEP_WEIGHT)
            else:
                raw.append(REGULAR_WEIGHT)
        total = sum(raw)
        scale = min(1.0, PAIR_CAP / total) if total else 1.0
        for index, value in zip(indexes, raw, strict=True):
            weights[index] = round(value * scale, 4)
    return weights


def _rate(hits: float, total: float, samples: int) -> dict[str, Any]:
    enough = samples >= MIN_PROOFS and total > 0
    return {
        "rate": round(hits / total, 3) if enough else None,
        "weighted_hits": round(hits, 3),
        "weighted_total": round(total, 3),
        "samples": samples,
        "enough_evidence": enough,
    }


async def profile(fid: str) -> dict[str, Any]:
    """A provider's reputation, one measure per dimension, with its evidence."""
    from app.services.federation import discovery, integrity, privacy

    require_type(fid, "workspace")
    rows = await _rows(fid)
    check = await integrity.assess(fid, rows)
    # Depth weight times the integrity factor (P4-3): self-dealing and pairs
    # pending verification count for nothing, new requesters for a quarter.
    weights = [round(w * f, 4) for w, f in zip(weigh(rows), check["factors"], strict=True)]
    counted = [f > 0 for f in check["factors"]]
    outcome = [r["outcome"] for r in rows]
    delivered = {"accepted", "disputed"}

    completed_w = sum(w for w, o in zip(weights, outcome, strict=True) if o in delivered)
    all_w = sum(weights)
    accepted_w = sum(w for w, o in zip(weights, outcome, strict=True) if o == "accepted")
    judged = [i for i, o in enumerate(outcome) if o in delivered and counted[i]]
    first_pass_w = sum(
        weights[i]
        for i in judged
        if outcome[i] == "accepted" and int(rows[i]["proof"]["payload"].get("revisions", 0)) == 0
    )
    judged_w = sum(weights[i] for i in judged)

    pairs: dict[str, list[datetime]] = defaultdict(list)
    for row in rows:
        pairs[row["requester_fid"]].append(row["closed_at"])
    spans = [(max(t) - min(t)).days for t in pairs.values()]

    async with async_session_factory() as platform:
        speed = (
            (
                await platform.execute(
                    text(
                        f"SELECT median, samples FROM ({discovery._RESPONSE_SQL}) s "
                        "WHERE s.fid = :f"
                    ),
                    {"f": fid},
                )
            )
            .mappings()
            .first()
        )
        skills = (
            (
                await platform.execute(
                    text(
                        "SELECT count(DISTINCT p.name) AS skills, avg(r.stars)::float AS avg, "
                        "count(r.stars) AS n FROM registry_packages p LEFT JOIN registry_ratings "
                        "r ON r.name = p.name WHERE p.publisher_fid = :f"
                    ),
                    {"f": fid},
                )
            )
            .mappings()
            .one()
        )
        sanctions = (
            (
                await platform.execute(
                    text(
                        "SELECT count(*) FILTER (WHERE state = 'active' AND (expires_at IS NULL "
                        "OR expires_at > NOW())) AS in_force, count(*) FILTER (WHERE state IN "
                        "('active', 'lifted')) AS ever FROM federation_sanctions "
                        "WHERE subject_fid = :f"
                    ),
                    {"f": fid},
                )
            )
            .mappings()
            .one()
        )

    return {
        "fid": fid,
        "single_score": None,  # by design: read the dimensions
        "completion": {
            **_rate(completed_w, all_w, sum(counted)),
            "meaning": "work taken on that was delivered (accepted or disputed after delivery)",
            "counts": {o: outcome.count(o) for o in OUTCOMES.values()},
        },
        "acceptance": {
            **_rate(accepted_w, judged_w, len(judged)),
            "meaning": "delivered work the requester accepted",
            "first_pass": _rate(first_pass_w, judged_w, len(judged)),
        },
        "response": discovery._response(
            speed["median"] if speed else None, speed["samples"] if speed else 0
        ),
        "skills": {
            "published": int(skills["skills"] or 0),
            **discovery._rating(skills["avg"], int(skills["n"] or 0)),
        },
        "governance": {
            "sanctions_in_force": int(sanctions["in_force"] or 0),
            "sanctions_ever": int(sanctions["ever"] or 0),
            # Open reports are not shown: a report is not a finding (vision
            # §13.1). Resolved ones are, without anything private (P4-4).
            "open_reports": "not shown",
            "complaints": await privacy.complaint_record(fid),
        },
        "cooperation": {
            "counterparts": len(pairs),
            "repeat_counterparts": sum(1 for t in pairs.values() if len(t) > 1),
            "first_at": _iso(rows[0]["closed_at"]) if rows else None,
            "last_at": _iso(rows[-1]["closed_at"]) if rows else None,
            "longest_partnership_days": max(spans) if spans else 0,
        },
        "weighting": {
            "first_job_with_a_requester": FIRST_JOB_WEIGHT,
            "later_jobs": REGULAR_WEIGHT,
            "deep_cooperation": DEEP_WEIGHT,
            "deep_after": {"jobs": DEEP_AFTER_JOBS, "days": DEEP_AFTER_DAYS},
            "cap_per_requester": PAIR_CAP,
            "minimum_samples": MIN_PROOFS,
        },
        "proofs": len(rows),
        "integrity": {k: v for k, v in check.items() if k not in ("factors", "reasons", "_pairs")},
        "evidence": {
            "signed_by_counterparts": ["completion", "acceptance", "cooperation"],
            "measured_by_platform": ["response"],
            "from_other_workspaces": ["skills"],
            "platform_decisions": ["governance"],
        },
    }


async def proofs(
    fid: str, *, limit: int = 100, viewer_fid: str | None = None
) -> list[dict[str, Any]]:
    """The signed proofs behind a provider's measures, newest first.

    Who worked with whom is private (vision §9.5, §12.4): the provider sees
    all of its proofs, a requester only the proofs it signed itself. Weights
    are computed over all proofs either way. ``viewer_fid=None`` is the
    platform itself."""
    from app.services.federation import integrity

    require_type(fid, "workspace")
    rows = await _rows(fid)
    check = await integrity.assess(fid, rows)
    weights = weigh(rows)
    out = [
        {"proof": row["proof"], "weight": round(weight * factor, 4), "counted_as": why}
        for row, weight, factor, why in zip(
            rows, weights, check["factors"], check["reasons"], strict=True
        )
        if viewer_fid is None or viewer_fid == fid or row["requester_fid"] == viewer_fid
    ]
    return list(reversed(out))[: max(1, min(int(limit), 500))]

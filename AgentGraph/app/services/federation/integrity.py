"""Reputation integrity: same-controller, collusion and cold-start rules (P4-3).

Contract: docs/federation/ICD_P4_REPUTATION.md §10 (vision §12.3, F-025).

Each fulfilment proof gets a trust factor on top of its depth weight:

* 0, "same_controller": requester and provider share a controller (the same
  subject on this deployment, or linked by an operator). Self-dealing never
  counts, whatever the volume.
* 0, "pending_verification": the pair looks like farming (work flowing both
  ways between the two, or jobs closed implausibly fast) and no operator has
  confirmed it as genuine yet. A pair judged collusive stays at 0 and is
  linked as one controller.
* 0.25, "fresh_requester": the requester's identity was under 7 days old when
  it signed. New addresses are cheap; their word grows with age.
* 1 otherwise.

A provider is in its cold start for its first 14 days: its measures are
shown, but the directory holds its evidence at the neutral value, so early
volume cannot buy a place. Batches of identities created together under
one subject are flagged.

Nothing here punishes on suspicion alone: flagged proofs stop counting
until a person looks; no sanction follows from a flag (vision §12.3).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from app.database import async_session_factory
from app.services.federation.errors import FederationError
from app.services.federation.ids import require_type

COLD_START_DAYS = 14
FRESH_REQUESTER_DAYS = 7
FRESH_REQUESTER_FACTOR = 0.25
RECIPROCAL_MIN = 2  # jobs each way before a two-way flow is flagged
FAST_SECONDS = 600  # offer to close under 10 minutes ...
FAST_MIN = 3  # ... this many times for one pair is flagged
CONCENTRATION = 0.6  # one requester behind this share of proofs (informational)
CONCENTRATION_MIN = 5
BATCH_WINDOW = timedelta(hours=24)
BATCH_MIN = 5  # workspaces created under one subject within the window


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def _identities(fids: set[str]) -> dict[str, dict[str, Any]]:
    if not fids:
        return {}
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT federation_id, parent_id, created_at, status FROM "
                        "federation_identities WHERE federation_id = ANY(:f)"
                    ),
                    {"f": list(fids)},
                )
            )
            .mappings()
            .all()
        )
    return {r["federation_id"]: dict(r) for r in rows}


async def related(a: str, b: str) -> list[str]:
    """Why two workspaces count as one controller (empty: they do not)."""
    if a == b:
        return ["same_identity"]
    info = await _identities({a, b})
    reasons = []
    if (
        a in info
        and b in info
        and info[a]["parent_id"]
        and (info[a]["parent_id"] == info[b]["parent_id"])
    ):
        reasons.append("same_subject")
    fa, fb = _pair(a, b)
    async with async_session_factory() as platform:
        if await platform.scalar(
            text("SELECT 1 FROM federation_controller_links WHERE fid_a = :a AND fid_b = :b"),
            {"a": fa, "b": fb},
        ):
            reasons.append("operator_linked")
    return reasons


async def assess(provider_fid: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Trust factors for a provider's proofs (same order as ``rows``)."""
    requesters = {r["requester_fid"] for r in rows}
    info = await _identities(requesters | {provider_fid})
    provider = info.get(provider_fid, {})
    async with async_session_factory() as platform:
        links = {
            (r["fid_a"], r["fid_b"])
            for r in (
                await platform.execute(
                    text(
                        "SELECT fid_a, fid_b FROM federation_controller_links "
                        "WHERE fid_a = :p OR fid_b = :p"
                    ),
                    {"p": provider_fid},
                )
            ).mappings()
        }
        reviews = {
            r["requester_fid"]: r["verdict"]
            for r in (
                await platform.execute(
                    text(
                        "SELECT requester_fid, verdict FROM federation_pair_reviews "
                        "WHERE provider_fid = :p"
                    ),
                    {"p": provider_fid},
                )
            ).mappings()
        }
        # Work flowing the other way: the provider as requester of each partner.
        reverse = {
            r["provider_fid"]: int(r["n"])
            for r in (
                await platform.execute(
                    text(
                        "SELECT provider_fid, count(*) AS n FROM federation_proofs "
                        "WHERE requester_fid = :p GROUP BY provider_fid"
                    ),
                    {"p": provider_fid},
                )
            ).mappings()
        }
        batch = 0
        if provider.get("parent_id"):
            batch = int(
                await platform.scalar(
                    text(
                        "SELECT count(*) FROM federation_identities WHERE parent_id = :s AND "
                        "id_type = 'workspace' AND created_at BETWEEN :t0 AND :t1"
                    ),
                    {
                        "s": provider["parent_id"],
                        "t0": provider["created_at"] - BATCH_WINDOW,
                        "t1": provider["created_at"] + BATCH_WINDOW,
                    },
                )
                or 0
            )

    forward: dict[str, int] = defaultdict(int)
    fast: dict[str, int] = defaultdict(int)
    for row in rows:
        forward[row["requester_fid"]] += 1
        payload = row["proof"]["payload"]
        opened, closed = _ts(payload.get("offered_at")), _ts(payload.get("closed_at"))
        if opened and closed and (closed - opened).total_seconds() < FAST_SECONDS:
            fast[row["requester_fid"]] += 1

    pair_flags: dict[str, list[str]] = defaultdict(list)
    for requester in requesters:
        if forward[requester] >= RECIPROCAL_MIN and reverse.get(requester, 0) >= RECIPROCAL_MIN:
            pair_flags[requester].append("two_way_flow")
        if fast[requester] >= FAST_MIN:
            pair_flags[requester].append("implausibly_fast")

    same_controller = {}
    for requester in requesters:
        reasons = []
        req = info.get(requester, {})
        if req.get("parent_id") and req.get("parent_id") == provider.get("parent_id"):
            reasons.append("same_subject")
        if _pair(requester, provider_fid) in links:
            reasons.append("operator_linked")
        if reviews.get(requester) == "collusive":
            reasons.append("judged_collusive")
        same_controller[requester] = reasons

    factors: list[float] = []
    reasons: list[str] = []
    for row in rows:
        requester = row["requester_fid"]
        verdict = reviews.get(requester)
        if same_controller[requester]:
            factor, why = 0.0, "same_controller"
        elif pair_flags[requester] and verdict != "genuine":
            factor, why = 0.0, "pending_verification"
        else:
            req_created = info.get(requester, {}).get("created_at")
            age = row["closed_at"] - req_created if req_created else timedelta(days=365)
            if age < timedelta(days=FRESH_REQUESTER_DAYS) and verdict != "genuine":
                factor, why = FRESH_REQUESTER_FACTOR, "fresh_requester"
            else:
                factor, why = 1.0, "counted"
        factors.append(factor)
        reasons.append(why)

    total = len(rows)
    top = max(forward.values()) if forward else 0
    flags = []
    if any(same_controller.values()):
        flags.append("self_dealing_excluded")
    if any(pair_flags.values()):
        flags.append("pending_verification")
    if total >= CONCENTRATION_MIN and top / total >= CONCENTRATION:
        flags.append("concentrated_on_one_requester")
    if batch >= BATCH_MIN:
        flags.append("created_in_a_batch")
    created = provider.get("created_at")
    cold = bool(created and datetime.now(timezone.utc) - created < timedelta(days=COLD_START_DAYS))
    if cold:
        flags.append("cold_start")
    return {
        "factors": factors,
        "reasons": reasons,
        "flags": flags,
        "cold_start": cold,
        "cold_start_ends": (created + timedelta(days=COLD_START_DAYS)).isoformat()
        if cold and created
        else None,
        "excluded": {
            "same_controller": reasons.count("same_controller"),
            "pending_verification": reasons.count("pending_verification"),
        },
        "discounted": {"fresh_requester": reasons.count("fresh_requester")},
        # Only for operators: which pairs to look at, and why.
        "_pairs": {r: f for r, f in pair_flags.items() if f and reviews.get(r) != "genuine"},
    }


async def anomalies(limit: int = 100) -> list[dict[str, Any]]:
    """Operators' queue: provider/requester pairs whose proofs wait for a look."""
    async with async_session_factory() as platform:
        providers = [
            r[0]
            for r in (
                await platform.execute(
                    text(
                        "SELECT provider_fid FROM federation_proofs GROUP BY provider_fid "
                        "ORDER BY max(closed_at) DESC LIMIT :l"
                    ),
                    {"l": limit},
                )
            ).all()
        ]
    from app.services.federation import reputation

    out = []
    for provider in providers:
        rows = await reputation._rows(provider)
        check = await assess(provider, rows)
        for requester, flags in check["_pairs"].items():
            out.append(
                {
                    "provider": provider,
                    "requester": requester,
                    "flags": flags,
                    "proofs": sum(1 for r in rows if r["requester_fid"] == requester),
                }
            )
    return out


async def review_pair(
    requester_fid: str, provider_fid: str, *, verdict: str, note: str, operator: str
) -> dict[str, Any]:
    """An operator's verdict on a flagged pair. Collusive also links the two
    as one controller, so their proofs never count again."""
    require_type(requester_fid, "workspace")
    require_type(provider_fid, "workspace")
    if verdict not in ("genuine", "collusive"):
        raise FederationError("FED_E_PACKAGE_INVALID", "verdict is genuine or collusive")
    if not isinstance(note, str) or not 5 <= len(note.strip()) <= 1000:
        raise FederationError("FED_E_PACKAGE_INVALID", "a review needs a note")
    async with async_session_factory() as platform:
        await platform.execute(
            text(
                "INSERT INTO federation_pair_reviews (requester_fid, provider_fid, verdict, note, "
                "reviewed_by) VALUES (:r, :p, :v, :n, :by) ON CONFLICT (requester_fid, "
                "provider_fid) DO UPDATE SET verdict = EXCLUDED.verdict, note = EXCLUDED.note, "
                "reviewed_by = EXCLUDED.reviewed_by, reviewed_at = NOW()"
            ),
            {
                "r": requester_fid,
                "p": provider_fid,
                "v": verdict,
                "n": note.strip(),
                "by": f"operator:{operator}",
            },
        )
        await platform.commit()
    if verdict == "collusive":
        await link(requester_fid, provider_fid, reason=note, operator=operator)
    return {"requester": requester_fid, "provider": provider_fid, "verdict": verdict}


async def link(a: str, b: str, *, reason: str, operator: str) -> dict[str, Any]:
    """An operator records that two identities share a controller."""
    require_type(a, "workspace")
    require_type(b, "workspace")
    if a == b:
        raise FederationError("FED_E_PACKAGE_INVALID", "link two different workspaces")
    if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 1000:
        raise FederationError("FED_E_PACKAGE_INVALID", "a link needs a reason")
    fa, fb = _pair(a, b)
    async with async_session_factory() as platform:
        await platform.execute(
            text(
                "INSERT INTO federation_controller_links (fid_a, fid_b, reason, created_by) "
                "VALUES (:a, :b, :r, :by) ON CONFLICT (fid_a, fid_b) DO NOTHING"
            ),
            {"a": fa, "b": fb, "r": reason.strip(), "by": f"operator:{operator}"},
        )
        await platform.commit()
    return {"linked": [fa, fb]}

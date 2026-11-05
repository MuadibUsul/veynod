"""Federation directory and explainable ranking (P4-2).

Contract: docs/federation/ICD_P4_REPUTATION.md §8 (vision §12.1, §12.3, §17.2).

* A workspace is in the directory only when a person of it lists it. The
  listing says what the owner chooses (headline, capability tags,
  availability); the rest of an entry comes from signed fulfilment proofs,
  platform measurements and published skills: never task or chat content,
  never who the counterparts were.
* Ranking is a published formula with separate factors, and every result
  carries its own explanation:

      score = safety x (0.45 relevance + 0.35 evidence + 0.20 availability)

  - relevance: how well the listing and published skills match the query;
  - evidence: acceptance and completion from proofs, pulled towards a
    neutral 0.5 until there are enough weighted proofs (a newcomer is not
    ranked as if it had failed);
  - availability: declared status, recent answers and response speed;
  - safety: a sanction in force removes the entry; past sanctions multiply
    by 0.8.
* Commercial promotion is separate: a labelled list that never changes the
  organic order (there is no paid placement yet).
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text

from app.database import async_session_factory
from app.services.federation import registry, reputation
from app.services.federation.errors import FederationError
from app.services.federation.grants import Actor
from app.services.federation.ids import require_type

WEIGHTS = {"relevance": 0.45, "evidence": 0.35, "availability": 0.20}
PRIOR = 0.5  # evidence for a workspace with no proofs yet
CONFIDENT_AT = 10.0  # weighted proofs at which evidence speaks for itself
PAST_SANCTION_FACTOR = 0.8
RECENT_DAYS = 14
AVAILABILITY = ("open", "busy", "closed")
SORTS = ("rank", "evidence", "availability", "relevance")
MAX_LIMIT = 50
MAX_CANDIDATES = 500


def _invalid(message: str, **detail: Any) -> FederationError:
    return FederationError("FED_E_PACKAGE_INVALID", message, **detail)


async def set_listing(
    workspace_id: str,
    *,
    listed: bool,
    headline: str,
    capabilities: list[str],
    availability: str,
    actor: Actor,
    disclosure: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """A person lists (or unlists) the workspace, says what it offers and
    chooses which reputation measures others may see (P4-4)."""
    from app.services.federation import grants
    from app.services.federation.delegations import scrub

    if actor.kind != "user":
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", "a person lists a workspace")
    headline = (headline or "").strip()
    if len(headline) > 200:
        raise _invalid("a headline is up to 200 characters")
    caps = sorted(set(capabilities or []))
    if len(caps) > 10 or not all(registry.CAPABILITY.fullmatch(c) for c in caps):
        raise _invalid("capabilities are up to 10 lowercase tags (letters, digits, dashes)")
    if availability not in AVAILABILITY:
        raise _invalid("availability is open, busy or closed")
    scrub({"headline": headline})
    import json

    from app.services.federation import privacy

    if disclosure is not None and (
        not isinstance(disclosure, dict) or set(disclosure) - set(privacy.DISCLOSABLE)
    ):
        raise _invalid("disclosure covers " + ", ".join(privacy.DISCLOSABLE))
    if disclosure is None:
        shown = await disclosure_of(await _fid_of(workspace_id))
    else:
        shown = privacy.normalise(disclosure)
    async with async_session_factory() as platform:
        fid = await grants._local_fid(platform, workspace_id)
        await platform.execute(
            text(
                "INSERT INTO federation_directory_listings (workspace_id, tenant_id, "
                "federation_id, listed, headline, capabilities, availability, disclosure, "
                "updated_by) VALUES (CAST(:w AS uuid), '', :f, :l, :h, :c, :a, "
                "CAST(:d AS jsonb), :by) ON CONFLICT (workspace_id) DO UPDATE SET listed = "
                "EXCLUDED.listed, headline = EXCLUDED.headline, capabilities = "
                "EXCLUDED.capabilities, availability = EXCLUDED.availability, disclosure = "
                "EXCLUDED.disclosure, updated_by = EXCLUDED.updated_by, updated_at = NOW()"
            ),
            {
                "w": str(workspace_id),
                "f": fid,
                "l": bool(listed),
                "h": headline,
                "c": caps,
                "a": availability,
                "d": json.dumps(shown),
                "by": actor.ref,
            },
        )
        await platform.commit()
    return await my_listing(workspace_id)


async def _fid_of(workspace_id: str) -> str:
    from app.services.federation import grants

    async with async_session_factory() as platform:
        return await grants._local_fid(platform, workspace_id)


async def my_listing(workspace_id: str) -> dict[str, Any]:
    async with async_session_factory() as platform:
        row = (
            (
                await platform.execute(
                    text(
                        "SELECT * FROM federation_directory_listings "
                        "WHERE workspace_id = CAST(:w AS uuid)"
                    ),
                    {"w": str(workspace_id)},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return {
            "listed": False,
            "headline": "",
            "capabilities": [],
            "availability": "open",
            "disclosure": _disclosure(None),
        }
    return _listing_out(dict(row))


def _listing_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "fid": row["federation_id"],
        "listed": row["listed"],
        "headline": row["headline"],
        "capabilities": list(row["capabilities"] or []),
        "availability": row["availability"],
        "disclosure": _disclosure(row.get("disclosure")),
        "updated_at": row["updated_at"].isoformat(),
    }


def _disclosure(value: Any) -> dict[str, bool]:
    import json

    from app.services.federation import privacy

    return privacy.normalise(json.loads(value) if isinstance(value, str) else value)


async def disclosure_of(fid: str) -> dict[str, bool]:
    """What the owner chose to show (defaults when it never listed)."""
    async with async_session_factory() as platform:
        value = await platform.scalar(
            text("SELECT disclosure FROM federation_directory_listings WHERE federation_id = :f"),
            {"f": fid},
        )
    return _disclosure(value)


async def is_listed(fid: str) -> bool:
    async with async_session_factory() as platform:
        return bool(
            await platform.scalar(
                text("SELECT listed FROM federation_directory_listings WHERE federation_id = :f"),
                {"f": fid},
            )
        )


async def can_view(fid: str, viewer_fid: str | None) -> bool:
    """Reputation of a workspace is visible to itself, to workspaces it is
    connected with, and to everyone once it is listed."""
    if viewer_fid is None or viewer_fid == fid or await is_listed(fid):
        return True
    async with async_session_factory() as platform:
        return bool(
            await platform.scalar(
                text(
                    "SELECT 1 FROM federation_grants WHERE local_workspace_fid = :v AND "
                    "peer_workspace_fid = :f AND state IN ('ACTIVE', 'SUSPENDED', 'EXPIRED') "
                    "LIMIT 1"
                ),
                {"v": viewer_fid, "f": fid},
            )
        )


def _words(value: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", value.lower()) if len(w) >= 2]


def _relevance(
    q: str | None,
    capability: str | None,
    listing: dict,
    skill_caps: set[str],
    skill_names: list[str],
) -> tuple[float, str]:
    caps = set(listing["capabilities"]) | skill_caps
    if capability:
        if capability in listing["capabilities"]:
            return 1.0, f"lists the capability '{capability}'"
        if capability in skill_caps:
            return 0.8, f"publishes a skill tagged '{capability}'"
        return 0.0, f"does not offer '{capability}'"
    if not q:
        return 1.0, "no query: every listing is equally relevant"
    words = _words(q)
    if not words:
        return 1.0, "no query words"
    haystack = (
        set(_words(listing["headline"])) | caps | {w for name in skill_names for w in _words(name)}
    )
    tag_words = {w for c in caps for w in _words(c)}
    hits = [w for w in words if w in haystack or w in tag_words]
    value = round(len(hits) / len(words), 3)
    return value, (
        f"matches {len(hits)} of {len(words)} query words" if hits else "matches no query word"
    )


def _evidence(page: dict[str, Any]) -> tuple[float, str]:
    integrity = page.get("integrity") or {}
    if integrity.get("cold_start"):
        # P4-3: early volume cannot buy a place.
        return PRIOR, f"new address, cold start until {integrity['cold_start_ends'][:10]}: neutral"
    acc, comp = page["acceptance"], page["completion"]
    if acc.get("hidden_by_owner") or comp.get("hidden_by_owner"):
        # P4-4: what the owner does not show cannot help its place.
        return PRIOR, "track record hidden by the owner: neutral"
    weight = comp["weighted_total"]
    confidence = min(1.0, weight / CONFIDENT_AT)
    acc_rate = acc["weighted_hits"] / acc["weighted_total"] if acc["weighted_total"] else PRIOR
    comp_rate = comp["weighted_hits"] / comp["weighted_total"] if comp["weighted_total"] else PRIOR
    observed = 0.6 * acc_rate + 0.4 * comp_rate
    value = round(PRIOR * (1 - confidence) + observed * confidence, 3)
    if not weight:
        return value, "no counted fulfilment proofs yet: neutral, not penalised"
    held = sum((integrity.get("excluded") or {}).values())
    note = f"; {held} proof(s) not counted (self-dealing or awaiting review)" if held else ""
    return value, (
        f"{page['proofs']} signed proofs{note} (weight {round(weight, 2)}, confidence "
        f"{round(confidence, 2)}): accepted {round(acc_rate * 100)}%, delivered "
        f"{round(comp_rate * 100)}%"
    )


def _availability(listing: dict, page: dict[str, Any], recent: bool) -> tuple[float, str]:
    status = {"open": 1.0, "busy": 0.3, "closed": 0.0}[listing["availability"]]
    if listing["availability"] == "open" and not recent:
        status = 0.7
    median = page["response"].get("median_seconds")
    if page["response"].get("hidden_by_owner"):
        speed, speed_note = 0.5, "response time hidden by the owner"
    elif median is None:
        speed, speed_note = 0.5, "response time unknown"
    elif median <= 3600:
        speed, speed_note = 1.0, "answers within an hour"
    elif median <= 86400:
        speed, speed_note = 0.7, "answers within a day"
    else:
        speed, speed_note = 0.4, "answers slowly"
    value = round(0.6 * status + 0.4 * speed, 3)
    return value, (
        f"{listing['availability']}"
        f"{', answered offers recently' if recent else ', no recent answers'}; {speed_note}"
    )


async def _recent(fid: str) -> bool:
    async with async_session_factory() as platform:
        return bool(
            await platform.scalar(
                text(
                    "SELECT 1 FROM federation_delegation_history h JOIN federation_local_map m "
                    "ON m.workspace_id = h.workspace_id AND m.id_type = 'workspace' "
                    "WHERE m.federation_id = :f AND h.from_state = 'OFFERED' AND h.to_state IN "
                    f"('ACCEPTED', 'REJECTED') AND h.created_at > NOW() - INTERVAL "
                    f"'{RECENT_DAYS} days' LIMIT 1"
                ),
                {"f": fid},
            )
        )


async def _skills(fid: str) -> tuple[set[str], list[dict[str, Any]]]:
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT p.name, p.capabilities, p.latest_version, (SELECT count(*) FROM "
                        "registry_downloads d WHERE d.name = p.name) AS downloads FROM "
                        "registry_packages p WHERE p.publisher_fid = :f AND EXISTS (SELECT 1 "
                        "FROM registry_versions v WHERE v.name = p.name AND v.state = "
                        "'published') ORDER BY downloads DESC, p.name LIMIT 20"
                    ),
                    {"f": fid},
                )
            )
            .mappings()
            .all()
        )
    caps = {c for r in rows for c in (r["capabilities"] or [])}
    return caps, [
        {"name": r["name"], "latest_version": r["latest_version"], "downloads": int(r["downloads"])}
        for r in rows
    ]


async def _card(
    row: dict[str, Any], *, q: str | None, capability: str | None, owner_view: bool = False
) -> dict[str, Any]:
    from app.services.federation import privacy

    listing = _listing_out(row)
    fid = listing["fid"]
    full = await reputation.profile(fid)
    # The owner sees its own measures; everyone else what it chose to show.
    # Ranking always uses the disclosed view, so hiding cannot help.
    shown = privacy.apply_disclosure(full, listing["disclosure"])
    page = full if owner_view else shown
    skill_caps, skills = await _skills(fid)
    if not listing["disclosure"]["skills"]:
        skills = []
    recent = await _recent(fid)
    relevance, why_rel = _relevance(q, capability, listing, skill_caps, [s["name"] for s in skills])
    evidence, why_ev = _evidence(shown)
    available, why_av = _availability(listing, shown, recent)
    past = page["governance"]["sanctions_ever"]
    complaints = page["governance"]["complaints"]
    complaint_factor = privacy.complaint_factor(complaints)
    safety = round((PAST_SANCTION_FACTOR if past else 1.0) * complaint_factor, 4)
    safety_why = (
        "; ".join(
            part
            for part in (
                f"{past} past sanction(s): x{PAST_SANCTION_FACTOR}" if past else "",
                f"{complaints['upheld_last_year']} upheld complaint(s) in the last year: "
                f"x{complaint_factor}"
                if complaints["upheld_last_year"]
                else "",
            )
            if part
        )
        or "no sanctions and no upheld complaints"
    )
    factors = {"relevance": relevance, "evidence": evidence, "availability": available}
    base = sum(WEIGHTS[k] * v for k, v in factors.items())
    score = round(safety * base, 4)
    return {
        "fid": fid,
        "handle": row.get("handle"),
        "headline": listing["headline"],
        "capabilities": listing["capabilities"],
        "availability": listing["availability"],
        "skills": skills[:5],
        "price": {"model": "free", "amount": None},
        "reputation": {
            "acceptance": page["acceptance"],
            "completion": {k: v for k, v in page["completion"].items() if k != "meaning"},
            "response": page["response"],
            "skills": page["skills"],
            "governance": page["governance"],
            "cooperation": page["cooperation"]
            if page["cooperation"].get("hidden_by_owner")
            else {
                "counterparts": page["cooperation"]["counterparts"],
                "repeat_counterparts": page["cooperation"]["repeat_counterparts"],
                "longest_partnership_days": page["cooperation"]["longest_partnership_days"],
            },
            "disclosure": listing["disclosure"],
            "proofs": page["proofs"],
            "integrity": {
                k: page["integrity"][k] for k in ("flags", "cold_start", "excluded", "discounted")
            },
        },
        "rank": {
            "score": score,
            "explanation": [
                {
                    "factor": name,
                    "value": factors[name],
                    "weight": WEIGHTS[name],
                    "contribution": round(WEIGHTS[name] * factors[name] * safety, 4),
                    "why": why,
                }
                for name, why in (
                    ("relevance", why_rel),
                    ("evidence", why_ev),
                    ("availability", why_av),
                )
            ]
            + [
                {
                    "factor": "safety",
                    "value": safety,
                    "weight": "multiplier",
                    "contribution": None,
                    "why": safety_why,
                },
                {
                    "factor": "promotion",
                    "value": 0,
                    "weight": 0,
                    "contribution": 0,
                    "why": "paid promotion never changes the organic order",
                },
            ],
        },
    }


async def search(
    *,
    q: str | None = None,
    capability: str | None = None,
    min_acceptance: float | None = None,
    max_response_seconds: float | None = None,
    price: str | None = None,
    availability: str | None = None,
    sort: str = "rank",
    include_closed: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Listed workspaces, filtered and ranked, each with its explanation."""
    if capability is not None and not registry.CAPABILITY.fullmatch(capability):
        raise _invalid("a capability is a lowercase tag")
    if availability is not None and availability not in AVAILABILITY:
        raise _invalid("availability is open, busy or closed")
    if price not in (None, "any", "free", "paid"):
        raise _invalid("price is any, free or paid")
    if sort not in SORTS:
        raise _invalid("unknown sort", sorts=list(SORTS))
    if min_acceptance is not None and not 0 <= min_acceptance <= 1:
        raise _invalid("min_acceptance is between 0 and 1")
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    filters = {
        k: v
        for k, v in {
            "q": q,
            "capability": capability,
            "min_acceptance": min_acceptance,
            "max_response_seconds": max_response_seconds,
            "price": price,
            "availability": availability,
        }.items()
        if v is not None
    }
    out = {
        "items": [],
        "total": 0,
        "filters": filters,
        "sort": sort,
        "formula": {
            "weights": WEIGHTS,
            "safety": "multiplier",
            "evidence_prior": PRIOR,
            "confident_at_weight": CONFIDENT_AT,
        },
        "excluded": {"sanctioned": 0, "frozen": 0},
        "promoted": {"items": [], "note": "no paid placement; shown apart when there is"},
    }
    if price == "paid":
        return out
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT l.*, i.status AS identity_status, a.display_handle AS handle "
                        "FROM federation_directory_listings l JOIN federation_identities i ON "
                        "i.federation_id = l.federation_id LEFT JOIN federation_addresses a ON "
                        "a.target_id = l.federation_id AND a.status = 'active' "
                        "WHERE l.listed ORDER BY l.updated_at DESC LIMIT :c"
                    ),
                    {"c": MAX_CANDIDATES},
                )
            )
            .mappings()
            .all()
        )
    cards = []
    for row in rows:
        row = dict(row)
        if row["identity_status"] != "active":
            out["excluded"]["frozen"] += 1
            continue
        if row["availability"] == "closed" and not include_closed and availability != "closed":
            continue
        if availability and row["availability"] != availability:
            continue
        card = await _card(row, q=q, capability=capability)
        if card["reputation"]["governance"]["sanctions_in_force"]:
            out["excluded"]["sanctioned"] += 1
            continue
        factors = {e["factor"]: e["value"] for e in card["rank"]["explanation"]}
        if (capability or q) and factors["relevance"] == 0:
            continue
        acc = card["reputation"]["acceptance"]
        if min_acceptance is not None and (acc["rate"] is None or acc["rate"] < min_acceptance):
            continue
        resp = card["reputation"]["response"]
        if max_response_seconds is not None and (
            resp["median_seconds"] is None or resp["median_seconds"] > max_response_seconds
        ):
            continue
        cards.append(card)
    key = {
        "rank": lambda c: c["rank"]["score"],
        "evidence": lambda c: c["rank"]["explanation"][1]["value"],
        "availability": lambda c: c["rank"]["explanation"][2]["value"],
        "relevance": lambda c: c["rank"]["explanation"][0]["value"],
    }[sort]
    cards.sort(key=lambda c: (-key(c), -c["rank"]["score"], c["fid"]))
    for position, card in enumerate(cards, start=1):
        card["rank"]["position"] = position
    out["total"] = len(cards)
    out["items"] = cards[offset : offset + limit]
    return out


async def entry(fid: str, *, viewer_fid: str | None = None) -> dict[str, Any]:
    """One listed workspace's directory entry."""
    require_type(fid, "workspace")
    async with async_session_factory() as platform:
        row = (
            (
                await platform.execute(
                    text(
                        "SELECT l.*, a.display_handle AS handle FROM "
                        "federation_directory_listings l LEFT JOIN federation_addresses a ON "
                        "a.target_id = l.federation_id AND a.status = 'active' "
                        "WHERE l.federation_id = :f"
                    ),
                    {"f": fid},
                )
            )
            .mappings()
            .first()
        )
    if row is None or (not row["listed"] and viewer_fid != fid):
        raise FederationError("FED_E_NOT_LISTED", "this workspace is not in the directory")
    card = await _card(dict(row), q=None, capability=None, owner_view=viewer_fid == fid)
    card["listed"] = row["listed"]
    return card

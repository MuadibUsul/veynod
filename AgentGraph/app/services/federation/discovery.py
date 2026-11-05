"""Skill registry: search and discovery (P3-2).

Contract: docs/federation/ICD_P3_REGISTRY.md §7 (vision §11, §12.1–12.3).

Every number on an entry says where it comes from (vision §12.1):

* self-declared by the publisher: description, category, capability tags;
* machine-verified: digests and the publisher's signatures (P3-1);
* from other workspaces: downloads (distinct workspaces that pulled a
  version, so re-installing does not inflate them) and ratings (one per
  workspace that installed the package, never the publisher's own);
* measured by the platform: how fast the publisher answers delegation offers
  as a provider (median over the last 90 days).

An average rating or a response time is only shown with enough samples;
below that the entry says "not enough evidence" rather than a weak number.
Nothing costs money yet: every entry is free (owner decision F8).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.database import async_session_factory
from app.services.federation import registry
from app.services.federation.errors import FederationError
from app.services.federation.grants import Actor

MIN_RATINGS = 3  # an average needs at least this many ratings
MIN_RESPONSES = 3  # a response time needs at least this many answered offers
RESPONSE_WINDOW_DAYS = 90
MAX_LIMIT = 50
SORTS = ("relevance", "downloads", "rating", "response", "recent")
PRICE = {"model": "free", "amount": None}

# The publisher's median time, as a provider, from receiving a delegation
# offer to accepting or rejecting it (measured by this deployment).
_RESPONSE_SQL = f"""
    SELECT m.federation_id AS fid,
           percentile_cont(0.5) WITHIN GROUP (
               ORDER BY EXTRACT(EPOCH FROM (h.created_at - d.created_at))) AS median,
           count(*) AS samples
      FROM federation_delegations d
      JOIN federation_local_map m
        ON m.workspace_id = d.workspace_id AND m.id_type = 'workspace'
      JOIN federation_delegation_history h
        ON h.workspace_id = d.workspace_id AND h.peer_workspace_fid = d.peer_workspace_fid
       AND h.federation_job_id = d.federation_job_id
     WHERE d.role = 'provider' AND h.from_state = 'OFFERED'
       AND h.to_state IN ('ACCEPTED', 'REJECTED')
       AND d.created_at > NOW() - INTERVAL '{RESPONSE_WINDOW_DAYS} days'
     GROUP BY m.federation_id
"""


def _rating(avg: float | None, count: int) -> dict[str, Any]:
    enough = count >= MIN_RATINGS and avg is not None
    return {
        "average": round(float(avg), 2) if enough else None,
        "count": int(count),
        "enough_evidence": enough,
    }


def _response(median: float | None, samples: int | None) -> dict[str, Any]:
    samples = int(samples or 0)
    enough = samples >= MIN_RESPONSES and median is not None
    return {
        "median_seconds": round(float(median), 1) if enough else None,
        "samples": samples,
        "window_days": RESPONSE_WINDOW_DAYS,
        "enough_evidence": enough,
    }


async def search(
    *,
    q: str | None = None,
    category: str | None = None,
    capability: str | None = None,
    kind: str | None = None,
    min_rating: float | None = None,
    price: str | None = None,
    max_response_seconds: float | None = None,
    sort: str = "relevance",
    include_yanked: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Find skills by words, category, capability, reputation, price and the
    publisher's response speed."""
    if category is not None and category not in registry.CATEGORIES:
        raise registry._invalid("unknown category", categories=list(registry.CATEGORIES))
    if capability is not None and not registry.CAPABILITY.fullmatch(capability):
        raise registry._invalid("a capability is a lowercase tag")
    if kind is not None and kind not in registry.KINDS:
        raise registry._invalid("kind is declarative or executable")
    if sort not in SORTS:
        raise registry._invalid("unknown sort", sorts=list(SORTS))
    if price not in (None, "any", "free", "paid"):
        raise registry._invalid("price is any, free or paid")
    if min_rating is not None and not 1 <= min_rating <= 5:
        raise registry._invalid("min_rating is between 1 and 5")
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    applied = {
        k: v
        for k, v in {
            "q": q,
            "category": category,
            "capability": capability,
            "kind": kind,
            "min_rating": min_rating,
            "price": price,
            "max_response_seconds": max_response_seconds,
            "include_yanked": include_yanked or None,
        }.items()
        if v is not None
    }
    if price == "paid":
        # Nothing is sold yet (owner decision F8): an honest empty answer.
        return {"items": [], "total": 0, "filters": applied, "sort": sort}

    where = ["TRUE"]
    params: dict[str, Any] = {"lim": limit, "off": offset}
    if q:
        needle = q.strip().lower()[:100]
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.update(q=needle, like=f"%{escaped}%", prefix=f"{escaped}%")
        where.append("(p.name LIKE :like OR lower(coalesce(p.description, '')) LIKE :like)")
    if category:
        where.append("p.category = :cat")
        params["cat"] = category
    if capability:
        where.append("CAST(:cap AS text) = ANY(p.capabilities)")
        params["cap"] = capability
    if kind:
        where.append("lv.kind = :kind")
        params["kind"] = kind
    if not include_yanked:
        where.append("lv.name IS NOT NULL")
    if min_rating is not None:
        where.append(f"coalesce(rt.n, 0) >= {MIN_RATINGS} AND rt.avg >= :minr")
        params["minr"] = float(min_rating)
    if max_response_seconds is not None:
        where.append(f"coalesce(sp.samples, 0) >= {MIN_RESPONSES} AND sp.median <= :maxresp")
        params["maxresp"] = float(max_response_seconds)

    rated_first = f"(coalesce(rt.n, 0) >= {MIN_RATINGS}) DESC"
    order = {
        "downloads": "downloads DESC, p.name",
        "rating": f"{rated_first}, rt.avg DESC NULLS LAST, downloads DESC, p.name",
        "response": f"(coalesce(sp.samples, 0) >= {MIN_RESPONSES}) DESC, sp.median ASC NULLS "
        "LAST, p.name",
        "recent": "p.updated_at DESC, p.name",
        "relevance": (
            "CASE WHEN p.name = :q THEN 0 WHEN p.name LIKE :prefix THEN 1 ELSE 2 END, "
            "downloads DESC, p.name"
            if q
            else "downloads DESC, p.updated_at DESC, p.name"
        ),
    }[sort]
    sql = f"""
        WITH dl AS (SELECT name, count(*) AS downloads FROM registry_downloads GROUP BY name),
             rt AS (SELECT name, avg(stars)::float AS avg, count(*) AS n
                      FROM registry_ratings GROUP BY name),
             sp AS ({_RESPONSE_SQL}),
             lv AS (SELECT DISTINCT ON (name) name, version, kind, digest
                      FROM registry_versions WHERE state = 'published'
                     ORDER BY name, major DESC, minor DESC, patch DESC)
        SELECT p.name, p.publisher_fid, p.description, p.category, p.capabilities,
               p.latest_version, p.updated_at, coalesce(dl.downloads, 0) AS downloads,
               rt.avg AS rating_avg, coalesce(rt.n, 0) AS rating_n,
               sp.median AS response_median, sp.samples AS response_samples,
               lv.version AS installable_version, lv.kind AS kind, lv.digest AS digest,
               a.display_handle AS publisher_handle, count(*) OVER () AS total
          FROM registry_packages p
          LEFT JOIN dl ON dl.name = p.name
          LEFT JOIN rt ON rt.name = p.name
          LEFT JOIN sp ON sp.fid = p.publisher_fid
          LEFT JOIN lv ON lv.name = p.name
          LEFT JOIN federation_addresses a
            ON a.target_id = p.publisher_fid AND a.status = 'active'
         WHERE {" AND ".join(where)}
         ORDER BY {order}
         LIMIT :lim OFFSET :off
    """
    async with async_session_factory() as platform:
        rows = (await platform.execute(text(sql), params)).mappings().all()
    items = [
        {
            "name": r["name"],
            "description": r["description"],
            "category": r["category"],
            "capabilities": list(r["capabilities"] or []),
            "kind": r["kind"],
            "latest_version": r["latest_version"],
            "installable_version": r["installable_version"],
            "digest": r["digest"],
            "publisher": {"fid": r["publisher_fid"], "handle": r["publisher_handle"]},
            "downloads": int(r["downloads"]),
            "rating": _rating(r["rating_avg"], r["rating_n"]),
            "response": _response(r["response_median"], r["response_samples"]),
            "price": PRICE,
            "updated_at": r["updated_at"].isoformat(),
        }
        for r in rows
    ]
    total = int(rows[0]["total"]) if rows else 0
    return {"items": items, "total": total, "filters": applied, "sort": sort}


async def entry(name: str) -> dict[str, Any]:
    """A skill's entry page: address, history, ratings, dependencies and the
    publisher's representative works."""
    head = await registry.package(name)
    publisher = head["publisher"]
    async with async_session_factory() as platform:
        meta = (
            (
                await platform.execute(
                    text(
                        "SELECT p.category, p.capabilities, p.latest_version, p.updated_at, "
                        "a.display_handle FROM registry_packages p LEFT JOIN "
                        "federation_addresses a ON a.target_id = p.publisher_fid AND "
                        "a.status = 'active' WHERE p.name = :n"
                    ),
                    {"n": name},
                )
            )
            .mappings()
            .one()
        )
        downloads = {
            r["version"]: int(r["n"])
            for r in (
                await platform.execute(
                    text(
                        "SELECT version, count(*) AS n FROM registry_downloads WHERE name = :n "
                        "GROUP BY version"
                    ),
                    {"n": name},
                )
            ).mappings()
        }
        ratings = (
            (
                await platform.execute(
                    text(
                        "SELECT version, stars, review, updated_at FROM registry_ratings "
                        "WHERE name = :n ORDER BY updated_at DESC"
                    ),
                    {"n": name},
                )
            )
            .mappings()
            .all()
        )
        response = (
            (
                await platform.execute(
                    text(f"SELECT median, samples FROM ({_RESPONSE_SQL}) s WHERE s.fid = :f"),
                    {"f": publisher},
                )
            )
            .mappings()
            .first()
        )
        accepted_results = await platform.scalar(
            text(
                "SELECT count(*) FROM federation_delegations d JOIN federation_local_map m "
                "ON m.workspace_id = d.workspace_id AND m.id_type = 'workspace' "
                "WHERE m.federation_id = :f AND d.role = 'provider' "
                "AND d.state = 'ACCEPTED_RESULT'"
            ),
            {"f": publisher},
        )
        dependants = (
            (
                await platform.execute(
                    text(
                        "SELECT DISTINCT ON (v.name) v.name, v.version FROM registry_versions v, "
                        "jsonb_array_elements(v.dependencies) dep WHERE dep->>'name' = :n "
                        "AND v.state = 'published' ORDER BY v.name, v.major DESC, v.minor DESC, "
                        "v.patch DESC LIMIT 20"
                    ),
                    {"n": name},
                )
            )
            .mappings()
            .all()
        )
        works = (
            (
                await platform.execute(
                    text(
                        "SELECT p.name, p.description, p.category, p.latest_version, "
                        "(SELECT count(*) FROM registry_downloads d WHERE d.name = p.name) AS dl, "
                        "(SELECT avg(stars)::float FROM registry_ratings r WHERE r.name = p.name) "
                        "AS avg, (SELECT count(*) FROM registry_ratings r WHERE r.name = p.name) "
                        "AS n FROM registry_packages p "
                        "WHERE p.publisher_fid = :f AND p.name <> :n "
                        "ORDER BY dl DESC, p.updated_at DESC LIMIT 3"
                    ),
                    {"f": publisher, "n": name},
                )
            )
            .mappings()
            .all()
        )
        verdicts = {
            r["digest"]: {"verdict": r["verdict"], "checker": r["checker"]}
            for r in (
                await platform.execute(
                    text(
                        "SELECT s.digest, s.verdict, s.checker FROM registry_sandbox_reports s "
                        "JOIN registry_versions v ON v.digest = s.digest WHERE v.name = :n"
                    ),
                    {"n": name},
                )
            ).mappings()
        }
        packages = await platform.scalar(
            text("SELECT count(*) FROM registry_packages WHERE publisher_fid = :f"),
            {"f": publisher},
        )
        dep_states = {}
        versions = head["versions"]
        latest = versions[-1] if versions else None
        for dep in (latest or {}).get("dependencies", []):
            dep_states[dep["digest"]] = await platform.scalar(
                text("SELECT state FROM registry_versions WHERE digest = :d"), {"d": dep["digest"]}
            )

    by_version: dict[str, list[int]] = {}
    distribution = {str(s): 0 for s in range(1, 6)}
    for r in ratings:
        by_version.setdefault(r["version"], []).append(int(r["stars"]))
        distribution[str(r["stars"])] += 1
    total_stars = [int(r["stars"]) for r in ratings]
    installable = [v for v in versions if v["state"] == "published"]
    current = installable[-1] if installable else None
    history = [
        {
            "version": v["version"],
            "digest": v["digest"],
            "kind": v["kind"],
            "permissions": v["manifest"].get("permissions", []),
            "state": v["state"],
            "published_at": v["published_at"],
            "yanked_at": v["yanked_at"],
            "yank_reason": v["yank_reason"],
            "downloads": downloads.get(v["version"], 0),
            "sandbox": verdicts.get(v["digest"]),
            "rating": _rating(
                sum(by_version.get(v["version"], [])) / len(by_version[v["version"]])
                if by_version.get(v["version"])
                else None,
                len(by_version.get(v["version"], [])),
            ),
        }
        for v in reversed(versions)
    ]
    return {
        "name": name,
        "address": {
            "package": name,
            "publisher": publisher,
            "publisher_handle": meta["display_handle"],
            # What an install pins: the newest installable version and its digest.
            "pin": f"{name}@{current['version']}" if current else None,
            "digest": current["digest"] if current else None,
        },
        "description": head["description"],
        "category": meta["category"],
        "capabilities": list(meta["capabilities"] or []),
        "kind": current["kind"] if current else None,
        "latest_version": meta["latest_version"],
        "installable": current is not None,
        "price": PRICE,
        "downloads": {"total": sum(downloads.values()), "by_version": downloads},
        "rating": {
            **_rating(
                sum(total_stars) / len(total_stars) if total_stars else None, len(total_stars)
            ),
            "distribution": distribution,
            # Reviews are shown without the reviewer's address (vision §12.4).
            "recent_reviews": [
                {
                    "version": r["version"],
                    "stars": int(r["stars"]),
                    "review": r["review"],
                    "at": r["updated_at"].isoformat(),
                }
                for r in ratings[:5]
                if r["review"]
            ],
        },
        "history": history,
        "dependencies": [
            {**dep, "state": dep_states.get(dep["digest"])}
            for dep in (latest or {}).get("dependencies", [])
        ],
        "dependants": [{"name": d["name"], "version": d["version"]} for d in dependants],
        "publisher": {
            "fid": publisher,
            "handle": meta["display_handle"],
            "packages": int(packages or 0),
            "accepted_results": int(accepted_results or 0),
            "response": _response(
                response["median"] if response else None, response["samples"] if response else 0
            ),
        },
        "representative_works": [
            {
                "name": w["name"],
                "description": w["description"],
                "category": w["category"],
                "latest_version": w["latest_version"],
                "downloads": int(w["dl"]),
                "rating": _rating(w["avg"], w["n"]),
            }
            for w in works
        ],
        "evidence": {
            "self_declared": ["description", "category", "capabilities"],
            "machine_verified": [
                "history[].digest",
                "signature per version",
                "history[].sandbox",
            ],
            "from_other_workspaces": ["downloads", "rating"],
            "measured_by_platform": ["publisher.response", "publisher.accepted_results"],
        },
        "updated_at": meta["updated_at"].isoformat(),
    }


async def rate(
    workspace_id: str, name: str, *, stars: int, review: str | None, actor: Actor
) -> dict[str, Any]:
    """A person rates a skill their workspace installed (one rating per
    workspace and package; rating again replaces it)."""
    from app.services.federation import grants
    from app.services.federation.delegations import scrub

    if actor.kind != "user":
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", "a person rates a skill")
    if not isinstance(stars, int) or not 1 <= stars <= 5:
        raise registry._invalid("stars is 1 to 5")
    review = (review or "").strip() or None
    if review is not None:
        if len(review) > 1000:
            raise registry._invalid("a review is up to 1000 characters")
        scrub({"review": review})
    async with async_session_factory() as platform:
        me = await grants._local_fid(platform, workspace_id)
        owner = await platform.scalar(
            text("SELECT publisher_fid FROM registry_packages WHERE name = :n"), {"n": name}
        )
        if owner is None:
            raise FederationError("FED_E_PACKAGE_UNKNOWN", "no such package")
        if owner == me:
            raise FederationError(
                "FED_E_RATING_NOT_ALLOWED", "a publisher does not rate its own skill"
            )
        # The version rated: what this workspace has pinned, or else the last
        # version it pulled. A workspace that never pulled the skill cannot rate it.
        version = await platform.scalar(
            text(
                "SELECT version FROM registry_installs WHERE workspace_id = CAST(:w AS uuid) "
                "AND name = :n"
            ),
            {"w": str(workspace_id), "n": name},
        ) or await platform.scalar(
            text(
                "SELECT version FROM registry_downloads WHERE workspace_id = CAST(:w AS uuid) "
                "AND name = :n ORDER BY first_at DESC LIMIT 1"
            ),
            {"w": str(workspace_id), "n": name},
        )
        if version is None:
            raise FederationError("FED_E_RATING_NOT_ALLOWED", "install a skill before rating it")
        await platform.execute(
            text(
                "INSERT INTO registry_ratings (name, rater_fid, version, stars, review, rated_by) "
                "VALUES (:n, :f, :v, :s, :r, :by) ON CONFLICT (name, rater_fid) DO UPDATE SET "
                "version = EXCLUDED.version, stars = EXCLUDED.stars, review = EXCLUDED.review, "
                "rated_by = EXCLUDED.rated_by, updated_at = NOW()"
            ),
            {"n": name, "f": me, "v": version, "s": stars, "r": review, "by": actor.ref},
        )
        await platform.commit()
    return {"name": name, "version": version, "stars": stars, "review": review}


def categories() -> list[str]:
    return list(registry.CATEGORIES)

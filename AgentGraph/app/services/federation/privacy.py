"""Privacy boundary of the directory and the public complaint record (P4-4).

Contract: docs/federation/PRIVACY.md (vision §9.5, §12.4, §13.1, F-026).

* What the directory may show about a workspace is (a) what its owner wrote
  in the listing, (b) the reputation measures the owner chose to disclose,
  (c) safety information that is never optional: sanctions in force and the
  outcomes of resolved complaints.
* Never shown to anyone outside the two parties: task terms, chat text,
  results, job ids, proof contents, who worked with whom.
* A complaint becomes public only once it is resolved, and then only as its
  category, outcome, date and any sanction: never the complainant, the
  statement, the evidence or the operators' notes. Open cases are not
  shown; a report is not a finding (vision §13.1).
* A hidden measure is not used for ranking either: what is not shown cannot
  help a workspace's place.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from app.database import async_session_factory

DISCLOSABLE = ("completion", "acceptance", "response", "skills", "cooperation")
DEFAULT_DISCLOSURE = dict.fromkeys(DISCLOSABLE, True)
UPHELD = ("DECIDED", "APPEAL_WINDOW", "CLOSED")
RESOLVED_OUTCOME = {
    "DECIDED": "upheld",
    "APPEAL_WINDOW": "upheld",
    "CLOSED": "upheld",
    "DISMISSED": "dismissed",
    "INSUFFICIENT_EVIDENCE": "insufficient_evidence",
}
RECENT_YEAR = timedelta(days=365)
COMPLAINT_FACTOR = 0.9  # per upheld complaint in the last year, in ranking
COMPLAINT_FLOOR = 0.6

POLICY: dict[str, Any] = {
    "version": "2026-11-05",
    "public_when_listed": {
        "always": [
            "headline",
            "capability tags",
            "availability",
            "sanctions in force and ever",
            "resolved complaints: category, outcome, date, sanction kind",
        ],
        "if_the_owner_discloses": list(DISCLOSABLE),
    },
    "parties_only": [
        "fulfilment proofs (they name both workspaces and hash the terms and result)",
        "delegation terms, results and their history",
        "chat messages",
    ],
    "never_published": [
        "task terms, chat text and results",
        "job ids and proof contents",
        "who a workspace worked with",
        "complainants, complaint statements, evidence and operator notes",
        "open (unresolved) complaints",
        "integrity review notes",
    ],
    "unlisted_workspaces": "reputation visible only to the workspace itself and to "
    "workspaces it is connected with",
    "ranking": "a measure hidden by its owner is treated as unknown (neutral) in ranking",
}


def normalise(disclosure: Any) -> dict[str, bool]:
    out = dict(DEFAULT_DISCLOSURE)
    if isinstance(disclosure, dict):
        for key in DISCLOSABLE:
            if key in disclosure:
                out[key] = bool(disclosure[key])
    return out


async def complaint_record(fid: str) -> dict[str, Any]:
    """The public record of resolved complaints about a workspace."""
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT r.report_id, r.category, r.case_state, r.updated_at, "
                        "(SELECT s.kind FROM federation_sanctions s WHERE s.report_id = "
                        "r.report_id AND s.state IN ('active', 'lifted') ORDER BY s.created_at "
                        "DESC LIMIT 1) AS sanction FROM federation_reports r "
                        "WHERE r.accused_fid = :f AND r.case_state = ANY(:states) "
                        "ORDER BY r.updated_at DESC"
                    ),
                    {"f": fid, "states": list(RESOLVED_OUTCOME)},
                )
            )
            .mappings()
            .all()
        )
    now = datetime.now(timezone.utc)
    counts = {"upheld": 0, "dismissed": 0, "insufficient_evidence": 0}
    by_category: dict[str, int] = {}
    recent_upheld = 0
    for r in rows:
        outcome = RESOLVED_OUTCOME[r["case_state"]]
        counts[outcome] += 1
        if outcome == "upheld":
            by_category[r["category"]] = by_category.get(r["category"], 0) + 1
            if now - r["updated_at"] <= RECENT_YEAR:
                recent_upheld += 1
    return {
        "resolved": len(rows),
        **counts,
        "upheld_by_category": by_category,
        "upheld_last_year": recent_upheld,
        "under_appeal": sum(1 for r in rows if r["case_state"] == "APPEAL_WINDOW"),
        "recent": [
            {
                "category": r["category"],
                "outcome": RESOLVED_OUTCOME[r["case_state"]],
                "final": r["case_state"] != "APPEAL_WINDOW",
                "sanction": r["sanction"],
                "resolved_on": r["updated_at"].date().isoformat(),
            }
            for r in rows[:10]
        ],
        "not_shown": "open cases, complainants, statements, evidence and notes",
    }


def complaint_factor(record: dict[str, Any]) -> float:
    return max(COMPLAINT_FLOOR, COMPLAINT_FACTOR ** record["upheld_last_year"])


def apply_disclosure(page: dict[str, Any], disclosure: dict[str, bool]) -> dict[str, Any]:
    """The reputation view an outsider gets: hidden measures replaced by a
    plain marker. Governance and integrity are never hidden."""
    out = copy.deepcopy(page)
    for key in DISCLOSABLE:
        if not disclosure.get(key, True) and key in out:
            out[key] = {"hidden_by_owner": True}
    out["disclosure"] = disclosure
    return out

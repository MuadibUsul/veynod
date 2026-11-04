"""Encryption-policy negotiation (ICD_I_MSG §6, ADR-009, F-029).

Policies are ordered weakest to strongest. A connection takes the strongest
policy both sides support; once recorded, nothing may move it to a weaker one
(MSG-INV-1), and an offer this side cannot honour is refused, never answered
with plaintext (MSG-INV-2). P1 implements only ``server_readable``.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.services.federation.errors import FederationError

ORDER = ("server_readable", "e2ee")
SUPPORTED = ("server_readable",)


def _rank(policy: str) -> int:
    if policy not in ORDER:
        raise FederationError(
            "FED_E_POLICY_UNSUPPORTED", "unknown encryption policy", policy=policy
        )
    return ORDER.index(policy)


def negotiate(
    offered: Iterable[str], *, supported: Iterable[str] = SUPPORTED, current: str | None = None
) -> str:
    """The policy for a connection, given what the peer offered.

    ``current`` is the policy already recorded on the connection, if any.
    """
    offered_set = {p for p in offered if p in ORDER}
    common = offered_set & set(supported)
    if not common:
        raise FederationError(
            "FED_E_POLICY_UNSUPPORTED",
            "no encryption policy both sides support",
            offered=sorted(offered_set),
            supported=sorted(set(supported)),
        )
    chosen = max(common, key=_rank)
    if current is not None and _rank(chosen) < _rank(current):
        raise FederationError(
            "FED_E_POLICY_DOWNGRADE",
            "the connection's encryption policy cannot be weakened",
            current=current,
            offered=chosen,
        )
    return chosen if current is None else max(chosen, current, key=_rank)


def check_message(declared: str, connection_policy: str) -> None:
    """A message may not declare a weaker policy than its connection."""
    if _rank(declared) < _rank(connection_policy):
        raise FederationError(
            "FED_E_POLICY_DOWNGRADE",
            "message policy is weaker than the connection's",
            current=connection_policy,
            declared=declared,
        )
    if declared not in SUPPORTED:
        raise FederationError(
            "FED_E_POLICY_UNSUPPORTED", "policy not implemented here", declared=declared
        )

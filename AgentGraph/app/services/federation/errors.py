"""Federation error codes (I-ID §7.4, ICD_I_GRANT §8, ICD_I_MSG §7).

A FederationError never carries a local id (ID-INV-8): callers put only
federation ids, key ids and reason codes into ``detail``.
"""

from __future__ import annotations

from typing import Any

# code -> HTTP status
CODES: dict[str, int] = {
    "FED_E_DISABLED": 404,
    "FED_E_KEK_MISSING": 503,
    "FED_E_UNREACHABLE": 503,
    # identity (I-ID)
    "FED_E_IDENTITY_UNKNOWN": 404,
    "FED_E_IDENTITY_INVALID": 422,
    "FED_E_IDENTITY_UNTRUSTED": 403,
    "FED_E_IDENTITY_EXPIRED": 403,
    "FED_E_IDENTITY_FROZEN": 403,
    "FED_E_IDENTITY_TOMBSTONED": 410,
    "FED_E_TYPE_MISMATCH": 422,
    "FED_E_ALIAS_INVALID": 422,
    "FED_E_ALIAS_TAKEN": 409,
    "FED_E_ALIAS_AMBIGUOUS": 409,
    "FED_E_ALIAS_TOMBSTONED": 410,
    "FED_E_ALIAS_NOT_FOUND": 404,
    "FED_E_KEY_NOT_CURRENT": 403,
    "FED_E_KEY_REVOKED": 403,
    # envelope (I-MSG)
    "FED_E_ENVELOPE_INVALID": 422,
    "FED_E_PROTOCOL_VERSION": 422,
    "FED_E_TOO_LARGE": 413,
    "FED_E_AUDIENCE_MISMATCH": 403,
    "FED_E_EXPIRED": 403,
    "FED_E_NOT_YET_VALID": 403,
    "FED_E_BAD_DIGEST": 422,
    "FED_E_BAD_SIGNATURE": 403,
    "FED_E_REPLAY": 409,
    "FED_E_CREDENTIAL_IN_PAYLOAD": 422,
    "FED_E_POLICY_DOWNGRADE": 403,
    "FED_E_POLICY_UNSUPPORTED": 422,
    # grants (I-GRANT)
    "FED_E_GRANT_UNKNOWN": 404,
    "FED_E_GRANT_INACTIVE": 403,
    "FED_E_GRANT_EXPIRED": 403,
    "FED_E_GRANT_STALE": 409,
    "FED_E_GRANT_TRANSITION": 409,
    "FED_E_SCOPE_DENIED": 403,
    "FED_E_SCOPE_UNKNOWN": 422,
    "FED_E_APPROVAL_NOT_HUMAN": 403,
    # task bridge (I-BRIDGE)
    "FED_E_JOB_UNKNOWN": 404,
    "FED_E_JOB_TRANSITION": 409,
    "FED_E_JOB_STALE": 409,
    "FED_E_ACCEPTANCE_MISMATCH": 409,
    "FED_E_TERMS_INVALID": 422,
    "FED_E_SETTLEMENT_DISABLED": 422,
    "FED_E_BUDGET_EXCEEDED": 403,
    # messages (I-MSG §4, §5)
    "FED_E_MESSAGE_UNKNOWN": 404,
    "FED_E_RATE_LIMITED": 429,
    # governance (F-10)
    "FED_E_CASE_UNKNOWN": 404,
    "FED_E_CASE_TRANSITION": 409,
    "FED_E_SANCTION_RULE": 409,
    "FED_E_AUTHORITY_INVALID": 403,
    # other deployments (F-11)
    "FED_E_EGRESS_DISABLED": 403,
    "FED_E_REMOTE_INVALID": 422,
}


class FederationError(Exception):
    def __init__(self, code: str, message: str, **detail: Any) -> None:
        if code not in CODES:
            raise ValueError(f"unregistered federation error code {code}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = CODES[code]
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}

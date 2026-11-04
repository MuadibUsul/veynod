"""Federation layer (federation diagram F-01/F-02).

Contracts: doc://federation-i-id-identity-icd (ADR-001a),
docs/federation/ICD_I_GRANT.md and docs/federation/ICD_I_MSG.md.

Off by default: nothing here runs, and no federation identity exists, until
``FEDERATION_ENABLED`` is true (ADR-008, ID-INV-5).

Modules:
  errors     FederationError and the FED_E_* codes
  canonical  RFC 8785 (JCS) canonical JSON for everything that is signed
  ids        self-certifying ids, key ids, connection and object ids
  keys       Ed25519 keys and KEK sealing of hosted private keys (ADR-013)
  envelope   the signed envelope: build and verify (ICD_I_MSG §2–§3)
  policy     encryption-policy negotiation, no silent downgrade (F-029)
  identity   hosted identities: create, resolve, rotate, revoke, freeze, aliases
"""

from app.config import get_settings


def is_enabled() -> bool:
    return bool(get_settings().federation_enabled)

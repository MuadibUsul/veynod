"""The optional ``model_policy`` delegation term (ICD_I_BRIDGE §2.1).

Pure unit tests. Absent, the terms and their acceptance hash are exactly what
they were before the field existed, so existing delegations and peers on an
older version are unaffected.
"""

from __future__ import annotations

import pytest

from app.services.federation import delegations
from app.services.federation.canonical import sha256_hex
from app.services.federation.errors import FederationError
from tests.test_federation_delegations import _terms


def test_without_model_policy_nothing_changes():
    frozen = delegations.validate_terms(_terms())
    assert "model_policy" not in frozen
    legacy = sha256_hex({key: frozen[key] for key in delegations.TERM_FIELDS})
    assert delegations.acceptance_hash(frozen) == legacy


def test_a_pinned_model_class_is_frozen_and_hashed():
    policy = {"mode": "exact", "model_class": "claude-sonnet-5"}
    frozen = delegations.validate_terms({**_terms(), "model_policy": policy})
    assert frozen["model_policy"] == policy
    plain = delegations.validate_terms(_terms())
    # Same other terms, different hash: the class is part of what was accepted.
    assert delegations.acceptance_hash(frozen) != delegations.acceptance_hash(
        {**plain, "deadline": frozen["deadline"]}
    )


@pytest.mark.parametrize(
    "policy",
    [
        {"mode": "floor", "model_class": "claude-sonnet-5"},  # only exact for now
        {"mode": "exact"},
        {"mode": "exact", "model_class": "Claude Sonnet!"},
        {"mode": "exact", "model_class": "x", "extra": 1},
        "claude-sonnet-5",
        None,
    ],
)
def test_a_malformed_model_policy_is_refused(policy):
    with pytest.raises(FederationError) as err:
        delegations.validate_terms({**_terms(), "model_policy": policy})
    assert err.value.code == "FED_E_TERMS_INVALID"


def test_unknown_fields_are_still_refused():
    with pytest.raises(FederationError):
        delegations.validate_terms({**_terms(), "model": "anything"})

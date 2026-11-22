"""The Cognito trigger that copies the admin-only tenant attribute into the tokens."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SOURCE = Path(__file__).parents[2] / "infra" / "terraform" / "lambda" / "pre_token_generation.py"
spec = importlib.util.spec_from_file_location("pre_token_generation", SOURCE)
assert spec is not None and spec.loader is not None
trigger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trigger)


def event(attributes: dict[str, str]) -> dict[str, Any]:
    """The parts of a V2 pre token generation event the trigger reads and writes."""
    return {
        "version": "2",
        "triggerSource": "TokenGeneration_Authentication",
        "request": {"userAttributes": {"sub": "3f1c6a52", **attributes}},
        "response": {"claimsAndScopeOverrideDetails": None},
    }


def test_the_tenant_is_added_to_the_access_token() -> None:
    result = trigger.handler(event({"custom:tenant_id": "nsi-amer"}), None)

    details = result["response"]["claimsAndScopeOverrideDetails"]
    assert details["accessTokenGeneration"]["claimsToAddOrOverride"] == {"tenant_id": "nsi-amer"}


@pytest.mark.parametrize("tenant", [None, "", "NSI AMER", "nsi-amer;drop", "x" * 65])
def test_no_valid_tenant_means_no_claim(tenant: str | None) -> None:
    attributes = {} if tenant is None else {"custom:tenant_id": tenant}

    result = trigger.handler(event(attributes), None)

    assert result["response"]["claimsAndScopeOverrideDetails"] is None  # the API answers 403

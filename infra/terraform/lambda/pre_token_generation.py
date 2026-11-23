"""Amazon Cognito pre token generation trigger (V2 event): adds the tenant to the tokens.

The tenant comes from the ``custom:tenant_id`` attribute, which only administrators can
set: the app client has no write permission on it, so a user cannot move themselves to
another tenant. A user without a tenant gets no claim, and the API answers 403.
"""

from __future__ import annotations

import re
from typing import Any

TENANT = re.compile(r"^[a-z0-9-]{1,64}$")


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    tenant = event["request"]["userAttributes"].get("custom:tenant_id", "")
    if TENANT.match(tenant):
        claims = {"claimsToAddOrOverride": {"tenant_id": tenant}}
        event["response"]["claimsAndScopeOverrideDetails"] = {
            "accessTokenGeneration": claims,
            "idTokenGeneration": claims,
        }
    return event

import pytest
from fastapi import HTTPException

from app.api.dependencies import get_tenant_id
from app.core.config import Settings


def test_the_tenant_header_is_accepted_locally() -> None:
    assert get_tenant_id(Settings(app_env="local"), "nsi-amer") == "nsi-amer"


def test_the_tenant_header_is_refused_outside_local_and_test() -> None:
    # Deployed environments must authenticate the caller, never trust a header.
    with pytest.raises(HTTPException) as error:
        get_tenant_id(Settings(app_env="staging"), "nsi-amer")

    assert error.value.status_code == 401

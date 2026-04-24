from __future__ import annotations

import pytest

from jp_adopt_api.auth import (
    DEV_BEARER_TOKEN,
    DevelopmentAuthForbiddenError,
    authenticate_bearer,
)
from jp_adopt_api.config import Settings


def test_production_env_rejects_strict_auth_off() -> None:
    with pytest.raises(ValueError, match="STRICT_AUTH must be true"):
        Settings(app_env="production", strict_auth=False)


def test_production_env_accepts_strict_auth_on() -> None:
    s = Settings(app_env="production", strict_auth=True)
    assert s.is_production


def test_prod_alias_env_var_name() -> None:
    s = Settings(app_env="prod", strict_auth=True)
    assert s.is_production


def test_dev_local_forbidden_in_production_even_if_strict_auth_false() -> None:
    s = Settings.model_construct(app_env="production", strict_auth=False)
    with pytest.raises(DevelopmentAuthForbiddenError):
        authenticate_bearer(DEV_BEARER_TOKEN, s)


def test_dev_local_allowed_when_not_production_and_strict_auth_false() -> None:
    s = Settings(app_env="development", strict_auth=False)
    user = authenticate_bearer(DEV_BEARER_TOKEN, s)
    assert user.sub == "dev-local"

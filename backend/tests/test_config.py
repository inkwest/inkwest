"""Unit tests for startup configuration validation (app.core.config.validate_settings).

These guard the production fail-fast posture: a misconfigured deploy MUST crash-loop
rather than boot green with the default JWT secret or unauthenticated dev-admin login.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.core.config import validate_settings

# The literal default that ships in config.py; a prod deploy leaving it unchanged lets
# anyone forge an admin session JWT with the public key.
_DEFAULT_SECRET = "change-me-in-production"  # pragma: allowlist secret


def _settings(**over):
    """A settings mock with a safe production baseline; override per test.

    validate_engines()/inkwest_agent_enabled() read the real default settings (their
    get_settings is import-bound), so only the attributes validate_settings reads off
    the patched object need setting. connectors_enabled/aws_sso_role_filter are disabled
    so prod-only connector branches don't interfere with the checks under test.
    """
    base = {
        "app_env": "production",
        "secret_key": "a-strong-non-default-secret-value",  # pragma: allowlist secret
        "oidc_enabled": True,
        "connectors_enabled": False,
        "aws_sso_role_filter": None,
        "aws_region": "us-east-1",
        "llm_broker_enabled": False,
    }
    base.update(over)
    return MagicMock(**base)


def _run(**over):
    with patch("app.core.config.get_settings", return_value=_settings(**over)):
        validate_settings()


def test_rejects_default_secret_in_prod():
    with pytest.raises(ValueError, match="SECRET_KEY must be changed"):
        _run(secret_key=_DEFAULT_SECRET)


def test_rejects_empty_secret_in_prod():
    with pytest.raises(ValueError, match="SECRET_KEY must not be empty"):
        _run(secret_key="   ")


def test_rejects_oidc_disabled_in_prod():
    with pytest.raises(ValueError, match="OIDC_ENABLED must be true in production"):
        _run(oidc_enabled=False)


def test_passes_valid_prod_config():
    # A correctly configured production deployment boots.
    _run()


def test_allows_dev_with_oidc_disabled():
    # Dev auto-login is intended in development — the guards only apply to production.
    _run(app_env="development", oidc_enabled=False, secret_key=_DEFAULT_SECRET)

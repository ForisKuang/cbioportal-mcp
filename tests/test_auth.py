import os
from unittest.mock import Mock, patch

from cbioportal_mcp.auth import _build_auth_provider

_ALL_GOOGLE_ENV = {
    "CBIOPORTAL_MCP_GOOGLE_CLIENT_ID": "123-abc.apps.googleusercontent.com",
    "CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET": "fake-secret",
    "CBIOPORTAL_MCP_GOOGLE_BASE_URL": "https://mcp.cbioportal.org",
}


def _without(*names: str) -> dict:
    return {k: v for k, v in _ALL_GOOGLE_ENV.items() if k not in names}


def test_returns_none_when_no_google_env_set():
    with patch.dict(os.environ, {}, clear=True):
        assert _build_auth_provider() is None


def test_returns_none_when_client_id_missing():
    with patch.dict(os.environ, _without("CBIOPORTAL_MCP_GOOGLE_CLIENT_ID"), clear=True):
        assert _build_auth_provider() is None


def test_returns_none_when_client_secret_missing():
    with patch.dict(os.environ, _without("CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET"), clear=True):
        assert _build_auth_provider() is None


def test_returns_none_when_base_url_missing():
    with patch.dict(os.environ, _without("CBIOPORTAL_MCP_GOOGLE_BASE_URL"), clear=True):
        assert _build_auth_provider() is None


def test_builds_google_provider_when_all_three_set():
    # GoogleProvider's default client_storage creates a DiskStore under the
    # platformdirs data directory as a side effect of construction, so the
    # class itself is mocked here to keep this test hermetic (no filesystem
    # writes) rather than depending on that side effect in tests.
    fake_provider = Mock(name="GoogleProvider instance")
    with (
        patch.dict(os.environ, _ALL_GOOGLE_ENV, clear=True),
        patch(
            "cbioportal_mcp.auth.GoogleProvider", return_value=fake_provider
        ) as mock_google_provider,
    ):
        provider = _build_auth_provider()

    assert provider is fake_provider
    mock_google_provider.assert_called_once_with(
        client_id=_ALL_GOOGLE_ENV["CBIOPORTAL_MCP_GOOGLE_CLIENT_ID"],
        client_secret=_ALL_GOOGLE_ENV["CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET"],
        base_url=_ALL_GOOGLE_ENV["CBIOPORTAL_MCP_GOOGLE_BASE_URL"],
        require_authorization_consent=False,
    )

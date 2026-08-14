import base64
from unittest.mock import Mock, patch

from fastmcp.server.auth import AccessToken
from mcp.types import Implementation

from cbioportal_mcp.telemetry import (
    _extract_mcp_client_info,
    _extract_oauth_identity,
    _extract_session_id,
    _extract_user_identity,
    _resolve_caller_identity,
)


def _access_token(**claims) -> AccessToken:
    return AccessToken(token="fake-token", client_id="fake-client", scopes=[], claims=claims)


def test_extracts_user_id_from_header():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": "507f1f77bcf86cd799439011"}):
        user_id, user_email, client = _extract_user_identity()
        assert user_id == "507f1f77bcf86cd799439011"
        assert user_email is None
        assert client == "librechat"


def test_extracts_user_email_from_header():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": "abc123", "x-user-email": "user@example.com"}):
        user_id, user_email, client = _extract_user_identity()
        assert user_id == "abc123"
        assert user_email == "user@example.com"
        assert client == "librechat"


def test_decodes_base64_email():
    encoded = "b64:" + base64.b64encode(b"user+tag@example.com").decode()
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-email": encoded}):
        _, user_email, client = _extract_user_identity()
        assert user_email == "user+tag@example.com"
        assert client == "direct"


def test_returns_none_when_headers_absent():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
        user_id, user_email, client = _extract_user_identity()
        assert user_id is None
        assert user_email is None
        assert client == "direct"


def test_returns_none_when_empty_string():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": "", "x-user-email": ""}):
        user_id, user_email, client = _extract_user_identity()
        assert user_id is None
        assert user_email is None
        assert client == "librechat"


def _context_with_client_info(name: str, version: str) -> Mock:
    context = Mock()
    context.fastmcp_context.session.client_params.clientInfo = Implementation(
        name=name, version=version
    )
    return context


def test_extracts_mcp_client_info_for_claude_code():
    context = _context_with_client_info("claude-code", "1.2.3")
    name, version = _extract_mcp_client_info(context)
    assert name == "claude-code"
    assert version == "1.2.3"


def test_extracts_mcp_client_info_for_codex():
    context = _context_with_client_info("codex", "0.7.9")
    name, version = _extract_mcp_client_info(context)
    assert name == "codex"
    assert version == "0.7.9"


def test_client_info_none_when_no_fastmcp_context():
    context = Mock()
    context.fastmcp_context = None
    name, version = _extract_mcp_client_info(context)
    assert name is None
    assert version is None


def test_client_info_none_when_client_params_missing():
    context = Mock()
    context.fastmcp_context.session.client_params = None
    name, version = _extract_mcp_client_info(context)
    assert name is None
    assert version is None


def test_extracts_session_id_from_context():
    context = Mock()
    context.fastmcp_context.session_id = "session-abc-123"
    assert _extract_session_id(context) == "session-abc-123"


def test_session_id_none_when_no_fastmcp_context():
    context = Mock()
    context.fastmcp_context = None
    assert _extract_session_id(context) is None


def test_extracts_oauth_identity_from_access_token():
    token = _access_token(sub="keycloak-user-abc", email="alice@example.org")
    with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
        user_id, user_email = _extract_oauth_identity()
        assert user_id == "keycloak-user-abc"
        assert user_email == "alice@example.org"


def test_oauth_identity_none_when_no_access_token():
    with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
        user_id, user_email = _extract_oauth_identity()
        assert user_id is None
        assert user_email is None


def test_oauth_identity_none_when_sub_claim_missing():
    token = _access_token(email="alice@example.org")
    with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
        user_id, user_email = _extract_oauth_identity()
        assert user_id is None
        assert user_email == "alice@example.org"


def test_resolve_caller_identity_prefers_oauth_over_header():
    token = _access_token(sub="keycloak-user-abc", email="alice@example.org")
    with (
        patch("fastmcp.server.dependencies.get_access_token", return_value=token),
        patch(
            "fastmcp.server.dependencies.get_http_headers",
            return_value={"x-user-id": "librechat-user-1"},
        ),
    ):
        user_id, user_email, client = _resolve_caller_identity()
        assert user_id == "keycloak-user-abc"
        assert user_email == "alice@example.org"
        assert client == "oauth"


def test_resolve_caller_identity_falls_back_to_header_when_no_oauth_token():
    with (
        patch("fastmcp.server.dependencies.get_access_token", return_value=None),
        patch(
            "fastmcp.server.dependencies.get_http_headers",
            return_value={"x-user-id": "librechat-user-1"},
        ),
    ):
        user_id, user_email, client = _resolve_caller_identity()
        assert user_id == "librechat-user-1"
        assert client == "librechat"


def test_resolve_caller_identity_direct_when_neither_present():
    with (
        patch("fastmcp.server.dependencies.get_access_token", return_value=None),
        patch("fastmcp.server.dependencies.get_http_headers", return_value={}),
    ):
        user_id, user_email, client = _resolve_caller_identity()
        assert user_id is None
        assert client == "direct"

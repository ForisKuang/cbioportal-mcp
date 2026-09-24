import base64
from unittest.mock import Mock, patch

from mcp.types import Implementation

from cbioportal_mcp.telemetry import (
    _extract_mcp_client_info,
    _extract_request_source,
    _extract_session_id,
    _extract_user_identity,
)


def test_extracts_user_id_from_header():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": "507f1f77bcf86cd799439011"}):
        user_id, user_email = _extract_user_identity()
        assert user_id == "507f1f77bcf86cd799439011"
        assert user_email is None


def test_extracts_user_email_from_header():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": "abc123", "x-user-email": "user@example.com"}):
        user_id, user_email = _extract_user_identity()
        assert user_id == "abc123"
        assert user_email == "user@example.com"


def test_decodes_base64_email():
    encoded = "b64:" + base64.b64encode(b"user+tag@example.com").decode()
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-email": encoded}):
        _, user_email = _extract_user_identity()
        assert user_email == "user+tag@example.com"


def test_returns_none_when_headers_absent():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
        user_id, user_email = _extract_user_identity()
        assert user_id is None
        assert user_email is None


def test_returns_none_when_empty_string():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={"x-user-id": "", "x-user-email": ""}):
        user_id, user_email = _extract_user_identity()
        assert user_id is None
        assert user_email is None


def test_extracts_user_id_from_trusted_proxy_header_without_librechat_header():
    # No x-user-id (LibreChat's convention) but a Keycloak/oauth2-proxy-style
    # header is present — this should still resolve to a real identity so
    # non-LibreChat, proxy-fronted deployments show up correctly in traces.
    with patch(
        "fastmcp.server.dependencies.get_http_headers",
        return_value={
            "x-auth-request-user": "keycloak-sub-123",
            "x-auth-request-email": "bob@example.org",
        },
    ):
        user_id, user_email = _extract_user_identity()
        assert user_id == "keycloak-sub-123"
        assert user_email == "bob@example.org"


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


def test_extracts_mcp_client_info_for_librechat():
    context = _context_with_client_info("librechat", "0.7.9")
    name, version = _extract_mcp_client_info(context)
    assert name == "librechat"
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


def test_request_source_authenticated_proxy_when_identity_headers_present():
    with patch(
        "fastmcp.server.dependencies.get_http_headers",
        return_value={"x-user-id": "abc123"},
    ):
        assert _extract_request_source() == "authenticated-proxy"


def test_request_source_direct_when_no_identity_headers():
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
        assert _extract_request_source() == "direct"


def test_extracts_session_id_from_context():
    context = Mock()
    context.fastmcp_context.session_id = "session-abc-123"
    assert _extract_session_id(context) == "session-abc-123"


def test_session_id_none_when_no_fastmcp_context():
    context = Mock()
    context.fastmcp_context = None
    assert _extract_session_id(context) is None

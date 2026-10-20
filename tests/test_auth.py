import asyncio
import logging
import os
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleProvider

from cbioportal_mcp.auth import (
    EmailAllowlistGoogleProvider,
    _build_auth_provider,
    ensure_auth_configured,
    is_email_allowed,
)

_ALL_GOOGLE_ENV = {
    "CBIOPORTAL_MCP_GOOGLE_CLIENT_ID": "123-abc.apps.googleusercontent.com",
    "CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET": "fake-secret",
    "CBIOPORTAL_MCP_GOOGLE_BASE_URL": "https://mcp.cbioportal.org",
}

_EXPECTED_PROVIDER_KWARGS = dict(
    client_id=_ALL_GOOGLE_ENV["CBIOPORTAL_MCP_GOOGLE_CLIENT_ID"],
    client_secret=_ALL_GOOGLE_ENV["CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET"],
    base_url=_ALL_GOOGLE_ENV["CBIOPORTAL_MCP_GOOGLE_BASE_URL"],
    required_scopes=["openid", "email", "profile"],
)


def _without(*names: str) -> dict:
    return {k: v for k, v in _ALL_GOOGLE_ENV.items() if k not in names}


# --- Provider construction / partial config -------------------------------


def test_returns_none_when_no_google_env_set():
    with patch.dict(os.environ, {}, clear=True):
        assert _build_auth_provider() is None


@pytest.mark.parametrize(
    "missing",
    [
        ("CBIOPORTAL_MCP_GOOGLE_CLIENT_ID",),
        ("CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET",),
        ("CBIOPORTAL_MCP_GOOGLE_BASE_URL",),
        ("CBIOPORTAL_MCP_GOOGLE_CLIENT_ID", "CBIOPORTAL_MCP_GOOGLE_BASE_URL"),
    ],
)
def test_partial_google_config_is_a_startup_error_naming_missing_vars(missing):
    with (
        patch.dict(os.environ, _without(*missing), clear=True),
        pytest.raises(ValueError, match="partially configured") as excinfo,
    ):
        _build_auth_provider()
    for name in missing:
        assert name in str(excinfo.value)


def test_builds_google_provider_with_consent_enabled_by_default():
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
        **_EXPECTED_PROVIDER_KWARGS,
        require_authorization_consent=True,
    )


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes"])
def test_skip_auth_consent_opt_out_disables_consent_and_warns(value, caplog):
    env = {**_ALL_GOOGLE_ENV, "CBIOPORTAL_MCP_SKIP_AUTH_CONSENT": value}
    with (
        patch.dict(os.environ, env, clear=True),
        patch("cbioportal_mcp.auth.GoogleProvider") as mock_google_provider,
        caplog.at_level(logging.WARNING, logger="cbioportal_mcp.auth"),
    ):
        _build_auth_provider()

    mock_google_provider.assert_called_once_with(
        **_EXPECTED_PROVIDER_KWARGS,
        require_authorization_consent=False,
    )
    assert "CBIOPORTAL_MCP_SKIP_AUTH_CONSENT" in caplog.text


@pytest.mark.parametrize("value", ["false", "0", "", "tru"])
def test_skip_auth_consent_non_true_values_keep_consent(value):
    env = {**_ALL_GOOGLE_ENV, "CBIOPORTAL_MCP_SKIP_AUTH_CONSENT": value}
    with (
        patch.dict(os.environ, env, clear=True),
        patch("cbioportal_mcp.auth.GoogleProvider") as mock_google_provider,
    ):
        _build_auth_provider()
    assert mock_google_provider.call_args.kwargs["require_authorization_consent"] is True


def test_no_allowlist_builds_plain_google_provider():
    with (
        patch.dict(os.environ, _ALL_GOOGLE_ENV, clear=True),
        patch("cbioportal_mcp.auth.GoogleProvider") as mock_google_provider,
        patch("cbioportal_mcp.auth.EmailAllowlistGoogleProvider") as mock_allowlist,
    ):
        _build_auth_provider()
    mock_google_provider.assert_called_once()
    mock_allowlist.assert_not_called()


def test_allowlist_env_builds_allowlist_provider_with_parsed_values():
    env = {
        **_ALL_GOOGLE_ENV,
        "CBIOPORTAL_MCP_ALLOWED_EMAIL_DOMAINS": " MSKCC.org, example.org ,,",
        "CBIOPORTAL_MCP_ALLOWED_EMAILS": "Alice@Gmail.com",
    }
    with (
        patch.dict(os.environ, env, clear=True),
        patch("cbioportal_mcp.auth.GoogleProvider") as mock_google_provider,
        patch("cbioportal_mcp.auth.EmailAllowlistGoogleProvider") as mock_allowlist,
    ):
        _build_auth_provider()
    mock_google_provider.assert_not_called()
    mock_allowlist.assert_called_once_with(
        allowed_emails=frozenset({"alice@gmail.com"}),
        allowed_domains=frozenset({"mskcc.org", "example.org"}),
        **_EXPECTED_PROVIDER_KWARGS,
        require_authorization_consent=True,
    )


# --- Transport gate -------------------------------------------------------


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_http_transports_refuse_to_start_without_auth(transport):
    with (
        patch.dict(os.environ, {}, clear=True),
        pytest.raises(RuntimeError, match="CBIOPORTAL_MCP_ALLOW_UNAUTHENTICATED"),
    ):
        ensure_auth_configured(transport, None)


@pytest.mark.parametrize("value", ["false", "0", "", "tru"])
def test_non_true_opt_out_values_still_refuse(value):
    env = {"CBIOPORTAL_MCP_ALLOW_UNAUTHENTICATED": value}
    with patch.dict(os.environ, env, clear=True), pytest.raises(RuntimeError):
        ensure_auth_configured("http", None)


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_allow_unauthenticated_opt_out_starts_with_warning(transport, caplog):
    env = {"CBIOPORTAL_MCP_ALLOW_UNAUTHENTICATED": "true"}
    with (
        patch.dict(os.environ, env, clear=True),
        caplog.at_level(logging.WARNING, logger="cbioportal_mcp.auth"),
    ):
        ensure_auth_configured(transport, None)
    assert "NO authentication" in caplog.text


def test_stdio_needs_no_auth_or_opt_out():
    with patch.dict(os.environ, {}, clear=True):
        ensure_auth_configured("stdio", None)


def test_http_with_auth_provider_starts():
    with patch.dict(os.environ, {}, clear=True):
        ensure_auth_configured("http", Mock(name="auth provider"))


def test_server_main_exits_when_http_has_no_auth():
    from cbioportal_mcp import server

    env = {"CLICKHOUSE_MCP_SERVER_TRANSPORT": "http"}
    with (
        patch.dict(os.environ, env, clear=True),
        patch.object(server.mcp, "auth", None),
        patch.object(server, "ensure_db_permissions") as mock_db_check,
        patch.object(server.mcp, "run") as mock_run,
        pytest.raises(SystemExit) as excinfo,
    ):
        server.main()
    assert excinfo.value.code == 2
    mock_db_check.assert_not_called()
    mock_run.assert_not_called()


# --- Email allowlist ------------------------------------------------------


def _claims(email, verified=True):
    return {
        "sub": "1234",
        "email": email,
        "google_user_data": {"id": "1234", "email": email, "verified_email": verified},
        "google_token_info": {},
    }


_DOMAINS = frozenset({"mskcc.org"})
_EMAILS = frozenset({"alice@gmail.com"})


@pytest.mark.parametrize(
    "email, allowed",
    [
        ("bob@mskcc.org", True),
        ("Bob@MSKCC.ORG", True),
        ("alice@gmail.com", True),
        ("ALICE@Gmail.com", True),
        ("bob@evilmskcc.org", False),
        ("bob@mskcc.org.evil.com", False),
        ("bob@sub.mskcc.org", False),
        ("mskcc.org@evil.com", False),
        ("alice@gmail.com.evil.com", False),
        ("eve@gmail.com", False),
        (None, False),
        ("not-an-email", False),
    ],
)
def test_is_email_allowed_exact_case_insensitive_matching(email, allowed):
    assert (
        is_email_allowed(_claims(email), allowed_emails=_EMAILS, allowed_domains=_DOMAINS)
        is allowed
    )


@pytest.mark.parametrize("verified", [False, None, "true"])
def test_is_email_allowed_rejects_unverified_email(verified):
    assert not is_email_allowed(
        _claims("bob@mskcc.org", verified=verified),
        allowed_emails=_EMAILS,
        allowed_domains=_DOMAINS,
    )


def test_is_email_allowed_accepts_oidc_email_verified_from_tokeninfo():
    claims = {
        "email": "bob@mskcc.org",
        "google_user_data": {},
        "google_token_info": {"email_verified": True},
    }
    assert is_email_allowed(claims, allowed_emails=(), allowed_domains=_DOMAINS)


def test_allowlist_provider_constructs_and_keeps_consent():
    from key_value.aio.stores.memory import MemoryStore

    provider = EmailAllowlistGoogleProvider(
        allowed_emails=["Alice@Gmail.com"],
        allowed_domains=["MSKCC.org"],
        **_EXPECTED_PROVIDER_KWARGS,
        require_authorization_consent=True,
        client_storage=MemoryStore(),
        jwt_signing_key="test-signing-key",
    )
    assert isinstance(provider, GoogleProvider)
    assert provider._allowed_emails == frozenset({"alice@gmail.com"})
    assert provider._allowed_domains == frozenset({"mskcc.org"})
    assert provider._require_authorization_consent is True


def _allowlist_provider() -> EmailAllowlistGoogleProvider:
    # Bypass GoogleProvider.__init__ (creates a DiskStore); load_access_token
    # only needs the allowlist attributes plus the mocked parent method.
    provider = EmailAllowlistGoogleProvider.__new__(EmailAllowlistGoogleProvider)
    provider._allowed_emails = _EMAILS
    provider._allowed_domains = _DOMAINS
    return provider


def _access_token(email, verified=True) -> AccessToken:
    return AccessToken(token="t", client_id="c", scopes=["openid"], claims=_claims(email, verified))


@pytest.mark.parametrize(
    "email, verified, accepted",
    [
        ("bob@mskcc.org", True, True),
        ("bob@evilmskcc.org", True, False),
        ("bob@mskcc.org", False, False),
    ],
)
def test_allowlist_provider_verify_token_accepts_or_rejects(email, verified, accepted):
    upstream = _access_token(email, verified)
    provider = _allowlist_provider()
    with patch.object(GoogleProvider, "load_access_token", AsyncMock(return_value=upstream)):
        # verify_token is what FastMCP's bearer auth middleware calls; None
        # there becomes a 401 before any tool/resource handler runs.
        result = asyncio.run(provider.verify_token("fastmcp-jwt"))
    if accepted:
        assert result is upstream
    else:
        assert result is None


def test_allowlist_provider_passes_through_invalid_upstream_token():
    provider = _allowlist_provider()
    with patch.object(GoogleProvider, "load_access_token", AsyncMock(return_value=None)):
        assert asyncio.run(provider.verify_token("bad")) is None

"""Native MCP OAuth for cBioPortal MCP, backed directly by Google.

Opt-in via environment: set all three CBIOPORTAL_MCP_GOOGLE_* variables and
every caller — including direct connectors like Claude.ai, Claude Desktop, and
Claude Code — must complete a real Google login before any tool call succeeds.

Fails closed: setting only some of the three is a startup error, and an
HTTP/SSE server with none of them refuses to start unless
CBIOPORTAL_MCP_ALLOW_UNAUTHENTICATED=true is set explicitly (e.g. the existing
internal deployment LibreChat talks to). stdio is unaffected — it's a local
process the caller already trusts.

By default any Google account can authenticate; there is no Workspace-domain
restriction. This is deliberate: not everyone who should get MCP access has
an account in the org's existing Keycloak instance, so Keycloak (which would
otherwise be the natural reuse of existing infra) was ruled out as a hard
requirement for authentication here. Deployments that do want to restrict
access can set CBIOPORTAL_MCP_ALLOWED_EMAIL_DOMAINS and/or
CBIOPORTAL_MCP_ALLOWED_EMAILS (see `EmailAllowlistGoogleProvider`).

Deliberately scoped to authentication only. It says nothing about which
studies an authenticated caller can see — that's `cbioportal_mcp.authentication`,
a separate, independent piece of work.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from fastmcp.server.auth import AccessToken, AuthProvider
from fastmcp.server.auth.providers.google import GoogleProvider

from cbioportal_mcp.env import TransportType, get_mcp_config

logger = logging.getLogger(__name__)


def is_email_allowed(
    claims: dict,
    *,
    allowed_emails: Iterable[str],
    allowed_domains: Iterable[str],
) -> bool:
    """Whether a Google-verified token's claims satisfy the email allowlist.

    `claims` comes from FastMCP's GoogleTokenVerifier, which fetched it
    server-side from Google's userinfo/tokeninfo endpoints using the upstream
    token — never from request headers. Requires Google to report the email
    as verified. Matching is case-insensitive and exact: a full email in
    `allowed_emails`, or the part after the last "@" equal to an entry in
    `allowed_domains` (so "evilmskcc.org" or "a.mskcc.org" never match
    "mskcc.org").
    """
    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email:
        return False

    user_data = claims.get("google_user_data") or {}
    token_info = claims.get("google_token_info") or {}
    # Google's v2 userinfo / v1 tokeninfo call it `verified_email`; OIDC
    # id_token/userinfo call it `email_verified`. Only a real boolean True
    # counts.
    verified = any(
        source.get(key) is True
        for source in (user_data, token_info, claims)
        for key in ("verified_email", "email_verified")
    )
    if not verified:
        return False

    email = email.strip().lower()
    domain = email.rsplit("@", 1)[1]
    return email in set(allowed_emails) or domain in set(allowed_domains)


class EmailAllowlistGoogleProvider(GoogleProvider):
    """GoogleProvider that additionally rejects users outside an email allowlist.

    Hooks `load_access_token` — the point where OAuthProxy has already
    re-validated the upstream Google token — so a disallowed user's request
    gets a 401 from FastMCP's bearer auth before any tool or resource runs.
    """

    def __init__(
        self,
        *,
        allowed_emails: Iterable[str] = (),
        allowed_domains: Iterable[str] = (),
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._allowed_emails = frozenset(e.lower() for e in allowed_emails)
        self._allowed_domains = frozenset(d.lower() for d in allowed_domains)

    async def load_access_token(self, token: str) -> AccessToken | None:
        access_token = await super().load_access_token(token)
        if access_token is None:
            return None
        if not is_email_allowed(
            access_token.claims,
            allowed_emails=self._allowed_emails,
            allowed_domains=self._allowed_domains,
        ):
            logger.warning("Rejected authenticated user not on the email allowlist")
            return None
        return access_token


def _build_auth_provider() -> GoogleProvider | None:
    """Build the OAuth provider for this deployment, or None when unconfigured.

    Requires all three CBIOPORTAL_MCP_GOOGLE_* environment variables to be
    set; with none set, returns None (whether that's acceptable is decided
    per-transport by `ensure_auth_configured`). With only some set, raises
    ValueError rather than silently starting unauthenticated.
    """
    config = get_mcp_config()

    client_id = config.google_client_id
    client_secret = config.google_client_secret
    base_url = config.google_base_url

    if not (client_id and client_secret and base_url):
        missing = [
            name
            for name, value in (
                ("CBIOPORTAL_MCP_GOOGLE_CLIENT_ID", client_id),
                ("CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET", client_secret),
                ("CBIOPORTAL_MCP_GOOGLE_BASE_URL", base_url),
            )
            if not value
        ]
        if len(missing) < 3:
            raise ValueError(
                "Google OAuth partially configured — missing "
                f"{', '.join(missing)}. Set all three CBIOPORTAL_MCP_GOOGLE_* "
                "variables to enable OAuth, or none of them to disable it."
            )
        return None

    skip_consent = config.skip_auth_consent
    if skip_consent:
        logger.warning(
            "⚠️ CBIOPORTAL_MCP_SKIP_AUTH_CONSENT=true — FastMCP's OAuth consent "
            "screen is disabled; a malicious MCP client could be authorized "
            "without the user seeing which app is asking."
        )

    provider_kwargs = dict(
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        # Request `openid`, `email`, AND `profile` so `enduser.email`
        # populates on spans. Empirically, FastMCP's GoogleTokenVerifier
        # hits Google's legacy /oauth2/v2/userinfo endpoint which only
        # returns the `email` field when the token also carries `profile`
        # scope — even though the token was granted `email`. Requesting
        # `email` alone leaves telemetry.py's `_extract_oauth_identity`
        # with only `sub` (opaque Google account ID), which can't be
        # mapped back to a person without a live OAuth session for that
        # user. Neither `email` nor `profile` is a sensitive Google scope;
        # no verification cycle required, adds ~one line each to the
        # consent screen. The email allowlist also depends on it.
        required_scopes=["openid", "email", "profile"],
        # FastMCP's own consent interstitial is shown by default: it's what
        # protects against a malicious MCP client silently getting authorized
        # without the user seeing which app is asking for access ("confused
        # deputy" problem). The cost is one extra click before Google's login
        # page. CBIOPORTAL_MCP_SKIP_AUTH_CONSENT=true skips it (previous
        # behavior) — only for local dev or a fully trusted client set.
        require_authorization_consent=not skip_consent,
    )

    allowed_emails = config.allowed_emails
    allowed_domains = config.allowed_email_domains
    if allowed_emails or allowed_domains:
        logger.info(
            "✅ Google OAuth enabled for client %s, restricted to %d email(s) and domain(s) %s",
            client_id,
            len(allowed_emails),
            ", ".join(sorted(allowed_domains)) or "(none)",
        )
        return EmailAllowlistGoogleProvider(
            allowed_emails=allowed_emails,
            allowed_domains=allowed_domains,
            **provider_kwargs,
        )

    logger.info("✅ Google OAuth enabled for client %s", client_id)
    return GoogleProvider(**provider_kwargs)


def ensure_auth_configured(transport: str, auth_provider: AuthProvider | None) -> None:
    """Refuse to serve HTTP/SSE without auth unless explicitly opted out.

    Raises RuntimeError for an unauthenticated HTTP/SSE server when
    CBIOPORTAL_MCP_ALLOW_UNAUTHENTICATED isn't set; logs a prominent warning
    when it is. stdio needs no auth (local process trust).
    """
    if auth_provider is not None:
        return
    if transport not in (TransportType.HTTP.value, TransportType.SSE.value):
        return
    if not get_mcp_config().allow_unauthenticated:
        raise RuntimeError(
            f"Refusing to start the {transport} transport without authentication. "
            "Set CBIOPORTAL_MCP_GOOGLE_CLIENT_ID, CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET "
            "and CBIOPORTAL_MCP_GOOGLE_BASE_URL to enable Google OAuth, or set "
            "CBIOPORTAL_MCP_ALLOW_UNAUTHENTICATED=true to run unauthenticated "
            "(only behind a trusted network boundary)."
        )
    logger.warning(
        "⚠️⚠️⚠️ CBIOPORTAL_MCP_ALLOW_UNAUTHENTICATED=true — serving %s with NO "
        "authentication. Anyone who can reach this port can run every tool, "
        "including raw SQL. ⚠️⚠️⚠️",
        transport,
    )

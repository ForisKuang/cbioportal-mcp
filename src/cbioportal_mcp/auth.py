"""Native MCP OAuth for cBioPortal MCP, backed directly by Google.

Opt-in via environment: unset any of the three CBIOPORTAL_MCP_GOOGLE_* variables
and the server runs exactly as it does today, unauthenticated (the existing
internal deployment LibreChat talks to). Set all three and every caller —
including direct connectors like Claude.ai, Claude Desktop, and Claude Code —
must complete a real Google login before any tool call succeeds.

Any Google account can authenticate; there is no Workspace-domain
restriction. This is deliberate: not everyone who should get MCP access has
an account in the org's existing Keycloak instance, so Keycloak (which would
otherwise be the natural reuse of existing infra) was ruled out as a hard
requirement for authentication here.

Deliberately scoped to authentication only. It says nothing about which
studies an authenticated caller can see — that's `cbioportal_mcp.authentication`,
a separate, independent piece of work.
"""

from __future__ import annotations

import logging

from fastmcp.server.auth.providers.google import GoogleProvider

from cbioportal_mcp.env import get_mcp_config

logger = logging.getLogger(__name__)


def _build_auth_provider() -> GoogleProvider | None:
    """Build the OAuth provider for this deployment, or None to stay unauthenticated.

    Requires all three CBIOPORTAL_MCP_GOOGLE_* environment variables to be
    set; with any missing, returns None (current unauthenticated behavior)
    rather than starting in a partially-configured state.
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
        if 0 < len(missing) < 3:
            logger.warning(
                "OAuth partially configured (missing %s) — running unauthenticated.",
                ", ".join(missing),
            )
        return None

    logger.info("✅ Google OAuth enabled for client %s", client_id)
    return GoogleProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        # Skips FastMCP's own consent interstitial so the flow goes straight
        # to Google's real login page — a temporary UX call, not a security
        # one: FastMCP's docs flag this as normally only for local dev, since
        # the consent screen is what protects against a malicious MCP client
        # silently getting authorized without the user seeing which app is
        # asking for access ("confused deputy" problem). Revisit before wide
        # rollout — the fix there is a custom-branded consent page, not this.
        require_authorization_consent=False,
    )

"""Environment configuration for cBioPortal MCP server."""

import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TransportType(str, Enum):
    """Supported MCP server transport types."""

    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"

    @classmethod
    def values(cls) -> list[str]:
        """Get all valid transport values."""
        return [transport.value for transport in cls]


@dataclass
class McpConfig:
    """Configuration for Mcp connection settings."""

    def __init__(self):
        """Initialize the configuration from environment variables."""

    @property
    def mcp_server_transport(self) -> str:
        """Get the MCP server transport method.

        Valid options: "stdio", "http", "sse"
        Default: "stdio"
        """
        transport = os.getenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", TransportType.STDIO.value).lower()

        # Validate transport type
        if transport not in TransportType.values():
            valid_options = ", ".join(f'"{t}"' for t in TransportType.values())
            raise ValueError(f"Invalid transport '{transport}'. Valid options: {valid_options}")
        return transport

    @property
    def mcp_bind_host(self) -> str:
        """Get the host to bind the MCP server to.

        Only used when transport is "http" or "sse".
        Default: "127.0.0.1"
        """
        return os.getenv("CLICKHOUSE_MCP_BIND_HOST", "127.0.0.1")

    @property
    def mcp_bind_port(self) -> int:
        """Get the port to bind the MCP server to.

        Only used when transport is "http" or "sse".
        Default: 8000
        """
        return int(os.getenv("CLICKHOUSE_MCP_BIND_PORT", "8000"))

    @property
    def mcp_http_path(self) -> Optional[str]:
        """Get the HTTP path under which to mount the MCP server.

        Set when the server is reverse-proxied behind a sub-path so that
        FastMCP's generated URLs (e.g. trailing-slash redirects) include
        the external prefix. Example: "/db/mcp" when served at
        https://host/db/mcp via Traefik without stripPrefix.

        Only used when transport is "http" or "sse". When unset, FastMCP
        uses its default path ("/mcp").
        """
        value = os.getenv("CLICKHOUSE_MCP_HTTP_PATH")
        return value if value else None

    @property
    def mcp_forwarded_allow_ips(self) -> Optional[str]:
        """Get the set of upstream IPs whose X-Forwarded-* headers uvicorn
        should trust. Forwarded as `forwarded_allow_ips` to `uvicorn.Config`.

        Set this when the server sits behind a reverse proxy that
        terminates TLS (e.g. Traefik with an https Ingress) — otherwise
        uvicorn ignores `X-Forwarded-Proto: https` and builds redirect
        Locations from its own scheme (http), which strict clients refuse
        to follow.

        Common values:
          - "*"                  trust any upstream (fine inside a Pod
                                 reachable only via in-cluster Services)
          - "10.0.0.0/8,..."     scope to a CIDR (e.g. the Traefik LB
                                 source range)

        Only used when transport is "http" or "sse". When unset, uvicorn
        defaults apply (trust only 127.0.0.1, no proxy_headers).
        """
        value = os.getenv("CLICKHOUSE_MCP_FORWARDED_ALLOW_IPS")
        return value if value else None

    @property
    def mcp_user(self) -> str:
        """Get the clickhouse user for which the MCP server is running for.

        Default: "app_user"
        """

        return str(os.getenv("CLICKHOUSE_USER", "app_user"))

    @property
    def mcp_database(self) -> str:
        """Get the clickhouse db for which the MCP server is running against

        Default: "cgds_public_2025_06_24"
        """

        return str(os.getenv("CLICKHOUSE_DATABASE", "cgds_public_2025_06_24"))

    @property
    def google_client_id(self) -> Optional[str]:
        """Google OAuth client ID (from Google Cloud Console).

        Example: "123-abc.apps.googleusercontent.com".

        One of the three Google OAuth settings gating whether OAuth is enabled
        at all — see `cbioportal_mcp.auth._build_auth_provider`. Any Google
        account can authenticate; there is no Workspace-domain restriction.
        """
        value = os.getenv("CBIOPORTAL_MCP_GOOGLE_CLIENT_ID")
        return value if value else None

    @property
    def google_client_secret(self) -> Optional[str]:
        """Google OAuth client secret (from Google Cloud Console)."""
        value = os.getenv("CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET")
        return value if value else None

    @property
    def google_base_url(self) -> Optional[str]:
        """This server's own public URL, used to construct its OAuth redirect/callback URIs.

        Example: "https://mcp.cbioportal.org". Must match a redirect URI
        registered on the Google OAuth client (base_url + "/auth/callback").
        """
        value = os.getenv("CBIOPORTAL_MCP_GOOGLE_BASE_URL")
        return value if value else None

    @property
    def query_timeout_seconds(self) -> int:
        """Client-side query timeout in seconds (also default HTTP read timeout).

        Default: 30 (kept above max_execution_time_seconds so ClickHouse cancels
        slow queries server-side before the client socket times out).
        """
        return max(1, int(os.getenv("CBIOPORTAL_MCP_QUERY_TIMEOUT", "30")))

    @property
    def max_execution_time_seconds(self) -> int:
        """ClickHouse server-side ``max_execution_time`` setting in seconds.

        Default: 25 (strictly below the default 30s client timeout so ClickHouse
        terminates slow queries server-side instead of letting them run until
        ``send_receive_timeout``). Set to 0 to disable.
        """
        return max(0, int(os.getenv("CBIOPORTAL_MCP_MAX_EXECUTION_TIME", "25")))

    @property
    def max_result_rows(self) -> int:
        """ClickHouse server-side ``max_result_rows`` setting (used with
        ``result_overflow_mode='break'``).

        Default: 10000. Set to 0 to disable.
        """
        return max(0, int(os.getenv("CBIOPORTAL_MCP_MAX_RESULT_ROWS", "10000")))

    @property
    def max_result_bytes(self) -> int:
        """ClickHouse server-side ``max_result_bytes`` setting in bytes (used with
        ``result_overflow_mode='break'``).

        Default: 50000000 (~50 MB). Set to 0 to disable.
        """
        return max(0, int(os.getenv("CBIOPORTAL_MCP_MAX_RESULT_BYTES", "50000000")))

    @property
    def max_returned_rows(self) -> int:
        """Maximum rows returned to the LLM by ``clickhouse_run_select_query``.

        When a query returns more rows than this cap, only the first
        ``max_returned_rows`` rows are returned along with ``truncated: true``
        and a guidance message.

        Default: 1000. Set to 0 to disable client-side row capping.
        """
        return max(0, int(os.getenv("CBIOPORTAL_MCP_MAX_ROWS", "1000")))

    @property
    def llmobs_max_output_chars(self) -> int:
        """Maximum characters of tool output attached to Datadog LLMObs spans.

        Outputs exceeding this limit are truncated without running ``json.dumps``
        on the full result payload. Set to 0 to omit output capture completely.

        Default: 16384.
        """
        return max(0, int(os.getenv("CBIOPORTAL_MCP_LLMOBS_MAX_OUTPUT_CHARS", "16384")))


# Global instance placeholders for the singleton pattern
_MCP_CONFIG_INSTANCE = None


def get_mcp_config():
    """
    Gets the singleton instance of McpConfig.
    Instantiates it on the first call.
    """
    global _MCP_CONFIG_INSTANCE
    if _MCP_CONFIG_INSTANCE is None:
        # Instantiate the config object here, ensuring load_dotenv() has likely run
        _MCP_CONFIG_INSTANCE = McpConfig()
    return _MCP_CONFIG_INSTANCE

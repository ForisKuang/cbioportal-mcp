"""Pool the ClickHouse client the vendored mcp-clickhouse package creates.

mcp_clickhouse.mcp_server.create_clickhouse_client() is called fresh on every
tool invocation (execute_query, run_select_query, list_databases,
list_tables), each paying a full clickhouse_connect handshake -- TLS setup
plus a version/timezone round trip -- before the actual query even starts.
This module makes that function return one shared, reused client instead, by
monkey-patching it at runtime rather than editing the installed package.

Two non-obvious things had to be confirmed against the installed versions
before this was safe to ship, not assumed from the package's docs:

1. Patch target. mcp_clickhouse/__init__.py re-exports create_clickhouse_client
   from .mcp_server, but every internal call site (execute_query,
   run_select_query, list_databases, list_tables) resolves the bare name
   against mcp_server.py's own module globals -- not the package-level
   re-export. Patching mcp_clickhouse.create_clickhouse_client (the re-export)
   is a no-op for all of them; verified live that a patch there is never
   called. mcp_clickhouse.mcp_server.create_clickhouse_client is the only
   name any internal call site actually reads.

2. Session concurrency. clickhouse_connect stamps every client with a random
   session_id by default (the 'autogenerate_session_id' common setting, True
   by default) and refuses concurrent queries within one session
   ("Attempt to execute concurrent queries within the same session"). This
   server runs SELECTs from mcp_clickhouse's own multi-worker QUERY_EXECUTOR
   thread pool over HTTP transport, so a single shared, session-bound client
   breaks under real concurrent load -- verified live: 10 concurrent queries
   against one shared client raised ProgrammingError until this setting was
   disabled.
"""

import logging
import threading
import time

import clickhouse_connect.common
import mcp_clickhouse.mcp_server as _ch_mcp_server

logger = logging.getLogger(__name__)

# How long to keep reusing one pooled client before rebuilding it. This is a
# coarse safety net for staleness that clickhouse_connect's own connection
# recycling doesn't cover (e.g. rotated credentials) -- not a retry-on-error
# mechanism. Individual dropped/expired TCP connections are already recycled
# by clickhouse_connect itself via its 'max_connection_age' common setting
# (checked on every request, default 600s), independent of this TTL.
CLIENT_TTL_SECONDS = 30 * 60

_lock = threading.Lock()
_cached_client = None
_cached_at: float | None = None
_original_create_clickhouse_client = None


def _pooled_create_clickhouse_client():
    global _cached_client, _cached_at

    now = time.monotonic()
    if _cached_client is not None and now - _cached_at < CLIENT_TTL_SECONDS:
        return _cached_client

    with _lock:
        # Re-check: another thread may have already rebuilt the client while
        # this one was waiting for the lock.
        now = time.monotonic()
        if _cached_client is not None and now - _cached_at < CLIENT_TTL_SECONDS:
            return _cached_client

        stale_client = _cached_client
        client = _original_create_clickhouse_client()
        _cached_client = client
        _cached_at = now

    if stale_client is not None:
        try:
            stale_client.close()
        except Exception as exc:
            logger.debug("Failed to close expired pooled ClickHouse client: %s", exc)

    return client


def install_pooled_clickhouse_client() -> None:
    """Make every mcp_clickhouse call site reuse one ClickHouse client
    instead of creating (and TLS-handshaking) a new one per call.

    Must run before any tool call. ensure_db_permissions() at server startup
    is already the first thing that triggers client creation, so calling this
    at module import time -- before main() runs -- is early enough to not
    race the first real query.
    """
    global _original_create_clickhouse_client

    if _original_create_clickhouse_client is not None:
        return  # already installed (e.g. module reloaded under test)

    if not hasattr(_ch_mcp_server, "create_clickhouse_client"):
        raise AttributeError(
            "mcp_clickhouse.mcp_server.create_clickhouse_client not found. "
            "The installed mcp-clickhouse version likely renamed or "
            "restructured client creation -- refusing to silently skip "
            "connection pooling; update this patch to match the new "
            "internals before removing this guard."
        )

    _original_create_clickhouse_client = _ch_mcp_server.create_clickhouse_client
    clickhouse_connect.common.set_setting("autogenerate_session_id", False)
    _ch_mcp_server.create_clickhouse_client = _pooled_create_clickhouse_client
    logger.info(f"Pooled ClickHouse client installed (TTL={CLIENT_TTL_SECONDS}s)")

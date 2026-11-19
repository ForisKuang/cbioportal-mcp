"""Long-lived, thread-safe ClickHouse client management and query execution.

Thread-safety design
--------------------
``clickhouse_connect``'s ``HttpClient`` is **not** safe for concurrent queries
with its default ``autogenerate_session_id=True`` because every query reuses the
same ``session_id`` and ``HttpClient._raw_request`` raises ``ProgrammingError``
(and ClickHouse rejects concurrent queries in one session).

When constructed with ``autogenerate_session_id=False`` (the same pattern used
by ``clickhouse_connect.driver.create_async_client``), every HTTP query is
stateless:
  - ``HttpClient.http`` is a thread-safe ``urllib3.PoolManager`` with HTTP
    keep-alive connection pooling.
  - ``NativeTransform.parse_response`` is a stateless ``@staticmethod``.
  - ``HttpClient._query_with_context`` constructs fresh per-call ``headers``,
    ``params``, and ``QueryContext`` objects without mutating client state.

Therefore a single lazily-initialized module-level ``HttpClient`` configured
with ``autogenerate_session_id=False`` safely serves concurrent read-only
queries across worker threads while eliminating per-query client construction
(3 initialization round-trips + new session setup per call).
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.exceptions import (
    InterfaceError,
    OperationalError,
    StreamFailureError,
)
from dotenv import load_dotenv
from fastmcp.exceptions import ToolError
from urllib3.exceptions import ConnectTimeoutError, HTTPError, ReadTimeoutError

from cbioportal_mcp.env import McpConfig, get_mcp_config
from cbioportal_mcp.telemetry import (
    DbQueryContext,
    traced_db_client_setup,
    traced_db_query,
)

logger = logging.getLogger(__name__)

load_dotenv()

_client_lock = threading.Lock()
_client: Any = None
_readonly_settings_warned: set[str] = set()


def _build_client_config(config: McpConfig) -> dict[str, Any]:
    """Build keyword arguments for ``clickhouse_connect.get_client()`` from env vars.

    Mirrors ``mcp_clickhouse.mcp_env.ClickHouseConfig.get_client_config()`` for
    full backward compatibility, while disabling ``autogenerate_session_id`` for
    concurrent thread-safe reuse and defaulting ``send_receive_timeout`` to
    ``config.query_timeout_seconds`` when ``CLICKHOUSE_SEND_RECEIVE_TIMEOUT``
    is not explicitly set.
    """
    missing = [
        var
        for var in ("CLICKHOUSE_HOST", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD")
        if var not in os.environ
    ]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    secure = os.getenv("CLICKHOUSE_SECURE", "true").lower() == "true"
    verify = os.getenv("CLICKHOUSE_VERIFY", "true").lower() == "true"
    default_port = 8443 if secure else 8123
    port = int(os.environ["CLICKHOUSE_PORT"]) if "CLICKHOUSE_PORT" in os.environ else default_port
    connect_timeout = int(os.getenv("CLICKHOUSE_CONNECT_TIMEOUT", "30"))
    send_receive_timeout = int(
        os.getenv("CLICKHOUSE_SEND_RECEIVE_TIMEOUT", str(config.query_timeout_seconds))
    )

    client_config: dict[str, Any] = {
        "host": os.environ["CLICKHOUSE_HOST"],
        "port": port,
        "username": os.environ["CLICKHOUSE_USER"],
        "password": os.environ["CLICKHOUSE_PASSWORD"],
        "secure": secure,
        "verify": verify,
        "connect_timeout": connect_timeout,
        "send_receive_timeout": send_receive_timeout,
        "client_name": "cbioportal_mcp",
        "autogenerate_session_id": False,
    }

    database = os.getenv("CLICKHOUSE_DATABASE")
    if database:
        client_config["database"] = database

    proxy_path = os.getenv("CLICKHOUSE_PROXY_PATH")
    if proxy_path:
        client_config["proxy_path"] = proxy_path

    return client_config


def _create_clickhouse_client(config: McpConfig | None = None) -> Any:
    """Create a new ClickHouse client, timed separately from query execution."""
    cfg = config or get_mcp_config()
    with traced_db_client_setup():
        client_config = _build_client_config(cfg)
        logger.info(
            "Creating ClickHouse client connection to %s:%s as %s "
            "(secure=%s, verify=%s, connect_timeout=%ss, send_receive_timeout=%ss)",
            client_config["host"],
            client_config["port"],
            client_config["username"],
            client_config["secure"],
            client_config["verify"],
            client_config["connect_timeout"],
            client_config["send_receive_timeout"],
        )
        client = clickhouse_connect.get_client(**client_config)
        version = getattr(client, "server_version", "unknown")
        logger.info("Connected to ClickHouse server version %s", version)
        return client


def get_clickhouse_client(config: McpConfig | None = None) -> Any:
    """Return the lazily-initialized, thread-safe module-level ClickHouse client."""
    global _client
    client = _client
    if client is not None:
        return client
    with _client_lock:
        if _client is None:
            _client = _create_clickhouse_client(config=config)
        return _client


def reset_clickhouse_client(failed_client: Any = None) -> None:
    """Close and discard the cached ClickHouse client.

    When ``failed_client`` is provided, only discards the cached client if it is
    still that exact instance, preventing a thread that failed on an old client
    from clobbering a freshly reconnected one.
    """
    global _client
    old_client = None
    with _client_lock:
        if _client is not None and (failed_client is None or _client is failed_client):
            old_client = _client
            _client = None
    if old_client is not None:
        try:
            old_client.close()
        except Exception as exc:
            logger.debug("Ignoring error closing ClickHouse client: %s", exc)


atexit.register(reset_clickhouse_client)


def _get_readonly_setting(client: Any) -> str:
    """Resolve the ``readonly`` setting to send on queries.

    ClickHouse ``readonly`` modes:
      - ``0``: reads, writes, and setting changes allowed
      - ``1``: read-only queries, setting changes forbidden
      - ``2``: read-only queries, per-query settings allowed (except ``readonly``)

    When the server user has ``readonly=0`` (e.g., local dev), we enforce
    ``readonly=2`` rather than ``1`` so queries remain strictly read-only while
    still permitting per-query limits (``max_execution_time``,
    ``max_result_rows``, ``result_overflow_mode``, ``log_comment``) in the same
    HTTP request. When the server already enforces ``readonly=1`` or ``2``, we
    preserve the server's value so ``clickhouse_connect`` does not raise a
    ``Setting readonly is unknown or readonly`` error.
    """
    server_settings = getattr(client, "server_settings", None)
    if isinstance(server_settings, dict):
        read_only = server_settings.get("readonly")
        if read_only is not None:
            raw_val = read_only.value if hasattr(read_only, "value") else str(read_only)
            if str(raw_val) == "0":
                return "2"
            return str(raw_val)
    return "2"


def _is_setting_readonly_on_server(client: Any, key: str) -> bool:
    """Return True if ``client.server_settings`` marks ``key`` as non-writable."""
    server_settings = getattr(client, "server_settings", None)
    if not isinstance(server_settings, dict) or not server_settings:
        return False
    setting_def = server_settings.get(key)
    if setting_def is None:
        return False
    return getattr(setting_def, "readonly", 0) == 1


def _build_query_settings(
    client: Any,
    query_ctx: DbQueryContext,
    config: McpConfig,
) -> dict[str, Any]:
    """Build per-query ClickHouse settings: readonly, server-side limits, and
    trace/span correlation (``query_id`` + ``log_comment``)."""
    settings: dict[str, Any] = {
        "readonly": _get_readonly_setting(client),
        # Transport setting: always permitted even on strict readonly=1 users.
        "query_id": query_ctx.query_id,
    }

    def _add_if_writable(key: str, value: Any) -> None:
        if _is_setting_readonly_on_server(client, key):
            if key not in _readonly_settings_warned:
                _readonly_settings_warned.add(key)
                logger.warning(
                    "ClickHouse server marks setting '%s' readonly; omitting from query settings. "
                    "Grant readonly=2 on the ClickHouse user to enable per-query limits "
                    "and log_comment.",
                    key,
                )
            return
        settings[key] = value

    if config.max_execution_time_seconds > 0:
        _add_if_writable("max_execution_time", config.max_execution_time_seconds)
    if config.max_result_rows > 0:
        _add_if_writable("max_result_rows", config.max_result_rows)
    if config.max_result_bytes > 0:
        _add_if_writable("max_result_bytes", config.max_result_bytes)
    if config.max_result_rows > 0 or config.max_result_bytes > 0:
        _add_if_writable("result_overflow_mode", "break")
    _add_if_writable("log_comment", query_ctx.log_comment)

    return settings


def _iter_exception_chain(err: BaseException):
    seen: set[int] = set()
    cur: BaseException | None = err
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def _is_timeout_error(err: BaseException) -> bool:
    for cur in _iter_exception_chain(err):
        if isinstance(cur, (TimeoutError, ReadTimeoutError, ConnectTimeoutError)):
            return True
        if "timed out" in str(cur).lower():
            return True
    return False


def _is_connection_error(err: BaseException) -> bool:
    """Return True for transport/client errors where reconnecting can help.

    SQL/query errors (plain ``DatabaseError``, ``ProgrammingError``) return False
    so healthy pooled connections are not discarded on user syntax errors.
    """
    for cur in _iter_exception_chain(err):
        if isinstance(
            cur,
            (
                OperationalError,
                InterfaceError,
                StreamFailureError,
                ConnectionError,
                OSError,
                HTTPError,
            ),
        ):
            return True
    return False


def execute_query(query: str, *, query_label: str) -> dict[str, Any]:
    """Execute a read-only ClickHouse query using the reused client.

    Client setup/reconnect is timed outside ``traced_db_query`` (under
    ``db.client.setup`` / ``db_client.setup_duration_ms``) so
    ``db_query.duration_ms`` measures ClickHouse query execution without
    conflating connection setup.

    On a transient connection error (excluding timeouts), discards the broken
    client, reconnects once, and retries the read-only query.
    """
    config = get_mcp_config()

    for attempt in range(2):
        try:
            client = get_clickhouse_client(config=config)
        except Exception as err:
            logger.error("Failed to acquire ClickHouse client for %s: %s", query_label, err)
            raise ToolError(f"Query execution failed: {err}") from err

        try:
            with traced_db_query(query_label) as query_ctx:
                settings = _build_query_settings(client, query_ctx, config)
                res = client.query(query, settings=settings)
                query_ctx.span.set_attribute("db.query.row_count", len(res.result_rows))
            logger.debug(
                "ClickHouse query %s (query_id=%s) returned %d rows",
                query_label,
                query_ctx.query_id,
                len(res.result_rows),
            )
            return {
                "columns": res.column_names,
                "rows": res.result_rows,
                "summary": getattr(res, "summary", None) or {},
            }
        except Exception as err:
            if _is_connection_error(err):
                reset_clickhouse_client(failed_client=client)
                if attempt == 0 and not _is_timeout_error(err):
                    logger.warning(
                        "ClickHouse connection error on %s (%s); reconnecting and retrying once",
                        query_label,
                        err,
                    )
                    continue
            logger.error("Error executing query (%s): %s", query_label, err)
            raise ToolError(f"Query execution failed: {err}") from err

    raise ToolError("Query execution failed after reconnect attempt")

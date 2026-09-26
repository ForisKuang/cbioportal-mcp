"""Reusable ClickHouse clients for SELECT queries.

mcp_clickhouse's execute_query() builds a fresh clickhouse_connect client per
query, and every new client pays init round trips (server version/timezone,
system.settings) before the real query runs. Here each worker thread of a
bounded pool owns one long-lived client instead, so those init queries run
once per worker rather than once per call.

Client creation and the readonly-setting logic are still upstream's, so all
CLICKHOUSE_* env configuration is honored unchanged. The upstream 30-second
caller deadline (queue time included) is preserved.
"""

import atexit
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from clickhouse_connect.driver.exceptions import OperationalError
from fastmcp.exceptions import ToolError
from mcp_clickhouse.mcp_server import create_clickhouse_client, get_readonly_setting
from urllib3.exceptions import HTTPError

logger = logging.getLogger(__name__)

SELECT_QUERY_TIMEOUT_SECS = 30
# Same size as upstream's QUERY_EXECUTOR; bounds the number of open clients.
MAX_WORKERS = 10

# Errors that mean the client's connection is unusable. Anything else (e.g. a
# DatabaseError for bad SQL) leaves the client in place.
_CONNECTION_ERRORS = (OperationalError, HTTPError, ConnectionError, TimeoutError, OSError)

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="clickhouse")
_local = threading.local()
_clients = []
_clients_lock = threading.Lock()


def _get_client():
    client = getattr(_local, "client", None)
    if client is None:
        client = create_clickhouse_client()
        _local.client = client
        with _clients_lock:
            _clients.append(client)
    return client


def _discard_client(client):
    _local.client = None
    with _clients_lock:
        if client in _clients:
            _clients.remove(client)
    try:
        client.close()
    except Exception:
        logger.debug("Failed to close discarded ClickHouse client", exc_info=True)


def _execute_query(query: str) -> dict:
    """Run on the calling worker thread using that thread's client.

    On a connection error the client is discarded so the next query on this
    thread reconnects. The failed query itself is not replayed:
    clickhouse_connect already retries stale keep-alive connections
    internally, and replaying here could double the load of a slow query.
    """
    try:
        client = _get_client()
        try:
            result = client.query(query, settings={"readonly": get_readonly_setting(client)})
        except _CONNECTION_ERRORS:
            _discard_client(client)
            raise
        return {"columns": result.column_names, "rows": result.result_rows}
    except Exception as exc:
        raise ToolError(f"Query execution failed: {exc}") from exc


def execute_query(query: str) -> dict:
    """Drop-in for mcp_clickhouse's execute_query/run_select_query.

    Returns {"columns": [...], "rows": [...]}; raises ToolError on failure or
    after SELECT_QUERY_TIMEOUT_SECS. A timed-out query keeps running on its
    worker (as upstream), and that worker's client stays owned by it.
    """
    future = _executor.submit(_execute_query, query)
    try:
        return future.result(timeout=SELECT_QUERY_TIMEOUT_SECS)
    except TimeoutError as exc:
        future.cancel()
        logger.warning("Query timed out after %s seconds: %s", SELECT_QUERY_TIMEOUT_SECS, query)
        raise ToolError(f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds") from exc


def _shutdown():
    _executor.shutdown(wait=True)
    with _clients_lock:
        clients, _clients[:] = list(_clients), []
    for client in clients:
        try:
            client.close()
        except Exception:
            logger.debug("Failed to close ClickHouse client at shutdown", exc_info=True)


atexit.register(_shutdown)

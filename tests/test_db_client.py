"""Client ownership, recovery, timeout, and telemetry regressions."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError
from fastmcp.exceptions import ToolError

from cbioportal_mcp import db_client, server


@pytest.fixture
def client_factory(monkeypatch):
    factory = Mock(
        side_effect=lambda: Mock(
            server_settings={},
            query=Mock(return_value=SimpleNamespace(column_names=["x"], result_rows=[[1]])),
        )
    )
    monkeypatch.setattr(db_client, "create_clickhouse_client", factory)
    monkeypatch.setattr(db_client, "get_readonly_setting", lambda _: "1")
    monkeypatch.setattr(db_client, "_local", threading.local())
    monkeypatch.setattr(db_client, "_clients", [])
    yield factory
    for client in db_client._clients:
        client.close()


def test_consecutive_queries_reuse_one_client_per_thread(client_factory):
    barrier = threading.Barrier(3)

    def worker():
        barrier.wait(timeout=5)
        for _ in range(5):
            assert db_client._execute_query("SELECT 1")["rows"] == [[1]]
        return db_client._local.client

    with ThreadPoolExecutor(max_workers=3) as executor:
        clients = list(executor.map(lambda _: worker(), range(3)))
    assert client_factory.call_count == 3
    assert len({id(client) for client in clients}) == 3
    for client in clients:
        assert client.query.call_count == 5
        client.query.assert_called_with("SELECT 1", settings={"readonly": "1"})


@pytest.mark.parametrize("error", [OperationalError("disconnected"), ConnectionError("reset")])
def test_connection_failure_recreates_client_on_next_query(client_factory, error):
    db_client._execute_query("SELECT 1")
    old = db_client._local.client
    old.query.side_effect = error
    with pytest.raises(ToolError, match="Query execution failed"):
        db_client._execute_query("SELECT 1")
    old.close.assert_called_once()
    db_client._execute_query("SELECT 1")
    assert client_factory.call_count == 2
    assert db_client._local.client is not old


def test_sql_error_does_not_recreate_client(client_factory):
    db_client._execute_query("SELECT 1")
    db_client._local.client.query.side_effect = DatabaseError("bad SQL")
    with pytest.raises(ToolError, match="bad SQL"):
        db_client._execute_query("invalid")
    assert client_factory.call_count == 1
    db_client._local.client.close.assert_not_called()


def test_timeout_keeps_busy_client_owned_until_query_finishes(client_factory, monkeypatch):
    started, release = threading.Event(), threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(db_client, "_executor", executor)
    monkeypatch.setattr(db_client, "SELECT_QUERY_TIMEOUT_SECS", 0.05)
    client = Mock(server_settings={})

    def query(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return SimpleNamespace(column_names=["x"], result_rows=[[1]])

    client.query.side_effect = query
    client_factory.side_effect = lambda: client
    try:
        with pytest.raises(ToolError, match="Query timed out"):
            db_client.execute_query("SELECT 1")
        assert started.is_set()
        client.close.assert_not_called()
        release.set()
        monkeypatch.setattr(db_client, "SELECT_QUERY_TIMEOUT_SECS", 30)
        assert db_client.execute_query("SELECT 1")["rows"] == [[1]]
        assert client_factory.call_count == 1
    finally:
        release.set()
        executor.shutdown()


def test_select_preserves_query_label_and_result_conversion(monkeypatch):
    labels = []

    @contextmanager
    def traced(label):
        labels.append(label)
        yield

    monkeypatch.setattr(server, "traced_db_query", traced)
    monkeypatch.setattr(server, "execute_query", lambda _: {"columns": ["x"], "rows": [[1]]})
    assert server.run_select_query("SELECT 1", query_label="study_guide.counts") == [{"x": 1}]
    assert labels == ["study_guide.counts"]


def test_disabled_debug_does_not_format_result(monkeypatch):
    class Unformattable(list):
        def __str__(self):
            pytest.fail("result formatted with debug disabled")

    monkeypatch.setattr(server, "run_select_query", lambda *a, **kw: Unformattable())
    monkeypatch.setattr(server.logger, "level", 20)
    assert server.clickhouse_run_select_query.fn("SELECT 1") == {"rows": []}


def test_public_execute_query_reuses_worker_clients(client_factory, monkeypatch):
    executor = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(db_client, "_executor", executor)
    try:
        for _ in range(20):
            assert db_client.execute_query("SELECT 1") == {"columns": ["x"], "rows": [[1]]}
    finally:
        executor.shutdown()
    # At most one client per worker thread, never one per query.
    assert 1 <= client_factory.call_count <= 2
    assert len(db_client._clients) == client_factory.call_count


def test_connect_failure_is_tool_error_and_retried_next_call(client_factory):
    good = client_factory.side_effect
    client_factory.side_effect = OperationalError("connection refused")
    with pytest.raises(ToolError, match="connection refused"):
        db_client._execute_query("SELECT 1")
    client_factory.side_effect = good
    assert db_client._execute_query("SELECT 1")["rows"] == [[1]]
    assert client_factory.call_count == 2

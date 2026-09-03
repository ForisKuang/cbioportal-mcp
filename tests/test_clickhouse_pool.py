import concurrent.futures
import threading

import mcp_clickhouse.mcp_server as ch_mcp_server
import pytest

from cbioportal_mcp import clickhouse_pool


@pytest.fixture(autouse=True)
def _reset_pool_state(monkeypatch):
    """Undo module-level pool state between tests, including the real patch
    applied once at cbioportal_mcp.server import time."""
    original = clickhouse_pool._original_create_clickhouse_client
    if original is not None:
        monkeypatch.setattr(ch_mcp_server, "create_clickhouse_client", original)
    monkeypatch.setattr(clickhouse_pool, "_original_create_clickhouse_client", None)
    monkeypatch.setattr(clickhouse_pool, "_cached_client", None)
    monkeypatch.setattr(clickhouse_pool, "_cached_at", None)
    yield


def test_install_patches_mcp_server_module_not_package_reexport():
    calls = []
    fake_client = object()

    def fake_create():
        calls.append(1)
        return fake_client

    ch_mcp_server.create_clickhouse_client = fake_create
    try:
        clickhouse_pool.install_pooled_clickhouse_client()

        # The vendored package's own internal call sites resolve the bare
        # name against mcp_server.py's globals -- patching only the
        # package-level re-export (mcp_clickhouse.create_clickhouse_client)
        # would leave those call sites untouched.
        assert (
            ch_mcp_server.create_clickhouse_client
            is clickhouse_pool._pooled_create_clickhouse_client
        )

        client = ch_mcp_server.create_clickhouse_client()
        assert client is fake_client
        assert calls == [1]
    finally:
        ch_mcp_server.create_clickhouse_client = fake_create


def test_pooled_client_is_reused_across_calls(monkeypatch):
    calls = []

    def fake_create():
        calls.append(1)
        return object()

    ch_mcp_server.create_clickhouse_client = fake_create
    clickhouse_pool.install_pooled_clickhouse_client()

    first = ch_mcp_server.create_clickhouse_client()
    second = ch_mcp_server.create_clickhouse_client()
    third = ch_mcp_server.create_clickhouse_client()

    assert first is second is third
    assert len(calls) == 1


def test_pooled_client_is_rebuilt_after_ttl_expires(monkeypatch):
    created = [object(), object()]
    calls = []

    def fake_create():
        calls.append(1)
        return created[len(calls) - 1]

    ch_mcp_server.create_clickhouse_client = fake_create
    clickhouse_pool.install_pooled_clickhouse_client()

    fake_time = [1000.0]
    monkeypatch.setattr(clickhouse_pool.time, "monotonic", lambda: fake_time[0])

    first = ch_mcp_server.create_clickhouse_client()
    fake_time[0] += clickhouse_pool.CLIENT_TTL_SECONDS + 1
    second = ch_mcp_server.create_clickhouse_client()

    assert first is not second
    assert len(calls) == 2


def test_concurrent_calls_create_client_only_once(monkeypatch):
    calls = []
    create_lock = threading.Lock()

    def fake_create():
        with create_lock:
            calls.append(1)
        return object()

    ch_mcp_server.create_clickhouse_client = fake_create
    clickhouse_pool.install_pooled_clickhouse_client()

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(lambda _: ch_mcp_server.create_clickhouse_client(), range(20)))

    assert len(calls) == 1
    assert len(set(id(r) for r in results)) == 1


def test_install_sets_autogenerate_session_id_false(monkeypatch):
    import clickhouse_connect.common as ch_common

    ch_common.set_setting("autogenerate_session_id", True)
    ch_mcp_server.create_clickhouse_client = lambda: object()

    clickhouse_pool.install_pooled_clickhouse_client()

    assert ch_common.get_setting("autogenerate_session_id") is False


def test_install_is_idempotent(monkeypatch):
    calls = []

    def fake_create():
        calls.append(1)
        return object()

    ch_mcp_server.create_clickhouse_client = fake_create
    clickhouse_pool.install_pooled_clickhouse_client()
    patched_after_first_install = ch_mcp_server.create_clickhouse_client

    # A second install call should not re-wrap the already-pooled function as
    # "original", which would otherwise nest wrappers on repeated imports.
    clickhouse_pool.install_pooled_clickhouse_client()

    assert ch_mcp_server.create_clickhouse_client is patched_after_first_install


def test_install_raises_if_upstream_removed_create_clickhouse_client(monkeypatch):
    monkeypatch.delattr(ch_mcp_server, "create_clickhouse_client")

    with pytest.raises(AttributeError):
        clickhouse_pool.install_pooled_clickhouse_client()


def test_stale_client_is_closed_on_ttl_rebuild(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    clients = [FakeClient(), FakeClient()]
    calls = []

    def fake_create():
        calls.append(1)
        return clients[len(calls) - 1]

    ch_mcp_server.create_clickhouse_client = fake_create
    clickhouse_pool.install_pooled_clickhouse_client()

    fake_time = [1000.0]
    monkeypatch.setattr(clickhouse_pool.time, "monotonic", lambda: fake_time[0])

    first = ch_mcp_server.create_clickhouse_client()
    fake_time[0] += clickhouse_pool.CLIENT_TTL_SECONDS + 1
    ch_mcp_server.create_clickhouse_client()

    assert first.closed is True

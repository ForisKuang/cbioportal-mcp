"""Tests for ClickHouse query concurrency, client reuse, server-side limits,
row capping, lazy logging/LLMObs capping, trace-correlated log_comment, and
client-setup vs query-execution metric separation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from urllib3.exceptions import ReadTimeoutError

from cbioportal_mcp import clickhouse, server, telemetry


def _fake_result(columns, rows, summary=None):
    return SimpleNamespace(
        column_names=list(columns),
        result_rows=list(rows),
        summary=summary or {},
    )


def _setting_def(name: str, value: str, readonly: int = 0):
    return SimpleNamespace(name=name, value=value, readonly=readonly)


@pytest.fixture(autouse=True)
def _clean_clickhouse_client(monkeypatch):
    """Ensure every test starts and ends with a clean ClickHouse client singleton."""
    clickhouse.reset_clickhouse_client()
    clickhouse._readonly_settings_warned.clear()
    monkeypatch.setenv("CLICKHOUSE_HOST", "ch.example.internal")
    monkeypatch.setenv("CLICKHOUSE_USER", "app_user")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "secret")
    monkeypatch.setenv("CLICKHOUSE_DATABASE", "cgds_test")
    yield
    clickhouse.reset_clickhouse_client()
    clickhouse._readonly_settings_warned.clear()


# ---------------------------------------------------------------------------
# A. Non-blocking concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_clickhouse_run_select_query_calls_do_not_serialize(monkeypatch):
    """Two concurrent slow clickhouse_run_select_query calls must execute in
    parallel off the event loop, not serialize."""
    barrier = threading.Barrier(2, timeout=2.0)

    class SlowFakeClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            # If the two calls serialized on the event loop, the first would
            # block here until the barrier timed out and raised BrokenBarrierError.
            barrier.wait()
            time.sleep(0.15)
            return _fake_result(["q"], [(query,)])

        def close(self):
            pass

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", lambda **kw: SlowFakeClient())

    started = time.perf_counter()
    res1, res2 = await asyncio.gather(
        server.clickhouse_run_select_query.fn("SELECT 1"),
        server.clickhouse_run_select_query.fn("SELECT 2"),
    )
    elapsed = time.perf_counter() - started

    assert res1 == {"rows": [{"q": "SELECT 1"}]}
    assert res2 == {"rows": [{"q": "SELECT 2"}]}
    # Two 0.15s calls in parallel finish well under 2 * 0.15s = 0.30s (allow CI headroom)
    assert elapsed < 0.28, f"Expected parallel execution, took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_fastmcp_function_tool_run_executes_concurrently(monkeypatch):
    """Verify through FastMCP's FunctionTool.run() entrypoint that concurrent
    invocations run in parallel and return valid structured output."""
    barrier = threading.Barrier(2, timeout=2.0)

    class FakeClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            barrier.wait()
            return _fake_result(["val"], [(1,)])

        def close(self):
            pass

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", lambda **kw: FakeClient())

    r1, r2 = await asyncio.gather(
        server.clickhouse_run_select_query.run({"query": "SELECT 1"}),
        server.clickhouse_run_select_query.run({"query": "SELECT 2"}),
    )

    assert r1.structured_content == {"rows": [{"val": 1}]}
    assert r2.structured_content == {"rows": [{"val": 1}]}


@pytest.mark.asyncio
async def test_concurrent_schema_tools_do_not_serialize(monkeypatch):
    """clickhouse_list_tables and clickhouse_list_table_columns also run off
    the event loop and do not block each other."""
    barrier = threading.Barrier(2, timeout=2.0)

    class FakeClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            barrier.wait()
            if query == "SHOW TABLES":
                return _fake_result(["name"], [("cancer_study",)])
            return _fake_result(
                ["name", "type", "default_type", "default_expression", "comment"],
                [("cancer_study_id", "Int32", "", "", "Study PK")],
            )

        def close(self):
            pass

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", lambda **kw: FakeClient())

    tables_res, cols_res = await asyncio.gather(
        server.clickhouse_list_tables.fn(),
        server.clickhouse_list_table_columns.fn("cancer_study"),
    )

    assert tables_res == {"tables": [{"name": "cancer_study"}]}
    assert cols_res == {
        "columns": [{"name": "cancer_study_id", "type": "Int32", "comment": "Study PK"}]
    }


@pytest.mark.asyncio
async def test_client_side_query_timeout_returns_error_message(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_QUERY_TIMEOUT", "1")
    unblock = threading.Event()

    class HangingClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            unblock.wait(timeout=5.0)
            return _fake_result(["x"], [(1,)])

        def close(self):
            pass

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", lambda **kw: HangingClient())

    try:
        result = await server.clickhouse_run_select_query.fn("SELECT sleep(10)")
        assert "error_message" in result
        assert "timed out after 1 seconds" in result["error_message"]
    finally:
        unblock.set()


# ---------------------------------------------------------------------------
# B. Long-lived ClickHouse client reuse & reconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_constructed_once_across_many_sequential_and_concurrent_calls(monkeypatch):
    """clickhouse_connect.get_client must be called once across many queries,
    with autogenerate_session_id=False for thread-safe reuse."""
    created_configs = []

    class FakeClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            return _fake_result(["n"], [(1,)])

        def close(self):
            pass

    def fake_get_client(**kwargs):
        created_configs.append(kwargs)
        return FakeClient()

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", fake_get_client)

    # Fire 10 concurrent calls first (tests thread-safe double-checked init),
    # followed by 10 sequential calls.
    concurrent_results = await asyncio.gather(
        *(server.clickhouse_run_select_query.fn(f"SELECT {i}") for i in range(10))
    )
    for _ in range(10):
        await server.clickhouse_run_select_query.fn("SELECT 99")

    assert len(concurrent_results) == 10
    assert len(created_configs) == 1, f"Expected 1 client creation, got {len(created_configs)}"
    assert created_configs[0]["autogenerate_session_id"] is False
    assert created_configs[0]["send_receive_timeout"] == 30


@pytest.mark.asyncio
async def test_reconnects_and_retries_once_on_transient_connection_error(monkeypatch):
    """A broken connection triggers client reset, reconnect, and one retry."""
    clients_created = []
    closed_clients = []

    class FlakyClient:
        server_version = "24.8.1"
        server_settings = {}

        def __init__(self, idx: int):
            self.idx = idx

        def query(self, query, settings=None):
            if self.idx == 0:
                raise OperationalError("Connection reset by peer")
            return _fake_result(["ok"], [(self.idx,)])

        def close(self):
            closed_clients.append(self.idx)

    def fake_get_client(**kwargs):
        client = FlakyClient(len(clients_created))
        clients_created.append(client)
        return client

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", fake_get_client)

    result = await server.clickhouse_run_select_query.fn("SELECT 1")

    assert result == {"rows": [{"ok": 1}]}
    assert len(clients_created) == 2
    assert closed_clients == [0]


@pytest.mark.asyncio
async def test_does_not_reconnect_or_retry_on_sql_database_error(monkeypatch):
    """A SQL error (plain DatabaseError) must keep the healthy pooled client."""
    clients_created = []
    query_calls = 0
    close_calls = 0

    class FakeClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            nonlocal query_calls
            query_calls += 1
            raise DatabaseError("Unknown identifier 'bad_col'")

        def close(self):
            nonlocal close_calls
            close_calls += 1

    def fake_get_client(**kwargs):
        c = FakeClient()
        clients_created.append(c)
        return c

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", fake_get_client)

    result = await server.clickhouse_run_select_query.fn("SELECT bad_col")

    assert "error_message" in result
    assert "Unknown identifier" in result["error_message"]
    assert len(clients_created) == 1
    assert query_calls == 1
    assert close_calls == 0


@pytest.mark.asyncio
async def test_timeout_resets_client_without_retrying_slow_query(monkeypatch):
    """A socket read timeout discards the potentially wedged client without
    re-running the slow query."""
    clients_created = []
    closed_clients = []
    query_calls = 0

    class TimeoutClient:
        server_version = "24.8.1"
        server_settings = {}

        def __init__(self, idx: int):
            self.idx = idx

        def query(self, query, settings=None):
            nonlocal query_calls
            query_calls += 1
            if self.idx == 0:
                raise OperationalError("Read timed out") from ReadTimeoutError(
                    None, "https://ch", "Read timed out."
                )
            return _fake_result(["ok"], [(1,)])

        def close(self):
            closed_clients.append(self.idx)

    def fake_get_client(**kwargs):
        c = TimeoutClient(len(clients_created))
        clients_created.append(c)
        return c

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", fake_get_client)

    res1 = await server.clickhouse_run_select_query.fn("SELECT heavy()")
    assert "error_message" in res1
    assert query_calls == 1  # did NOT retry
    assert closed_clients == [0]  # discarded stale client

    # Subsequent query cleanly reconnects
    res2 = await server.clickhouse_run_select_query.fn("SELECT 1")
    assert res2 == {"rows": [{"ok": 1}]}
    assert len(clients_created) == 2


def test_reset_clickhouse_client_cas_does_not_clobber_newer_client():
    c1 = Mock(name="old_client")
    c2 = Mock(name="new_client")
    clickhouse._client = c2

    clickhouse.reset_clickhouse_client(failed_client=c1)

    assert clickhouse._client is c2
    c2.close.assert_not_called()


# ---------------------------------------------------------------------------
# C. Server-side limits & readonly setting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_side_limits_and_readonly_passed_by_default(monkeypatch):
    captured_settings = []

    class FakeClient:
        server_version = "24.8.1"
        server_settings = {"readonly": _setting_def("readonly", "0", readonly=0)}

        def query(self, query, settings=None):
            captured_settings.append(dict(settings or {}))
            return _fake_result(["x"], [(1,)])

        def close(self):
            pass

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", lambda **kw: FakeClient())

    res = await server.clickhouse_run_select_query.fn("SELECT 1")
    assert res == {"rows": [{"x": 1}]}

    assert len(captured_settings) == 1
    s = captured_settings[0]
    assert s["readonly"] == "2"
    assert s["max_execution_time"] == 25
    assert s["max_result_rows"] == 10000
    assert s["max_result_bytes"] == 50_000_000
    assert s["result_overflow_mode"] == "break"
    assert s["query_id"]
    assert s["log_comment"]


@pytest.mark.asyncio
async def test_server_side_limits_configurable_via_env_vars(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_MAX_EXECUTION_TIME", "12")
    monkeypatch.setenv("CBIOPORTAL_MCP_MAX_RESULT_ROWS", "2500")
    monkeypatch.setenv("CBIOPORTAL_MCP_MAX_RESULT_BYTES", "1048576")
    monkeypatch.setenv("CBIOPORTAL_MCP_QUERY_TIMEOUT", "18")

    captured_client_kwargs = {}
    captured_settings = []

    class FakeClient:
        server_version = "24.8.1"
        server_settings = {"readonly": _setting_def("readonly", "2", readonly=1)}

        def query(self, query, settings=None):
            captured_settings.append(dict(settings or {}))
            return _fake_result(["x"], [(1,)])

        def close(self):
            pass

    def fake_get_client(**kwargs):
        captured_client_kwargs.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", fake_get_client)

    await server.clickhouse_run_select_query.fn("SELECT 1")

    assert captured_client_kwargs["send_receive_timeout"] == 18
    s = captured_settings[0]
    assert s["readonly"] == "2"
    assert s["max_execution_time"] == 12
    assert s["max_result_rows"] == 2500
    assert s["max_result_bytes"] == 1048576
    assert s["result_overflow_mode"] == "break"


@pytest.mark.asyncio
async def test_strict_readonly_1_server_omits_non_writable_settings_but_keeps_query_id(monkeypatch):
    captured_settings = []

    class StrictReadonlyClient:
        server_version = "24.8.1"
        server_settings = {
            "readonly": _setting_def("readonly", "1", readonly=1),
            "max_execution_time": _setting_def("max_execution_time", "0", readonly=1),
            "max_result_rows": _setting_def("max_result_rows", "0", readonly=1),
            "max_result_bytes": _setting_def("max_result_bytes", "0", readonly=1),
            "result_overflow_mode": _setting_def("result_overflow_mode", "throw", readonly=1),
            "log_comment": _setting_def("log_comment", "", readonly=1),
        }

        def query(self, query, settings=None):
            captured_settings.append(dict(settings or {}))
            return _fake_result(["x"], [(1,)])

        def close(self):
            pass

    monkeypatch.setattr(
        clickhouse.clickhouse_connect, "get_client", lambda **kw: StrictReadonlyClient()
    )

    res = await server.clickhouse_run_select_query.fn("SELECT 1")
    assert res == {"rows": [{"x": 1}]}

    s = captured_settings[0]
    assert s["readonly"] == "1"
    assert "query_id" in s
    assert "max_execution_time" not in s
    assert "log_comment" not in s


# ---------------------------------------------------------------------------
# D. LLM row capping & truncation indicator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_cap_does_not_add_truncated_flag_when_within_limit(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_MAX_ROWS", "5")

    class FakeClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            return _fake_result(["id"], [(i,) for i in range(5)])

        def close(self):
            pass

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", lambda **kw: FakeClient())

    result = await server.clickhouse_run_select_query.fn("SELECT id FROM t LIMIT 5")
    assert result == {"rows": [{"id": i} for i in range(5)]}
    assert "truncated" not in result
    assert "message" not in result


@pytest.mark.asyncio
async def test_row_cap_truncates_and_sets_truncated_flag_and_message(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_MAX_ROWS", "5")

    class FakeClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            return _fake_result(["id"], [(i,) for i in range(12)])

        def close(self):
            pass

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", lambda **kw: FakeClient())

    result = await server.clickhouse_run_select_query.fn("SELECT id FROM t")
    assert len(result["rows"]) == 5
    assert result["rows"] == [{"id": i} for i in range(5)]
    assert result["truncated"] is True
    assert "truncated" in result["message"].lower()
    assert "5" in result["message"]


@pytest.mark.asyncio
async def test_row_cap_zero_disables_client_side_truncation(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_MAX_ROWS", "0")

    class FakeClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            return _fake_result(["id"], [(i,) for i in range(25)])

        def close(self):
            pass

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", lambda **kw: FakeClient())

    result = await server.clickhouse_run_select_query.fn("SELECT id FROM t")
    assert len(result["rows"]) == 25
    assert "truncated" not in result


@pytest.mark.asyncio
async def test_byte_overflow_summary_sets_truncated_flag(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_MAX_ROWS", "1000")
    monkeypatch.setenv("CBIOPORTAL_MCP_MAX_RESULT_BYTES", "1000")

    class FakeClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            return _fake_result(
                ["blob"],
                [("x" * 600,), ("y" * 600,)],
                summary={"result_bytes": "1200"},
            )

        def close(self):
            pass

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", lambda **kw: FakeClient())

    result = await server.clickhouse_run_select_query.fn("SELECT blob FROM t")
    assert len(result["rows"]) == 2
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# E. No eager debug formatting & LLMObs payload capping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_eager_debug_formatting_of_query_result_at_info_level(monkeypatch):
    """At INFO log level, clickhouse_run_select_query must never eagerly format
    returned row objects via f-strings."""

    class ExplodingRepr:
        def __repr__(self):
            raise AssertionError("__repr__ should not be called by logger.debug at INFO")

        def __str__(self):
            raise AssertionError("__str__ should not be called by logger.debug at INFO")

    class FakeClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            return _fake_result(["obj"], [(ExplodingRepr(),)])

        def close(self):
            pass

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", lambda **kw: FakeClient())

    prev_level = server.logger.level
    server.logger.setLevel(logging.INFO)
    try:
        result = await server.clickhouse_run_select_query.fn("SELECT 1")
    finally:
        server.logger.setLevel(prev_level)

    assert len(result["rows"]) == 1
    assert isinstance(result["rows"][0]["obj"], ExplodingRepr)


def test_llmobs_finish_caps_large_text_output_without_json_dumps(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_LLMOBS_MAX_OUTPUT_CHARS", "100")

    class LargeBlock:
        text = "A" * 5000

        def model_dump(self):
            raise AssertionError("model_dump should not be called on oversized text payload")

    fake_result = SimpleNamespace(content=[LargeBlock()])
    fake_span = Mock()
    annotated = {}

    with (
        patch("ddtrace.llmobs.LLMObs.annotate", side_effect=lambda **kw: annotated.update(kw)),
        patch(
            "cbioportal_mcp.telemetry.json.dumps",
            side_effect=AssertionError("json.dumps should not run on oversized text payload"),
        ),
    ):
        telemetry._llmobs_finish(fake_span, fake_result, error=False)

    assert annotated["span"] is fake_span
    assert annotated["output_data"].startswith("A" * 100)
    assert "truncated 5000 chars" in annotated["output_data"]
    fake_span.finish.assert_called_once()


def test_llmobs_finish_skips_output_when_max_chars_is_zero(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_LLMOBS_MAX_OUTPUT_CHARS", "0")

    class ExplodingBlock:
        def model_dump(self):
            raise AssertionError("should not serialize when max_output_chars=0")

    fake_result = SimpleNamespace(content=[ExplodingBlock()])
    fake_span = Mock()
    annotated = {}

    with patch("ddtrace.llmobs.LLMObs.annotate", side_effect=lambda **kw: annotated.update(kw)):
        telemetry._llmobs_finish(fake_span, fake_result, error=False)

    assert annotated["output_data"] == "[output omitted]"
    fake_span.finish.assert_called_once()


def test_llmobs_finish_serializes_small_payload_normally(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_LLMOBS_MAX_OUTPUT_CHARS", "16384")

    class SmallBlock:
        text = '{"rows":[{"x":1}]}'

        def model_dump(self):
            return {"type": "text", "text": self.text}

    fake_result = SimpleNamespace(content=[SmallBlock()])
    fake_span = Mock()
    annotated = {}

    with patch("ddtrace.llmobs.LLMObs.annotate", side_effect=lambda **kw: annotated.update(kw)):
        telemetry._llmobs_finish(fake_span, fake_result, error=False)

    parsed = json.loads(annotated["output_data"])
    assert parsed == [{"type": "text", "text": '{"rows":[{"x":1}]}'}]
    fake_span.finish.assert_called_once()


# ---------------------------------------------------------------------------
# F. log_comment & query_id correlation with OTel spans/traces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_comment_and_query_id_correlate_to_tool_and_db_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)

    captured_settings = []

    class FakeClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            captured_settings.append(dict(settings or {}))
            return _fake_result(["x"], [(1,)])

        def close(self):
            pass

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", lambda **kw: FakeClient())

    middleware = telemetry.TelemetryMiddleware()
    context = SimpleNamespace(
        message=SimpleNamespace(
            name="clickhouse_run_select_query", arguments={"query": "SELECT 1"}
        ),
        fastmcp_context=None,
    )

    async def call_next(_ctx):
        return await server.clickhouse_run_select_query.fn("SELECT 1")

    with (
        patch("cbioportal_mcp.telemetry.trace.get_tracer", return_value=tracer),
        patch(
            "cbioportal_mcp.telemetry._resolve_caller_identity",
            return_value=(None, None, "direct"),
        ),
        patch("cbioportal_mcp.telemetry._extract_mcp_client_info", return_value=(None, None)),
        patch("cbioportal_mcp.telemetry._extract_session_id", return_value=None),
        patch("cbioportal_mcp.telemetry._llmobs_tool_span", return_value=None),
    ):
        middleware._tracer = tracer
        res = await middleware.on_call_tool(context, call_next)

    assert res == {"rows": [{"x": 1}]}

    spans = exporter.get_finished_spans()
    tool_span = next(s for s in spans if s.name == "mcp.tool/clickhouse_run_select_query")
    db_span = next(s for s in spans if s.name == "db.query/clickhouse_run_select_query")

    assert db_span.context.trace_id == tool_span.context.trace_id
    assert db_span.parent.span_id == tool_span.context.span_id

    settings = captured_settings[0]
    comment = json.loads(settings["log_comment"])
    assert comment["query_label"] == "clickhouse_run_select_query"
    assert comment["tool"] == "clickhouse_run_select_query"
    assert comment["trace_id"] == format(tool_span.context.trace_id, "032x")
    assert comment["span_id"] == format(db_span.context.span_id, "016x")
    assert comment["query_id"] == settings["query_id"]
    assert db_span.attributes["db.clickhouse.query_id"] == settings["query_id"]
    assert db_span.attributes["db.query.label"] == "clickhouse_run_select_query"


@pytest.mark.asyncio
async def test_get_study_guide_parallel_queries_inherit_parent_trace_in_log_comment(monkeypatch):
    """All 7 dynamic study-guide queries (including the 6 parallel sections) must
    inherit the active OTel trace and tag their distinct query_label in log_comment."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)

    seen_comments = []
    lock = threading.Lock()

    class FakeClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            with lock:
                seen_comments.append(json.loads(settings["log_comment"]))
            if "from cancer_study" in query.lower():
                return _fake_result(
                    ["cancer_study_identifier", "name", "description", "type_of_cancer_id"],
                    [("nbl_test_2026", "Test NBL", "Desc", "nbl")],
                )
            return _fake_result(["x"], [])

        def close(self):
            pass

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", lambda **kw: FakeClient())

    with patch("cbioportal_mcp.telemetry.trace.get_tracer", return_value=tracer):
        with tracer.start_as_current_span("mcp.tool/get_study_guide") as parent_span:
            tool_tok = telemetry._current_tool_name.set("get_study_guide")
            try:
                out = await server.get_study_guide.fn("nbl_test_2026")
            finally:
                telemetry._current_tool_name.reset(tool_tok)

    assert "Test NBL" in out
    expected_trace = format(parent_span.get_span_context().trace_id, "032x")
    assert len(seen_comments) == 7
    assert {c["query_label"] for c in seen_comments} == {
        "study_guide.study_info",
        "study_guide.counts",
        "study_guide.profiles",
        "study_guide.panels",
        "study_guide.attrs",
        "study_guide.top_genes",
        "study_guide.sample_types",
    }
    for c in seen_comments:
        assert c["trace_id"] == expected_trace
        assert c["tool"] == "get_study_guide"


# ---------------------------------------------------------------------------
# G. Separate client setup duration from db_query.duration_ms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_query_duration_excludes_client_setup_time(monkeypatch):
    """Client creation latency must be recorded under db_client.setup_duration_ms
    and NOT conflated into db_query.duration_ms."""
    distributions: list[tuple[str, float, dict]] = []

    class FakeDogStatsD:
        def increment(self, metric, tags):
            pass

        def distribution(self, metric, value, tags):
            distributions.append((metric, value, tags))

    class FastQueryClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            time.sleep(0.01)
            return _fake_result(["x"], [(1,)])

        def close(self):
            pass

    def slow_get_client(**kwargs):
        time.sleep(0.15)
        return FastQueryClient()

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", slow_get_client)

    with patch("cbioportal_mcp.telemetry._get_dogstatsd_client", return_value=FakeDogStatsD()):
        await server.clickhouse_run_select_query.fn("SELECT 1")
        await server.clickhouse_run_select_query.fn("SELECT 2")

    setup_metrics = [m for m in distributions if m[0] == "db_client.setup_duration_ms"]
    query_metrics = [m for m in distributions if m[0] == "db_query.duration_ms"]

    # Client setup ran once (cold start only) and captured the ~150ms delay.
    assert len(setup_metrics) == 1
    assert setup_metrics[0][1] >= 120.0
    assert setup_metrics[0][2] == {"success": "true"}

    # Both queries recorded ~10ms execution time, NOT 160ms on the first call.
    assert len(query_metrics) == 2
    for _metric, duration_ms, tags in query_metrics:
        assert duration_ms < 80.0, f"db_query.duration_ms conflated setup time: {duration_ms}ms"
        assert tags == {"query_label": "clickhouse_run_select_query", "success": "true"}


# ---------------------------------------------------------------------------
# Public MCP tool signatures unchanged
# ---------------------------------------------------------------------------


def test_public_mcp_tool_names_and_parameter_schemas_unchanged():
    assert server.clickhouse_run_select_query.name == "clickhouse_run_select_query"
    assert set(server.clickhouse_run_select_query.parameters["properties"].keys()) == {"query"}
    assert server.clickhouse_run_select_query.parameters["required"] == ["query"]

    assert server.clickhouse_list_tables.name == "clickhouse_list_tables"
    assert server.clickhouse_list_tables.parameters["properties"] == {}

    assert server.clickhouse_list_table_columns.name == "clickhouse_list_table_columns"
    assert set(server.clickhouse_list_table_columns.parameters["properties"].keys()) == {"table"}
    assert server.clickhouse_list_table_columns.parameters["required"] == ["table"]

    assert server.get_study_guide.name == "get_study_guide"
    assert set(server.get_study_guide.parameters["properties"].keys()) == {"study_id"}

    assert server.list_studies.name == "list_studies"
    assert set(server.list_studies.parameters["properties"].keys()) == {
        "search",
        "limit",
        "verbose",
    }


def test_ensure_db_permissions_reuses_single_client_and_tags_queries(monkeypatch):
    from cbioportal_mcp.authentication.permissions import ensure_db_permissions
    from cbioportal_mcp.env import McpConfig

    created = 0
    seen_labels = []

    class FakeClient:
        server_version = "24.8.1"
        server_settings = {}

        def query(self, query, settings=None):
            comment = json.loads(settings["log_comment"])
            seen_labels.append(comment["query_label"])
            if "CHECK GRANT SELECT ON" in query:
                return _fake_result(["result"], [(1,)])
            return _fake_result(["result"], [(0,)])

        def close(self):
            pass

    def fake_get_client(**kwargs):
        nonlocal created
        created += 1
        return FakeClient()

    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", fake_get_client)

    ensure_db_permissions(config=McpConfig())

    assert created == 1
    assert len(seen_labels) >= 10
    assert set(seen_labels) == {"permissions.check_grant"}

"""Schema and dynamic-guide cache behavior through the actual tools."""

from unittest.mock import Mock

import pytest

from cbioportal_mcp import metadata_cache, server


@pytest.fixture(autouse=True)
def cache_clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(metadata_cache.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(metadata_cache, "METADATA_CACHE_TTL_SECONDS", 3600)
    server._clear_schema_cache()
    server._clear_study_guide_cache()
    yield now
    server._clear_schema_cache()
    server._clear_study_guide_cache()


@pytest.mark.parametrize("table", [None, "sample"])
def test_schema_hit_expiry_errors_and_clear(monkeypatch, cache_clock, table):
    query = Mock(return_value={"columns": ["name", "type"], "rows": [["sample", "String"]]})
    monkeypatch.setattr(server, "execute_query", query)
    call = (
        (lambda: server.clickhouse_list_tables.fn())
        if table is None
        else (lambda: server.clickhouse_list_table_columns.fn(table))
    )
    first = call()
    assert call() == first
    assert query.call_count == 1
    # Returned mutable objects cannot poison the stored response.
    first[next(iter(first))].clear()
    assert call()[next(iter(first))]
    cache_clock[0] += 3600
    call()
    assert query.call_count == 2
    server._clear_schema_cache()
    query.side_effect = RuntimeError("offline")
    assert "error_message" in call()
    query.side_effect = None
    assert "error_message" not in call()
    assert query.call_count == 4


def test_columns_keyed_by_table(monkeypatch):
    query = Mock(return_value={"columns": [], "rows": []})
    monkeypatch.setattr(server, "execute_query", query)
    for table in ["sample", "patient", "sample"]:
        server.clickhouse_list_table_columns.fn(table)
    assert query.call_count == 2


def test_dynamic_guide_hit_expiry_keys_and_clear(monkeypatch, cache_clock):
    monkeypatch.setattr(server, "_load_study_guide", lambda _: None)

    def result(query, *, query_label):
        return [{"name": "Test"}] if query_label == "study_guide.study_info" else []

    query = Mock(side_effect=result)
    monkeypatch.setattr(server, "run_select_query", query)
    first = server.get_study_guide.fn("alpha")
    assert server.get_study_guide.fn("ALPHA") == first
    assert query.call_count == 7
    server.get_study_guide.fn("beta")
    assert query.call_count == 14
    cache_clock[0] += 3600
    server.get_study_guide.fn("alpha")
    assert query.call_count == 21
    server._clear_study_guide_cache()
    server.get_study_guide.fn("alpha")
    assert query.call_count == 28


@pytest.mark.parametrize("failing_label", ["study_guide.study_info", "study_guide.panels"])
def test_dynamic_guide_errors_not_cached(monkeypatch, failing_label):
    monkeypatch.setattr(server, "_load_study_guide", lambda _: None)
    fail = [True]

    def result(query, *, query_label):
        if fail[0] and query_label == failing_label:
            raise RuntimeError("offline")
        return [{"name": "Test"}] if query_label == "study_guide.study_info" else []

    monkeypatch.setattr(server, "run_select_query", result)
    assert server.get_study_guide.fn("alpha").startswith("Error generating")
    fail[0] = False
    assert server.get_study_guide.fn("alpha").startswith("# Study Guide")


def test_cache_can_be_disabled(monkeypatch):
    monkeypatch.setattr(metadata_cache, "METADATA_CACHE_TTL_SECONDS", 0)
    cache = metadata_cache.MetadataCache()
    cache.put("key", [])
    assert cache.get("key") is None


@pytest.mark.parametrize(
    "raw, expected", [(None, 3600.0), ("120", 120.0), ("0", 0.0), ("bogus", 3600.0)]
)
def test_ttl_env_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("CBIOPORTAL_MCP_METADATA_CACHE_TTL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("CBIOPORTAL_MCP_METADATA_CACHE_TTL_SECONDS", raw)
    assert metadata_cache._ttl_from_env() == expected

from cbioportal_mcp import server


def _rows(n):
    return [{"i": i} for i in range(n)]


def test_select_query_under_default_limit_is_not_truncated(monkeypatch):
    monkeypatch.setattr(server, "run_select_query", lambda query, query_label=None: _rows(5))

    result = server.clickhouse_run_select_query.fn("SELECT 1")

    assert result == {"rows": _rows(5)}
    assert "truncated" not in result


def test_select_query_over_default_limit_is_truncated(monkeypatch):
    monkeypatch.setattr(
        server, "run_select_query", lambda query, query_label=None: _rows(server.DEFAULT_SELECT_MAX_ROWS + 50)
    )

    result = server.clickhouse_run_select_query.fn("SELECT * FROM huge_table")

    assert result["truncated"] is True
    assert result["returned_rows"] == server.DEFAULT_SELECT_MAX_ROWS
    assert result["total_rows"] == server.DEFAULT_SELECT_MAX_ROWS + 50
    assert len(result["rows"]) == server.DEFAULT_SELECT_MAX_ROWS
    assert "max_rows" in result["note"]


def test_select_query_max_rows_can_be_raised(monkeypatch):
    monkeypatch.setattr(
        server, "run_select_query", lambda query, query_label=None: _rows(server.DEFAULT_SELECT_MAX_ROWS + 50)
    )

    result = server.clickhouse_run_select_query.fn(
        "SELECT * FROM huge_table", max_rows=server.DEFAULT_SELECT_MAX_ROWS + 50
    )

    assert "truncated" not in result
    assert len(result["rows"]) == server.DEFAULT_SELECT_MAX_ROWS + 50


def test_select_query_max_rows_is_clamped_to_hard_cap(monkeypatch):
    monkeypatch.setattr(
        server, "run_select_query", lambda query, query_label=None: _rows(server.MAX_SELECT_MAX_ROWS + 500)
    )

    result = server.clickhouse_run_select_query.fn(
        "SELECT * FROM huge_table", max_rows=server.MAX_SELECT_MAX_ROWS + 5000
    )

    assert result["truncated"] is True
    assert result["returned_rows"] == server.MAX_SELECT_MAX_ROWS

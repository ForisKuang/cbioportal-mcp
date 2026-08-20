from cbioportal_mcp import server


def _fake_rows():
    return [
        {
            "cancer_study_identifier": "study_alpha",
            "name": "Alpha Study",
            "description": "Long description",
            "type_of_cancer_id": "brca",
            "sample_count": 10,
        }
    ]


def test_list_studies_default_uses_trimmed_sample_count_query(monkeypatch):
    queries = []

    def fake_run_select_query(query):
        queries.append(query)
        return _fake_rows()

    server._all_studies_query.cache_clear()
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    rows = server.list_studies.fn()

    assert rows[0]["cancer_study_identifier"] == "study_alpha"
    assert "description" not in rows[0]
    assert "clinical_data_derived" not in queries[0]
    assert "LEFT JOIN sample" in queries[0]
    assert "LEFT JOIN patient" in queries[0]


def test_list_studies_caches_repeated_calls_across_search_terms(monkeypatch):
    call_count = 0

    def fake_run_select_query(query):
        nonlocal call_count
        call_count += 1
        return _fake_rows()

    server._all_studies_query.cache_clear()
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    assert server.list_studies.fn(limit=20) == server.list_studies.fn(limit=20)
    # A never-before-seen search term still hits the same cached snapshot,
    # unlike the old per-query-shape cache which would have missed here.
    server.list_studies.fn(search="alpha")
    assert call_count == 1


def test_list_studies_verbose_includes_description(monkeypatch):
    queries = []

    def fake_run_select_query(query):
        queries.append(query)
        return _fake_rows()

    server._all_studies_query.cache_clear()
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    rows = server.list_studies.fn(verbose=True)

    assert rows[0]["description"] == "Long description"
    assert "cs.description" in queries[0]


def test_list_studies_search_filters_in_python(monkeypatch):
    def fake_run_select_query(query):
        return [
            {
                "cancer_study_identifier": "study_alpha",
                "name": "Alpha Study",
                "description": "Long description",
                "type_of_cancer_id": "brca",
                "sample_count": 10,
            },
            {
                "cancer_study_identifier": "study_beta",
                "name": "Beta Study",
                "description": None,
                "type_of_cancer_id": "luad",
                "sample_count": 5,
            },
        ]

    server._all_studies_query.cache_clear()
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    rows = server.list_studies.fn(search="beta")

    assert [row["cancer_study_identifier"] for row in rows] == ["study_beta"]

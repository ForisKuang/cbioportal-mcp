from cbioportal_mcp import server


def test_list_studies_default_uses_trimmed_sample_count_query(monkeypatch):
    queries = []

    def fake_run_select_query(query):
        queries.append(query)
        return [
            {
                "cancer_study_identifier": "study_alpha",
                "name": "Alpha Study",
                "type_of_cancer_id": "brca",
                "sample_count": 10,
            }
        ]

    server._list_studies_query.cache_clear()
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    rows = server.list_studies.fn()

    assert rows[0]["cancer_study_identifier"] == "study_alpha"
    assert "description" not in rows[0]
    assert "clinical_data_derived" not in queries[0]
    assert "LEFT JOIN sample" in queries[0]
    assert "LEFT JOIN patient" in queries[0]


def test_list_studies_caches_repeated_calls(monkeypatch):
    call_count = 0

    def fake_run_select_query(query):
        nonlocal call_count
        call_count += 1
        return [
            {
                "cancer_study_identifier": "study_alpha",
                "name": "Alpha Study",
                "type_of_cancer_id": "brca",
                "sample_count": 10,
            }
        ]

    server._list_studies_query.cache_clear()
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    assert server.list_studies.fn(limit=20) == server.list_studies.fn(limit=20)
    assert call_count == 1


def test_list_studies_verbose_includes_description(monkeypatch):
    queries = []

    def fake_run_select_query(query):
        queries.append(query)
        return [
            {
                "cancer_study_identifier": "study_alpha",
                "name": "Alpha Study",
                "description": "Long description",
                "type_of_cancer_id": "brca",
                "sample_count": 10,
            }
        ]

    server._list_studies_query.cache_clear()
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    rows = server.list_studies.fn(verbose=True)

    assert rows[0]["description"] == "Long description"
    assert "cs.description" in queries[0]

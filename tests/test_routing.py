def test_release_route_eliminates_worker_below_task_classification():
    from paos_core.routing import route_release

    route = route_release()

    assert route["selected"]["id"] == "release-worker"
    assert route["selected"]["reason"]
    assert route["excluded"][0]["id"] == "public-preview-worker"
    assert "PERSONAL" in route["excluded"][0]["reason"]
    assert route["hard_constraints"]["data_classification"] == "PERSONAL"

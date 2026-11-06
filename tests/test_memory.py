import json


def test_memory_projects_only_the_accepted_in_scope_claim():
    from paos_core.memory import memory_view

    view = memory_view()

    assert view["task_classification"] == "PERSONAL"
    assert len(view["facts"]) == 1
    fact = view["facts"][0]
    assert "accessibility review" in fact["statement"]
    assert fact["evidence"] == "Synthetic release checklist, item 3"
    assert len(view["withheld"]) == 2
    assert {item["id"] for item in view["withheld"]}.isdisjoint({fact["id"]})
    assert all(set(item) == {"id", "reason"} for item in view["withheld"])
    assert "awaiting review" in json.dumps(view["withheld"])
    assert "exceeds the task classification" in json.dumps(view["withheld"])
    assert "canary rotation" not in json.dumps(view)
    assert "weekend rollout" not in json.dumps(view)


def test_withheld_claims_do_not_change_the_projection(monkeypatch):
    from paos_core import memory

    full = memory.memory_view()
    excluded = {item["id"] for item in full["withheld"]}
    monkeypatch.setattr(
        memory,
        "_FIXTURES",
        tuple(claim for claim in memory._FIXTURES if str(claim.id) not in excluded),
    )
    assert memory.memory_view()["facts"] == full["facts"]

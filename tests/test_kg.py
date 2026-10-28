"""Temporal KG: закрытие старых фактов, запрос «что было истинно тогда»."""

from __future__ import annotations

from vault_agents.kg.store import KG


def test_add_and_query(tmp_path):
    kg = KG(tmp_path / "graph.sqlite3")
    kg.add_entity("проект demo", "project")
    kg.add_fact("проект demo", "backend", "glm", valid_from="2026-01-01")
    facts = kg.query("проект demo", "backend")
    assert len(facts) == 1
    assert facts[0].object == "glm"
    assert facts[0].valid_to is None


def test_new_fact_closes_previous(tmp_path):
    kg = KG(tmp_path / "graph.sqlite3")
    kg.add_fact("сервер", "модель", "qwen3.5", valid_from="2026-01-01")
    kg.add_fact("сервер", "модель", "gemma4", valid_from="2026-06-01")

    all_facts = kg.query("сервер", "модель")
    assert len(all_facts) == 2
    old = [f for f in all_facts if f.object == "qwen3.5"][0]
    new = [f for f in all_facts if f.object == "gemma4"][0]
    assert old.valid_to == "2026-05-31"  # закрыт вчерашним днём
    assert new.valid_to is None

    # «что было истинно на дату»
    assert kg.query("сервер", "модель", at="2026-03-01")[0].object == "qwen3.5"
    assert kg.query("сервер", "модель", at="2026-07-01")[0].object == "gemma4"


def test_timeline_and_stats(tmp_path):
    kg = KG(tmp_path / "graph.sqlite3")
    kg.add_fact("A", "p", "1", valid_from="2026-01-01")
    kg.add_fact("A", "p", "2", valid_from="2026-02-01")
    tl = kg.timeline("A")
    assert [f.object for f in tl] == ["1", "2"]
    st = kg.stats()
    assert st["triples"] == 2 and st["open"] == 1


def test_entity_upsert(tmp_path):
    kg = KG(tmp_path / "graph.sqlite3")
    kg.add_entity("Аня", "person")
    kg.add_entity("Аня", "")
    kinds = {e.name: e.kind for e in kg.entities()}
    assert kinds["Аня"] == "person"  # пустой kind не затирает существующий

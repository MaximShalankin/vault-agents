"""Внешние коллекции tickets/inbox (ticket-004) и exclude_tags (ticket-002)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from vault_agents import config as config_mod
from vault_agents import notes as notes_mod
from vault_agents import schema as schema_mod
from vault_agents.rag.index import FORMAT, Index

from .conftest import note_text

TICKET = """# Ticket-001: Починить квантование сигналов

**Status:** OPEN

Выгрузка записей на телефон по BLE падает при квантовании сигналов.
Регрессионные тесты это ловят.
"""

INBOX = """---
proto: 1
kind: request
from: demo_project
to: project
date: 2026-10-28
status: actual
---

# Оценка боли и желание спать

Нужно посчитать метрику по выгрузке записей.
"""


def write_config(project: Path, **overrides) -> None:
    path = project / config_mod.CONFIG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(overrides, allow_unicode=True), encoding="utf-8")


def add_external(project: Path) -> None:
    (project / "tickets/archive").mkdir(parents=True, exist_ok=True)
    (project / "inbox/archive").mkdir(parents=True, exist_ok=True)
    (project / "tickets/ticket-001.md").write_text(TICKET, encoding="utf-8")
    (project / "tickets/readme.md").write_text("# readme\n\nслужебный файл\n", encoding="utf-8")
    (project / "tickets/archive/ticket-000.md").write_text("# Старый тикет\n\nархивный шум\n", encoding="utf-8")
    (project / "inbox/2026-10-28_from-demo-project_ble.md").write_text(INBOX, encoding="utf-8")
    (project / "inbox/archive/2026-10-01_old.md").write_text(
        "# Старое сообщение\n\nархивный шум\n", encoding="utf-8"
    )


def _index(project: Path) -> Index:
    return Index(config_mod.load_config(project))


def test_external_paths_metadata_and_skips(project: Path):
    add_external(project)
    write_config(project, include_dirs=["tickets", "inbox"])
    cfg = config_mod.load_config(project)
    schema = schema_mod.load(cfg.schema_path)

    paths = list(
        notes_mod.iter_note_paths(
            project,
            schema,
            cfg.include_dirs,
            skip_names=cfg.include_skip_names,
            skip_dirs=cfg.include_skip_dirs,
        )
    )
    rels = {notes_mod.rel_for_path(p, project) for p in paths}
    assert "tickets/ticket-001.md" in rels
    assert "inbox/2026-10-28_from-demo-project_ble.md" in rels
    assert "tickets/readme.md" not in rels
    assert "tickets/archive/ticket-000.md" not in rels
    assert "inbox/archive/2026-10-01_old.md" not in rels
    assert len(rels) == 4  # 2 vault + 2 внешних

    ticket = notes_mod.read_note(project / "tickets/ticket-001.md", project, schema)
    assert ticket.source == "tickets"
    assert ticket.type == "ticket"
    assert ticket.title == "Ticket-001: Починить квантование сигналов"
    assert ticket.status == "OPEN"
    assert ticket.chunks and ticket.chunks[0].id == "tickets/ticket-001.md#0"

    inbox = notes_mod.read_note(
        project / "inbox/2026-10-28_from-demo-project_ble.md", project, schema
    )
    assert inbox.source == "inbox"
    assert inbox.type == "inbox"
    assert inbox.title == "Оценка боли и желание спать"
    assert inbox.status == "actual"


def test_reindex_external_hybrid_and_source_filter(project: Path, fake_embeddings):
    add_external(project)
    write_config(project, include_dirs=["tickets", "inbox"])
    idx = _index(project)
    res = idx.reindex()

    assert res["notes_changed"] == 4
    assert res["vectors_updated"] == res["chunks_added"]
    st = idx.status()
    assert st.notes_total == 4
    assert st.sources == {"vault": 2, "tickets": 1, "inbox": 1}
    assert st.chunks == st.vectors  # внешние чанки тоже с векторами
    assert st.index_bytes > 0

    payload = json.loads((project / "vault/.index/manifest.json").read_text(encoding="utf-8"))
    assert payload["format"] == FORMAT

    # семантика + ключевые слова: тикет находится
    hits = idx.search("квантование сигналов")
    assert hits and hits[0].source == "tickets"
    assert hits[0].rel == "tickets/ticket-001.md"

    # фильтр по коллекции
    hits = idx.search("регрессионные тесты", source="tickets")
    assert hits and all(h.source == "tickets" for h in hits)
    # алиас типа из плана: source=ticket
    assert all(h.source == "tickets" for h in idx.search("квантование сигналов", source="ticket"))
    # BM25-фильтр не знает слова — пусто; vector всегда отдаёт ближайшее,
    # но только из запрошенной коллекции
    assert idx.search("квантование", source="inbox", mode="bm25") == []
    assert all(h.source == "inbox" for h in idx.search("квантование", source="inbox"))
    assert all(h.source == "vault" for h in idx.search("квантование", source="vault"))

    hits = idx.search("альфа счётчики", source="vault")
    assert hits and all(h.source == "vault" for h in hits)


def test_chunk_ids_unique_across_vault_and_external(project: Path, fake_embeddings):
    # одинаковый stem в разных папках vault + такой же в tickets
    for d in ("vault/20_Results", "tickets"):
        (project / d).mkdir(parents=True, exist_ok=True)
    dup = "20261003-note-dup"
    (project / f"vault/10_Notes/{dup}.md").write_text(
        note_text(dup, "Дубль в заметках", "Первый дубль."), encoding="utf-8"
    )
    (project / f"vault/20_Results/{dup}.md").write_text(
        note_text(dup, "Дубль в результатах", "Второй дубль."), encoding="utf-8"
    )
    (project / f"tickets/{dup}.md").write_text("# Дубль в тикетах\n\nТретий дубль.\n", encoding="utf-8")
    write_config(project, include_dirs=["tickets"])

    idx = _index(project)
    idx.reindex()
    ids = idx._corpus["ids"]
    assert len(ids) == len(set(ids)), "chunk id обязаны быть уникальны по проекту"
    assert idx.status().chunks == idx.status().vectors


def test_external_incremental_update_and_removal(project: Path, fake_embeddings):
    add_external(project)
    write_config(project, include_dirs=["tickets", "inbox"])
    idx = _index(project)
    idx.reindex()

    inbox = project / "inbox/2026-10-28_from-demo-project_ble.md"
    inbox.write_text(INBOX.replace("метрику", "динамику"), encoding="utf-8")
    res = idx.reindex(changed=["inbox/2026-10-28_from-demo-project_ble.md"])
    assert res["notes_changed"] == 1
    assert idx.search("динамику")[0].rel == "inbox/2026-10-28_from-demo-project_ble.md"

    ticket = project / "tickets/ticket-001.md"
    ticket.unlink()
    res = idx.reindex()
    assert res["notes_removed"] == 1
    assert all(h.source != "tickets" for h in idx.search("квантование сигналов"))
    assert idx.status().notes_total == 3


def test_exclude_tags_trash_and_return(project: Path, fake_embeddings):
    write_config(project, exclude_tags=["trash"])
    trash = project / "vault/10_Notes/20261003-note-trash.md"
    trash.write_text(
        note_text(
            "20261003-note-trash",
            "Оценка боли",
            "Оценка боли и желание спать — мусорная заметка.",
            extra="tags: [trash, temp]\n",  # YAML-flow, как в живом корпусе demo_project
        ),
        encoding="utf-8",
    )
    idx = _index(project)
    idx.reindex()

    st = idx.status()
    assert st.notes_total == 2  # alpha/beta; trash не считается
    assert st.notes_indexed == 2
    assert all(h.note_id != "20261003-note-trash" for h in idx.search("оценка боли"))
    # файл остаётся читаемым напрямую (get_note)
    assert "Оценка боли" in trash.read_text(encoding="utf-8")

    # снятие тега возвращает заметку в индекс
    trash.write_text(
        note_text(
            "20261003-note-trash",
            "Оценка боли",
            "Оценка боли и желание спать — уже не мусор.",
            extra="tags: temp\n",
        ),
        encoding="utf-8",
    )
    res = idx.reindex()
    assert res["notes_changed"] == 1
    assert idx.status().notes_total == 3
    assert idx.search("оценка боли")[0].note_id == "20261003-note-trash"
    assert idx.status().chunks == idx.status().vectors


def test_new_project_defaults_enable_external_collections(tmp_path: Path):
    path = config_mod.write_default_config(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["include_dirs"] == ["tickets", "inbox"]
    assert data["exclude_tags"] == []


def test_existing_project_without_key_stays_opt_in(project: Path):
    write_config(project)  # конфиг без include_dirs — как у старых проектов
    cfg = config_mod.load_config(project)
    assert cfg.include_dirs == []


def test_config_accepts_string_lists(project: Path):
    write_config(project, include_dirs="tickets", exclude_tags="trash, temp")
    cfg = config_mod.load_config(project)
    assert cfg.include_dirs == ["tickets"]
    assert cfg.exclude_tags == {"trash", "temp"}


def test_parse_tags_formats():
    assert notes_mod.parse_tags("trash, temp") == ("trash", "temp")
    assert notes_mod.parse_tags("[trash]") == ("trash",)
    assert notes_mod.parse_tags("[trash, temp]") == ("trash", "temp")
    assert notes_mod.parse_tags("'trash', \"temp\"") == ("trash", "temp")
    assert notes_mod.parse_tags("") == ()


def test_exclude_tags_default_empty_does_not_change_behavior(project: Path, fake_embeddings):
    trash = project / "vault/10_Notes/20261003-note-trash.md"
    trash.write_text(
        note_text("20261003-note-trash", "Мусор", "Оценка боли.", extra="tags: trash\n"),
        encoding="utf-8",
    )
    idx = _index(project)
    idx.reindex()
    assert idx.status().notes_total == 3  # дефолт не исключает
    assert idx.search("оценка боли")

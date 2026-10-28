"""ticket-005: веса источников/статусов и порог ask."""

from __future__ import annotations

from pathlib import Path

import yaml

from vault_agents import ask as ask_mod
from vault_agents import config as config_mod
from vault_agents import notes as notes_mod
from vault_agents.rag.hybrid import rrf_fuse
from vault_agents.rag.index import Index, SearchHit

from .conftest import note_text


def _write_config(project: Path, **overrides) -> Path:
    path = project / config_mod.CONFIG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(overrides, allow_unicode=True), encoding="utf-8")
    return path


def _index(project: Path) -> Index:
    return Index(config_mod.load_config(project))


def test_legacy_russian_status_label(project: Path):
    (project / "tickets").mkdir(parents=True, exist_ok=True)
    path = project / "tickets/ticket-001.md"
    path.write_text("# Ticket\n\n**Статус:** closed\n\nтело\n", encoding="utf-8")
    cfg = config_mod.load_config(project)
    from vault_agents import schema as schema_mod

    note = notes_mod.read_note(path, project, schema_mod.load(cfg.schema_path))
    assert note.status == "closed"
    assert note.status_norm == "closed"


def test_metadata_refresh_without_reembedding(project: Path, fake_embeddings, monkeypatch):
    idx = _index(project)
    idx.reindex()

    # имитируем устаревшие метаданные на диске (как после смены парсера)
    mpath = project / "vault/.index/manifest.json"
    import json

    payload = json.loads(mpath.read_text(encoding="utf-8"))
    rel = "10_Notes/20261003-note-alpha.md"
    payload["notes"][rel]["status"] = "WRONG"
    payload["notes"][rel]["status_norm"] = "unknown"
    mpath.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def no_embed(*args, **kwargs):
        raise AssertionError("metadata refresh не должен пересчитывать эмбеддинги")

    monkeypatch.setattr("vault_agents.rag.embeddings.embed_texts", no_embed)

    res = idx.reindex()
    assert res["notes_changed"] == 0
    assert res["metadata_updated"] == 1
    hit = idx.search("альфа счётчики", source="vault")[0]
    assert hit.status == "actual"
    assert hit.status_norm == "open"


def test_normalize_status():
    assert notes_mod.normalize_status("OPEN") == "open"
    assert notes_mod.normalize_status("DONE") == "closed"
    assert notes_mod.normalize_status("ANSWERED") == "closed"
    assert notes_mod.normalize_status("in_progress") == "in_progress"
    assert notes_mod.normalize_status("P1–P4 DONE; skill OPEN") == "in_progress"
    assert notes_mod.normalize_status("actual") == "open"
    assert notes_mod.normalize_status("reviewed") == "unknown"
    assert notes_mod.normalize_status("") == "unknown"


def test_rrf_doc_weight_can_remove_and_boost():
    fused = dict(
        rrf_fuse(
            {"bm25": ["a", "b"]},
            {"bm25": 1.0},
            doc_weight=lambda src, cid: 0.0 if cid == "a" else 1.0,
        )
    )
    assert "a" not in fused
    assert "b" in fused


def _add_competing_notes(project: Path, *, ticket_status: str = "OPEN") -> None:
    """Vault-заметка и тикет с одним и тем же ключевым словом."""
    alpha = project / "vault/10_Notes/20261003-note-alpha.md"
    alpha.write_text(
        note_text("20261003-note-alpha", "Альфа", "Квантование сигналов в альфе."),
        encoding="utf-8",
    )
    (project / "tickets").mkdir(parents=True, exist_ok=True)
    (project / "tickets/ticket-001.md").write_text(
        f"# Ticket-001: Квантование\n\n**Status:** {ticket_status}\n\n"
        "Квантование сигналов в тикете.\n",
        encoding="utf-8",
    )


def test_source_weights_change_order(project: Path, fake_embeddings):
    _add_competing_notes(project)

    _write_config(
        project,
        include_dirs=["tickets"],
        retrieval={"source_weights": {"vault": 1.0, "tickets": 0.0}},
    )
    idx = _index(project)
    idx.reindex()
    assert idx.search("квантование")[0].source == "vault"

    _write_config(
        project,
        include_dirs=["tickets"],
        retrieval={"source_weights": {"vault": 1.0, "tickets": 5.0}},
    )
    idx = _index(project)
    idx.reindex(all=True)
    assert idx.search("квантование")[0].source == "tickets"


def test_status_weights_remove_closed_ticket(project: Path, fake_embeddings):
    _add_competing_notes(project, ticket_status="DONE")
    _write_config(
        project,
        include_dirs=["tickets"],
        retrieval={"status_weights": {"closed": 0.0}},
    )
    idx = _index(project)
    idx.reindex()
    hits = idx.search("квантование")
    assert hits and all(h.rel != "tickets/ticket-001.md" for h in hits)
    assert idx.search("квантование")[0].source == "vault"


def test_search_hit_exposes_status_and_match_flags(project: Path, fake_embeddings):
    _add_competing_notes(project, ticket_status="OPEN")
    _write_config(project, include_dirs=["tickets"])
    idx = _index(project)
    idx.reindex()
    hit = idx.search("квантование", source="tickets")[0]
    assert hit.status == "OPEN"
    assert hit.status_norm == "open"
    assert hit.matched_bm25 is True
    assert hit.similarity > 0.0


def _hit(*, similarity: float, matched: bool) -> SearchHit:
    return SearchHit(
        chunk_id="x#0",
        note_id="x",
        rel="x",
        title="x",
        type="note",
        score=0.01,
        text="x",
        matched_bm25=matched,
        similarity=similarity,
    )


def test_ask_gate_rejects_vector_noise(project: Path, monkeypatch):
    _write_config(project, retrieval={"ask_min_similarity": 0.5})
    cfg = config_mod.load_config(project)
    monkeypatch.setattr(ask_mod, "chat", lambda messages, cfg: "ответ LLM")

    weak = [_hit(similarity=0.2, matched=False)]
    assert ask_mod.hits_relevant(weak, cfg) is False
    text, used = ask_mod.answer("вопрос вне корпуса", weak, cfg)
    assert "ничего не нашлось" in text and used is False

    assert ask_mod.hits_relevant([_hit(similarity=0.6, matched=False)], cfg) is True
    assert ask_mod.hits_relevant([_hit(similarity=0.0, matched=True)], cfg) is True

    text, used = ask_mod.answer("вопрос", [_hit(similarity=0.02, matched=True)], cfg)
    assert used is True and text == "ответ LLM"


def test_ask_gate_default_requires_bm25(project: Path):
    cfg = config_mod.load_config(project)
    assert cfg.retrieval["ask_require_bm25"] is True
    assert cfg.retrieval["ask_min_similarity"] == 0.0
    # векторный сосед без лексического совпадения — не основание отвечать
    assert ask_mod.hits_relevant([_hit(similarity=0.99, matched=False)], cfg) is False
    assert ask_mod.hits_relevant([_hit(similarity=0.0, matched=True)], cfg) is True


def test_ask_gate_can_be_disabled(project: Path, monkeypatch):
    _write_config(project, retrieval={"ask_require_bm25": False})
    cfg = config_mod.load_config(project)
    assert ask_mod.hits_relevant([_hit(similarity=0.0, matched=False)], cfg) is True

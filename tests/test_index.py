"""Индекс: инкремент по sha256, гибридный поиск, деградация, сниппеты."""

from __future__ import annotations

from pathlib import Path

from vault_agents import config as config_mod
from vault_agents import notes as notes_mod
from vault_agents.rag import embeddings
from vault_agents.rag.bm25 import BM25Index
from vault_agents.rag.hybrid import rrf_fuse
from vault_agents.rag.index import Index, human_bytes, snippet

from .conftest import note_text


def _index(project: Path) -> Index:
    cfg = config_mod.load_config(project)
    return Index(cfg)


def test_chunking_basic():
    assert notes_mod.chunk_text("") == []
    assert notes_mod.chunk_text("короткий абзац") == ["короткий абзац"]
    long_para = "x" * 3000
    chunks = notes_mod.chunk_text(long_para, size=1200, overlap=150)
    assert len(chunks) >= 3
    assert all(len(c) <= 1200 for c in chunks)


def test_rrf_fusion():
    fused = dict(
        rrf_fuse(
            {"bm25": ["a", "b"], "vector": ["b", "c"]},
            {"bm25": 0.4, "vector": 0.6},
            k=60,
        )
    )
    # b встречается в обоих списках — должен быть первым
    assert max(fused, key=fused.get) == "b"
    assert set(fused) == {"a", "b", "c"}


def test_bm25_russian_stemming():
    idx = BM25Index(stemmer="russian")
    idx.build(["1", "2"], ["регрессионные тесты прогоняются", "альфа хранит счётчики"])
    hits = idx.search("регрессия тест", top_k=2)
    assert hits and hits[0].chunk_id == "1"


def test_reindex_incremental_and_search(project: Path, fake_embeddings):
    idx = _index(project)
    res = idx.reindex()
    assert res["notes_changed"] == 2
    assert res["vectors_updated"] == 2

    # повторный reindex без изменений — no-op
    assert idx.reindex()["notes_changed"] == 0

    # правка заметки → переиндексируется только она
    beta = project / "vault/10_Notes/20261003-note-beta.md"
    beta.write_text(note_text("20261003-note-beta", "Бета", "Бета теперь про квантование."), encoding="utf-8")
    res2 = idx.reindex()
    assert res2["notes_changed"] == 1
    assert res2["changed"] == ["10_Notes/20261003-note-beta.md"]

    hits = idx.search("квантование")
    assert hits and hits[0].note_id == "20261003-note-beta"

    # удаление заметки → чанки уходят из корпуса и манифеста
    beta.unlink()
    res3 = idx.reindex()
    assert res3["notes_removed"] == 1
    assert all(h.note_id != "20261003-note-beta" for h in idx.search("квантование"))


def test_reindex_removes_old_vectors(project: Path, fake_embeddings):
    idx = _index(project)
    idx.reindex()
    alpha = project / "vault/10_Notes/20261003-note-alpha.md"
    alpha.write_text(
        note_text("20261003-note-alpha", "Альфа", "Совсем другой текст без прежних слов."),
        encoding="utf-8",
    )
    idx.reindex()
    st = idx.status()
    assert st.chunks == st.vectors, f"чанков {st.chunks}, векторов {st.vectors} — остались мусорные"


def test_degradation_without_ollama(project: Path, monkeypatch):
    def boom(texts, cfg, **kwargs):
        raise embeddings.EmbeddingsUnavailable("ollama недоступна (тест)")

    monkeypatch.setattr(embeddings, "embed_texts", boom)
    monkeypatch.setattr(embeddings, "ping", lambda cfg, timeout=3.0: False)

    idx = _index(project)
    res = idx.reindex()
    assert res["vector_stale"] is True
    assert res["chunks_total"] > 0  # BM25-часть проиндексирована

    st = idx.status()
    assert st.mode == "bm25-only"

    # поиск работает в BM25-режиме
    hits = idx.search("альфа счётчики")
    assert hits and hits[0].note_id == "20261003-note-alpha"


def test_vector_catch_up_after_ollama_outage(project: Path, fake_embeddings, monkeypatch):
    real_embed = embeddings.embed_texts

    def boom(texts, cfg, **kwargs):
        raise embeddings.EmbeddingsUnavailable("ollama недоступна (тест)")

    monkeypatch.setattr(embeddings, "embed_texts", boom)
    monkeypatch.setattr(embeddings, "ping", lambda cfg, timeout=3.0: False)

    idx = _index(project)
    res = idx.reindex()
    assert res["vector_stale"] is True
    assert idx.status().mode == "bm25-only"

    # Ollama вернулась: без изменений заметок incremental reindex догоняет вектора
    monkeypatch.setattr(embeddings, "embed_texts", real_embed)
    monkeypatch.setattr(embeddings, "ping", lambda cfg, timeout=3.0: True)
    res = idx.reindex()
    assert res["notes_changed"] == 0
    assert res["vectors_updated"] > 0
    assert res["vector_stale"] is False

    st = idx.status()
    assert st.mode == "hybrid"
    assert st.chunks == st.vectors


def test_point_update_changed_file(project: Path, fake_embeddings):
    idx = _index(project)
    idx.reindex()
    gamma = project / "vault/10_Notes/20261003-note-gamma.md"
    gamma.write_text(note_text("20261003-note-gamma", "Гамма", "Гамма про очереди."), encoding="utf-8")
    res = idx.reindex(changed=["vault/10_Notes/20261003-note-gamma.md"])
    assert res["notes_changed"] == 1
    assert idx.search("очереди")[0].note_id == "20261003-note-gamma"


def test_human_bytes():
    assert human_bytes(0) == "0 B"
    assert human_bytes(1536) == "1.5 KiB"
    assert human_bytes(115 * 1024 * 1024) == "115.0 MiB"


def test_snippet_centers_on_query_and_word_boundaries():
    text = "первое предложение. " * 40 + "квантование сигналов в реальном времени. " + "хвост. " * 40
    s = snippet(text, "квантование", limit=120)
    assert "квантование" in s
    assert s.startswith("…") and s.endswith("…")
    assert not s.startswith("… ")  # без ведущего пробела

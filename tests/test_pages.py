"""Постраничные чанки для PDF-производных коллекций (ticket-009).

Якоря `<!-- page: N -->`, режим chunk_boundary=page, метаданные страницы
в SearchHit и тип источника papers→paper.
"""

from __future__ import annotations

import yaml

from vault_agents import config as config_mod
from vault_agents import notes as notes_mod
from vault_agents import schema as schema_mod
from vault_agents.rag.index import Index

PAPER = """# Attention Is All You Need (фрагмент)

<!-- page: 1 -->
Первый абзац страницы один. Здесь описана архитектура Transformer и
multi-head attention. Ещё немного текста, чтобы чанк был не один.

<!-- page: 2 -->
Второй абзац страницы два. Здесь описан positional encoding и обучение
на WMT14 с BLEU. Ещё немного текста для второго чанка.

<!-- page: 3 -->
Третий абзац страницы три. Здесь описаны гиперпараметры и label smoothing.
"""


def write_config(project, **overrides) -> None:
    path = project / config_mod.CONFIG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(overrides, allow_unicode=True), encoding="utf-8")


def add_paper(project) -> None:
    d = project / "papers/aiayn"
    d.mkdir(parents=True, exist_ok=True)
    (d / "aiayn.md").write_text(PAPER, encoding="utf-8")


def test_split_pages_returns_page_numbers():
    pages = notes_mod.split_pages(PAPER)
    assert [p for p, _ in pages] == [1, 2, 3]
    assert all("<!-- page:" not in text for _, text in pages)


def test_chunk_text_pages_never_crosses_boundary():
    pieces = notes_mod.chunk_text_pages(PAPER, size=80, overlap=10)
    assert pieces
    assert {p for p, _ in pieces} == {1, 2, 3}
    segments = dict(notes_mod.split_pages(PAPER))
    for page, chunk in pieces:
        assert page is not None
        # чанк целиком принадлежит своей странице
        assert chunk in segments[page]
        assert "<!-- page:" not in chunk


def test_read_note_page_mode_metadata(project):
    add_paper(project)
    write_config(
        project,
        include_dirs=["papers"],
        chunk_boundary="page",
        chunk_size=80,
        chunk_overlap=10,
    )
    cfg = config_mod.load_config(project)
    schema = schema_mod.load(cfg.schema_path)
    note = notes_mod.read_note(
        project / "papers/aiayn/aiayn.md",
        project,
        schema,
        chunk_size=cfg.chunk_size,
        chunk_overlap=cfg.chunk_overlap,
        chunk_boundary=cfg.chunk_boundary,
    )
    assert note.source == "papers"
    assert note.type == "paper"
    assert {c.page for c in note.chunks} == {1, 2, 3}


def test_default_paragraph_mode_unchanged(project):
    add_paper(project)
    write_config(project, include_dirs=["papers"])
    cfg = config_mod.load_config(project)
    assert cfg.chunk_boundary == "paragraph"
    schema = schema_mod.load(cfg.schema_path)
    note = notes_mod.read_note(
        project / "papers/aiayn/aiayn.md",
        project,
        schema,
        chunk_size=10000,
        chunk_overlap=0,
        chunk_boundary=cfg.chunk_boundary,
    )
    # в обычном режиме чанк может включать несколько страниц, page не ставится
    assert len(note.chunks) == 1
    assert "<!-- page: 1 -->" in note.chunks[0].text
    assert "<!-- page: 3 -->" in note.chunks[0].text
    assert note.chunks[0].page is None


def test_index_search_returns_page(project, fake_embeddings):
    add_paper(project)
    write_config(
        project,
        include_dirs=["papers"],
        chunk_boundary="page",
        chunk_size=200,
        chunk_overlap=20,
    )
    idx = Index(config_mod.load_config(project))
    idx.reindex()

    hits = idx.search("label smoothing", top_k=5, source="papers")
    assert hits
    assert all(h.source == "papers" for h in hits)
    assert any(h.page == 3 for h in hits), [(h.rel, h.page, h.text[:40]) for h in hits]
    assert all(h.type == "paper" for h in hits)


def test_chunk_boundary_invalid_falls_back_to_paragraph(project):
    write_config(project, chunk_boundary="bogus")
    cfg = config_mod.load_config(project)
    assert cfg.chunk_boundary == "paragraph"

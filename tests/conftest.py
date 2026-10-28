"""Общие фикстуры: временный проект с vault-скелетом и предсказуемые эмбеддинги.

Тесты герметичны: Ollama не требуется — embed_texts/embed_query/ping
подменяются детерминированными заглушками.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vault_agents import config as config_mod
from vault_agents.rag import embeddings

SCHEMA = {
    "version": 1,
    "naming": "{YYYYMMDD}-{type}-{slug}.md",
    "id": "имя файла без .md",
    "types": {
        "note": {
            "dir": "10_Notes",
            "statuses": ["actual", "stale", "archived"],
            "required": ["id", "type", "status", "title", "created"],
            "optional": ["tags"],
        },
        "result": {
            "dir": "20_Results",
            "statuses": ["active", "superseded"],
            "required": ["id", "type", "status", "title", "created", "outcome"],
            "enums": {"outcome": ["confirmed", "refuted"]},
        },
    },
    "created_format": "YYYY-MM-DD",
    "skip_dirs": ["00_System", "90_Archive"],
    "state": {"path": "STATE.md", "max_lines": 120, "required_fields": ["updated"]},
}


def note_text(note_id: str, title: str, body: str, *, ntype: str = "note", extra: str = "") -> str:
    return (
        "---\n"
        f"id: {note_id}\n"
        f"type: {ntype}\n"
        "status: actual\n"
        f"title: {title}\n"
        "created: 2026-10-03\n"
        f"{extra}"
        "---\n\n"
        f"{body}\n"
    )


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    for d in ("vault/00_System", "vault/10_Notes", "vault/20_Results", "vault/90_Archive"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / config_mod.SCHEMA_REL).write_text(json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "vault/10_Notes/20261003-note-alpha.md").write_text(
        note_text(
            "20261003-note-alpha",
            "Заметка про альфу",
            "Альфа отвечает за первичную обработку сигналов и хранение счётчиков.",
        ),
        encoding="utf-8",
    )
    (tmp_path / "vault/10_Notes/20261003-note-beta.md").write_text(
        note_text(
            "20261003-note-beta",
            "Заметка про бету",
            "Бета занимается регрессионными тестами и сверкой базовых линий.",
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def fake_embeddings(monkeypatch) -> None:
    """Детерминированные эмбеддинги: без Ollama, но с осмысленным ранжированием
    (близость по пересечению слов)."""

    def _vec(text: str) -> list[float]:
        import re

        words = {w for w in re.findall(r"\w+", text.lower()) if len(w) > 3}
        dims = [0.0] * 64  # пошире, чтобы хэш-коллизии слов не решали исход
        for w in words:
            dims[sum(map(ord, w)) % 64] += 1.0
        norm = sum(x * x for x in dims) ** 0.5 or 1.0
        return [x / norm for x in dims]

    monkeypatch.setattr(
        embeddings, "embed_texts", lambda texts, cfg, **kwargs: [_vec(t) for t in texts]
    )
    monkeypatch.setattr(embeddings, "embed_query", lambda text, cfg, **kwargs: _vec(text))
    monkeypatch.setattr(embeddings, "ping", lambda cfg, timeout=3.0: True)

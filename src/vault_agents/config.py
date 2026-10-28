"""Конфиг проекта и поиск корня vault.

Приоритет: аргумент --project → $VAULT_PROJECT → поиск vault/ вверх от cwd.
Конфиг: <project>/vault/00_System/vault.config.yaml (отсутствует → дефолты,
файл создаётся `vault-agents init`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_REL = Path("vault/00_System/vault.config.yaml")
SCHEMA_REL = Path("vault/00_System/vault_schema.json")
STATE_REL = Path("vault/00_System/STATE.md")

DEFAULTS: dict[str, Any] = {
    "index_dir": "vault/.index",
    # Дефолт для НОВЫХ проектов (`vault-agents init` → vault.config.yaml).
    # Существующие проекты без ключа получают [] (Config.include_dirs), т.е.
    # остаются opt-in — ticket-004 P5.
    "include_dirs": ["tickets", "inbox"],
    "exclude_tags": [],          # заметки с этими тегами не индексируются (trash)
    "include_skip_names": ["readme.md"],  # имена файлов, не индексируемые во внешних dirs
    "include_skip_dirs": ["archive"],     # подкаталоги, не индексируемые во внешних dirs
    "chunk_size": 1200,          # символов на чанк
    "chunk_overlap": 150,
    "embedding": {
        "provider": "ollama",
        "model": "bge-m3:latest",
        "base_url": "http://127.0.0.1:11434",
        "timeout_sec": 60,
    },
    "bm25": {
        "backend": "rank_bm25",  # rank_bm25 | fts5
        "stemmer": "russian",    # russian | none
    },
    "retrieval": {
        "mode": "hybrid",        # hybrid | bm25 | vector
        "top_k": 8,
        "weights": {"bm25": 0.4, "vector": 0.6},
        "rrf_k": 60,
        "snippet_chars": 400,
        # ticket-005: калибровка на пилотах показала, что глобальные веса
        # источников/статусов ухудшают recall (запросы к тикетам находят
        # закрытые/недавние). Дефолты нейтральные; веса — opt-in на проект.
        "source_weights": {},    # напр. {"vault": 1.0, "tickets": 0.8}
        "status_weights": {},    # напр. {"closed": 0.5}
        "ask_require_bm25": True,    # ask не отвечает по одним векторным соседям
        "ask_min_similarity": 0.0,   # >0: разрешить semantic-only выше порога
    },
    "ask": {
        "model": "gemma4:e4b",
        "max_snippets": 6,
    },
    "watcher": {
        "enabled": True,
        "debounce_sec": 2,
    },
}


def _as_list(value: Any) -> list[str]:
    """Конфиг-список: терпим к строке («trash» / «a, b») и None."""
    if value is None:
        return []
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


class VaultNotFound(Exception):
    """vault/ проекта не найден."""


def find_project(start: str | os.PathLike[str] | None = None) -> Path:
    """Ищет корень проекта: каталог, содержащий vault/00_System/vault_schema.json
    (или хотя бы vault/), поднимаясь вверх от start/cwd."""
    if start is None:
        start = os.environ.get("VAULT_PROJECT") or os.getcwd()
    p = Path(start).expanduser().resolve()
    for cand in [p, *p.parents]:
        if (cand / SCHEMA_REL).is_file():
            return cand
    # фолбэк: есть vault/, но схема ещё не заведена
    for cand in [p, *p.parents]:
        if (cand / "vault").is_dir():
            return cand
    raise VaultNotFound(
        f"не найден vault/ (искал {SCHEMA_REL} вверх от {p}); "
        "укажи --project или создай скелет через vault-agents init"
    )


@dataclass
class Config:
    project: Path
    raw: dict[str, Any] = field(default_factory=dict)

    # --- точечный доступ с дефолтами ---
    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    @property
    def index_dir(self) -> Path:
        return self.project / str(self.get("index_dir", default=DEFAULTS["index_dir"]))

    @property
    def include_dirs(self) -> list[str]:
        return _as_list(self.get("include_dirs", default=[]))

    @property
    def exclude_tags(self) -> set[str]:
        return {t.lower() for t in _as_list(self.get("exclude_tags", default=[]))}

    @property
    def include_skip_names(self) -> set[str]:
        return {n.lower() for n in _as_list(self.get("include_skip_names", default=DEFAULTS["include_skip_names"]))}

    @property
    def include_skip_dirs(self) -> set[str]:
        return {d.lower() for d in _as_list(self.get("include_skip_dirs", default=DEFAULTS["include_skip_dirs"]))}

    @property
    def schema_path(self) -> Path:
        return self.project / SCHEMA_REL

    @property
    def state_path(self) -> Path:
        return self.project / STATE_REL

    @property
    def chunk_size(self) -> int:
        return int(self.get("chunk_size", default=DEFAULTS["chunk_size"]))

    @property
    def chunk_overlap(self) -> int:
        return int(self.get("chunk_overlap", default=DEFAULTS["chunk_overlap"]))

    @property
    def retrieval(self) -> dict[str, Any]:
        merged = dict(DEFAULTS["retrieval"])
        merged.update(self.get("retrieval", default={}) or {})
        return merged

    @property
    def bm25(self) -> dict[str, Any]:
        merged = dict(DEFAULTS["bm25"])
        merged.update(self.get("bm25", default={}) or {})
        return merged

    @property
    def embedding(self) -> dict[str, Any]:
        merged = dict(DEFAULTS["embedding"])
        merged.update(self.get("embedding", default={}) or {})
        return merged

    @property
    def ask(self) -> dict[str, Any]:
        merged = dict(DEFAULTS["ask"])
        merged.update(self.get("ask", default={}) or {})
        return merged

    @property
    def watcher(self) -> dict[str, Any]:
        merged = dict(DEFAULTS["watcher"])
        merged.update(self.get("watcher", default={}) or {})
        return merged


def load_config(project: str | os.PathLike[str] | None = None) -> Config:
    root = find_project(project)
    cfg_path = root / CONFIG_REL
    raw: dict[str, Any] = {}
    if cfg_path.is_file():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{cfg_path}: конфиг должен быть YAML-словарём")
    return Config(project=root, raw=raw)


def write_default_config(project: Path, *, force: bool = False) -> Path:
    """Создаёт vault.config.yaml с дефолтами; существующий не трогает."""
    path = project / CONFIG_REL
    if path.exists() and not force:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_default_yaml(), encoding="utf-8")
    return path


def _render_default_yaml() -> str:
    header = (
        "# vault-agents: конфиг проекта (ticket-051). Дефолты — в src/vault_agents/config.py;\n"
        "# здесь достаточно переопределить нужное.\n"
    )
    return header + yaml.safe_dump(DEFAULTS, allow_unicode=True, sort_keys=False)

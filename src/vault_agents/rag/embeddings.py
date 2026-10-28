"""Эмбеддинги через Ollama (BGE-M3 по умолчанию).

Единая точка отказа всей гибридной схемы (аудит ticket-041), поэтому любой
сетевой сбой поднимается наверх как EmbeddingsUnavailable — вызывающий код
обязан деградировать, а не падать.
"""

from __future__ import annotations

from typing import Callable

import httpx

from ..config import Config

# Ollama принимает пачку; слишком большие батчи не нужны — режем для прогресса
BATCH = 32


class EmbeddingsUnavailable(RuntimeError):
    """Ollama недоступна или модель не отвечает — работаем в BM25-режиме."""


def _base_url(cfg: Config) -> str:
    return str(cfg.embedding.get("base_url", "http://127.0.0.1:11434")).rstrip("/")


def _model(cfg: Config) -> str:
    return str(cfg.embedding.get("model", "bge-m3:latest"))


def ping(cfg: Config, timeout: float = 3.0) -> bool:
    """Жива ли Ollama вообще (для status/doctor)."""
    try:
        r = httpx.get(f"{_base_url(cfg)}/api/tags", timeout=timeout)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def has_model(cfg: Config, timeout: float = 5.0) -> bool:
    """Есть ли модель эмбеддингов локально (doctor)."""
    try:
        r = httpx.get(f"{_base_url(cfg)}/api/tags", timeout=timeout)
        r.raise_for_status()
        names = {m.get("name", "") for m in r.json().get("models", [])}
        model = _model(cfg)
        return model in names or model.split(":")[0] in {n.split(":")[0] for n in names}
    except httpx.HTTPError:
        return False


def embed_texts(
    texts: list[str],
    cfg: Config,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> list[list[float]]:
    """Эмбеддинги пачки текстов. Пустой список → пустой список.

    progress(done, total) вызывается после каждого батча — для reindex
    больших корпусов (большой корпус: 6 536 чанков, ~4 мин).
    """
    if not texts:
        return []
    timeout = float(cfg.embedding.get("timeout_sec", 60))
    out: list[list[float]] = []
    total = len(texts)
    try:
        with httpx.Client(timeout=timeout) as client:
            for i in range(0, total, BATCH):
                batch = texts[i : i + BATCH]
                r = client.post(
                    f"{_base_url(cfg)}/api/embed",
                    json={"model": _model(cfg), "input": batch},
                )
                r.raise_for_status()
                data = r.json()
                embs = data.get("embeddings")
                if not embs or len(embs) != len(batch):
                    raise EmbeddingsUnavailable(
                        f"ollama вернула {0 if not embs else len(embs)} эмбеддингов на {len(batch)} текстов"
                    )
                out.extend(embs)
                if progress:
                    progress(len(out), total)
    except httpx.HTTPError as e:
        raise EmbeddingsUnavailable(f"ollama недоступна ({_base_url(cfg)}): {e}") from e
    return out


def embed_query(text: str, cfg: Config) -> list[float]:
    embs = embed_texts([text], cfg)
    if not embs:
        raise EmbeddingsUnavailable("пустой запрос")
    return embs[0]

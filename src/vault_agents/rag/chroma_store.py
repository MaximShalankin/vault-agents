"""Векторный слой: ChromaDB с внешними эмбеддингами.

Встроенные embedding-функции Chroma сознательно НЕ используем: эмбеддинги
приходят из Ollama (BGE-M3), поэтому в зависимостях нет ничего лишнего, а
точка отказа одна и та же — Ollama (и деградация тоже одна, см. embeddings.py).

Запись режется на батчи: у Chroma есть предел на один upsert (на этой машине
5461; ticket-001 §14.2), а большие проекты дают 6 536+ чанков.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

import chromadb
from chromadb.config import Settings

from .bm25 import Hit

# Верхняя граница своего батча: меньше лимита Chroma и даёт прогресс.
MAX_BATCH = 1000


def _slices(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class VectorStore:
    def __init__(self, path: Path, collection: str = "vault"):
        path.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(
            path=str(path), settings=Settings(anonymized_telemetry=False)
        )
        self.col = self.client.get_or_create_collection(
            collection, metadata={"hnsw:space": "cosine"}
        )

    def _batch_size(self) -> int:
        try:
            limit = int(self.client.get_max_batch_size())
        except Exception:  # noqa: BLE001 — старые Chroma не умеют этот метод
            return MAX_BATCH
        return max(1, min(limit, MAX_BATCH))

    def count(self) -> int:
        return self.col.count()

    def ids(self) -> set[str]:
        """Все id векторов — для догонки после деградации (vector_stale)."""
        if self.count() == 0:
            return set()
        res = self.col.get(include=[])
        return set(res.get("ids") or [])

    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        if not ids:
            return
        size = self._batch_size()
        total = len(ids)
        for start in range(0, total, size):
            end = start + size
            self.col.upsert(
                ids=ids[start:end],
                embeddings=embeddings[start:end],
                documents=documents[start:end],
                metadatas=metadatas[start:end],
            )
            if progress:
                progress(min(end, total), total)

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        size = self._batch_size()
        for batch in _slices(list(ids), size):
            self.col.delete(ids=batch)

    def update_metadatas(self, ids: list[str], metadatas: list[dict[str, Any]]) -> None:
        """Обновить метаданные без пересчёта эмбеддингов (metadata refresh)."""
        if not ids:
            return
        size = self._batch_size()
        for start in range(0, len(ids), size):
            end = start + size
            self.col.update(ids=ids[start:end], metadatas=metadatas[start:end])

    def query(
        self, embedding: list[float], top_k: int, where: dict[str, Any] | None = None
    ) -> list[Hit]:
        if self.count() == 0:
            return []
        kwargs: dict[str, Any] = {"query_embeddings": [embedding], "n_results": top_k}
        if where:
            kwargs["where"] = where
        res = self.col.query(**kwargs)
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        # cosine distance → similarity: больше — ближе
        return [Hit(chunk_id=cid, score=float(1.0 - d)) for cid, d in zip(ids, dists)]

    def reset(self) -> None:
        name = self.col.name
        self.client.delete_collection(name)
        self.col = self.client.get_or_create_collection(
            name, metadata={"hnsw:space": "cosine"}
        )

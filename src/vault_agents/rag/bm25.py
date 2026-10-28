"""BM25-слой.

Бэкенд по умолчанию — rank-bm25 с русским стеммером nltk;
Бэкенд переключаем конфигом (`bm25.backend`: rank_bm25 |
fts5) — FTS5-вариант появится отдельным модулем, интерфейс общий.

Индекс держим в оперативной памяти: для десятков тысяч чанков перестройка
занимает секунды, а «инкрементальность» даёт sha256 на уровне заметок —
дорогая часть (эмбеддинги) пересчитывается только для изменённых.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from rank_bm25 import BM25Plus

from ..config import Config

_WORD_RE = re.compile(r"[\w\-]+", re.UNICODE)


class _Stemmer:
    def __init__(self, kind: str):
        self.kind = kind
        self._impl = None
        if kind == "russian":
            # Snowball-стеммеры nltk работают без скачивания корпусов
            from nltk.stem import SnowballStemmer

            self._impl = SnowballStemmer("russian")

    def __call__(self, token: str) -> str:
        if self._impl is None:
            return token
        return self._impl.stem(token)


@dataclass
class Hit:
    chunk_id: str
    score: float


class BM25Index:
    def __init__(self, stemmer: str = "russian"):
        self.stem = _Stemmer(stemmer)
        self.ids: list[str] = []
        self.docs: list[str] = []
        self._bm25: BM25Okapi | None = None

    @staticmethod
    def tokenize(text: str, stem: _Stemmer) -> list[str]:
        return [stem(t.lower()) for t in _WORD_RE.findall(text)]

    def build(self, ids: list[str], texts: list[str]) -> None:
        self.ids = list(ids)
        self.docs = list(texts)
        corpus = [self.tokenize(t, self.stem) for t in self.docs]
        self._tokens = [set(toks) for toks in corpus]
        # BM25Plus, а не Okapi: у Okapi idf отрицателен для терминов,
        # встречающихся почти во всех документах (и вырождается в ноль на
        # корпусе из 1–2 заметок) — совпавший документ оказывался НИЖЕ
        # несовпавших. BM25Plus: idf = log((N+1)/df) > 0 всегда.
        self._bm25 = BM25Plus(corpus) if corpus else None

    def search(
        self,
        query: str,
        top_k: int,
        allow: Callable[[str], bool] | None = None,
    ) -> list[Hit]:
        """allow — фильтр по chunk_id (например, source=tickets): применяется
        до обрезки top_k, иначе малая коллекция не попадает в глобальный top-N."""
        if self._bm25 is None or not self.ids:
            return []
        tokens = self.tokenize(query, self.stem)
        if not tokens:
            return []
        qset = set(tokens)
        scores = self._bm25.get_scores(tokens)
        # В ранжирование RRF берём только документы с реально совпавшим
        # термом: у BM25Plus документы без совпадений получают ненулевой
        # baseline (delta * idf по каждому терму запроса), и «просто
        # существует» давало бы очки наравне с найденным.
        ranked = sorted(
            (
                (cid, float(s))
                for cid, s, toks in zip(self.ids, scores, self._tokens)
                if qset & toks and (allow is None or allow(cid))
            ),
            key=lambda x: x[1],
            reverse=True,
        )
        return [Hit(chunk_id=cid, score=s) for cid, s in ranked[:top_k]]


def build_from_config(ids: list[str], texts: list[str], cfg: Config) -> BM25Index:
    backend = str(cfg.bm25.get("backend", "rank_bm25"))
    if backend != "rank_bm25":
        # Интерфейс един: FTS5-бэкенд добавится сюда, не меняя вызывающий код.
        raise NotImplementedError(f"bm25.backend={backend!r} пока не поддержан (есть rank_bm25)")
    idx = BM25Index(stemmer=str(cfg.bm25.get("stemmer", "russian")))
    idx.build(ids, texts)
    return idx

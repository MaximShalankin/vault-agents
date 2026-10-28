"""Слияние результатов поиска: weighted RRF.

Reciprocal Rank Fusion — конвергентный стандарт экосистемы (аудит ticket-041:
8+ проектов независимо пришли к BM25 + вектор + RRF с весами 0.4/0.6, k=60).
Слияние ранговое, поэтому не требует калибровки шкал BM25 и cosine между собой.
"""

from __future__ import annotations

from typing import Callable


def rrf_fuse(
    rankings: dict[str, list[str]],
    weights: dict[str, float],
    k: int = 60,
    doc_weight: Callable[[str, str], float] | None = None,
) -> list[tuple[str, float]]:
    """rankings: источник → список chunk_id по убыванию релевантности.

    score(d) = Σ_src weight[src] * doc_weight(src, d) / (k + rank_src(d)).

    doc_weight — множитель конкретного документа (source/status-веса,
    ticket-005); без него поведение прежнее.
    """
    scores: dict[str, float] = {}
    for src, ids in rankings.items():
        w = float(weights.get(src, 1.0))
        for rank, cid in enumerate(ids, start=1):
            dw = float(doc_weight(src, cid)) if doc_weight else 1.0
            if dw <= 0:
                continue
            scores[cid] = scores.get(cid, 0.0) + w * dw / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)

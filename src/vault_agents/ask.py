"""Ответ по vault: сниппеты → локальная LLM (Ollama) → ответ с цитатами [N].

Деградация обязательна: LLM недоступна → возвращаем честный фолбэк со
сниппетами (источники всё равно полезны), а не ошибку.
"""

from __future__ import annotations

import httpx

from .config import Config
from .rag.embeddings import _base_url
from .rag.index import SearchHit, snippet

SYSTEM = (
    "Ты — поисковый ассистент по базе знаний проекта. Отвечай ТОЛЬКО по "
    "приведённым фрагментам, ссылайся на них номерами [N]. Если во фрагментах "
    "нет ответа — прямо скажи, что не нашёл. Кратко, по делу."
)

NOT_FOUND_TEXT = "По этому вопросу в памяти проекта ничего не нашлось."


class LLMUnavailable(RuntimeError):
    pass


def chat(messages: list[dict[str, str]], cfg: Config) -> str:
    model = str(cfg.ask.get("model", "gemma4:e4b"))
    timeout = float(cfg.embedding.get("timeout_sec", 60)) * 2
    try:
        r = httpx.post(
            f"{_base_url(cfg)}/api/chat",
            json={"model": model, "messages": messages, "stream": False},
            timeout=timeout,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMUnavailable(f"ollama недоступна: {e}") from e
    return (r.json().get("message") or {}).get("content", "").strip()


def build_context(hits: list[SearchHit], cfg: Config) -> str:
    limit = int((cfg.retrieval.get("snippet_chars") or 400))
    parts = []
    for i, h in enumerate(hits, start=1):
        parts.append(f"[{i}] {h.title} ({h.rel})\n{snippet(h.text, '', limit)}")
    return "\n\n".join(parts)


def hits_relevant(hits: list[SearchHit], cfg: Config) -> bool:
    """Есть ли основание отвечать: BM25-совпадение или similarity выше порога.

    Vector-поиск всегда возвращает ближайших соседей, поэтому без этого гейта
    `ask` отвечает по случайным чанкам (ticket-005). Калибровка на пилотах
    показала, что similarity позитивов и негативов пересекается, поэтому
    дефолт — требовать хотя бы один BM25-hit; semantic-only разрешается
    явным `ask_min_similarity` (или отключением `ask_require_bm25`).
    """
    if not hits:
        return False
    if any(h.matched_bm25 for h in hits):
        return True
    min_sim = float(cfg.retrieval.get("ask_min_similarity", 0.0) or 0.0)
    if min_sim > 0:
        return any(h.similarity >= min_sim for h in hits)
    return not bool(cfg.retrieval.get("ask_require_bm25", True))


def answer(question: str, hits: list[SearchHit], cfg: Config) -> tuple[str, bool]:
    """(текст ответа, был ли использован LLM)."""
    if not hits_relevant(hits, cfg):
        return NOT_FOUND_TEXT, False
    max_snippets = int(cfg.ask.get("max_snippets", 6))
    ctx = build_context(hits[:max_snippets], cfg)
    try:
        text = chat(
            [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Вопрос: {question}\n\nФрагменты:\n{ctx}"},
            ],
            cfg,
        )
        return text, True
    except LLMUnavailable:
        return (
            "LLM недоступна — вот наиболее релевантные фрагменты:\n\n" + ctx,
            False,
        )

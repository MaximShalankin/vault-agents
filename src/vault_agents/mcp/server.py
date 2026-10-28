"""MCP-сервер vault-agents: search / ask / get_note / status / reindex + KG-тулсы.

Канонический набор из аудита трёх live-проектов (ticket-041):
`snippets`-режим по умолчанию (×10 меньше токенов — критично для telegram-сессий),
`ask` с inline-цитатами, честная деградация при недоступной LLM.

Внутри процесса MCP поднимается watcher (авто-реиндекс, ticket-051); его вывод
идёт в stderr — stdout занят протоколом MCP.
"""

from __future__ import annotations

import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .. import __version__
from .. import ask as ask_mod
from ..config import load_config
from ..kg.store import KG
from ..rag.index import Index, acquire_mcp_lock, human_bytes, release_mcp_lock, snippet
from ..watch import VaultWatcher

INSTRUCTIONS = (
    "Память проекта: vault, а также tickets/ и inbox/ (если включены в "
    "include_dirs). search — найти заметки/тикеты/сообщения, ask — ответ по "
    "ним с цитатами [N], get_note — прочитать заметку целиком, reindex — "
    "обновить индекс, kg_* — структурированные факты с историей."
)


def _find_note(project, schema, note_id: str, cfg) -> Path | None:
    from .. import notes as notes_mod

    for path in notes_mod.iter_note_paths(
        project,
        schema,
        cfg.include_dirs,
        skip_names=cfg.include_skip_names,
        skip_dirs=cfg.include_skip_dirs,
    ):
        rel = notes_mod.rel_for_path(path, project)
        if note_id in (path.stem, rel, rel[: -len(".md")]):
            return path
    return None


def build_server(
    project: str | None = None,
    *,
    cfg=None,
    index: Index | None = None,
) -> MCPServer:
    from .. import schema as schema_mod

    cfg = cfg or load_config(project)
    index = index or Index(cfg)
    kg = KG(cfg.project / "vault" / ".kg" / "graph.sqlite3")
    schema = schema_mod.load(cfg.schema_path) if cfg.schema_path.is_file() else None

    server = MCPServer(name="vault", version=__version__, instructions=INSTRUCTIONS)

    @server.tool(description="Поиск по vault, tickets и inbox (если включены). source=vault|tickets|inbox — фильтр по коллекции. mode=snippets (по умолчанию, компактно) или full (чанк целиком).")
    def search(query: str, top_k: int = 8, mode: str = "snippets", source: str = "") -> str:
        hits = index.search(query, top_k=top_k, source=source or None)
        if not hits:
            return "ничего не найдено"
        limit = int(index.cfg.retrieval.get("snippet_chars") or 400)
        out = []
        for i, h in enumerate(hits, start=1):
            text = h.text if mode == "full" else snippet(h.text, query, limit)
            out.append(f"[{i}] {h.title} ({h.rel}, score {h.score:.3f})\n{text}")
        return "\n\n".join(out)

    @server.tool(description="Ответ на вопрос по vault, tickets и inbox с inline-цитатами [N] и источниками (локальная LLM; без неё — честный фолбэк в сниппеты).")
    def ask(question: str) -> str:
        hits = index.search(question, top_k=int(index.cfg.ask.get("max_snippets", 6)))
        text, used_llm = ask_mod.answer(question, hits, index.cfg)
        if used_llm and hits:
            srcs = "\n".join(
                f"  [{i}] {h.title} — {h.rel}" for i, h in enumerate(hits, start=1)
            )
            return f"{text}\n\nИсточники:\n{srcs}"
        return text

    @server.tool(description="Прочитать заметку/тикет/сообщение целиком по id (имя файла без .md) или rel-пути.")
    def get_note(note_id: str) -> str:
        path = _find_note(index.cfg.project, schema, note_id, index.cfg) if schema else None
        if path is None:
            return f"заметка {note_id!r} не найдена"
        return path.read_text(encoding="utf-8", errors="replace")

    @server.tool(description="Состояние индекса: заметки/чанки/режим (hybrid|bm25-only), KG-статистика.")
    def status() -> str:
        st = index.status()
        ks = kg.stats()
        sources = ", ".join(f"{k} {v}" for k, v in sorted(st.sources.items()))
        return (
            f"заметки: {st.notes_indexed}/{st.notes_total} (ждут {st.pending_notes})\n"
            f"источники: {sources or '—'}\n"
            f"чанки: {st.chunks}, векторов: {st.vectors}, режим: {st.mode}\n"
            f"KG: entities {ks['entities']}, triples {ks['triples']} (открытых {ks['open']})\n"
            f"индекс: {st.index_dir} ({human_bytes(st.index_bytes)})"
        )

    @server.tool(description="Переиндексировать vault: mode=incremental (изменившиеся) или all (полная пересборка).")
    def reindex(mode: str = "incremental") -> str:
        res = index.reindex(all=(mode == "all"))
        if res.get("notes_changed") == 0 and res.get("notes_removed", 0) == 0:
            msg = f"индекс актуален ({res.get('chunks_total', 0)} чанков)"
            if res.get("metadata_updated"):
                msg += f", обновлено метаданных {res['metadata_updated']}"
            if res.get("vectors_updated"):
                msg += f", догнано векторов {res['vectors_updated']}"
            return msg
        msg = (
            f"переиндексировано заметок: {res['notes_changed']}"
            + (f", удалено {res['notes_removed']}" if res.get("notes_removed") else "")
            + f", чанков {res.get('chunks_total', 0)}, векторов обновлено {res.get('vectors_updated', 0)}"
            + (f", метаданных {res['metadata_updated']}" if res.get("metadata_updated") else "")
            + (f", {res['seconds']} с" if res.get("seconds") is not None else "")
        )
        if res.get("vector_stale"):
            msg += "\n! ollama недоступна — вектора отложены (bm25-only)"
        return msg

    # ---------------- KG ----------------

    @server.tool(description="KG: добавить сущность (kind — тип: person/project/metric…).")
    def kg_add_entity(name: str, kind: str = "") -> str:
        e = kg.add_entity(name, kind)
        return f"сущность {e.name!r} ({e.kind or 'без типа'}) записана"

    @server.tool(description="KG: добавить факт (subject, predicate, object) с датой начала действия; предыдущий открытый факт по этой паре закрывается автоматически.")
    def kg_add_fact(
        subject: str,
        predicate: str,
        object: str,
        valid_from: str = "",
        valid_to: str = "",
        source: str = "",
    ) -> str:
        f = kg.add_fact(subject, predicate, object, valid_from or None, valid_to or None, source)
        return f"факт: {f.subject} {f.predicate} {f.object} (с {f.valid_from})"

    @server.tool(description="KG: запрос фактов; at=YYYY-MM-DD — что было истинно на дату.")
    def kg_query(subject: str = "", predicate: str = "", at: str = "") -> str:
        facts = kg.query(subject, predicate, at or None)
        if not facts:
            return "фактов не найдено"
        return "\n".join(_fact_line(f) for f in facts)

    @server.tool(description="KG: временная шкала фактов сущности.")
    def kg_timeline(subject: str) -> str:
        facts = kg.timeline(subject)
        if not facts:
            return f"по {subject!r} фактов нет"
        return "\n".join(_fact_line(f) for f in facts)

    return server


def _fact_line(f) -> str:
    period = f.valid_from if not f.valid_to else f"{f.valid_from}…{f.valid_to}"
    src = f" [{f.source}]" if f.source else ""
    return f"{f.subject} — {f.predicate} → {f.object}  ({period}){src}"


def run_server(project: str | None = None) -> None:
    cfg = load_config(project)
    # Lifetime-lock: пока MCP жив, CLI-reindex не должен открывать Chroma
    # параллельно (ticket-006 — иначе SIGSEGV на больших корпусах).
    mcp_lock = acquire_mcp_lock(cfg.index_dir)
    if mcp_lock is None:
        print(
            "[mcp] ! другой MCP уже держит индекс — работаю без lifetime-lock",
            file=sys.stderr,
            flush=True,
        )
    # Один Index на процесс: его делят тулсы и watcher (bug-022).
    index = Index(cfg)
    watcher = None
    if cfg.watcher.get("enabled", True):
        watcher = VaultWatcher(cfg, index=index)
        watcher.start()
        print(f"[mcp] watcher запущен ({cfg.project})", file=sys.stderr, flush=True)
    try:
        build_server(cfg=cfg, index=index).run("stdio")
    finally:
        if watcher:
            watcher.stop()
        release_mcp_lock(mcp_lock)

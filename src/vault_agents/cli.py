"""CLI vault-agents: init / status / doctor / reindex / search / ask / lint / mcp / watch."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from . import config as config_mod
from . import schema as schema_mod
from .rag.index import Index, MCPActive, human_bytes, snippet

MIN_SCHEMA = """{
  "version": 1,
  "naming": "{YYYYMMDD}-{type}-{slug}.md",
  "id": "имя файла без .md",
  "types": {
    "note": {
      "dir": "10_Notes",
      "statuses": ["actual", "stale", "archived"],
      "required": ["id", "type", "status", "title", "created"],
      "optional": ["tags", "related", "source"]
    }
  },
  "created_format": "YYYY-MM-DD",
  "skip_dirs": ["00_System", "90_Archive"],
  "state": { "path": "STATE.md", "max_lines": 120, "required_fields": ["updated"] }
}
"""


def _project(args) -> Path:
    return config_mod.find_project(getattr(args, "project", None))


# ---------------------------------------------------------------- init ----

def _ensure_skeleton(project: Path) -> list[str]:
    """Минимальный fallback-скелет. Канонический шаблон — во внешнем scaffold'е
    (ticket-051); сюда попадаем только на проектах, созданных
    без него."""
    created: list[str] = []
    vault = project / "vault"
    for d in ("00_System", "10_Notes", "90_Archive"):
        p = vault / d
        if not p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            created.append(str(p.relative_to(project)))
    schema_p = project / config_mod.SCHEMA_REL
    if not schema_p.is_file():
        schema_p.write_text(MIN_SCHEMA, encoding="utf-8")
        created.append(str(config_mod.SCHEMA_REL))
    return created


def _ensure_mcp_json(project: Path) -> str:
    """MCP-сервер проекта: одна общая установка vault-agents, per-project —
    только запись в .mcp.json (существующие серверы не трогаем)."""
    path = project / ".mcp.json"
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    servers = data.setdefault("mcpServers", {})
    servers["vault"] = {
        "command": sys.executable,
        "args": ["-m", "vault_agents.mcp", "--project", str(project)],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path.relative_to(project))


def _ensure_gitignore(project: Path) -> bool:
    """.gitignore проектов уже игнорирует индексы; дописываем строку, если
    файл есть (новый не создаём)."""
    path = project / ".gitignore"
    line = "vault/.index/"
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if line in text:
        return False
    path.write_text(text.rstrip("\n") + f"\n{line}\n", encoding="utf-8")
    return True


def cmd_init(args) -> int:
    project = _project(args)
    project.mkdir(parents=True, exist_ok=True)
    created = _ensure_skeleton(project)
    cfg_path = config_mod.write_default_config(project)
    mcp = _ensure_mcp_json(project)
    gitignore = _ensure_gitignore(project)

    print(f"vault-agents init: {project}")
    print(f"  скелет:      {'создано ' + ', '.join(created) if created else 'уже полный'}")
    print(f"  конфиг:      {cfg_path.relative_to(project)}")
    print(f"  MCP:         {mcp} (сервер vault)")
    if gitignore:
        print("  .gitignore:  добавлено vault/.index/")
    return 0


# -------------------------------------------------------------- status ----

def cmd_status(args) -> int:
    cfg = config_mod.load_config(getattr(args, "project", None))
    st = Index(cfg).status()
    last = "-" if st.last_reindex is None else time.strftime(
        "%Y-%m-%d %H:%M", time.localtime(st.last_reindex)
    )
    if args.json:
        print(
            json.dumps(
                {
                    "project": str(cfg.project),
                    "notes_total": st.notes_total,
                    "notes_indexed": st.notes_indexed,
                    "pending_notes": st.pending_notes,
                    "chunks": st.chunks,
                    "vectors": st.vectors,
                    "mode": st.mode,
                    "vector_stale": st.vector_stale,
                    "last_reindex": st.last_reindex,
                    "embedding_model": st.embedding_model,
                    "index_dir": st.index_dir,
                    "sources": st.sources,
                    "index_bytes": st.index_bytes,
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(f"проект:      {cfg.project}")
    print(f"заметки:     {st.notes_indexed}/{st.notes_total} в индексе" + (f", ждут {st.pending_notes}" if st.pending_notes else ""))
    sources = ", ".join(f"{k} {v}" for k, v in sorted(st.sources.items())) or "—"
    print(f"источники:   {sources}")
    print(f"чанки:       {st.chunks} (векторов {st.vectors})")
    print(f"режим:       {st.mode}" + (" (вектора отложены — ollama недоступна)" if st.vector_stale else ""))
    print(f"обновлён:    {last}")
    print(f"индекс:      {st.index_dir} ({human_bytes(st.index_bytes)})")
    return 0


# -------------------------------------------------------------- doctor ----

def cmd_doctor(args) -> int:
    ok = True
    print(f"vault-agents {__version__}  python {sys.version.split()[0]}")
    try:
        project = _project(args)
        print(f"✓ проект: {project}")
    except config_mod.VaultNotFound as e:
        print(f"✗ проект: {e}")
        return 1
    cfg = config_mod.load_config(project)

    try:
        schema = schema_mod.load(cfg.schema_path)
        types = ", ".join(sorted(schema.types)) or "—"
        print(f"✓ схема: {cfg.schema_path.relative_to(project)} (типы: {types})")
    except schema_mod.SchemaError as e:
        print(f"✗ схема: {e}")
        ok = False
        schema = None

    from .rag import embeddings

    if embeddings.ping(cfg):
        print(f"✓ ollama: {cfg.embedding.get('base_url')}")
        if embeddings.has_model(cfg):
            print(f"✓ модель эмбеддингов: {cfg.embedding.get('model')}")
        else:
            print(f"✗ модель эмбеддингов отсутствует: {cfg.embedding.get('model')} (ollama pull)")
            ok = False
    else:
        print(f"✗ ollama недоступна: {cfg.embedding.get('base_url')} — поиск будет bm25-only")
        ok = False

    try:
        st = Index(cfg).status()
        print(f"• индекс: {st.mode}, заметок {st.notes_indexed}/{st.notes_total}, чанков {st.chunks}")
    except Exception as e:  # noqa: BLE001 — doctor обязан пережить любой сбой индекса
        print(f"✗ индекс: {e}")
        ok = False

    if schema is not None:
        bad = 0
        for path in _iter_notes(project, schema):
            rel = path.relative_to(project / "vault").as_posix()
            probs = schema_mod.validate_note(schema, rel, path.read_text(encoding="utf-8", errors="replace"))
            if probs:
                bad += 1
        if bad:
            print(f"• lint: {bad} заметок с замечаниями (vault-agents lint)")
        else:
            print("✓ lint: все заметки по схеме")
    return 0 if ok else 1


def _iter_notes(project: Path, schema: schema_mod.Schema):
    from . import notes as notes_mod

    return notes_mod.iter_note_paths(project, schema)


# ------------------------------------------------------------- reindex ----

def cmd_reindex(args) -> int:
    cfg = config_mod.load_config(getattr(args, "project", None))
    changed = [args.changed] if args.changed else None
    try:
        res = Index(cfg).reindex(all=args.all, changed=changed, verbose=not args.quiet)
    except MCPActive as e:
        if args.json:
            print(json.dumps({"skipped": True, "reason": str(e)}, ensure_ascii=False))
        elif not args.quiet:
            print(f"reindex пропущен: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if res.get("notes_changed") == 0 and res.get("notes_removed", 0) == 0:
        if not args.quiet:
            total = res.get("chunks_total", res.get("chunks", 0))
            extra = []
            if res.get("metadata_updated"):
                extra.append(f"обновлено метаданных {res['metadata_updated']}")
            if res.get("vectors_updated"):
                extra.append(f"догнано векторов {res['vectors_updated']}")
            suffix = f"; {', '.join(extra)}" if extra else ""
            print(f"индекс актуален: {total} чанков{suffix}")
        return 0
    print(
        f"переиндексировано: заметок {res.get('notes_changed', 0)}"
        + (f" (+удалено {res['notes_removed']})" if res.get("notes_removed") else "")
        + f", чанков {res.get('chunks_total', 0)}, векторов обновлено {res.get('vectors_updated', 0)}"
        + (f", метаданных {res['metadata_updated']}" if res.get("metadata_updated") else "")
        + (f", {res['seconds']} с" if res.get("seconds") is not None else "")
    )
    if res.get("vector_stale"):
        print("  ! ollama недоступна: вектора отложены, поиск пока bm25-only")
    return 0


# -------------------------------------------------------------- search ----

def cmd_search(args) -> int:
    cfg = config_mod.load_config(getattr(args, "project", None))
    hits = Index(cfg).search(
        args.query, top_k=args.top_k, mode=args.mode, source=getattr(args, "source", None)
    )
    if args.json:
        print(json.dumps([h.__dict__ for h in hits], ensure_ascii=False))
        return 0
    if not hits:
        print("ничего не найдено")
        return 0
    limit = int((cfg.retrieval.get("snippet_chars") or 400))
    for i, h in enumerate(hits, start=1):
        text = h.text if args.full else snippet(h.text, args.query, limit)
        print(f"[{i}] {h.title}  ({h.rel})  score={h.score:.4f}")
        print(f"    {text}")
        print()
    return 0


# ----------------------------------------------------------------- ask ----

def cmd_ask(args) -> int:
    from . import ask as ask_mod

    cfg = config_mod.load_config(getattr(args, "project", None))
    count = args.snippets or int(cfg.ask.get("max_snippets", 6))
    hits = Index(cfg).search(args.question, top_k=count)
    text, used_llm = ask_mod.answer(args.question, hits, cfg)
    show_sources = bool(hits) and text != ask_mod.NOT_FOUND_TEXT
    if args.json:
        print(json.dumps({
            "answer": text,
            "llm": used_llm,
            "sources": [h.__dict__ for h in hits] if show_sources else [],
        }, ensure_ascii=False))
        return 0
    print(text)
    if show_sources:
        print("\nИсточники:")
        for i, h in enumerate(hits[:count], start=1):
            print(f"  [{i}] {h.title} — {h.rel}")
    return 0


# ---------------------------------------------------------------- lint ----

def cmd_lint(args) -> int:
    cfg = config_mod.load_config(getattr(args, "project", None))
    schema = schema_mod.load(cfg.schema_path)
    bad = 0
    total = 0
    for path in _iter_notes(cfg.project, schema):
        rel = path.relative_to(cfg.project / "vault").as_posix()
        total += 1
        probs = schema_mod.validate_note(schema, rel, path.read_text(encoding="utf-8", errors="replace"))
        if probs:
            bad += 1
            print(f"✗ {rel}")
            for p in probs:
                print(f"    {p}")
    print(f"проверено {total} заметок, замечаний {bad}")
    return 1 if bad else 0


# ----------------------------------------------------------------- mcp ----

def cmd_mcp(args) -> int:
    from .mcp.server import run_server

    run_server(getattr(args, "project", None))
    return 0


def cmd_watch(args) -> int:
    from .watch import run_watcher

    run_watcher(getattr(args, "project", None))
    return 0


# ------------------------------------------------------------------ CLI ----

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vault-agents", description="Ядро памяти проекта: RAG + MCP + KG")
    p.add_argument("--version", action="version", version=f"vault-agents {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, help_: str, fn):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--project", help="корень проекта (по умолчанию — вверх от cwd)")
        s.set_defaults(func=fn)
        return s

    add("init", "досоздать скелет/конфиг/.mcp.json (идемпотентно)", cmd_init)

    s = add("status", "состояние индекса", cmd_status)
    s.add_argument("--json", action="store_true")

    add("doctor", "проверка окружения", cmd_doctor)

    s = add("reindex", "переиндексация", cmd_reindex)
    s.add_argument("--all", action="store_true", help="полная пересборка")
    s.add_argument("--changed", metavar="FILE", help="точечно: одна заметка (для хука авто-реиндекса)")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--json", action="store_true")

    s = add("search", "поиск по vault, tickets и inbox", cmd_search)
    s.add_argument("query")
    s.add_argument("--top-k", type=int, default=None)
    s.add_argument("--mode", choices=["hybrid", "bm25", "vector"], default=None)
    s.add_argument("--source", default=None,
                   help="фильтр по коллекции (vault | tickets | inbox | <include_dir>)")
    s.add_argument("--full", action="store_true", help="печатать чанк целиком, не сниппет")
    s.add_argument("--json", action="store_true")

    s = add("ask", "ответ по vault с цитатами", cmd_ask)
    s.add_argument("question")
    s.add_argument("--snippets", type=int, default=None)
    s.add_argument("--json", action="store_true")

    add("lint", "проверка заметок по схеме", cmd_lint)
    add("mcp", "MCP-сервер (stdio)", cmd_mcp)
    add("watch", "авто-реиндекс (watchdog)", cmd_watch)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

"""Сбор заметок vault и внешних коллекций: обход, разбор, чанки, sha256.

Внешние коллекции (ticket-004, реализация F): `tickets/` и `inbox/` лежат вне
`vault/`, но индексируются вместе с ним. Файлы не переносятся — это контракт
проекта; индексируются по `include_dirs` из конфига.
"""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import frontmatter, schema as schema_mod

# Внешние коллекции: тип по расположению, если фронтматтера нет.
SOURCE_TYPES = {"tickets": "ticket", "inbox": "inbox", "papers": "paper", "runs": "run"}

# Якорь страницы в PDF-производном markdown (ticket-009).
PAGE_RE = re.compile(r"^\s*<!--\s*page:\s*(\d+)\s*-->\s*$", re.MULTILINE | re.IGNORECASE)

_STATUS_RE = re.compile(
    r"^\s*(?:\*\*(?:Status|Статус):\*\*|(?:Status|Статус):)\s*(.+)$",
    re.MULTILINE | re.IGNORECASE,
)

# Нормализация статуса для ранжирования: сырые статусы legacy свободные,
# поэтому вес применяем по группе, а не по точному значению (ticket-005).
_CLOSED_TOKENS = (
    "done", "closed", "cancelled", "canceled", "resolved", "answered",
    "rejected", "completed", "archived", "superseded",
    "готов", "закрыт", "заверш", "отмен", "решён", "решен",
)
_OPEN_TOKENS = ("open", "actual", "new", "открыт", "актуал", "нов")
_PROGRESS_TOKENS = (
    "in_progress", "in progress", "wip", "accepted", "planned", "decided",
    "approved", "blocked", "в работе", "принят", "план",
)


def normalize_status(raw: str) -> str:
    """open | in_progress | closed | unknown — для status_weights."""
    s = (raw or "").strip().lower()
    if not s:
        return "unknown"
    closed = any(t in s for t in _CLOSED_TOKENS)
    open_ = any(t in s for t in _OPEN_TOKENS)
    progress = any(t in s for t in _PROGRESS_TOKENS)
    if closed and open_:
        return "in_progress"  # смешанный статус legacy («P1–P4 DONE; skill OPEN»)
    if closed:
        return "closed"
    if open_:
        return "open"
    if progress:
        return "in_progress"
    return "unknown"


@dataclass
class Chunk:
    id: str          # "<rel>#<n>" — уникален по всему проекту (ticket-004)
    note_id: str
    rel: str         # путь от vault/ для заметок vault, от корня проекта — для внешних
    text: str
    title: str
    type: str
    source: str = "vault"  # vault | tickets | inbox | <первый сегмент include_dirs>
    status: str = ""
    status_norm: str = "unknown"
    page: int | None = None  # номер страницы для PDF-производных источников


@dataclass
class Note:
    rel: str
    path: Path
    note_id: str
    type: str
    title: str
    status: str
    sha256: str
    source: str = "vault"
    tags: tuple[str, ...] = ()
    excluded: bool = False
    status_norm: str = "unknown"
    chunks: list[Chunk] = field(default_factory=list)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def rel_for_path(path: Path, project: Path) -> str:
    """Rel-путь для индекса: внутри vault — от vault, снаружи — от проекта."""
    vault = project / "vault"
    try:
        return path.relative_to(vault).as_posix()
    except ValueError:
        return path.relative_to(project).as_posix()


def source_for_path(path: Path, project: Path) -> str:
    """Источник чанка: vault или первый сегмент пути от корня проекта."""
    try:
        path.relative_to(project / "vault")
        return "vault"
    except ValueError:
        return path.relative_to(project).parts[0]


def iter_note_paths(
    project: Path,
    schema: schema_mod.Schema,
    include_dirs: list[str] | None = None,
    *,
    skip_names: set[str] | None = None,
    skip_dirs: set[str] | None = None,
) -> Iterator[Path]:
    """Все .md заметки: vault (кроме skip_dirs схемы) + include_dirs проекта.

    Внешние каталоги задаются относительно корня проекта (tickets, inbox).
    Во внешних пропускаются служебные имена (`readme.md`), подкаталоги
    (`archive/`) и любые сегменты из skip_dirs схемы. Каталоги за пределами
    проекта игнорируются — индекс остаётся project-scoped.
    """
    vault = project / "vault"
    project_r = project.resolve()
    skip_names = {n.lower() for n in (skip_names or ())}
    skip_dirs = {d.lower() for d in (skip_dirs or ())}
    schema_skip = {d.lower() for d in (schema.skip_dirs or ())}
    seen: set[Path] = set()
    vault_rels: set[str] = set()

    if vault.is_dir():
        for path in sorted(vault.rglob("*.md")):
            rel = path.relative_to(vault).as_posix()
            if schema_mod.skip_rel(schema, rel):
                continue
            rp = path.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            vault_rels.add(rel)
            yield path

    for inc in include_dirs or []:
        try:
            base = (project / inc).resolve()
        except OSError:
            continue
        if not base.is_dir():
            continue
        if not base.is_relative_to(project_r):
            # project-scoped: внешний абсолютный путь не индексируем
            continue
        for path in sorted(base.rglob("*.md")):
            rel = path.relative_to(project_r).as_posix()
            parts = Path(rel).parts
            if rel in vault_rels:
                # проектный путь совпал с rel заметки vault (папка vault
                # называется как include_dir) — chunk id был бы не уникален
                print(
                    f"[notes] ! {rel}: внешний файл совпал по rel с заметкой vault — пропущен",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            if path.name.lower() in skip_names:
                continue
            if any(p.lower() in skip_dirs for p in parts[:-1]):
                continue
            if any(p.lower() in schema_skip for p in parts[:-1]):
                continue
            rp = path.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            yield path


def parse_tags(raw: str) -> tuple[str, ...]:
    """Теги из фронтматтера: «trash, foo» или YAML-flow «[trash, foo]».

    Фронтматтер-парсер минимальный (зеркало Go-гейта), поэтому flow-список
    приходит строкой в скобках — снимаем их здесь.
    """
    s = (raw or "").strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return tuple(
        t.strip().strip("'\"").lower() for t in s.split(",") if t.strip().strip("'\"")
    )


def _title_from_body(body: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return ""


def _status_from_body(body: str) -> str:
    m = _STATUS_RE.search(body)
    return m.group(1).strip() if m else ""


def read_note(
    path: Path,
    project: Path,
    schema: schema_mod.Schema,
    *,
    exclude_tags: set[str] | None = None,
    build_chunks: bool = True,
    chunk_size: int = 1200,
    chunk_overlap: int = 150,
    chunk_boundary: str = "paragraph",
) -> Note:
    """Заметка + чанки. Битый фронтматтер не роняет индексацию: заметка
    попадает в индекс с тем, что есть (гейт записи — отдельный внешний инструмент).

    Внешние файлы (tickets/inbox) могут быть без фронтматтера: тип берётся из
    расположения, заголовок — из первого `# …`, статус — из `**Status:**`.
    """
    rel = rel_for_path(path, project)
    source = source_for_path(path, project)
    text = path.read_text(encoding="utf-8", errors="replace")
    note_id = path.stem
    title, status, ntype, tags = "", "", "", ()
    body = text
    try:
        fm = frontmatter.parse(text)
        title = fm.get("title")
        status = fm.get("status")
        ntype = fm.get("type")
        tags = parse_tags(fm.get("tags", ""))
        body = fm.body
    except frontmatter.FrontmatterError:
        pass

    if not ntype:
        ntype = SOURCE_TYPES.get(source, "")
    if not title:
        title = _title_from_body(body) or note_id
    if not status and source != "vault":
        status = _status_from_body(body)

    exclude_tags = exclude_tags or set()
    excluded = bool(set(tags) & exclude_tags)
    status_norm = normalize_status(status)

    chunks: list[Chunk] = []
    if not excluded and build_chunks:
        pieces: list[tuple[int | None, str]]
        if chunk_boundary == "page":
            pieces = chunk_text_pages(body, size=chunk_size, overlap=chunk_overlap)
        else:
            pieces = [
                (None, c)
                for c in chunk_text(body, size=chunk_size, overlap=chunk_overlap)
            ]
        chunks = [
            Chunk(
                id=f"{rel}#{i}",
                note_id=note_id,
                rel=rel,
                text=c,
                title=title,
                type=ntype,
                source=source,
                status=status,
                status_norm=status_norm,
                page=page,
            )
            for i, (page, c) in enumerate(pieces)
        ]
    return Note(
        rel=rel,
        path=path,
        note_id=note_id,
        type=ntype,
        title=title,
        status=status,
        sha256=sha256_text(text),
        source=source,
        tags=tags,
        excluded=excluded,
        status_norm=status_norm,
        chunks=chunks,
    )


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Чанкование по абзацам с добором до size; огромный абзац режется жёстко
    с перекрытием overlap. Пустой текст → один пустой чанк не создаём."""
    paras = [p.strip() for p in text.split("\n\n")]
    paras = [p for p in paras if p]
    if not paras:
        return []

    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(p) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            step = max(size - overlap, 1)
            for i in range(0, len(p), step):
                chunks.append(p[i : i + size])
            continue
        if not buf:
            buf = p
        elif len(buf) + 2 + len(p) <= size:
            buf = f"{buf}\n\n{p}"
        else:
            chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    return chunks


def split_pages(text: str) -> list[tuple[int | None, str]]:
    """Разбить текст по якорям `<!-- page: N -->` на сегменты (page, text).

    Текст до первого якоря — (None, …). Пустые сегменты пропускаются.
    Без якорей — один сегмент (None, text): режим page эквивалентен
    обычному чанкованию.
    """
    matches = list(PAGE_RE.finditer(text))
    if not matches:
        return [(None, text)]
    out: list[tuple[int | None, str]] = []
    # текст до первого якоря — обычно заголовок/шапка первой страницы,
    # приклеиваем его к первой странице, чтобы не терять контекст
    pre = text[: matches[0].start()].strip()
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        if i == 0 and pre:
            body = f"{pre}\n\n{body}" if body else pre
        if body:
            out.append((int(m.group(1)), body))
    return out


def chunk_text_pages(
    text: str, size: int = 1200, overlap: int = 150
) -> list[tuple[int | None, str]]:
    """Чанкование с запретом пересекать границу страницы (ticket-009).

    Каждый сегмент страницы чанкуется отдельно; возвращаются пары
    (page, chunk). Для текста без якорей поведение совпадает с chunk_text.
    """
    out: list[tuple[int | None, str]] = []
    for page, body in split_pages(text):
        for c in chunk_text(body, size=size, overlap=overlap):
            out.append((page, c))
    return out

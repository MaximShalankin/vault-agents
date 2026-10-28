"""Контракт заметок: vault_schema.json и валидация.

Зеркало внешнего Go-гейта записи (ticket-051): набор проверок и формулировки
совпадают, чтобы deny хука и lint CLI говорили одно и то же. Схема — данные
проекта: расширение типов не требует правок кода ни здесь, ни в Go.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import frontmatter

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DEFAULT_NAMING = "{YYYYMMDD}-{type}-{slug}.md"


class SchemaError(Exception):
    pass


@dataclass
class NoteType:
    dir: str
    statuses: list[str] = field(default_factory=list)
    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    enums: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Schema:
    raw: dict
    types: dict[str, NoteType]
    naming: str = DEFAULT_NAMING
    created_format: str = "YYYY-MM-DD"
    skip_dirs: list[str] = field(default_factory=lambda: ["00_System", "90_Archive"])
    state: dict = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return Path(self.path).parent

    path: str = ""


def load(path: str | Path) -> Schema:
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise SchemaError(f"{p}: схема не найдена") from e
    except json.JSONDecodeError as e:
        raise SchemaError(f"{p}: невалидный JSON: {e}") from e

    types = {}
    for name, t in (raw.get("types") or {}).items():
        types[name] = NoteType(
            dir=t.get("dir", ""),
            statuses=list(t.get("statuses") or []),
            required=list(t.get("required") or []),
            optional=list(t.get("optional") or []),
            enums=dict(t.get("enums") or {}),
        )
    return Schema(
        raw=raw,
        types=types,
        naming=raw.get("naming") or DEFAULT_NAMING,
        created_format=raw.get("created_format") or "YYYY-MM-DD",
        skip_dirs=list(raw.get("skip_dirs") or ["00_System", "90_Archive"]),
        state=dict(raw.get("state") or {}),
        path=str(p),
    )


def skip_rel(schema: Schema, rel: str) -> bool:
    first = rel.split("/", 1)[0]
    return first in schema.skip_dirs


def parse_note_name(stem: str, schema: Schema) -> tuple[str, str, str]:
    """{YYYYMMDD}-{type}-{slug} → (date, type, slug); ValueError с причиной."""
    if len(stem) < 10 or not stem[:8].isdigit():
        raise ValueError(f"имя {stem}.md не по шаблону {schema.naming}")
    try:
        datetime.strptime(stem[:8], "%Y%m%d")
    except ValueError as e:
        raise ValueError(f"дата в имени {stem[:8]!r} неверна") from e
    rest = stem[8:]
    if not rest.startswith("-"):
        raise ValueError(f"имя {stem}.md не по шаблону {schema.naming}")
    rest = rest[1:]
    typ = ""
    for t in schema.types:
        if rest.startswith(t + "-") and len(t) > len(typ):
            typ = t
    if not typ:
        known = ", ".join(sorted(schema.types))
        raise ValueError(f"тип из имени {stem}.md не объявлен в vault_schema.json (types: {known})")
    slug = rest[len(typ) + 1 :]
    if not slug or not SLUG_RE.match(slug):
        raise ValueError(f"слаг {slug!r} не по конвенции (строчные латинские, цифры, дефис)")
    return stem[:8], typ, slug


def _date_layout(fmt: str) -> str:
    return (fmt or "YYYY-MM-DD").replace("YYYY", "%Y").replace("MM", "%m").replace("DD", "%d")


def validate_note(schema: Schema, rel: str, content: str) -> list[str]:
    """Проверки заметки против схемы. Пустой список — валидна.

    Набор и порядок — как в vault.go:
    имя → папка типа → фронтматтер → id/type/created → status → required → enums.
    """
    problems: list[str] = []
    base = Path(rel).name
    stem = base[: -len(Path(base).suffix)] if Path(base).suffix else base

    try:
        _, typ, _ = parse_note_name(stem, schema)
    except ValueError as e:
        return [str(e)]

    expected_dir = schema.types[typ].dir
    actual_dir = str(Path(rel).parent)
    if actual_dir != expected_dir.rstrip("/"):
        problems.append(f"тип {typ!r} обязан лежать в {expected_dir}/ (сейчас {actual_dir})")

    try:
        fm = frontmatter.parse(content)
    except frontmatter.FrontmatterError as e:
        return problems + [str(e)]

    if fm.get("id") and fm.get("id") != stem:
        problems.append(f"id {fm.get('id')!r} не совпадает с именем файла {stem!r}")
    if fm.get("type") and fm.get("type") != typ:
        problems.append(f"type {fm.get('type')!r} не совпадает с типом из имени файла ({typ!r})")
    if fm.get("created"):
        try:
            datetime.strptime(fm.get("created"), _date_layout(schema.created_format))
        except ValueError:
            problems.append(f"created {fm.get('created')!r} не по формату {schema.created_format}")

    t = schema.types[typ]
    if fm.get("status") and t.statuses and fm.get("status") not in t.statuses:
        problems.append(f"status {fm.get('status')!r} не из словаря типа {typ}: {', '.join(t.statuses)}")
    for f in t.required:
        if not fm.get(f, "").strip():
            problems.append(f'нет обязательного поля "{f}"')
    for f, allowed in t.enums.items():
        v = fm.get(f)
        if v and v not in allowed:
            problems.append(f"{f} {v!r} не из словаря: {', '.join(allowed)}")

    return problems

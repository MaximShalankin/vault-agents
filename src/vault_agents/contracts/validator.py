"""Валидатор контрактов tickets + inbox (ticket-012).

Правила — в JSON-спеках рядом (inbox-proto-1.json, tickets-v1.json);
здесь только механика разбора. Режимы (решение владельца 2026-11-02):
new — строгие гейты (enum, обязательные поля); edit — мягкий legacy
(синонимы валидны, неизвестное/отсутствие статуса — предупреждение).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from pathlib import Path

from .. import frontmatter

_TICKET_STATUS_VALUES: list[str] = []  # кэш из спеки (см. _spec)

_SPECS: dict[str, dict] = {}


def load_spec(name: str) -> dict:
    """Спецификация по имени файла ('inbox-proto-1.json' | 'tickets-v1.json')."""
    if name not in _SPECS:
        _SPECS[name] = json.loads(
            (resources.files("vault_agents.contracts") / name).read_text(encoding="utf-8")
        )
    return _SPECS[name]


def ticket_statuses() -> list[str]:
    spec = load_spec("tickets-v1.json")
    return list(spec["header"]["required"]["Status"]["enum"])


def _status_synonyms() -> dict[str, str]:
    """{синоним-в-нижнем-регистре: значение enum}; enum тоже входит."""
    syn: dict[str, str] = {}
    for canonical, variants in load_spec("tickets-v1.json")["legacy"]["status_synonyms"].items():
        syn[canonical.lower()] = canonical
        for v in variants:
            syn[v.lower()] = canonical
    return syn


# --------------------------------------------------------------- разбор ----

# <NNN> — цифры, опц. одиночная буква-подсерия (027a/b/c — конвенция корпуса),
# опц. .N — подтикет, опц. -слаг
_TICKET_NAME_RE = re.compile(r"^ticket-\d+[a-z]?(\.\d+)?(-[a-z0-9-]+)?\.md$")
_INBOX_NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_from-([a-z0-9-]+)_((?:re-)?[a-z0-9-]+)\.md$")
_STATUS_LINE_RE = re.compile(r"^\s*\**\s*(?:status|статус)\s*\**\s*:\s*(.+)$", re.IGNORECASE)
_DATE_LINE_RE = re.compile(r"^\s*\**\s*(?:date|дата)\s*\**\s*:\s*(.+)$", re.IGNORECASE)
_LEADING_NOISE_RE = re.compile(r"^[^a-zA-Zа-яА-ЯёЁ]+")


def slugify_from(value: str) -> str:
    """Ключ реестра → слаг [a-z0-9-]. Точное зеркало ProjectSlug во внешнем хуке записи (inbox.go): lower; всё вне [a-z0-9] → одиночный дефис (повторы
    сливаются); ведущие/хвостовые дефисы срезаются. CamelCase не разрывается.
    """
    s = value.strip().lower()
    out: list[str] = []
    for ch in s:
        if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).rstrip("-")


def _parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        return None


def _first_header_value(text: str, regex: re.Pattern) -> str | None:
    """Первая подходящая строка-шапка в первых 15 строках файла.

    Снимает markdown-украшения с краёв значения: «**Date:** ** 2026…**»
    (закрывающие звёздочки после двоеточия), бэктики, подчёркивания.
    """
    for line in text.split("\n")[:15]:
        m = regex.match(line)
        if m:
            return m.group(1).strip().strip("*`_ ").strip()
    return None


def normalize_status(raw: str) -> str | None:
    """Значение статуса → enum; None — не распознано.

    Терпим к украшениям: «🔄 IN PROGRESS (дата) — проза», «`resolved`»,
    «done (2026-10-18)» — берётся самый длинный префикс, совпадающий со
    словарём enum+синонимов (регистр и пробел/дефис эквивалентны).
    """
    low = _LEADING_NOISE_RE.sub("", raw).strip().lower()
    if not low:
        return None
    keys = sorted(_status_synonyms(), key=len, reverse=True)
    for key in keys:
        k = key.replace("-", " ")
        if low.startswith(k) or low.startswith(key):
            rest = low[len(key) :]
            if not rest or not (rest[0].isalnum() or rest[0] in "_-"):
                return _status_synonyms()[key]
    return None


# --------------------------------------------------------------- отчёт ----


@dataclass
class Report:
    valid: bool = True
    kind: str = ""
    status_normalized: str | None = None
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def problem(self, msg: str) -> None:
        self.valid = False
        self.problems.append(msg)

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)


# --------------------------------------------------------------- tickets ----


def validate_ticket(name: str, text: str, *, new_file: bool) -> Report:
    """Тикет tickets/ticket-*.md против tickets-v1.json."""
    rep = Report(kind="ticket")
    statuses = ticket_statuses()

    if not name.startswith("ticket-"):
        rep.warning("вне серии ticket-*: контракт не применяется (backlog-запись)")
        return rep
    if not _TICKET_NAME_RE.match(name):
        # «ticket-027a-seo.md» строгим паттерном читается как 027 + слаг
        # «a-seo» — валиден; сюда попадают имена с верхним регистром/кириллицей.
        msg = f"имя {name} не по паттерну ticket-<NNN>[-<slug>].md ([a-z0-9-])"
        if new_file:
            rep.problem(msg)
            return rep
        rep.warning(msg + " — легаси, читать можно")

    raw_status = _first_header_value(text, _STATUS_LINE_RE)
    if raw_status is None:
        if new_file:
            rep.problem(f"нет строки статуса: **Status:** <{' | '.join(statuses)}> сразу после # заголовка")
        else:
            rep.warning("нет строки статуса — валидно для легаси, доска покажет «—»")
    else:
        canonical = normalize_status(raw_status)
        rep.status_normalized = canonical
        first_word = raw_status.split()[0] if raw_status.split() else "—"
        if canonical is None:
            msg = f"статус {first_word!r} не распознан (enum+синонимы: tickets-v1.json)"
            (rep.problem if new_file else rep.warning)(msg)
        elif new_file:
            # строгий гейт новых: синоним допустим только в легаси; регистр
            # значения не важен (OPEN ≈ open), «IN PROGRESS (проза)» ≈ IN_PROGRESS
            compact = re.sub(r"\s+", "_", _LEADING_NOISE_RE.sub("", raw_status).strip().lower())
            if not compact.startswith(canonical.lower()):
                rep.problem(
                    f"Status {first_word!r} — синоним; новые тикеты пишут enum-значением ({', '.join(statuses)})"
                )

    raw_date = _first_header_value(text, _DATE_LINE_RE)
    if raw_date is None:
        if new_file:
            rep.problem("нет строки **Date:** YYYY-MM-DD (дата создания)")
    elif _parse_date(raw_date.split()[0] if raw_date.split() else "") is None:
        (rep.problem if new_file else rep.warning)(f"Date {raw_date!r} не по формату YYYY-MM-DD")

    if new_file and not text.lstrip().startswith("#"):
        rep.warning("нет строки заголовка «# …»")
    return rep


# ---------------------------------------------------------------- inbox ----


def validate_inbox_message(name: str, text: str, *, new_file: bool) -> Report:
    """Сообщение inbox/ против inbox-proto-1.json. Протокол зрелый — гейт
    строг и к новым, и к правкам (кроме compat-кейсов как предупреждений)."""
    rep = Report(kind="inbox")

    if name.lower() == "readme.md":
        rep.warning("readme.md в inbox — не сообщение, контракт не применяется")
        return rep

    m = _INBOX_NAME_RE.match(name)
    if not m:
        rep.problem("имя не по паттерну YYYY-MM-DD_from-<slug>_<topic>.md (слаги [a-z0-9-], нижний регистр)")
        return rep
    if _parse_date("-".join(m.group(1, 2, 3))) is None:
        rep.problem(f"дата в имени {m.group(1)}-{m.group(2)}-{m.group(3)} несуществующая")
        return rep

    try:
        fm = frontmatter.parse(text)
    except frontmatter.FrontmatterError as e:
        rep.problem(str(e))
        return rep

    if fm.get("proto") != "1":
        rep.problem(f"proto {fm.get('proto')!r} != 1")
    kind = fm.get("kind")
    if kind not in ("request", "info", "receipt"):
        rep.problem(f"kind {kind!r} не из словаря: request | info | receipt")
        kind = ""
    if not fm.get("from"):
        rep.problem("пустое поле from (ключ реестра отправителя)")
    else:
        expected = slugify_from(fm.get("from"))
        if m.group(4) != expected:
            rep.problem(f"слаг в имени from-{m.group(4)} != нормализованного from: (from-{expected})")
    if not fm.get("to"):
        rep.problem("пустое поле to (ключ реестра получателя)")
    if not fm.get("date") or _parse_date(fm.get("date")) is None:
        rep.problem(f"date {fm.get('date')!r} не по формату YYYY-MM-DD")

    status = fm.get("status")
    if kind == "receipt":
        if status:
            rep.warning("у kind=receipt не должно быть status (compat: читатель игнорирует)")
        if not m.group(5).startswith("re-"):
            rep.problem("квитанция: topic-слаг в имени обязан начинаться с re-")
    else:
        allowed = load_spec("inbox-proto-1.json")["frontmatter"]["status"][
            "kind_request" if kind == "request" else "kind_info"
        ]["values"]
        if not status:
            rep.problem(f"нет обязательного поля status (для kind={kind}: {', '.join(allowed)})")
        elif status not in allowed:
            rep.problem(f"status {status!r} не из словаря kind={kind}: {', '.join(allowed)}")
        rep.status_normalized = status or None

    if fm.get("due") and _parse_date(fm.get("due")) is None:
        rep.warning(f"due {fm.get('due')!r} не по формату YYYY-MM-DD")

    for link_field in ("parent", "reply"):
        v = fm.get(link_field)
        if v:
            if ".." in v:
                rep.problem(f"{link_field} {v!r}: выход за пределы проекта (..) запрещён")
            elif ":" not in v:
                rep.warning(f"{link_field} {v!r} не по формату проект:путь")
    for ref in [r.strip() for r in fm.get("refs", "").split(",") if r.strip()]:
        if ".." in ref:
            rep.problem(f"refs {ref!r}: выход за пределы проекта (..) запрещён")
        elif ":" not in ref:
            rep.warning(f"refs {ref!r} не по формату проект:путь")

    if kind == "request" and "Что требуется" not in text:
        rep.warning("kind=request без раздела «## Что требуется» (по шаблону обязателен)")
    return rep


# ------------------------------------------------------------ фасад CLI ----


def validate_file(path: str | Path, *, new_file: bool = False, kind: str = "auto") -> Report:
    """Валидация по расположению: tickets/ → тикет, inbox/ → сообщение.
    inbox/archive/ не проверяется (архив вне контракта). kind=ticket|inbox —
    явное указание (внешний хук валидирует контент, застейженный во временном
    файле: родительский каталог может не называться tickets/inbox)."""
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    if kind in ("ticket", "inbox"):
        fn = validate_ticket if kind == "ticket" else validate_inbox_message
        return fn(p.name, text, new_file=new_file)
    parent = p.parent.name
    if parent == "tickets":
        return validate_ticket(p.name, text, new_file=new_file)
    if parent == "archive" and p.parent.parent.name == "inbox":
        rep = Report(kind="inbox-archive")
        rep.warning("inbox/archive/ не проверяется (архив вне контракта)")
        return rep
    if parent == "inbox":
        return validate_inbox_message(p.name, text, new_file=new_file)
    rep = Report()
    rep.problem(f"{p}: не tickets/ и не inbox/ — контракт не применим")
    return rep

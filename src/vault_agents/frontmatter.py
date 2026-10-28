"""Минимальный YAML-поднабор фронтматтера заметок.

Зеркало разбора внешнего Go-гейта записи (ticket-051): «ключ: значение» + списки
«- элемент», значения — строки. Библиотека YAML для фронтматтера не тянется:
контракт должен читаться одинаково и Go-гейтом, и Python-линтером.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class FrontmatterError(ValueError):
    pass


@dataclass
class Frontmatter:
    fields: dict[str, str] = field(default_factory=dict)
    body: str = ""

    def get(self, key: str, default: str = "") -> str:
        return self.fields.get(key.lower(), default)


def _strip_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def parse(content: str) -> Frontmatter:
    """Разбирает фронтматтер; ошибка — с человекочитаемой причиной (для deny)."""
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        raise FrontmatterError("нет фронтматтера: заметка обязана начинаться со строки ---")

    fields: dict[str, str] = {}
    cur_list = ""
    for i, raw in enumerate(lines[1:], start=2):
        line = raw.strip()
        if line == "---":
            return Frontmatter(fields=fields, body="\n".join(lines[i:]))
        if not line:
            continue
        if line.startswith("- "):
            if not cur_list:
                raise FrontmatterError(f"фронтматтер, строка {i}: элемент списка без ключа")
            item = _strip_quotes(line[2:].strip())
            prev = fields.get(cur_list, "")
            fields[cur_list] = f"{prev}, {item}" if prev else item
            continue
        key, sep, val = line.partition(":")
        if not sep:
            raise FrontmatterError(f"фронтматтер, строка {i}: ожидается «ключ: значение», получено {line!r}")
        key = key.strip().lower()
        fields[key] = _strip_quotes(val.strip())
        cur_list = key if fields[key] == "" else ""

    raise FrontmatterError("фронтматтер не закрыт: нет второй строки ---")

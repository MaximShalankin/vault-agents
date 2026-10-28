"""Контракт заметок: фронтматтер и валидация по схеме.

Кейсы зеркалят Go-гейт записи — Go-гейт и Python-lint обязаны
соглашаться (ticket-051).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vault_agents import frontmatter, schema as schema_mod

from .conftest import SCHEMA, note_text


@pytest.fixture()
def schema(tmp_path: Path) -> schema_mod.Schema:
    p = tmp_path / "vault_schema.json"
    p.write_text(json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8")
    return schema_mod.load(p)


def test_parse_frontmatter_basic():
    fm = frontmatter.parse("---\nid: x\ntitle: Тест: с двоеточием\n---\n\nтело\n")
    assert fm.get("id") == "x"
    assert fm.get("title") == "Тест: с двоеточием"
    assert fm.body.strip() == "тело"


def test_parse_frontmatter_list():
    fm = frontmatter.parse("---\ntags:\n- a\n- b\n---\nтело")
    assert fm.get("tags") == "a, b"


def test_parse_frontmatter_errors():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse("без фронтматтера")
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse("---\nid: x\n")  # не закрыт


def test_validate_valid(schema):
    content = note_text("20261003-note-hello", "Привет", "тело")
    assert schema_mod.validate_note(schema, "10_Notes/20261003-note-hello.md", content) == []


@pytest.mark.parametrize(
    "rel,content,match",
    [
        ("20_Results/20261003-note-hello.md", None, "обязан лежать в 10_Notes/"),
        ("10_Notes/20261003-notex-hello.md", None, "не объявлен в vault_schema.json"),
        ("10_Notes/20261303-note-hello.md", None, "дата в имени"),
        ("10_Notes/20261003-note-Hello_World.md", None, "слаг"),
        ("10_Notes/20261003-note-hello.md", "без фронтматтера", "нет фронтматтера"),
        (
            "10_Notes/20261003-note-hello.md",
            "---\nid: 20261003-note-hello\ntype: note\nstatus: actual\ncreated: 2026-10-03\n---\nтело\n",
            'нет обязательного поля "title"',
        ),
        (
            "10_Notes/20261003-note-hello.md",
            note_text("20261003-note-hello", "T", "b", extra="status: bogus\n"),
            "не из словаря типа note",
        ),
        (
            "10_Notes/20261003-note-hello.md",
            note_text("20260101-note-other", "T", "b"),
            "не совпадает с именем файла",
        ),
        (
            "10_Notes/20261003-note-hello.md",
            "---\nid: 20261003-note-hello\ntype: note\nstatus: actual\ntitle: T\ncreated: 03.10.2026\n---\nтело\n",
            "не по формату",
        ),
    ],
)
def test_validate_problems(schema, rel, content, match):
    content = content if content is not None else note_text("20261003-note-hello", "T", "b")
    problems = schema_mod.validate_note(schema, rel, content)
    assert any(match in p for p in problems), problems


def test_validate_enum(schema):
    content = note_text(
        "20261003-result-run", "Замер", "тело", ntype="result", extra="outcome: maybe\n"
    )
    problems = schema_mod.validate_note(schema, "20_Results/20261003-result-run.md", content)
    assert any("не из словаря" in p for p in problems), problems

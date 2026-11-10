"""Гейт контрактов tickets + inbox (ticket-012, порция 3).

Кейсы — из обследования корпуса 2026-11-01 (ticket-012 §0.2–0.3):
зоопарк статусов, RU-шапки, эмодзи/проза, compat-кейсы inbox.
"""

from __future__ import annotations

import json

from vault_agents import contracts
from vault_agents.cli import main


def ticket_text(status: str = "OPEN", date: str = "2026-11-02") -> str:
    return (
        "# Ticket-013: Тест\n\n"
        f"**Status:** {status}\n"
        f"**Date:** {date}\n"
        "**Related:** —\n\n## 0. Цель\n\nТест.\n"
    )


# ----------------------------------------------------------- имена/шапка ----


def test_ticket_new_valid_all_enum(tmp_path):
    for st in ("OPEN", "IN_PROGRESS", "DONE", "DEFERRED", "CANCELLED"):
        rep = contracts.validate_ticket("ticket-013.md", ticket_text(st), new_file=True)
        assert rep.valid, (st, rep.problems)
        assert rep.status_normalized == st


def test_ticket_new_missing_status_denied():
    rep = contracts.validate_ticket("ticket-013.md", "# T\n\n## Цель\n", new_file=True)
    assert not rep.valid


def test_ticket_new_synonym_rejected_strict():
    for bad in ("resolved", "завершён", "todo", "отложен"):
        rep = contracts.validate_ticket("ticket-013.md", ticket_text(bad), new_file=True)
        assert not rep.valid, bad  # новые — только enum-токен (синонимы — легаси)
    # регистр значения не важен, проза после значения допустима
    for ok in ("done", "open", "IN PROGRESS (после ревью)"):
        rep = contracts.validate_ticket("ticket-013.md", ticket_text(ok), new_file=True)
        assert rep.valid, (ok, rep.problems)


def test_ticket_edit_synonyms_valid_and_normalized():
    """Легаси-правка: синоним валиден, маппится в enum (легаси-тикеты)."""
    cases = {
        "done": "DONE",
        "`resolved`": "DONE",
        "завершён": "DONE",
        "выполнен (приёмка 2026-09-26)": "DONE",
        "in progress": "IN_PROGRESS",
        "in_progress": "IN_PROGRESS",
        "в работе": "IN_PROGRESS",
        "открыт": "OPEN",
        "todo": "OPEN",
        "отложен до января": "DEFERRED",
    }
    for raw, norm in cases.items():
        rep = contracts.validate_ticket("ticket-007.md", ticket_text(raw), new_file=False)
        assert rep.valid, (raw, rep.problems)
        assert rep.status_normalized == norm, raw


def test_ticket_edit_ru_header_and_emoji_prose():
    """**Статус:** эквивалентен; эмодзи/проза не ломают первый токен."""
    body = (
        "# ticket-032: Demo\n\n"
        "**Статус:** 🔄 IN PROGRESS (2026-11-01) — Ф4 выполнена, Ф1 ждёт циклов\n"
        "**Date:** 2026-11-01\n"
    )
    rep = contracts.validate_ticket("ticket-032-demo-stability.md", body, new_file=False)
    assert rep.valid
    assert rep.status_normalized == "IN_PROGRESS"


def test_ticket_synonyms_1_1_0_by_external_receipt():
    """Parked/морфология — по внешней квитанции + набору кейсов."""
    cases = {
        "**PARKED (2026-10-26)** — bb-путь остановлен": "DEFERRED",
        "опубликовано (2026-10-28)": "DONE",
        "выполнено": "DONE",
        "решено": "DONE",
        "ANSWERED (2026-10-08) — ответ отправлен квитанцией": "DONE",
        "готов (2026-10-18)": "DONE",
        "готов к реализации": "OPEN",  # longest-prefix выигрывает у «готов»
    }
    for raw, norm in cases.items():
        rep = contracts.validate_ticket("ticket-047.md", ticket_text(raw), new_file=False)
        assert rep.status_normalized == norm, raw
        assert rep.valid, raw


def test_ticket_edit_broken_status_line_warning_only():
    """«**Status: DEFERRED**» (разорванные звёздочки, кейс доски): правка не блок."""
    body = (
        "# Ticket-005: Cherry-pick\n\n"
        "**Status: DEFERRED**\n"
        "**Date:** 2026-10-01\n"
    )
    rep = contracts.validate_ticket("ticket-005.md", body, new_file=False)
    assert rep.valid, rep.problems  # мягкий legacy: warning, не deny
    assert rep.status_normalized == "DEFERRED"


def test_ticket_no_status_edit_valid():
    """Тикеты без статуса: правки не требуют его добавить."""
    rep = contracts.validate_ticket("ticket-004.md", "# t\n\n**Date:** 2026-11-01\n", new_file=False)
    assert rep.valid
    assert rep.status_normalized is None
    assert rep.warnings


def test_ticket_names():
    ok_new = ["ticket-013.md", "ticket-13.md", "ticket-3.1.md", "ticket-032-demo-stability.md", "ticket-011-review.md"]
    for n in ok_new:
        assert contracts.validate_ticket(n, ticket_text(), new_file=True).valid, n
    # буквенная серия читается строгим паттерном как NNN + слаг: валидна
    assert contracts.validate_ticket("ticket-027a-seo-raw-metrics.md", ticket_text(), new_file=True).valid
    # верхний регистр/кириллица в имени: новые — deny, правка — предупреждение
    assert not contracts.validate_ticket("ticket-027A-SEO.md", ticket_text(), new_file=True).valid
    rep = contracts.validate_ticket("ticket-006_отчёт.md", ticket_text(), new_file=False)
    assert rep.valid and rep.warnings, rep.problems
    rep = contracts.validate_ticket("tooling-backlog.md", "# очередь\n", new_file=False)
    assert rep.valid and not rep.problems  # вне серии — контракт не применяется


def test_ticket_new_bad_date_denied_edit_warns():
    assert not contracts.validate_ticket("ticket-013.md", ticket_text(date="01.11.2026"), new_file=True).valid
    assert contracts.validate_ticket("ticket-002.md", ticket_text(date="01.11.2026"), new_file=False).valid


# ---------------------------------------------------------------- inbox ----


def inbox_text(kind: str = "request", status: str = "actual", extra: str = "") -> str:
    return (
        "---\n"
        "proto: 1\n"
        f"kind: {kind}\n"
        "from: demo_2026\n"
        "to: vault-agents\n"
        "date: 2026-11-01\n"
        + (f"status: {status}\n" if status else "")
        + extra +
        "---\n\n# Тема\n\n## Предпосылка\n\nКонтекст.\n\n## Суть\n\nСуть.\n"
        + ("\n\n## Что требуется\n\nСделать.\n" if kind == "request" else "")
    )


def test_inbox_request_info_receipt_valid(tmp_path):
    for kind, name in (
        ("request", "2026-11-01_from-demo-2026_advice.md"),
        ("info", "2026-11-01_from-demo-2026_status.md"),
        ("receipt", "2026-11-01_from-demo-2026_re-advice.md"),
    ):
        status = "" if kind == "receipt" else "actual"
        rep = contracts.validate_inbox_message(name, inbox_text(kind, status), new_file=True)
        assert rep.valid, (kind, rep.problems)


def test_inbox_slug_matches_frontmatter():
    """Слаг имени = нормализованный from: (правило proto-1)."""
    text = inbox_text("info").replace("from: demo_2026", "from: demo_bot_SERVER")
    rep = contracts.validate_inbox_message(
        "2026-11-01_from-demo-bot-server_report.md", text, new_file=True
    )
    assert rep.valid, rep.problems
    bad = contracts.validate_inbox_message("2026-11-01_from-sanb_report.md", text, new_file=True)
    assert not bad.valid
    assert contracts.slugify_from("DemoNPU-API") == "demonpu-api"
    assert contracts.slugify_from("demo_ai") == "demo-ai"


def test_inbox_bad_names_and_dates():
    for name in (
        "20261028-idea-picnic.md",  # кейс §0.3: без from-, дата без дефисов
        "2026-10-28_from-DEMO-SERVER_info-8003.md",  # верхний регистр
        "2026-13-01_from-a_b.md",  # несуществующая дата
    ):
        rep = contracts.validate_inbox_message(name, inbox_text(), new_file=True)
        assert not rep.valid, name


def test_inbox_receipt_status_warning_compat():
    """Легаси-квитанции со status: compat — предупреждение, не deny."""
    rep = contracts.validate_inbox_message(
        "2026-10-26_from-demo-tool_re-s3-tpu-case-v1.md",
        inbox_text("receipt")
        .replace("from: demo_2026", "from: demo_tool")
        .replace("---\n\n# Тема", "status: in_progress\n---\n\n# Тема"),
        new_file=False,
    )
    assert rep.valid, rep.problems
    assert any("status" in w for w in rep.warnings)


def test_inbox_receipt_without_re_prefix_denied():
    rep = contracts.validate_inbox_message(
        "2026-11-01_from-demo-2026_answer.md", inbox_text("receipt"), new_file=True
    )
    assert not rep.valid  # квитанция обязана начинаться с re-


def test_inbox_bad_frontmatter_and_enums():
    no_fm = "# Тема\n\nБез фронтматтера.\n"
    rep = contracts.validate_inbox_message("2026-11-01_from-demo-2026_t.md", no_fm, new_file=True)
    assert not rep.valid
    bad_status = inbox_text("request", "done")  # не из словаря request
    rep = contracts.validate_inbox_message("2026-11-01_from-demo-2026_t.md", bad_status, new_file=True)
    assert not rep.valid
    info_stale = inbox_text("info", "stale")
    assert contracts.validate_inbox_message("2026-11-01_from-demo-2026_t.md", info_stale, new_file=True).valid


def test_inbox_info_resolved_valid_since_1_1_0():
    """3 legacy-кейса (info+resolved) — легализованы в 1.1.0."""
    spec = contracts.load_spec("inbox-proto-1.json")
    assert spec["spec_version"] == "1.1.0"
    assert "resolved" in spec["frontmatter"]["status"]["kind_info"]["values"]
    rep = contracts.validate_inbox_message(
        "2026-10-14_from-demo-service_riskconf-ticket-045.md",
        inbox_text("info", "resolved").replace("from: demo_2026", "from: demo_service"),
        new_file=True,
    )
    assert rep.valid, rep.problems
    assert rep.status_normalized == "resolved"


def test_cli_check_kind_override(tmp_path, capsys):
    """Хук валидирует контент во временном каталоге: --kind задаёт контракт,
    а имя файла хук даёт финальное (стейдж под именем ticket-NNN.md)."""
    staged = tmp_path / "ticket-013.md"
    staged.write_text(ticket_text("OPEN"), encoding="utf-8")
    assert main(["check", "--kind", "ticket", str(staged)]) == 0
    staged.write_text(ticket_text("отложен"), encoding="utf-8")
    assert main(["check", "--kind", "ticket", "--new", str(staged)]) == 1

    msg = tmp_path / "2026-11-01_from-demo-2026_advice.md"
    msg.write_text(inbox_text("request"), encoding="utf-8")
    assert main(["check", "--kind", "inbox", str(msg)]) == 0


def test_cli_spec_prints_contract(capsys):
    assert main(["spec", "tickets"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["contract"] == "tickets"
    assert "OPEN" in out["header"]["required"]["Status"]["enum"]
    assert main(["spec", "inbox"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["proto"] == 1
    assert "resolved" in out["frontmatter"]["status"]["kind_info"]["values"]


def test_inbox_traversal_links_denied():
    extra = "parent: vault-agents:../outside/secret.md\nreply: a:b\n"
    rep = contracts.validate_inbox_message(
        "2026-11-01_from-demo-2026_t.md", inbox_text("request", extra=extra), new_file=True
    )
    assert not rep.valid


# ------------------------------------------------------------------ CLI ----


def _run(tmp_path, capsys, *argv: str) -> int:
    code = main(["check", *argv])
    out = capsys.readouterr().out
    assert out or code  # smoke: что-то напечатано или не 0
    return code


def test_cli_check_autodetect_and_exit_codes(tmp_path, capsys):
    t = tmp_path / "tickets" / "ticket-001.md"
    t.parent.mkdir()
    t.write_text(ticket_text("OPEN"), encoding="utf-8")
    assert _run(tmp_path, capsys, str(t)) == 0

    t.write_text(ticket_text("otmenen-nope"), encoding="utf-8")
    assert _run(tmp_path, capsys, str(t)) == 0  # правка: неизвестный статус → warning, не deny

    t.write_text(ticket_text("завершён"), encoding="utf-8")
    assert _run(tmp_path, capsys, str(t)) == 0


def test_cli_check_new_denies_synonym(tmp_path, capsys):
    t = tmp_path / "tickets" / "ticket-002.md"
    t.parent.mkdir()
    t.write_text(ticket_text("resolved"), encoding="utf-8")
    assert _run(tmp_path, capsys, "--new", str(t)) == 1


def test_cli_check_json_output(tmp_path, capsys):
    msg = tmp_path / "inbox" / "2026-11-01_from-demo-2026_advice.md"
    msg.parent.mkdir()
    msg.write_text(inbox_text("request"), encoding="utf-8")
    assert main(["check", "--json", str(msg)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["kind"] == "inbox" and data[0]["valid"] and data[0]["status_normalized"] == "actual"


def test_cli_check_archive_skipped(tmp_path, capsys):
    arch = tmp_path / "inbox" / "archive" / "2026-10-01_from-x_y.md"
    arch.parent.mkdir(parents=True)
    arch.write_text("старьё без фронтматтера", encoding="utf-8")
    assert _run(tmp_path, capsys, str(arch)) == 0


def test_cli_check_wrong_location(tmp_path, capsys):
    f = tmp_path / "README.md"
    f.write_text("# x", encoding="utf-8")
    assert _run(tmp_path, capsys, str(f)) == 1


def test_cli_check_on_real_corpus_files(tmp_path, capsys):
    """Живой кейс: чистая шапка — валиден."""
    body = (
        "# ticket-003 — контрольная проверка vault\n\n"
        "**Status:** done\n**Created:** 2026-11-01\n**Updated:** 2026-11-01\n"
    )
    rep = contracts.validate_ticket("ticket-003.md", body, new_file=False)
    assert rep.valid and rep.status_normalized == "DONE"

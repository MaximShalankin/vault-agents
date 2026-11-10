"""ticket-013: init на пустом каталоге + машинная схема для bootstrap."""

from __future__ import annotations

import json

from vault_agents import config as config_mod
from vault_agents import contracts
from vault_agents.cli import main


def test_init_empty_dir_creates_skeleton(tmp_path, capsys):
    assert main(["init", "--project", str(tmp_path)]) == 0
    assert (tmp_path / "vault/00_System/vault_schema.json").is_file()
    assert (tmp_path / "vault/00_System/vault.config.yaml").is_file()
    assert (tmp_path / ".mcp.json").is_file()
    assert (tmp_path / "vault/10_Notes").is_dir()
    assert (tmp_path / "vault/90_Archive").is_dir()
    cfg = config_mod.load_config(tmp_path)
    assert cfg.project == tmp_path.resolve()


def test_init_creates_missing_project_dir(tmp_path, capsys):
    project = tmp_path / "new-project"
    assert main(["init", "--project", str(project)]) == 0
    assert (project / "vault/00_System/vault_schema.json").is_file()


def test_init_without_project_uses_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("VAULT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert (tmp_path / "vault/00_System/vault_schema.json").is_file()


def test_init_inside_existing_project_no_nested_vault(project, monkeypatch, capsys):
    monkeypatch.chdir(project / "vault/10_Notes")
    assert main(["init"]) == 0
    assert (project / "vault/00_System/vault_schema.json").is_file()
    assert not (project / "vault/10_Notes/vault").exists()


def test_spec_schema_after_init_matches_default(tmp_path, capsys):
    assert main(["init", "--project", str(tmp_path)]) == 0
    capsys.readouterr()  # init печатает прогресс; нужен только JSON spec
    assert main(["spec", "schema", "--project", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["source"] == "project"
    assert out["schema_present"] is True
    assert out["schema_path"] == "vault/00_System/vault_schema.json"
    assert out["schema_version"] == out["default_schema_version"] == 1
    assert out["matches_default"] is True
    assert out["schema"] == out["default_schema"]


def test_spec_schema_missing_returns_default(tmp_path, capsys):
    assert main(["spec", "schema", "--project", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["source"] == "default"
    assert out["schema_present"] is False
    assert out["schema_path"] == "vault/00_System/vault_schema.json"
    assert out["default_schema"]["version"] == 1
    assert out["matches_default"] is False


def test_spec_schema_json_flag(tmp_path, capsys):
    assert main(["spec", "schema", "--json", "--project", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["default_schema_version"] == 1


def test_spec_schema_project_custom_schema(project, capsys):
    assert main(["spec", "schema", "--project", str(project)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema_present"] is True
    assert out["schema_version"] == 1
    assert out["matches_default"] is False  # fixture schema: note + result
    assert out["schema"]["types"]["result"]["dir"] == "20_Results"


def test_spec_tickets_exposes_status_synonyms(capsys):
    assert main(["spec", "tickets"]) == 0
    out = json.loads(capsys.readouterr().out)
    syn = out["legacy"]["status_synonyms"]
    assert "resolved" in syn["DONE"]
    assert "опубликовано" in syn["DONE"]
    assert "parked" in syn["DEFERRED"]


def test_status_open_with_prose_normalizes():
    body = "# Ticket-001: T\n\n**Status:** open — ждёт ревью\n**Date:** 2026-11-07\n"
    rep = contracts.validate_ticket("ticket-001.md", body, new_file=False)
    assert rep.valid
    assert rep.status_normalized == "OPEN"

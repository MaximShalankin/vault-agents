"""Запуск MCP-сервера: python -m vault_agents.mcp --project DIR."""

from __future__ import annotations

import argparse

from .server import run_server


def main() -> None:
    p = argparse.ArgumentParser(prog="vault_agents.mcp")
    p.add_argument("--project", default=None, help="корень проекта (по умолчанию — от cwd)")
    args = p.parse_args()
    run_server(args.project)


if __name__ == "__main__":
    main()

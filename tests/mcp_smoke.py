#!/usr/bin/env python
"""Живой smoke MCP-сервера по stdio: initialize → tools/list → несколько вызовов.

Запуск: .venv/bin/python tests/mcp_smoke.py [--project DIR]
Не часть pytest-набора (нужен реальный проект и Ollama); используется как
приёмочная проверка фазы B.

Важно: stdout сервера занят протоколом, поэтому весь вывод — в stderr.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT = sys.argv[sys.argv.index("--project") + 1] if "--project" in sys.argv else str(Path(__file__).resolve().parents[1])


def send(proc: subprocess.Popen, msg: dict) -> None:
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def recv(proc: subprocess.Popen) -> dict:
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("сервер закрыл stdout")
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "id" in msg:  # пропускаем нотификации
            return msg


def call(proc: subprocess.Popen, rid: int, method: str, params: dict | None = None) -> dict:
    send(proc, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
    return recv(proc)


def main() -> int:
    log = sys.stderr
    proc = subprocess.Popen(
        [sys.executable, "-m", "vault_agents.mcp", "--project", PROJECT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        init = call(proc, 1, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "vault-agents-smoke", "version": "0"},
        })
        server = init["result"]["serverInfo"]
        print(f"initialize: {server.get('name')} {server.get('version')}", file=log)
        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        tools = call(proc, 2, "tools/list")["result"]["tools"]
        names = sorted(t["name"] for t in tools)
        print(f"tools: {', '.join(names)}", file=log)

        def tool(name: str, args: dict, rid: int) -> str:
            res = call(proc, rid, "tools/call", {"name": name, "arguments": args})
            content = res["result"]["content"]
            text = "\n".join(c.get("text", "") for c in content)
            print(f"\n--- {name} {args} ---\n{text[:600]}", file=log)
            return text

        tool("status", {}, 3)
        tool("search", {"query": "деградация при недоступной ollama", "top_k": 2}, 4)
        tool("ask", {"question": "Что происходит с поиском без Ollama?"}, 5)
        tool("kg_add_fact", {"subject": "vault-agents", "predicate": "фаза", "object": "B", "valid_from": "2026-10-03"}, 6)
        tool("kg_query", {"subject": "vault-agents"}, 7)
        tool("reindex", {"mode": "incremental"}, 8)
        return 0
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())

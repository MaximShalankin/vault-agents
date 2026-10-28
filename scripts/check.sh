#!/bin/sh
# Локальный quality-gate: pytest + compileall. Запуск: ./scripts/check.sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "нет .venv — сначала: uv sync" >&2
    exit 1
fi
cd "$ROOT"
echo "pytest…"
"$PY" -m pytest -q
echo "compileall…"
"$PY" -m compileall -q src tests
echo "ok"

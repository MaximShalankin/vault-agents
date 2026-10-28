#!/bin/sh
# Шим ~/bin/vault-agents → venv проекта
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/bin/vault-agents"
mkdir -p "$HOME/bin"
cat > "$BIN" <<SHIM
#!/bin/sh
exec "$ROOT/.venv/bin/vault-agents" "\$@"
SHIM
chmod +x "$BIN"
echo "шим установлен: $BIN → $ROOT/.venv/bin/vault-agents"

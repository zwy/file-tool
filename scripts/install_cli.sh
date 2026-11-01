#!/usr/bin/env bash
# 安装 file-tool 全局 CLI（幂等，可重复执行）
#
# - ~/.local/bin/file-tool  → launcher 脚本，指向本仓库的 .venv
#
# launcher 固定指向仓库内的解释器 + PYTHONPATH，仓库改动后无需重装。
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

BIN_DIR="${FILE_TOOL_BIN_DIR:-$HOME/.local/bin}"

mkdir -p "$BIN_DIR"

LAUNCHER="$BIN_DIR/file-tool"
cat > "$LAUNCHER" <<EOF
#!/bin/sh
# file-tool CLI launcher（由 scripts/install_cli.sh 生成，勿手改）
REPO="$ROOT"
export PYTHONPATH="\$REPO\${PYTHONPATH:+:\$PYTHONPATH}"
PY="\$REPO/.venv/bin/python"
[ -x "\$PY" ] || PY="python3"
exec "\$PY" -m file_tool.cli "\$@"
EOF
chmod +x "$LAUNCHER"

echo "file-tool CLI 已安装：$LAUNCHER"
echo "提示：若命令找不到，执行 source ~/.zshrc 或新开一个终端。"

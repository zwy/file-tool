#!/usr/bin/env bash
# 安装 file-tool 全局 CLI（幂等，可重复执行）
#
# - ~/.local/bin/file-tool  → launcher 脚本，指向本仓库的 .venv
#
# launcher 固定指向仓库内的解释器 + PYTHONPATH，仓库改动后无需重装。
# Windows 上有对应的 scripts\install_cli.ps1，语义一致。
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

ADD_TO_PATH=0
# 注意：macOS 自带的是 bash 3.2，`set -u` 下展开无参数的 "$@" 会报
# unbound variable 直接退出。所以先判个数，再进循环。
if [ "$#" -gt 0 ]; then
  for arg in "$@"; do
    case "$arg" in
      --add-to-path) ADD_TO_PATH=1 ;;
      -h|--help)
        echo "用法：$0 [--add-to-path]"
        echo "  --add-to-path   把 launcher 目录写进 shell 配置（当前用户）"
        exit 0
        ;;
      *) echo "未知参数：$arg" >&2; exit 2 ;;
    esac
  done
fi

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

if [ "$ADD_TO_PATH" -eq 1 ]; then
  # 当前用户的 shell 配置里加一条 PATH（幂等）
  case ":$PATH:" in
    *":$BIN_DIR:"*)
      echo "PATH 里已经有 $BIN_DIR，跳过。"
      ;;
    *)
      RC="$HOME/.zshrc"
      if [ -f "$HOME/.bashrc" ] && [ ! -f "$RC" ]; then
        RC="$HOME/.bashrc"
      fi
      touch "$RC"
      if ! grep -qF "$BIN_DIR" "$RC" 2>/dev/null; then
        printf '\n# file-tool CLI\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$RC"
        echo "已把 $BIN_DIR 写进 $RC"
      fi
      ;;
  esac
fi

echo "提示：若命令找不到，执行 source ~/.zshrc 或新开一个终端。"

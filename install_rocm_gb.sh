#!/usr/bin/env bash
set -euo pipefail

MONITOR_PATH="${ROCM_GB_MONITOR_PATH:-$HOME/rocm-monitor.py}"
BASHRC="${HOME}/.bashrc"
DEFAULT_MONITOR_URL="https://raw.githubusercontent.com/Qiuyc18/server-bootstrap/main/rocm-monitor.py"
MONITOR_URL="${ROCM_GB_MONITOR_URL:-$DEFAULT_MONITOR_URL}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_MONITOR="${SCRIPT_DIR}/rocm-monitor.py"

echo "==== 1. 安装 ROCm GPU 监控脚本 ===="
mkdir -p "$(dirname -- "$MONITOR_PATH")"

if [ -f "$LOCAL_MONITOR" ]; then
  install -m 755 "$LOCAL_MONITOR" "$MONITOR_PATH"
elif command -v curl >/dev/null 2>&1; then
  tmpfile="$(mktemp)"
  trap 'rm -f "$tmpfile"' EXIT
  curl -fL --connect-timeout 10 --retry 3 "$MONITOR_URL" -o "$tmpfile"
  install -m 755 "$tmpfile" "$MONITOR_PATH"
else
  echo "错误：远程安装需要 curl，请先安装 curl 后重试。" >&2
  exit 1
fi

echo "已安装: $MONITOR_PATH"

echo "==== 2. 添加 rocm-gb alias ===="
touch "$BASHRC"

if grep -qE '^[[:space:]]*alias[[:space:]]+rocm-gb=' "$BASHRC"; then
  echo "~/.bashrc 中已存在 rocm-gb alias，跳过追加"
else
  printf -v monitor_command 'python3 %q' "$MONITOR_PATH"
  printf -v alias_value "watch -n 1 '%s'" "$monitor_command"
  {
    printf '\n%s\n' '# server-bootstrap: rocm-gb'
    printf 'alias rocm-gb=%q\n' "$alias_value"
  } >>"$BASHRC"
  echo "已将 rocm-gb alias 添加到 ~/.bashrc"
fi

echo "==== 完成 ===="
echo "执行以下命令使 alias 在当前 shell 生效："
echo "  source ~/.bashrc"
echo "之后运行："
echo "  rocm-gb"

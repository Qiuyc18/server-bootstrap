#!/usr/bin/env bash
set -euo pipefail

MONITOR_PATH="${ROCM_GB_MONITOR_PATH:-$HOME/rocm-monitor.py}"
REFRESH_INTERVAL="${ROCM_GB_INTERVAL:-5}"
BASHRC="${HOME}/.bashrc"
DEFAULT_MONITOR_URL="https://raw.githubusercontent.com/Qiuyc18/server-bootstrap/main/rocm-monitor.py"
MONITOR_URL="${ROCM_GB_MONITOR_URL:-$DEFAULT_MONITOR_URL}"
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
LOCAL_MONITOR=""

if ! [[ "$REFRESH_INTERVAL" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "错误：ROCM_GB_INTERVAL 必须是正数秒数。" >&2
  exit 1
fi

if [ -n "$SCRIPT_PATH" ]; then
  SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
  LOCAL_MONITOR="${SCRIPT_DIR}/rocm-monitor.py"
fi

echo "==== 1. 安装 ROCm GPU 监控脚本 ===="
mkdir -p "$(dirname -- "$MONITOR_PATH")"

if [ -n "$LOCAL_MONITOR" ] && [ -f "$LOCAL_MONITOR" ]; then
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

MANAGED_MARKER="# server-bootstrap: rocm-gb"
printf -v monitor_command 'python3 %q' "$MONITOR_PATH"
printf -v alias_value "watch -n %s '%s'" "$REFRESH_INTERVAL" "$monitor_command"
printf -v alias_line 'alias rocm-gb=%q' "$alias_value"

if grep -qF "$MANAGED_MARKER" "$BASHRC"; then
  bashrc_tmp="$(mktemp)"
  replace_next_alias=0
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$line" = "$MANAGED_MARKER" ]; then
      printf '%s\n%s\n' "$MANAGED_MARKER" "$alias_line"
      replace_next_alias=1
    elif [ "$replace_next_alias" -eq 1 ] && [[ "$line" =~ ^[[:space:]]*alias[[:space:]]+rocm-gb= ]]; then
      replace_next_alias=0
    else
      replace_next_alias=0
      printf '%s\n' "$line"
    fi
  done <"$BASHRC" >"$bashrc_tmp"
  cp "$bashrc_tmp" "$BASHRC"
  rm -f "$bashrc_tmp"
  echo "已将 ~/.bashrc 中的 rocm-gb alias 更新为每 ${REFRESH_INTERVAL} 秒刷新"
elif grep -qE '^[[:space:]]*alias[[:space:]]+rocm-gb=' "$BASHRC"; then
  echo "~/.bashrc 中已存在 rocm-gb alias，跳过追加"
else
  {
    printf '\n%s\n' "$MANAGED_MARKER"
    printf '%s\n' "$alias_line"
  } >>"$BASHRC"
  echo "已将 rocm-gb alias 添加到 ~/.bashrc（每 ${REFRESH_INTERVAL} 秒刷新）"
fi

echo "==== 完成 ===="
echo "执行以下命令使 alias 在当前 shell 生效："
echo "  source ~/.bashrc"
echo "之后运行："
echo "  rocm-gb"

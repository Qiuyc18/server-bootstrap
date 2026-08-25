#!/usr/bin/env bash
set -euo pipefail

GH_PROXY_ENABLED=false
GH_PROXY_BASE="${GH_PROXY_BASE:-https://gh-proxy.com}"

usage() {
  cat <<'EOF'
用法: bash init.sh [选项]

选项:
  --gh_proxy  通过国内 GitHub 代理下载 GitHub 资源
  -h, --help  显示帮助
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --gh_proxy)
      GH_PROXY_ENABLED=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "错误：未知选项: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

github_url() {
  local url="$1"

  if [ "$GH_PROXY_ENABLED" = true ]; then
    printf '%s/%s\n' "${GH_PROXY_BASE%/}" "$url"
  else
    printf '%s\n' "$url"
  fi
}

if [ "$GH_PROXY_ENABLED" = true ]; then
  echo "GitHub 国内代理已启用: ${GH_PROXY_BASE%/}"
fi

# 检查是否能执行需要 root 权限的系统初始化。无权限时仍继续用户级初始化。
ROOT_CMD=()
if [ "$(id -u)" -eq 0 ]; then
  :
elif command -v sudo >/dev/null 2>&1 && sudo -v; then
  ROOT_CMD=(sudo)
else
  echo "提示：当前用户没有 sudo 权限，将跳过系统软件包安装。"
fi

# 非交互 apt：避免 needrestart / 内核待重启 等 whiptail 在 SSH 里弹窗
apt_get() {
  "${ROOT_CMD[@]}" env DEBIAN_FRONTEND=noninteractive \
    NEEDRESTART_MODE="${NEEDRESTART_MODE:-a}" apt-get "$@"
}

echo "==== 1. 安装基础工具 ===="
if [ "$(id -u)" -eq 0 ] || [ "${#ROOT_CMD[@]}" -gt 0 ]; then
  apt_get update
  apt_get install -y curl git xz-utils ca-certificates openssh-client
else
  echo "跳过基础工具安装；请联系管理员安装: curl git xz-utils ca-certificates openssh-client"
fi

echo "==== 2. 安装 ble.sh ===="

if [ ! -d "$HOME/.local/share/blesh" ]; then
  tmpdir="$(mktemp -d)"
  cd "$tmpdir"

  curl -fL --connect-timeout 10 --retry 3 \
    "$(github_url 'https://github.com/akinomyoga/ble.sh/releases/download/nightly/ble-nightly.tar.xz')" \
    -o ble-nightly.tar.xz

  tar xJf ble-nightly.tar.xz
  bash ble-nightly/ble.sh --install "$HOME/.local/share"
  rm -rf "$tmpdir"
  cd "$HOME"
else
  echo "ble.sh 已存在，跳过"
fi

if ! grep -q "blesh/ble.sh" "$HOME/.bashrc"; then
  echo 'source -- ~/.local/share/blesh/ble.sh' >> "$HOME/.bashrc"
fi

echo "==== 3. 安装 oh-my-bash ===="

if [ ! -d "$HOME/.oh-my-bash" ]; then
  git clone --depth=1 \
    "$(github_url 'https://github.com/ohmybash/oh-my-bash.git')" \
    "$HOME/.oh-my-bash"
  cp "$HOME/.oh-my-bash/templates/bashrc.osh-template" "$HOME/.bashrc.oh-my-bash"

  if ! grep -q ".oh-my-bash/oh-my-bash.sh" "$HOME/.bashrc"; then
    cat >> "$HOME/.bashrc" <<'EOF'

# oh-my-bash
export OSH="$HOME/.oh-my-bash"
OSH_THEME="font"
source "$OSH/oh-my-bash.sh"
EOF
  fi
else
  echo "oh-my-bash 已存在，跳过"
fi

echo "==== 4. 检查 SSH 密钥 ===="

if [ ! -f "$HOME/.ssh/id_ed25519" ]; then
  echo "未找到 ~/.ssh/id_ed25519。如需生成，请手动执行："
  echo "  ssh-keygen -t ed25519 -C \"$(whoami)@$(hostname)\" -f \"$HOME/.ssh/id_ed25519\""
else
  echo "SSH 密钥已存在"
fi

echo "==== 5. 安装 uv ===="

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf --connect-timeout 10 --retry 3 https://astral.sh/uv/install.sh | sh
else
  echo "uv 已存在，跳过"
fi

echo "==== 完成 ===="
echo "重新登录 shell，或者执行：source ~/.bashrc"

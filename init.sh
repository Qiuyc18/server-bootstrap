#!/usr/bin/env bash
set -euo pipefail

# 非交互 apt：避免 needrestart / 内核待重启 等 whiptail 在 SSH 里弹窗
apt_get() {
  sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE="${NEEDRESTART_MODE:-a}" apt-get "$@"
}

echo "==== 1. 安装基础工具 ===="
apt_get update
apt_get install -y curl git xz-utils ca-certificates openssh-client

echo "==== 2. 安装 ble.sh ===="

if [ ! -d "$HOME/.local/share/blesh" ]; then
  tmpdir="$(mktemp -d)"
  cd "$tmpdir"

  curl -fL --connect-timeout 10 --retry 3 \
    https://github.com/akinomyoga/ble.sh/releases/download/nightly/ble-nightly.tar.xz \
    -o ble-nightly.tar.xz

  tar xJf ble-nightly.tar.xz
  bash ble-nightly/ble.sh --install "$HOME/.local/share"
  rm -rf "$tmpdir"
else
  echo "ble.sh 已存在，跳过"
fi

if ! grep -q "blesh/ble.sh" "$HOME/.bashrc"; then
  echo 'source -- ~/.local/share/blesh/ble.sh' >> "$HOME/.bashrc"
fi

echo "==== 3. 安装 oh-my-bash ===="

if [ ! -d "$HOME/.oh-my-bash" ]; then
  git clone --depth=1 https://github.com/ohmybash/oh-my-bash.git "$HOME/.oh-my-bash"
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

echo "==== 4. 生成 ed25519 密钥 ===="

if [ ! -f "$HOME/.ssh/id_ed25519" ]; then
  mkdir -p "$HOME/.ssh"
  chmod 700 "$HOME/.ssh"
  ssh-keygen -t ed25519 -C "$(whoami)@$(hostname)" -f "$HOME/.ssh/id_ed25519" -N ""
else
  echo "SSH 密钥已存在，跳过"
fi

echo "==== 5. 安装 uv ===="

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf --connect-timeout 10 --retry 3 https://astral.sh/uv/install.sh | sh
else
  echo "uv 已存在，跳过"
fi

echo "==== 完成 ===="
echo "重新登录 shell，或者执行：source ~/.bashrc"
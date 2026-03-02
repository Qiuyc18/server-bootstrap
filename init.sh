#!/usr/bin/env bash

set -e

echo "==== 1. 安装 ble.sh ===="

if [ ! -d "$HOME/.local/share/blesh" ]; then
  curl -L https://github.com/akinomyoga/ble.sh/releases/download/nightly/ble-nightly.tar.xz | tar xJf -
  bash ble-nightly/ble.sh --install ~/.local/share
  echo 'source -- ~/.local/share/blesh/ble.sh' >> ~/.bashrc
  rm -rf ble-nightly
else
  echo "ble.sh 已存在，跳过"
fi

if ! grep -q "blesh/ble.sh" "$HOME/.bashrc"; then
  echo 'source -- ~/.local/share/blesh/ble.sh' >> "$HOME/.bashrc"
fi

echo "==== 2. 安装 oh-my-bash ===="

if [ ! -d "$HOME/.oh-my-bash" ]; then
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/ohmybash/oh-my-bash/master/tools/install.sh)"
else
  echo "oh-my-bash 已存在，跳过"
fi

echo "==== 3. 生成 ed25519 密钥 ===="

if [ ! -f "$HOME/.ssh/id_ed25519" ]; then
  mkdir -p "$HOME/.ssh"
  chmod 700 "$HOME/.ssh"
  ssh-keygen -t ed25519 -C "$(whoami)@$(hostname)" -f "$HOME/.ssh/id_ed25519" -N ""
else
  echo "SSH 密钥已存在，跳过"
fi

echo "==== 4. 安装 uv ===="
curl -LsSf https://astral.sh/uv/install.sh | sh

echo "==== 完成 ===="
echo "重新打开终端，或者执行：source ~/.bashrc"

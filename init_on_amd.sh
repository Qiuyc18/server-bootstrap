#!/usr/bin/env bash
set -euo pipefail

echo "==== 1. 安装基础工具 ===="
sudo apt update
sudo apt install -y curl git xz-utils ca-certificates openssh-client
sudo apt install -y openmpi-bin libopenmpi-dev
sudo ldconfig

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

echo "==== 4. 安装 uv ===="

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf --connect-timeout 10 --retry 3 https://astral.sh/uv/install.sh | sh
else
  echo "uv 已存在，跳过"
fi

echo "==== 5. 创建环境 uv ===="

mkdir -p ~/envs/vllm-rocm
cd ~/envs/vllm-rocm
uv venv --python 3.12 --seed .venv
source .venv/bin/activate

echo "==== 5.1 安装基础工具 ===="
uv pip install -U pip setuptools wheel packaging ninja cmake

echo "==== 5.2 安装 vllm ===="
export VLLM_ROCM_VARIANT=$(curl -s https://wheels.vllm.ai/rocm/nightly | \
    grep -oP 'rocm\d+' | head -1 | sed 's/%2B/+/g')
echo "VLLM_ROCM_VARIANT=${VLLM_ROCM_VARIANT}"
uv pip install --pre vllm \
    --extra-index-url https://wheels.vllm.ai/rocm/nightly/${VLLM_ROCM_VARIANT} \
    --index-strategy unsafe-best-match

echo "==== 完成 ===="
echo "重新登录 shell，或者执行：source ~/.bashrc"
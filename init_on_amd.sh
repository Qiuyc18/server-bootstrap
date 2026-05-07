#!/usr/bin/env bash
#
# AMD / MI 系列服务器：宿主机常为 ROCm 6.x（例如仅有 libhipblaslt.so.0），
# 而 vLLM 的 rocm722 等 wheel 依赖 ROCm 7（libhipblaslt.so.1），在裸机混装易冲突。
# 建议在宿主机用 Docker 跑 AMD 维护的 ROCm 7 + vLLM 镜像，避免升级整台机器的 ROCm 影响他人：
#   https://hub.docker.com/r/rocm/vllm-dev/tags
#
set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/server-bootstrap"
ENV_FILE="${CONFIG_DIR}/vllm-rocm.env"
DOCKER_RC="${CONFIG_DIR}/vllm-rocm-docker.sh"
BASHRC_MARKER="# server-bootstrap: vllm-rocm-docker (managed)"

echo "==== 1. 安装基础工具 ===="
sudo apt update
sudo apt install -y curl git xz-utils ca-certificates openssh-client
sudo apt install -y openmpi-bin libopenmpi-dev
sudo apt install -y docker.io
sudo ldconfig

if command -v systemctl >/dev/null 2>&1; then
  sudo systemctl enable --now docker 2>/dev/null || true
fi

if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  sudo usermod -aG docker "$USER"
  echo "已将当前用户加入 docker 组；生效需重新登录或执行: newgrp docker"
fi

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

echo "==== 5. vLLM + ROCm 7：Docker 参数与辅助命令 ===="

mkdir -p "$CONFIG_DIR"

if [ ! -f "$ENV_FILE" ]; then
  cat >"$ENV_FILE" <<'EOF'
# ---------------------------------------------------------------------------
# mi250-002 等节点：宿主机多为 ROCm 6.x（libhipblaslt.so.0），与 vLLM rocm722
# wheel 所需的 ROCm 7（libhipblaslt.so.1）不一致，勿在裸机 pip 混装 torch/vLLM。
# 请使用 AMD 官方镜像（内含配好的 ROCm 7 用户态）：
#   https://hub.docker.com/r/rocm/vllm-dev/tags
# 从 Tags 页选择合适 tag 后修改下一行（勿使用与宿主机 ROCm 混用的 nightly wheel）。
# ---------------------------------------------------------------------------

# 完整镜像名:tag，例如 rocm/vllm-dev:xxx（以 Docker Hub 当前标签为准）
VLLM_ROCM_DOCKER_IMAGE="rocm/vllm-dev:latest"

# 挂载进容器的 Hugging Face 缓存目录（宿主机路径）
VLLM_DOCKER_HF_CACHE="${HOME}/.cache/huggingface"

# 挂载为 /workspace 的工作目录（宿主机路径）
VLLM_DOCKER_WORKSPACE="${HOME}/work/vllm-rocm"

# 可选：额外 docker run 参数（数组展开，留空即可）
# 例: VLLM_DOCKER_EXTRA_ARGS=( -e MY_VAR=value )
VLLM_DOCKER_EXTRA_ARGS=()
EOF
  echo "已写入默认环境文件（可编辑）: $ENV_FILE"
else
  echo "已存在 $ENV_FILE ，跳过写入（保留你的修改）"
fi

if [ ! -f "$DOCKER_RC" ]; then
  {
    printf '%s\n' '# 由 init_on_amd.sh 安装；可在此调整 docker run 行为。'
    printf 'VLLM_ROCM_SB_ROOT=%q\n\n' "$CONFIG_DIR"
    cat <<'DOCKER_BODY'

vllm_rocm_shell() {
  # shellcheck source=/dev/null
  [ -f "$VLLM_ROCM_SB_ROOT/vllm-rocm.env" ] && . "$VLLM_ROCM_SB_ROOT/vllm-rocm.env"

  : "${VLLM_ROCM_DOCKER_IMAGE:?请在 vllm-rocm.env 中设置 VLLM_ROCM_DOCKER_IMAGE}"
  : "${VLLM_DOCKER_HF_CACHE:=${HOME}/.cache/huggingface}"
  : "${VLLM_DOCKER_WORKSPACE:=${HOME}/work/vllm-rocm}"

  mkdir -p "$VLLM_DOCKER_HF_CACHE" "$VLLM_DOCKER_WORKSPACE"

  local -a extra=()
  if [ "${#VLLM_DOCKER_EXTRA_ARGS[@]:-0}" -gt 0 ]; then
    extra=("${VLLM_DOCKER_EXTRA_ARGS[@]}")
  fi

  local -a cmd=( "$@" )
  if [ "${#cmd[@]}" -eq 0 ]; then
    cmd=( bash )
  fi

  docker run -it --rm \
    --network=host \
    --device=/dev/kfd \
    --device=/dev/dri \
    --ipc=host \
    --group-add=video \
    --group-add=render \
    -v "$VLLM_DOCKER_HF_CACHE:/root/.cache/huggingface" \
    -v "$VLLM_DOCKER_WORKSPACE:/workspace" \
    -w /workspace \
    "${extra[@]}" \
    "$VLLM_ROCM_DOCKER_IMAGE" \
    "${cmd[@]}"
}
DOCKER_BODY
  } >"$DOCKER_RC"
  echo "已写入 Docker 辅助脚本: $DOCKER_RC"
else
  echo "已存在 $DOCKER_RC ，跳过写入（保留你的修改）"
fi

if ! grep -qF "$BASHRC_MARKER" "$HOME/.bashrc" 2>/dev/null; then
  cat >>"$HOME/.bashrc" <<EOF

$BASHRC_MARKER
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
[ -f "$DOCKER_RC" ] && . "$DOCKER_RC"
EOF
  echo "已在 ~/.bashrc 中加入加载 $ENV_FILE 与 $DOCKER_RC"
else
  echo "~/.bashrc 中已有 server-bootstrap vLLM Docker 片段，跳过追加"
fi

echo "==== 完成 ===="
echo ""
echo "说明（mi250-002 / ROCm 版本）：宿主机 ROCm 6.x 与 vLLM rocm722 wheel 不兼容，"
echo "请在容器内使用 rocm/vllm-dev 镜像，勿依赖裸机 pip 安装 vLLM。"
echo ""
echo "下一步："
echo "  1. 编辑镜像 tag:  $ENV_FILE"
echo "  2. 拉取镜像:       docker pull \"\$(grep '^VLLM_ROCM_DOCKER_IMAGE=' $ENV_FILE | cut -d= -f2- | tr -d '\"')\""
echo "  3. 重新登录或:     source ~/.bashrc   （以及 newgrp docker 若刚加入 docker 组）"
echo "  4. 进入环境:       vllm_rocm_shell        # 默认 bash；也可 vllm_rocm_shell python …"
echo ""

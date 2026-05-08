#!/usr/bin/env bash
set -euo pipefail

# =========================
# Default config
# =========================

IMAGE="${IMAGE:-rocm/vllm-dev:vllm-v0.20.0-aiter-v0.1.13-rc2-rocm7.2-hotfix-init}"
CONTAINER_NAME="${CONTAINER_NAME:-qwen-vllm-rocm-test}"

CHECKPOINT_DIR="${CHECKPOINT_DIR:-/etc/moreh/checkpoint}"
CONTAINER_CHECKPOINT_DIR="${CONTAINER_CHECKPOINT_DIR:-/etc/moreh/checkpoint}"

CODE_SPACE=""
CONTAINER_CODE_DIR="/workspace/code"

HIP_DEVICES="${HIP_VISIBLE_DEVICES:-0}"
PORT="${PORT:-8000}"

MODEL_PATH="${MODEL_PATH:-/etc/moreh/checkpoint/Qwen/Qwen3.5-0.8B}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-qwen}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-4096}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.80}"

MODE="bash"
FORCE_RECREATE=0

# =========================
# Help
# =========================

usage() {
  cat <<EOF
Usage:
  ./run_vllm_docker.sh [options]

Options:
  --code_space <path>        Mount your project directory into container as /workspace/code
  --serve                    Start vLLM OpenAI API server
  --bash                     Enter container bash shell, default mode
  --model <path>             Model path inside container
                             Default: ${MODEL_PATH}
  --served_model_name <name> Served model name
                             Default: ${SERVED_MODEL_NAME}
  --port <port>              vLLM server port
                             Default: ${PORT}
  --hip_devices <ids>        HIP_VISIBLE_DEVICES, for example 0 or 0,1
                             Default: ${HIP_DEVICES}
  --force-recreate           Remove existing container named CONTAINER_NAME and create a new one
  -h, --help                 Show this help

If a container with the same CONTAINER_NAME already exists:
  - running: attach with \`docker exec\` (bash) instead of failing
  - stopped: \`docker start -ai\` to resume

Examples:
  ./run_vllm_docker.sh --code_space ~/server-bootstrap

  ./run_vllm_docker.sh \\
    --code_space ~/server-bootstrap \\
    --serve \\
    --model /etc/moreh/checkpoint/Qwen/Qwen3.5-0.8B \\
    --served_model_name qwen3.5-0.8b \\
    --port 8000 \\
    --hip_devices 0

EOF
}

# =========================
# Parse args
# =========================

while [[ $# -gt 0 ]]; do
  case "$1" in
    --code_space|--code-space)
      CODE_SPACE="$2"
      shift 2
      ;;
    --serve)
      MODE="serve"
      shift
      ;;
    --bash)
      MODE="bash"
      shift
      ;;
    --model)
      MODEL_PATH="$2"
      shift 2
      ;;
    --served_model_name|--served-model-name)
      SERVED_MODEL_NAME="$2"
      shift 2
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --hip_devices|--hip-devices)
      HIP_DEVICES="$2"
      shift 2
      ;;
    --force-recreate)
      FORCE_RECREATE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown argument: $1"
      echo
      usage
      exit 1
      ;;
  esac
done

# =========================
# Basic checks
# =========================

if [[ ! -d "${CHECKPOINT_DIR}" ]]; then
  echo "[ERROR] Checkpoint dir not found: ${CHECKPOINT_DIR}"
  exit 1
fi

DOCKER_MOUNTS=(
  -v "${CHECKPOINT_DIR}:${CONTAINER_CHECKPOINT_DIR}"
)

if [[ -n "${CODE_SPACE}" ]]; then
  CODE_SPACE="$(realpath "${CODE_SPACE}")"

  if [[ ! -d "${CODE_SPACE}" ]]; then
    echo "[ERROR] code_space not found: ${CODE_SPACE}"
    exit 1
  fi

  DOCKER_MOUNTS+=(
    -v "${CODE_SPACE}:${CONTAINER_CODE_DIR}"
  )

  WORKDIR="${CONTAINER_CODE_DIR}"
else
  WORKDIR="/workspace"
fi

# =========================
# Print summary
# =========================

echo "===== Docker vLLM ROCm Runner ====="
echo "Image:                  ${IMAGE}"
echo "Container name:         ${CONTAINER_NAME}"
echo "Checkpoint mount:       ${CHECKPOINT_DIR} -> ${CONTAINER_CHECKPOINT_DIR}"
echo "Code space mount:       ${CODE_SPACE:-<none>} -> ${CONTAINER_CODE_DIR}"
echo "Workdir:                ${WORKDIR}"
echo "HIP_VISIBLE_DEVICES:    ${HIP_DEVICES}"
echo "Mode:                   ${MODE}"
echo "Model path:             ${MODEL_PATH}"
echo "Served model name:      ${SERVED_MODEL_NAME}"
echo "Port:                   ${PORT}"
echo

# =========================
# Reuse existing container (same name)
# =========================

if docker container inspect "${CONTAINER_NAME}" &>/dev/null; then
  if [[ "${FORCE_RECREATE}" -eq 1 ]]; then
    echo "[INFO] Removing existing container: ${CONTAINER_NAME}"
    docker rm -f "${CONTAINER_NAME}" >/dev/null
  else
    running="$(docker inspect -f '{{.State.Running}}' "${CONTAINER_NAME}")"
    if [[ "${running}" == "true" ]]; then
      echo "[INFO] Container ${CONTAINER_NAME} is already running; opening a new shell inside it."
      if [[ "${MODE}" == "serve" ]]; then
        echo "[WARN] --serve ignored: an instance with this name is already running."
        echo "[WARN] To replace it, run with --force-recreate (stops and removes the container)."
      fi
      exec docker exec -it -w "${WORKDIR}" "${CONTAINER_NAME}" bash
    else
      echo "[INFO] Starting existing stopped container: ${CONTAINER_NAME}"
      exec docker start -ai "${CONTAINER_NAME}"
    fi
  fi
fi

# =========================
# Docker run
# =========================

COMMON_DOCKER_ARGS=(
  --rm
  -it
  --name "${CONTAINER_NAME}"
  --entrypoint bash
  --device /dev/kfd
  --device /dev/dri
  --group-add video
  --ipc=host
  --network host
  --cap-add SYS_PTRACE
  --security-opt seccomp=unconfined
  "${DOCKER_MOUNTS[@]}"
  -w "${WORKDIR}"
  -e HIP_VISIBLE_DEVICES="${HIP_DEVICES}"
  -e PYTORCH_ROCM_ARCH=gfx90a
  -e HF_HOME=/workspace/.cache/huggingface
  -e TRANSFORMERS_CACHE=/workspace/.cache/huggingface
  "${IMAGE}"
)

if [[ "${MODE}" == "bash" ]]; then
  docker run "${COMMON_DOCKER_ARGS[@]}"
else
  docker run "${COMMON_DOCKER_ARGS[@]}" -lc "
    echo '===== Check GPU ====='
    rocm-smi || true

    echo
    echo '===== Check Python packages ====='
    python3 - <<'PY'
import torch
import vllm

print('torch:', torch.__version__)
print('torch hip:', torch.version.hip)
print('vllm:', vllm.__version__)
print('cuda available:', torch.cuda.is_available())
print('device count:', torch.cuda.device_count())

for i in range(torch.cuda.device_count()):
    print(i, torch.cuda.get_device_name(i))
PY

    echo
    echo '===== Start vLLM server ====='
    python3 -m vllm.entrypoints.openai.api_server \
      --model '${MODEL_PATH}' \
      --served-model-name '${SERVED_MODEL_NAME}' \
      --host 0.0.0.0 \
      --port '${PORT}' \
      --tensor-parallel-size 1 \
      --dtype bfloat16 \
      --max-model-len '${MAX_MODEL_LEN}' \
      --gpu-memory-utilization '${GPU_MEMORY_UTILIZATION}' \
      --trust-remote-code
  "
fi

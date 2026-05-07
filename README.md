# server-bootstrap

在 Linux 服务器上快速搭好 shell 与 Python 工具链；另含面向 AMD ROCm 的 vLLM 环境脚本，以及从 Hugging Face 搜索、下载模型与数据集的辅助脚本。

## 仓库内容

| 文件 | 说明 |
|------|------|
| `init.sh` | 通用 Debian/Ubuntu：基础包、[ble.sh](https://github.com/akinomyoga/ble.sh)、[Oh My Bash](https://github.com/ohmybash/oh-my-bash)、`~/.ssh/id_ed25519`（若不存在）、[uv](https://docs.astral.sh/uv/) |
| `init_on_amd.sh` | 在通用步骤基础上安装 Docker、OpenMPI，并写入 **vLLM ROCm 7 Docker** 相关配置（默认镜像 [`rocm/vllm-dev`](https://hub.docker.com/r/rocm/vllm-dev/tags)）。适用于宿主机仍为 ROCm 6.x（如 mi250-002）而 wheel 需 ROCm 7 的场景，避免裸机升级 ROCm；不生成 SSH 密钥 |
| `download.py` | 基于 [huggingface-hub](https://huggingface.co/docs/huggingface_hub) 的 `search` / `download`，可选镜像与 Token |

## 系统要求

- **Shell 脚本**：带 `apt` 的发行版（如 Debian、Ubuntu），当前用户有 `sudo`，网络可访问 GitHub、astral.sh；`init_on_amd.sh` 会安装 `docker.io` 并配置 `vllm_rocm_shell`（需能拉取 Docker Hub 上的 `rocm/vllm-dev`）。
- **Python 工具**：Python ≥ 3.10；推荐用本仓库的 [uv](https://docs.astral.sh/uv/) 管理依赖。

## 一键安装（远程 raw）

通用环境：

```bash
curl -fsSL https://raw.githubusercontent.com/Qiuyc18/server-bootstrap/main/init.sh | bash
```

AMD 服务器：Shell、uv、Docker，以及 **vLLM 官方 ROCm 7 容器** 的本地参数（见脚本内 mi250-002 / ROCm 6.x 与 rocm722 wheel 的说明）。安装后请编辑 `~/.config/server-bootstrap/vllm-rocm.env` 中的镜像 tag，执行 `docker pull`，再用 `vllm_rocm_shell` 进入容器。

```bash
curl -fsSL https://raw.githubusercontent.com/Qiuyc18/server-bootstrap/main/init_on_amd.sh | bash
```

使用自己的 fork 或分支时，将 URL 中的 `Qiuyc18/server-bootstrap` 与 `main` 改成你的仓库与分支名即可。

## 安装完成后

重新登录，或执行：

```bash
source ~/.bashrc
```

以使 ble.sh 与 Oh My Bash 生效。

## 本地运行 Shell 脚本

```bash
bash init.sh
# 或
bash init_on_amd.sh
```

## Hugging Face 下载工具（`download.py`）

在项目目录安装依赖并运行：

```bash
uv sync
uv run python download.py search Qwen --limit 20
uv run python download.py search gsm8k --type dataset --limit 5
uv run python download.py download Qwen/Qwen2.5-1.5B-Instruct
uv run python download.py download openai/gsm8k --type dataset
uv run python download.py download Qwen/Qwen2.5-1.5B-Instruct --local-dir ./models/qwen
```

环境变量可在 shell 中 `export`，或在**仓库根目录**的 `.env` 里配置（`download.py` 启动时会 `load_dotenv()`，且会从同目录的 `.env` 读取 Token 键）：

- **认证**：`HUGGINGFACE_API_TOKEN`、`HUGGINGFACE_HUB_TOKEN` 或 `HF_TOKEN`（私有或受限资源需要）；也可用 `download --token`
- **镜像**：`HF_ENDPOINT`（默认探测 `https://hf-mirror.com`，不可达时回退官方站）
- **默认下载目录**：`DEFAULT_MODEL_DIR`（模型，默认 `/etc/moreh/checkpoint/`）、`DEFAULT_DATA_DIR`（数据集，默认 `/etc/moreh/checkpoint/data/`）

更多子命令说明可执行：

```bash
uv run python download.py --help
```

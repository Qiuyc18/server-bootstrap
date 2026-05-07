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

## 常见问题

### 运行脚本时出现「Pending kernel upgrade / Newer kernel available」

磁盘里已经安装了比**当前正在运行**的内核更新的 `linux-image`（例如提示里：运行中是 `5.15.0-25`，系统期望/已安装的是 `5.15.0-176`），多半是以前做过 `apt upgrade` 但**还没重启**。本次脚本里的 `apt-get install` 会触发 `needrestart`、更新通知等钩子，于是用 whiptail 提醒你重启。

**怎么处理**：能在维护窗口重启时执行 `sudo reboot`，让新内核生效；暂时不能重启就选「确定」关掉对话框即可，一般不影响本次包安装。仓库里的 `init.sh` / `init_on_amd.sh` 已对 `apt-get` 传入 `DEBIAN_FRONTEND=noninteractive` 和 `NEEDRESTART_MODE=a`，尽量不在 SSH 里再弹交互窗；要彻底消除「已装内核 ≠ 运行内核」的状态，仍需要在方便时重启一次。

**对话框里怎么选 OK**：这类界面一般是 **whiptail**。焦点在「OK」上时直接按 **Enter** 即可；若焦点在别的按钮上，用 **Tab**（或左右方向键）切到 **OK**，再按 **Enter**。不要用鼠标点（纯终端里通常无效）。若怎么按键都没反应，多半是 SSH/终端未把键盘交给该界面，可另开一个普通 SSH 会话再操作，或先 **Ctrl+C** 中断当前 `apt`（可能留下半装状态，需谨慎），换用已更新脚本的 `curl … | bash` 重跑以减少弹窗。

### `git clone` 报 `Unable to read current working directory`

安装 ble.sh 时脚本会 `cd` 到临时目录再 `rm -rf` 删掉它，若未立刻 `cd` 回有效路径，当前目录会变成「已删除的目录」，后面的 `git clone`（Oh My Bash）就会失败。请使用已修复的 `init.sh` / `init_on_amd.sh`（删除临时目录后会 `cd "$HOME"`）；若已手动装了一半，可先 `cd ~` 再重新执行脚本或单独 `git clone` Oh My Bash。

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

# server-bootstrap

在 Linux 服务器上快速搭好 shell 与 Python 工具链；另含面向 AMD ROCm 的 vLLM 环境脚本，以及从 Hugging Face 搜索、下载模型与数据集的辅助脚本。

## 仓库内容

| 文件 | 说明 |
|------|------|
| `init.sh` | 通用 Debian/Ubuntu：基础包、[ble.sh](https://github.com/akinomyoga/ble.sh)、[Oh My Bash](https://github.com/ohmybash/oh-my-bash)、SSH 密钥生成命令提示、[uv](https://docs.astral.sh/uv/) |
| `init_on_amd.sh` | 在通用步骤基础上安装 Docker、OpenMPI，并写入 **vLLM ROCm 7 Docker** 相关配置（默认镜像 [`rocm/vllm-dev`](https://hub.docker.com/r/rocm/vllm-dev/tags)）。适用于宿主机仍为 ROCm 6.x（如 mi250-002）而 wheel 需 ROCm 7 的场景，避免裸机升级 ROCm；不生成 SSH 密钥 |
| `install_rocm_gb.sh` | 单独安装 `rocm-monitor.py`，并将 `rocm-gb` alias 幂等地添加到 `~/.bashrc` |
| `download.py` | Hugging Face / ModelScope 的 `search` / `download`；ModelScope 大文件走安全续传（`modelscope_safe_download.py`） |
| `modelscope_safe_download.py` | ModelScope 安全分块下载：严格校验 Range 状态、响应头与响应体，避免截断 `.incomplete` |

## 系统要求

- **Shell 脚本**：适用于带 `apt` 的发行版（如 Debian、Ubuntu），网络需能访问 GitHub、astral.sh。有 root 或 `sudo` 权限时会安装系统依赖；无 `sudo` 权限时跳过该步骤，继续安装用户目录下的 shell 工具和 uv，此时缺失的基础命令需由管理员安装。`init_on_amd.sh` 仅在有权限时安装、启动 `docker.io` 并配置用户组，但无权限时仍会生成 `vllm_rocm_shell` 配置（需管理员预先安装 Docker 并授予使用权限）。
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

## 安装 rocm-gb GPU 监控命令

在 AMD ROCm 服务器上，可以独立安装 GPU 显存与进程监控命令：

```bash
curl -fsSL https://raw.githubusercontent.com/Qiuyc18/server-bootstrap/main/install_rocm_gb.sh | bash
source ~/.bashrc
rocm-gb
```

脚本会把监控程序安装到 `~/rocm-monitor.py`，并在 `~/.bashrc` 中添加 `rocm-gb` alias。重复运行不会重复添加 alias。运行时需要 `python3`、`watch` 和 `rocm-smi`；若系统提供 `amd-smi`，监控脚本会优先使用它获取更准确的进程与 GPU 映射。

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

## Hugging Face / ModelScope 下载工具（`download.py`）

在项目目录安装依赖并运行：

```bash
uv sync
uv run python download.py search Qwen --limit 20
uv run python download.py search gsm8k --type dataset --limit 5
uv run python download.py download Qwen/Qwen2.5-1.5B-Instruct
uv run python download.py download openai/gsm8k --type dataset
uv run python download.py download Qwen/Qwen2.5-1.5B-Instruct --local-dir ./models/qwen
```

### ModelScope 大模型（安全续传）

默认的 ModelScope SDK 在服务端忽略 `Range`、返回 HTTP 200 时，可能用 `wb` 打开已有 `.incomplete`，把十几 GB 进度截断归零。本仓库对 `--source modelscope` 使用独立安全下载器：

- **文件级并发**（`--max-workers`，默认 1）：同时下载多少个仓库文件
- **文件内分块并发**（`--part-workers`，默认 4）：单个大文件同时拉多少个 Range 分块
- **分块大小**（`--part-size-mb`，默认 160）：建议 128～256 MiB
- 总 HTTP 连接上限约为 `max-workers × part-workers`（默认 1×4=4）
- HTTP 206 会严格校验 **Content-Range / Content-Length / 实际响应体长度**
- 仅当 HTTP 200 的 **Content-Range 起止位置与请求完全一致、total 等于远端 Size，且 Content-Length 与实际响应体长度均精确匹配**时，才作为 ModelScope 非标准 Range 响应接受；否则保留旧分块并拒绝
- 同一下载目录使用跨进程锁，避免多个下载进程同时修改分块；Range 致命错误仍会 fail-fast 取消未开始任务

推荐命令：

```bash
uv run python download.py download \
  MiniMax/MiniMax-M3-MXFP8 \
  --source modelscope \
  --max-workers 1 \
  --part-workers 4 \
  --part-size-mb 160 \
  --download-retries 10 \
  --local-dir /data/checkpoints/MiniMax/MiniMax-M3-MXFP8
```

如何判断在续传：

- 日志会出现 `复用已落盘字节` / `已从旧 .incomplete 安全提取可复用前缀` / `下载分块 ... (已有 …)`
- 进度条基于**已安全落盘、重启可复用**的累计字节，不会在重试后无解释地从 13GB 跳回 0
- 若服务端拒绝 Range，会看到类似：`服务器未接受 Range 请求；已保留现有 …，不会截断`

旧临时文件兼容（默认不删除）：

| 形态 | 行为 |
|------|------|
| `file.safetensors.incomplete` | 校验长度后，把完整分块前缀提取到 `.ms_parts/`；原文件保留 |
| `file.safetensors_<start>_<end>` | 长度正好等于分块则复用；为零或异常则忽略并保留 |
| `.ms_parts/file/.../part_*_{.ok,.part}` | 本实现自己的分块；长度不对会改名为 `.invalid` |

确认最终文件无误后，可：

- 按日志打印的 `rm -f …` 手动清理；或
- 加 `--clean-temp`：仅在**本文件校验成功后**删除对应旧 `.incomplete` / legacy 分块

`--force-restart-file`：仅当最终目标文件已存在但校验失败时，把旧文件改名为 `*.bad_existing` 再重下；**不会**在 Range 被拒绝时自动清空进度。

ModelScope 专用参数（见 `download --help`）：`--part-workers`、`--part-size-mb`、`--download-retries`、`--download-timeout`、`--clean-temp`、`--force-restart-file`。

本地离线测试（不访问公网）：

```bash
uv sync --group dev
uv run python -m unittest tests.test_modelscope_safe_download -v
```

环境变量可在 shell 中 `export`，或在**仓库根目录**的 `.env` 里配置（`download.py` 启动时会 `load_dotenv()`，且会从同目录的 `.env` 读取 Token 键）：

- **认证（HF）**：`HUGGINGFACE_API_TOKEN`、`HUGGINGFACE_HUB_TOKEN` 或 `HF_TOKEN`
- **认证（ModelScope）**：`MODELSCOPE_API_TOKEN` 或 `MODELSCOPE_TOKEN`；也可用 `download --token`
- **镜像（HF）**：`HF_ENDPOINT`（默认探测 `https://hf-mirror.com`，不可达时回退官方站）
- **默认下载目录**：`DEFAULT_MODEL_DIR`（模型，默认 `/etc/moreh/checkpoint/`）、`DEFAULT_DATA_DIR`（数据集，默认 `/etc/moreh/checkpoint/data/`）

更多子命令说明可执行：

```bash
uv run python download.py --help
uv run python download.py download --help
```

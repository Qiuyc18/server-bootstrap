#!/usr/bin/env python3
import argparse
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from textwrap import dedent

import dotenv

# 加载 .env 文件中的环境变量
dotenv.load_dotenv()

# --- 从环境变量读取默认路径，若未设置则使用默认值 ---
DEFAULT_MODEL_DIR = os.getenv("DEFAULT_MODEL_DIR", "/etc/moreh/checkpoint/")
DEFAULT_DATA_DIR = os.getenv("DEFAULT_DATA_DIR", "/etc/moreh/checkpoint/data/")
HF_PRIMARY_ENDPOINT = "https://huggingface.co"
HF_MIRROR_ENDPOINT = os.getenv("HF_ENDPOINT", "https://hf-mirror.com")

# --- 文档说明 ---
DESCRIPTION_TEXT = dedent(f"""\
    [常用命令示例]
    1. 搜索模型/数据集:
       python download.py search Qwen --limit 20
       python download.py search gsm8k --type dataset --limit 5

    2. 下载模型 (默认下载到 {DEFAULT_MODEL_DIR}，没有会创建):
       python download.py download Qwen/Qwen2.5-1.5B-Instruct

    3. 下载数据集 (默认下载到 {DEFAULT_DATA_DIR}):
       python download.py download openai/gsm8k --type dataset

    4. 指定目录下载:
       python download.py download Qwen/Qwen2.5-1.5B-Instruct --local-dir tmp

    5. 从 ModelScope 下载（公开仓库无需 Token；大文件默认低文件并发 + 文件内分块）:
       python download.py download MiniMax/MiniMax-M3-MXFP8 --source modelscope \\
         --max-workers 1 --part-workers 4 --part-size-mb 160 --download-retries 10
    """)


TOKEN_ENV_KEYS = {
    "hf": ("HUGGINGFACE_API_TOKEN", "HUGGINGFACE_HUB_TOKEN", "HF_TOKEN"),
    "modelscope": ("MODELSCOPE_API_TOKEN", "MODELSCOPE_TOKEN"),
}


def _read_env_token(env_path: Path, keys: tuple[str, ...]) -> str | None:
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in keys:
            return value or None
    return None


def _get_token(source: str, token: str | None = None) -> str | None:
    if token:
        return token

    keys = TOKEN_ENV_KEYS[source]
    repo_root = Path(__file__).resolve().parent
    token = _read_env_token(repo_root / ".env", keys)
    if token:
        return token
    return next((value for key in keys if (value := os.environ.get(key))), None)


def _is_endpoint_reachable(endpoint: str, timeout: int = 5) -> bool:
    probe_url = urllib.parse.urljoin(endpoint.rstrip("/") + "/", "api/models?limit=1")
    request = urllib.request.Request(probe_url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status < 500
    except urllib.error.HTTPError as err:
        # 4xx 表示服务可达但请求被拒绝，也视为网络连通。
        return 400 <= err.code < 500
    except Exception:
        return False


def _configure_hf_endpoint_or_exit() -> None:
    print(f"连通性测试: 镜像站 {HF_MIRROR_ENDPOINT}")
    if _is_endpoint_reachable(HF_MIRROR_ENDPOINT):
        os.environ["HF_ENDPOINT"] = HF_MIRROR_ENDPOINT
        print("连通性测试结果: 镜像站可用")
        return
    else:
        print(f"连通性测试: 主站 {HF_PRIMARY_ENDPOINT}")
        if _is_endpoint_reachable(HF_PRIMARY_ENDPOINT):
            os.environ.pop("HF_ENDPOINT", None)
            print("连通性测试结果: 主站可用，使用官方站点。")
            return


def _cmd_search(args: argparse.Namespace) -> int:
    _configure_hf_endpoint_or_exit()
    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("Missing dependency: huggingface_hub. Please install it first.")
        return 1

    api = HfApi(token=_get_token("hf"))
    if args.type == "dataset":
        results = api.list_datasets(search=args.query, limit=args.limit)
        for ds in results:
            print(ds.id)  # pyright: ignore[reportAttributeAccessIssue]
    else:
        results = api.list_models(search=args.query, limit=args.limit)
        for model in results:
            print(model.modelId)  # pyright: ignore[reportAttributeAccessIssue]
    return 0


def _format_bytes(num_bytes: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)}{unit}"
            return f"{size:.2f}{unit}"
        size /= 1024
    return f"{num_bytes:.0f}B"


def _format_speed(bytes_per_sec: float | None) -> str:
    if bytes_per_sec is None or bytes_per_sec <= 0:
        return "?B/s"
    return f"{_format_bytes(bytes_per_sec)}/s"


def _build_download_tqdm_class(stats: dict):
    from huggingface_hub.utils import tqdm as hf_tqdm

    class DownloadProgressTqdm(hf_tqdm):
        """显式展示瞬时/平均速度，并定时刷新进度条。

        HF HTTP 下载默认约每 10MB 才回调一次进度，慢速网络上默认条会长时间不动，
        速率也容易显示为 `?B/s`。这里用后台刷新 + 自算速率让速度持续可见。
        """

        def __init__(self, *args, **kwargs):
            # disable=True 时 tqdm 不保留 unit，从入参判断是否为字节进度条。
            is_bytes_bar = kwargs.get("unit") == "B" or bool(
                kwargs.get("unit_scale")
            )
            # `_create_progress_bar` 在非 TTY（管道/日志采集）下会把 disable 关掉，
            # 只剩 thread_map 的文件计数条，因而看不到下载速度。字节条强制开启。
            if is_bytes_bar:
                kwargs["disable"] = False
            kwargs.setdefault("miniters", 1)
            kwargs.setdefault("mininterval", 0.5)
            kwargs.setdefault("smoothing", 0.08)
            if is_bytes_bar:
                kwargs.setdefault(
                    "bar_format",
                    "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
                    "[{elapsed}<{remaining}] {postfix}",
                )
            else:
                kwargs.setdefault(
                    "bar_format",
                    "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
                    "[{elapsed}<{remaining}, {rate_fmt}]",
                )
            super().__init__(*args, **kwargs)
            self._is_bytes_bar = is_bytes_bar
            self._started_at = time.monotonic()
            self._last_n = float(self.n or 0)
            self._last_t = self._started_at
            self._instant_speed: float | None = None
            self._refresh_stop = threading.Event()
            self._refresh_thread: threading.Thread | None = None
            if not self.disable and self._is_bytes_bar:
                self._refresh_thread = threading.Thread(
                    target=self._auto_refresh, daemon=True
                )
                self._refresh_thread.start()

        def _auto_refresh(self) -> None:
            while not self._refresh_stop.wait(1.0):
                self._update_speed_postfix()
                self.refresh()

        def _track_bytes(self) -> None:
            if self._is_bytes_bar:
                stats["bytes"] = max(float(stats.get("bytes", 0)), float(self.n or 0))

        def _update_speed_postfix(self) -> None:
            if not self._is_bytes_bar:
                return

            now = time.monotonic()
            n = float(self.n or 0)
            elapsed = max(now - self._started_at, 1e-6)
            avg_speed = n / elapsed
            dt = now - self._last_t
            dn = n - self._last_n
            if dt >= 0.5 and dn > 0:
                self._instant_speed = dn / dt
                self._last_n = n
                self._last_t = now
            elif dt >= 8.0:
                # 长时间没有新字节（HF 大 chunk 回调间隔可能很长），清空瞬时速度。
                self._instant_speed = None
                self._last_t = now

            self._track_bytes()
            if self.disable:
                return
            instant = _format_speed(self._instant_speed)
            average = _format_speed(avg_speed if n > 0 else None)
            self.set_postfix_str(f"cur={instant}, avg={average}", refresh=False)

        def update(self, n: float | None = 1):
            result = super().update(n)
            self._update_speed_postfix()
            return result

        def close(self):
            self._refresh_stop.set()
            if self._refresh_thread is not None:
                self._refresh_thread.join(timeout=1.0)
                self._refresh_thread = None
            self._update_speed_postfix()
            return super().close()

    return DownloadProgressTqdm


def _download_from_hf(
    args: argparse.Namespace, local_dir: str, token: str | None
) -> float:
    _configure_hf_endpoint_or_exit()
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        raise RuntimeError(
            "缺少依赖库 huggingface_hub。请先运行: pip install huggingface_hub"
        )

    # huggingface_hub 在 logger 为 NOTSET 时会禁用字节进度条，只剩“Fetching N files”，
    # 因而看不到下载速度。显式打开 INFO 以启用体积/速率进度条。
    logging.getLogger("huggingface_hub").setLevel(logging.INFO)

    progress_stats: dict = {"bytes": 0.0}
    download_kwargs = {
        "repo_id": args.repo_id,
        "repo_type": args.type,
        "local_dir": local_dir,
        "token": token,
        "max_workers": args.max_workers,
        "tqdm_class": _build_download_tqdm_class(progress_stats),
    }
    if args.revision is not None:
        download_kwargs["revision"] = args.revision

    snapshot_download(**download_kwargs)
    return float(progress_stats.get("bytes", 0.0))


def _download_from_modelscope(
    args: argparse.Namespace, local_dir: str, token: str | None
) -> float:
    # Env must be set before importing modelscope / modelscope_hub.
    from modelscope_safe_download import (
        DownloadConfig,
        configure_modelscope_env,
        format_bytes,
        require_supported_modelscope_versions,
        snapshot_download_safe,
    )

    file_workers = args.max_workers
    part_workers = args.part_workers
    part_size_mb = args.part_size_mb
    retries = args.download_retries
    timeout = args.download_timeout

    configure_modelscope_env(
        file_workers=file_workers,
        part_workers=part_workers,
        part_size_mb=part_size_mb,
        retries=retries,
        timeout=timeout,
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )

    ms_ver, hub_ver = require_supported_modelscope_versions()
    print(f"ModelScope SDK: modelscope={ms_ver}, modelscope-hub={hub_ver}")
    print(
        "安全下载策略: "
        f"file_workers={file_workers}, part_workers={part_workers}, "
        f"part_size={part_size_mb}MiB, retries={retries}, timeout={timeout}s"
    )

    config = DownloadConfig(
        file_workers=file_workers,
        part_workers=part_workers,
        part_size=part_size_mb * 1024 * 1024,
        retries=retries,
        timeout=timeout,
        clean_temp=bool(args.clean_temp),
        force_restart_file=bool(args.force_restart_file),
    )
    _path, stats = snapshot_download_safe(
        args.repo_id,
        local_dir=local_dir,
        repo_type=args.type,
        revision=args.revision,
        token=token,
        config=config,
    )
    downloaded = float(sum(s.downloaded_bytes for s in stats))
    reused = float(sum(s.reused_bytes for s in stats))
    if reused > 0:
        print(f"复用已落盘字节: {format_bytes(reused)}")
    return downloaded


def _cmd_download(args: argparse.Namespace) -> int:
    token = _get_token(args.source, args.token)

    # 路径处理逻辑：若用户指定了 local_dir 则使用，否则根据 type 走默认路径
    if args.local_dir:
        local_dir = str(Path(args.local_dir))
    else:
        base_dir = DEFAULT_DATA_DIR if args.type == "dataset" else DEFAULT_MODEL_DIR
        local_dir = str(Path(base_dir) / args.repo_id)

    print(f"准备从 {args.source} 下载 [{args.type}]: {args.repo_id}")
    print(f"目标路径: {local_dir}")
    if token:
        print("状态: 使用已认证 Token")
    else:
        print("状态: 未检测到 Token，尝试匿名下载...")

    started_at = time.monotonic()
    try:
        if args.source == "modelscope":
            downloaded = _download_from_modelscope(args, local_dir, token)
        else:
            downloaded = _download_from_hf(args, local_dir, token)

        elapsed = max(time.monotonic() - started_at, 1e-6)
        print(f"\n[成功] {args.type} 已下载至: {os.path.abspath(local_dir)}")
        if downloaded > 0:
            print(
                f"传输量: {_format_bytes(downloaded)} | "
                f"耗时: {elapsed:.1f}s | "
                f"平均速度: {_format_speed(downloaded / elapsed)}"
            )
        else:
            print(f"耗时: {elapsed:.1f}s（详细传输速度见上方下载进度）")
        return 0
    except Exception as e:
        print(f"\n[失败] 下载出错: {e}")
        if "401" in str(e) or "403" in str(e):
            token_name = (
                "MODELSCOPE_API_TOKEN"
                if args.source == "modelscope"
                else "HF_TOKEN"
            )
            print(f"提示: 此资源可能需要权限认证。请提供有效的 {token_name}。")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=DESCRIPTION_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # 全局参数
    parser.add_argument(
        "--token",
        help="HF/ModelScope Token（可选，也可以通过环境变量设置）",
        default=None,
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Search 命令
    search_parser = subparsers.add_parser("search", help="按关键词搜索资源")
    search_parser.add_argument("query", help="搜索关键词, 例如: Qwen 或 gsm8k")
    search_parser.add_argument(
        "--type",
        choices=["model", "dataset"],
        default="model",
        help="搜索类型 (model 或 dataset)",
    )
    search_parser.add_argument("--limit", type=int, default=20, help="显示结果数量限制")
    search_parser.set_defaults(func=_cmd_search)

    # Download 命令
    download_parser = subparsers.add_parser("download", help="下载指定资源")
    download_parser.add_argument(
        "repo_id", help="仓库 ID, 例如: Qwen/Qwen2.5-1.5B-Instruct 或 openai/gsm8k"
    )
    download_parser.add_argument(
        "--type",
        choices=["model", "dataset"],
        default="model",
        help="下载类型 (model 或 dataset)",
    )
    download_parser.add_argument(
        "--source",
        choices=["hf", "modelscope"],
        default="hf",
        help="下载源（默认: hf）",
    )
    download_parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help=(
            "文件级并发数：同时下载多少个仓库文件。"
            "Hugging Face 默认 8；ModelScope 安全下载默认 1。"
        ),
    )
    download_parser.add_argument(
        "--part-workers",
        type=int,
        default=4,
        help=(
            "ModelScope only: 单文件内 Range 分块并发数（默认 4）。"
            "总 HTTP 连接上限约为 max-workers × part-workers。"
        ),
    )
    download_parser.add_argument(
        "--part-size-mb",
        type=int,
        default=160,
        help="ModelScope only: 分块大小 MiB（默认 160，建议 128～256）",
    )
    download_parser.add_argument(
        "--download-retries",
        type=int,
        default=10,
        help="ModelScope only: 单个分块下载失败时的最大重试次数（默认 10）",
    )
    download_parser.add_argument(
        "--download-timeout",
        type=int,
        default=60,
        help="ModelScope only: 单次 HTTP 超时秒数（默认 60）",
    )
    download_parser.add_argument(
        "--clean-temp",
        action="store_true",
        help=(
            "ModelScope only: 最终文件校验成功后，删除已成功合并对应的旧 "
            ".incomplete / legacy 分块；默认只打印手动清理命令"
        ),
    )
    download_parser.add_argument(
        "--force-restart-file",
        action="store_true",
        help=(
            "ModelScope only: 当目标最终文件存在但校验失败时，允许重新下载并替换；"
            "不会在服务器拒绝 Range 时自动截断已有分块进度"
        ),
    )
    download_parser.add_argument("--revision", default=None, help="分支或 Commit ID")
    download_parser.add_argument("--local-dir", default=None, help="下载目标路径")
    download_parser.set_defaults(func=_cmd_download)

    args = parser.parse_args()
    if getattr(args, "command", None) == "download":
        if args.max_workers is None:
            args.max_workers = 1 if args.source == "modelscope" else 8
        if args.max_workers < 1:
            parser.error("--max-workers 必须 >= 1")
        if args.source == "modelscope":
            if args.part_workers < 1:
                parser.error("--part-workers 必须 >= 1")
            if args.part_size_mb < 1:
                parser.error("--part-size-mb 必须 >= 1")
            if args.download_retries < 1:
                parser.error("--download-retries 必须 >= 1")
            if args.download_timeout < 1:
                parser.error("--download-timeout 必须 >= 1")
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n操作已取消。")
        sys.exit(1)

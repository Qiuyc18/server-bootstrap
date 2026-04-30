#!/usr/bin/env python3
import argparse
import os
import sys
from pathlib import Path
from textwrap import dedent

import dotenv

# 加载 .env 文件中的环境变量
dotenv.load_dotenv()

# --- 从环境变量读取默认路径，若未设置则使用默认值 ---
DEFAULT_MODEL_DIR = os.getenv("DEFAULT_MODEL_DIR", "/etc/moreh/checkpoint/")
DEFAULT_DATA_DIR = os.getenv("DEFAULT_DATA_DIR", "/etc/moreh/checkpoint/data/")

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
    """)


def _read_env_token(env_path: Path) -> str | None:
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in {"HUGGINGFACE_API_TOKEN", "HUGGINGFACE_HUB_TOKEN", "HF_TOKEN"}:
            return value or None
    return None


def _get_token(token: str | None = None) -> str | None:
    if token:
        return token
    repo_root = Path(__file__).resolve().parents[1]
    token = _read_env_token(repo_root / ".env") or os.environ.get(
        "HUGGINGFACE_API_TOKEN"
    )
    if token:
        return token
    return (
        os.environ.get("HUGGINGFACE_API_TOKEN")
        or os.environ.get("HUGGINGFACE_HUB_TOKEN")
        or os.environ.get("HF_TOKEN")
    )


def _cmd_search(args: argparse.Namespace) -> int:
    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("Missing dependency: huggingface_hub. Please install it first.")
        return 1

    api = HfApi(token=_get_token())
    if args.type == "dataset":
        results = api.list_datasets(search=args.query, limit=args.limit)
        for ds in results:
            print(ds.id)  # pyright: ignore[reportAttributeAccessIssue]
    else:
        results = api.list_models(search=args.query, limit=args.limit)
        for model in results:
            print(model.modelId)  # pyright: ignore[reportAttributeAccessIssue]
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("错误: 缺少依赖库 huggingface_hub。请先运行: pip install huggingface_hub")
        return 1

    token = _get_token(args.token)

    # 路径处理逻辑：若用户指定了 local_dir 则使用，否则根据 type 走默认路径
    if args.local_dir:
        local_dir = str(Path(args.local_dir))
    else:
        base_dir = DEFAULT_DATA_DIR if args.type == "dataset" else DEFAULT_MODEL_DIR
        local_dir = str(Path(base_dir) / args.repo_id)

    print(f"准备下载 [{args.type}]: {args.repo_id}")
    print(f"目标路径: {local_dir}")
    if token:
        print("状态: 使用已认证 Token")
    else:
        print("状态: 未检测到 Token，尝试匿名下载...")

    try:
        download_kwargs = {
            "repo_id": args.repo_id,
            "repo_type": args.type,  # 关键点：告诉 HF 下载的是模型还是数据集
            "local_dir": local_dir,
            "local_dir_use_symlinks": False,
            "resume_download": True,
            "token": token,
            "max_workers": 8,
        }
        if args.revision is not None:
            download_kwargs["revision"] = args.revision

        snapshot_download(**download_kwargs)
        print(f"\n[成功] {args.type} 已下载至: {os.path.abspath(local_dir)}")
        return 0
    except Exception as e:
        print(f"\n[失败] 下载出错: {e}")
        if "401" in str(e) or "403" in str(e):
            print("提示: 此资源可能需要权限认证。请提供有效的 HF_TOKEN。")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=DESCRIPTION_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # 全局参数
    parser.add_argument(
        "--token",
        help="HuggingFace Token (可选，也可以通过环境变量设置）",
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
    download_parser.add_argument("--revision", default=None, help="分支或 Commit ID")
    download_parser.add_argument("--local-dir", default=None, help="下载目标路径")
    download_parser.set_defaults(func=_cmd_download)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n操作已取消。")
        sys.exit(1)

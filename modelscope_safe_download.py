#!/usr/bin/env python3
"""Safe, resumable ModelScope downloads without truncating partial data.

This module deliberately does **not** call modelscope-hub's
``_download_with_resume`` / ``_download_part_with_retry``, which may open an
``.incomplete`` file with ``wb`` when the server ignores ``Range`` and returns
HTTP 200. Instead we:

* list files via a narrow Hub API adapter;
* download with strict ``206`` + ``Content-Range`` validation;
* keep byte-range parts that survive process restarts;
* merge to a temp file, verify size/SHA256, then ``os.replace`` into place.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import re
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

logger = logging.getLogger(__name__)

# Supported versions for the narrow Hub API adapter (list + download URL).
_SUPPORTED_MODELSCOPE = ((1, 36, 0), None)  # >= 1.36.0
_SUPPORTED_HUB = ((0, 1, 7), (0, 2, 0))  # >= 0.1.7, < 0.2.0

DEFAULT_FILE_WORKERS = 1
DEFAULT_PART_WORKERS = 4
DEFAULT_PART_SIZE_MB = 160
DEFAULT_RETRIES = 10
DEFAULT_TIMEOUT = 60
DEFAULT_CHUNK_SIZE = 1024 * 1024

LEGACY_INCOMPLETE_SUFFIX = ".incomplete"
PARTS_DIR_NAME = ".ms_parts"
MERGE_SUFFIX = ".ms_merging"
LEGACY_PART_RE = re.compile(r"^(.+)_(\d+)_(\d+)$")
CONTENT_RANGE_RE = re.compile(
    r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE
)


class SafeDownloadError(RuntimeError):
    """Base error for safe ModelScope downloads."""


class RangeRejectedError(SafeDownloadError):
    """Server ignored or rejected an HTTP Range request."""


class CorruptPartError(SafeDownloadError):
    """A part file failed size / range validation."""


class IntegrityError(SafeDownloadError):
    """Final size or SHA256 verification failed."""


class DownloadCancelled(SafeDownloadError):
    """Download cancelled via shared cancel_event (fail-fast)."""


def _signal_cancel(cancel_event: threading.Event, message: str) -> None:
    """Set cancel_event once and log the fail-fast stop message."""
    if cancel_event.is_set():
        return
    cancel_event.set()
    logger.error("%s", message)


def _ensure_not_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("下载已取消（fail-fast）")


def _sleep_backoff(attempt: int, cancel_event: threading.Event | None) -> None:
    """Exponential backoff with jitter; abort early if cancelled."""
    base = min(2 ** max(attempt - 1, 0), 30)
    delay = base + random.uniform(0, 0.5 * base)
    deadline = time.monotonic() + delay
    while True:
        _ensure_not_cancelled(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if cancel_event is None:
            time.sleep(remaining)
            return
        cancel_event.wait(timeout=min(0.2, remaining))


def _shutdown_cancel_futures(
    pool: ThreadPoolExecutor,
    futures: dict[Future, object],
    cancel_event: threading.Event,
    message: str,
) -> None:
    _signal_cancel(cancel_event, message)
    for fut in futures:
        fut.cancel()
    pool.shutdown(wait=True, cancel_futures=True)

@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int | None
    sha256: str | None


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int  # inclusive

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def key(self) -> str:
        return f"{self.start}_{self.end}"


@dataclass
class DownloadConfig:
    file_workers: int = DEFAULT_FILE_WORKERS
    part_workers: int = DEFAULT_PART_WORKERS
    part_size: int = DEFAULT_PART_SIZE_MB * 1024 * 1024
    retries: int = DEFAULT_RETRIES
    timeout: int = DEFAULT_TIMEOUT
    chunk_size: int = DEFAULT_CHUNK_SIZE
    clean_temp: bool = False
    force_restart_file: bool = False
    user_agent: str = "server-bootstrap-safe-download/1.0"


@dataclass
class FileDownloadStats:
    path: str
    total_size: int
    reused_bytes: int = 0
    downloaded_bytes: int = 0
    skipped: bool = False


def _parse_version(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.split("."):
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def _version_in_range(
    version: str,
    minimum: tuple[int, ...],
    exclusive_max: tuple[int, ...] | None,
) -> bool:
    parsed = _parse_version(version)
    if parsed < minimum:
        return False
    if exclusive_max is not None and parsed >= exclusive_max:
        return False
    return True


def require_supported_modelscope_versions() -> tuple[str, str]:
    """Validate modelscope / modelscope-hub versions for the API adapter."""
    try:
        import importlib.metadata as metadata
    except ImportError:  # pragma: no cover
        import importlib_metadata as metadata  # type: ignore

    try:
        ms_version = metadata.version("modelscope")
    except metadata.PackageNotFoundError as exc:
        raise SafeDownloadError(
            "未安装 modelscope。请运行: uv sync 或 pip install -U modelscope"
        ) from exc

    try:
        hub_version = metadata.version("modelscope-hub")
    except metadata.PackageNotFoundError:
        # modelscope may vendor/export hub without a separate dist in some builds.
        try:
            import modelscope_hub

            hub_version = getattr(modelscope_hub, "__version__", "0.0.0")
        except Exception as exc:  # pragma: no cover
            raise SafeDownloadError(
                "未找到 modelscope-hub。请安装 modelscope>=1.36（会拉取 modelscope-hub）。"
            ) from exc

    if not _version_in_range(ms_version, *_SUPPORTED_MODELSCOPE):
        raise SafeDownloadError(
            f"不支持的 modelscope 版本 {ms_version}；需要 >= 1.36.0"
        )
    if not _version_in_range(hub_version, *_SUPPORTED_HUB):
        raise SafeDownloadError(
            f"不支持的 modelscope-hub 版本 {hub_version}；需要 >= 0.1.7,<0.2.0"
        )
    return ms_version, hub_version


def configure_modelscope_env(
    *,
    file_workers: int,
    part_workers: int,
    part_size_mb: int,
    retries: int,
    timeout: int,
) -> None:
    """Set ModelScope env knobs **before** importing the SDK.

    Even though this module uses its own HTTP downloader, listing may import
    modelscope_hub, whose download constants are read at import time.
    """
    os.environ["MODELSCOPE_DOWNLOAD_PARALLEL_WORKERS"] = "1"
    os.environ["MODELSCOPE_DOWNLOAD_PARALLELS"] = "1"
    os.environ["MODELSCOPE_DOWNLOAD_MAX_RETRIES"] = str(max(1, retries))
    os.environ["MODELSCOPE_DOWNLOAD_RETRY_TIMES"] = str(max(1, retries))
    os.environ["MODELSCOPE_DOWNLOAD_TIMEOUT"] = str(max(1, timeout))
    os.environ["MODELSCOPE_DOWNLOAD_PART_SIZE_MB"] = str(max(1, part_size_mb))
    # Keep SDK file workers at 1; our executor owns real concurrency.
    os.environ.setdefault("MODELSCOPE_HUB_MAX_WORKERS", str(max(1, file_workers)))
    logger.info(
        "ModelScope env pre-configured: SDK inner part-workers=1 "
        "(safe downloader uses file_workers=%s part_workers=%s part_size=%sMiB)",
        file_workers,
        part_workers,
        part_size_mb,
    )


def _hub_api_list_and_urls(
    repo_id: str,
    repo_type: str,
    revision: str,
    token: str | None,
    endpoint: str | None = None,
) -> tuple[list[RemoteFile], Callable[[str], str], dict | None]:
    """Narrow Hub API adapter: list files + build download URLs.

    Isolated here so the rest of the downloader never touches private SDK
    download helpers. Uses ``legacy.list_repo_files`` / ``get_download_url``
    which are the stable listing surfaces used by modelscope-hub 0.1.x.
    """
    from modelscope_hub import HubApi

    # Let HubApi resolve endpoint itself:
    # explicit arg → MODELSCOPE_ENDPOINT env → SDK DEFAULT_ENDPOINT.
    api_kwargs = {"token": token}
    if endpoint:
        api_kwargs["endpoint"] = endpoint
    api = HubApi(**api_kwargs)

    legacy = getattr(api, "legacy", None)
    if legacy is None or not hasattr(legacy, "list_repo_files"):
        raise SafeDownloadError(
            "当前 modelscope-hub 缺少 legacy.list_repo_files；无法安全列举仓库文件"
        )
    if not hasattr(legacy, "get_download_url"):
        raise SafeDownloadError(
            "当前 modelscope-hub 缺少 legacy.get_download_url；无法构建下载地址"
        )

    raw_files = legacy.list_repo_files(
        repo_id=repo_id,
        repo_type=repo_type,
        revision=revision,
        recursive=True,
    )

    files: list[RemoteFile] = []
    for item in raw_files:
        path = item.get("Path") or item.get("path") or item.get("Name") or ""
        ftype = item.get("Type") or item.get("type") or "blob"
        if not path or ftype == "tree":
            continue
        raw_size = item.get("Size") or item.get("size")
        size = int(raw_size) if raw_size is not None and str(raw_size) != "" else None
        sha256 = item.get("Sha256") or item.get("sha256") or None
        if isinstance(sha256, str) and not sha256.strip():
            sha256 = None
        files.append(RemoteFile(path=path, size=size, sha256=sha256))

    cookies = {"m_session_id": token} if token else None

    def download_url(file_path: str) -> str:
        return legacy.get_download_url(
            repo_id=repo_id,
            repo_type=repo_type,
            file_path=file_path,
            revision=revision,
        )

    return files, download_url, cookies


def plan_parts(file_size: int, part_size: int) -> list[ByteRange]:
    if file_size < 0:
        raise ValueError("file_size must be >= 0")
    if part_size <= 0:
        raise ValueError("part_size must be > 0")
    if file_size == 0:
        return []

    parts: list[ByteRange] = []
    start = 0
    while start < file_size:
        end = min(start + part_size, file_size) - 1
        parts.append(ByteRange(start, end))
        start = end + 1
    return parts


def parse_content_range(header: str | None) -> tuple[int, int, int | None]:
    if not header:
        raise CorruptPartError("缺少 Content-Range 响应头")
    match = CONTENT_RANGE_RE.search(header.strip())
    if not match:
        raise CorruptPartError(f"无法解析 Content-Range: {header!r}")
    start = int(match.group(1))
    end = int(match.group(2))
    total_raw = match.group(3)
    total = None if total_raw == "*" else int(total_raw)
    return start, end, total


def format_bytes(num_bytes: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)}{unit}"
            return f"{size:.2f}{unit}"
        size /= 1024
    return f"{num_bytes:.0f}B"


class DurableProgress:
    """Progress based on bytes already safely on disk (restart-reusable)."""

    def __init__(self, total: int, desc: str = "Downloading") -> None:
        self.total = max(0, total)
        self.desc = desc
        self._lock = threading.Lock()
        self.n = 0
        self._last_print = 0.0
        try:
            from tqdm import tqdm

            self._bar = tqdm(
                total=self.total or None,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=desc,
                miniters=1,
                dynamic_ncols=True,
            )
        except Exception:  # pragma: no cover
            self._bar = None

    def set(self, n: int) -> None:
        with self._lock:
            self._set_unlocked(n)

    def _set_unlocked(self, n: int) -> None:
        n = max(0, n)
        if self.total:
            n = min(n, self.total)
        self.n = n
        if self._bar is not None:
            self._bar.n = n
            self._bar.refresh()
            return
        now = time.monotonic()
        if now - self._last_print >= 1.0:
            self._last_print = now
            print(
                f"{self.desc}: {format_bytes(self.n)}"
                + (f"/{format_bytes(self.total)}" if self.total else ""),
                flush=True,
            )

    def add(self, amount: int) -> None:
        if amount <= 0:
            return
        with self._lock:
            self._set_unlocked(self.n + amount)

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()


def parts_root_for(local_dir: Path, rel_path: str) -> Path:
    return local_dir / PARTS_DIR_NAME / rel_path


def part_paths(local_dir: Path, rel_path: str, br: ByteRange) -> tuple[Path, Path]:
    root = parts_root_for(local_dir, rel_path)
    base = root / f"part_{br.start:010d}_{br.end:010d}"
    return base.with_suffix(".ok"), base.with_suffix(".part")


def legacy_part_path(local_dir: Path, rel_path: str, br: ByteRange) -> Path:
    # modelscope-hub style: "<target_path>_{start}_{end}"
    return local_dir / f"{rel_path}_{br.start}_{br.end}"


def legacy_incomplete_path(local_dir: Path, rel_path: str) -> Path:
    return local_dir / f"{rel_path}{LEGACY_INCOMPLETE_SUFFIX}"


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _safe_copy_range(src: Path, dst: Path, length: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    with src.open("rb") as rf, tmp.open("wb") as wf:
        remaining = length
        while remaining > 0:
            chunk = rf.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            wf.write(chunk)
            remaining -= len(chunk)
    if tmp.stat().st_size != length:
        tmp.unlink(missing_ok=True)
        raise CorruptPartError(
            f"从 {src} 复制分块失败: 期望 {length} 字节, 实际 {tmp.stat().st_size if tmp.exists() else 0}"
        )
    os.replace(tmp, dst)


def adopt_legacy_artifacts(
    local_dir: Path,
    rel_path: str,
    parts: list[ByteRange],
    *,
    progress: DurableProgress | None = None,
) -> tuple[int, list[str]]:
    """Validate and reuse old ModelScope temp files without deleting them.

    Returns (reused_bytes, advisories).
    """
    advisories: list[str] = []
    reused = 0
    file_size = parts[-1].end + 1 if parts and parts[0].end >= 0 else 0

    incomplete = legacy_incomplete_path(local_dir, rel_path)
    if incomplete.exists():
        size = incomplete.stat().st_size
        if size <= 0:
            advisories.append(
                f"保留零字节旧文件 {incomplete}（不会当作有效进度）"
            )
        elif file_size and size > file_size:
            advisories.append(
                f"旧 .incomplete 长度异常 ({format_bytes(size)} > "
                f"{format_bytes(file_size)}): {incomplete}；已保留，请人工检查后删除"
            )
        else:
            # Reuse complete part prefixes covered by the incomplete file.
            offset = 0
            for br in parts:
                if offset + br.length > size:
                    # Remainder becomes in-progress part if any bytes remain.
                    ok_path, part_path = part_paths(local_dir, rel_path, br)
                    if not ok_path.exists() and size > offset:
                        rem = size - offset
                        if rem > 0:
                            part_path.parent.mkdir(parents=True, exist_ok=True)
                            if not part_path.exists() or part_path.stat().st_size < rem:
                                with incomplete.open("rb") as rf:
                                    rf.seek(offset)
                                    _write_exact(part_path, rf, rem)
                                reused += rem
                                if progress:
                                    progress.add(rem)
                    break
                ok_path, _ = part_paths(local_dir, rel_path, br)
                if not ok_path.exists():
                    with incomplete.open("rb") as rf:
                        rf.seek(offset)
                        _write_exact(ok_path, rf, br.length)
                    reused += br.length
                    if progress:
                        progress.add(br.length)
                offset += br.length
            advisories.append(
                f"已从旧 .incomplete 安全提取可复用前缀 {format_bytes(min(size, file_size or size))}:"
                f" {incomplete}（原文件未删除）"
            )

    # Legacy parallel parts: filename_start_end
    for br in parts:
        legacy = legacy_part_path(local_dir, rel_path, br)
        if not legacy.exists():
            continue
        size = legacy.stat().st_size
        ok_path, part_path = part_paths(local_dir, rel_path, br)
        if size == br.length:
            if not ok_path.exists():
                _safe_copy_range(legacy, ok_path, br.length)
                reused += br.length
                if progress:
                    progress.add(br.length)
            advisories.append(
                f"复用旧分块 {legacy.name} ({format_bytes(size)})；原文件保留"
            )
        elif 0 < size < br.length:
            if not ok_path.exists() and (
                not part_path.exists() or part_path.stat().st_size < size
            ):
                _safe_copy_range(legacy, part_path, size)
                reused += size
                if progress:
                    progress.add(size)
            advisories.append(
                f"旧分块 {legacy.name} 不完整 ({format_bytes(size)}/"
                f"{format_bytes(br.length)})，已保留并继续续传"
            )
        else:
            advisories.append(
                f"旧分块 {legacy.name} 长度异常 ({format_bytes(size)}), "
                f"期望 {format_bytes(br.length)}；已忽略且不删除"
            )

    return reused, advisories


def _write_exact(path: Path, reader, length: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as wf:
        remaining = length
        while remaining > 0:
            chunk = reader.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            wf.write(chunk)
            remaining -= len(chunk)
    if not tmp.exists() or tmp.stat().st_size != length:
        actual = tmp.stat().st_size if tmp.exists() else 0
        tmp.unlink(missing_ok=True)
        raise CorruptPartError(f"写入 {path} 失败: 期望 {length}, 实际 {actual}")
    os.replace(tmp, path)


def inventory_durable_bytes(
    local_dir: Path, rel_path: str, parts: list[ByteRange]
) -> int:
    total = 0
    for br in parts:
        ok_path, part_path = part_paths(local_dir, rel_path, br)
        if ok_path.exists() and ok_path.stat().st_size == br.length:
            total += br.length
        elif part_path.exists():
            size = part_path.stat().st_size
            if 0 < size <= br.length:
                total += size
    return total


def _reject_invalid_ok_parts(
    local_dir: Path, rel_path: str, parts: list[ByteRange]
) -> list[str]:
    notes: list[str] = []
    for br in parts:
        ok_path, part_path = part_paths(local_dir, rel_path, br)
        if ok_path.exists():
            size = ok_path.stat().st_size
            if size != br.length:
                quarantine = ok_path.with_suffix(".ok.invalid")
                os.replace(ok_path, quarantine)
                notes.append(
                    f"拒绝长度错误的完成分块 {ok_path.name} "
                    f"({format_bytes(size)} != {format_bytes(br.length)}); "
                    f"已改名为 {quarantine.name}"
                )
        if part_path.exists():
            size = part_path.stat().st_size
            if size == 0 or size > br.length:
                quarantine = part_path.with_suffix(".part.invalid")
                os.replace(part_path, quarantine)
                notes.append(
                    f"拒绝异常未完成分块 {part_path.name} "
                    f"({format_bytes(size)}); 已改名为 {quarantine.name}"
                )
    return notes


class StrictRangeSession:
    """HTTP helper that never truncates an existing partial file."""

    def __init__(
        self,
        *,
        timeout: int,
        chunk_size: int,
        cookies: dict | None,
        headers: dict[str, str],
        connection_semaphore: threading.Semaphore,
    ) -> None:
        self.timeout = timeout
        self.chunk_size = chunk_size
        self.cookies = cookies
        self.headers = headers
        self.connection_semaphore = connection_semaphore
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def fetch_range_to_file(
        self,
        url: str,
        dest: Path,
        br: ByteRange,
        *,
        already: int,
        on_bytes: Callable[[int], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> int:
        """Append validated range bytes into dest. Returns newly written bytes."""
        _ensure_not_cancelled(cancel_event)
        if already < 0 or already > br.length:
            raise CorruptPartError(
                f"分块已有长度非法: {already} (range {br.start}-{br.end})"
            )
        if already == br.length:
            return 0

        req_start = br.start + already
        req_end = br.end
        headers = dict(self.headers)
        headers["Range"] = f"bytes={req_start}-{req_end}"
        expected_len = req_end - req_start + 1

        dest.parent.mkdir(parents=True, exist_ok=True)
        written = 0

        with self.connection_semaphore:
            _ensure_not_cancelled(cancel_event)
            resp = self._session.get(
                url,
                headers=headers,
                cookies=self.cookies,
                stream=True,
                timeout=self.timeout,
            )
            try:
                _ensure_not_cancelled(cancel_event)
                # We always send Range. HTTP 200 means the server ignored it —
                # never write/truncate existing partial data.
                if resp.status_code == 200:
                    raise RangeRejectedError(
                        f"服务器未接受 Range 请求（期望 206，收到 200）；"
                        f"已保留现有 {format_bytes(already)} "
                        f"（绝对文件偏移 {req_start}），不会截断，也不会把完整响应写入分块。"
                    )
                if resp.status_code != 206:
                    raise RangeRejectedError(
                        f"Range 响应状态异常: HTTP {resp.status_code} "
                        f"(请求 bytes={req_start}-{req_end})"
                    )

                mode_note = "206"
                cr_start, cr_end, _total = parse_content_range(
                    resp.headers.get("Content-Range")
                )
                if cr_start != req_start or cr_end != req_end:
                    raise CorruptPartError(
                        f"Content-Range 与请求不一致: "
                        f"请求 {req_start}-{req_end}, 响应 {cr_start}-{cr_end}"
                    )
                content_length = resp.headers.get("Content-Length")
                if content_length is not None and int(content_length) != expected_len:
                    raise CorruptPartError(
                        f"Content-Length 与期望分块长度不符: "
                        f"{content_length} != {expected_len}"
                    )

                # Append-only open: never wb truncate.
                with dest.open("ab") as fh:
                    try:
                        for chunk in resp.iter_content(chunk_size=self.chunk_size):
                            _ensure_not_cancelled(cancel_event)
                            if not chunk:
                                continue
                            remaining = expected_len - written
                            if remaining <= 0:
                                raise CorruptPartError(
                                    f"响应体长于期望范围 ({expected_len} bytes); "
                                    f"mode={mode_note}"
                                )
                            if len(chunk) > remaining:
                                fh.write(chunk[:remaining])
                                fh.flush()
                                os.fsync(fh.fileno())
                                written += remaining
                                if on_bytes:
                                    on_bytes(remaining)
                                raise CorruptPartError(
                                    f"响应体长于期望范围 ({expected_len} bytes); "
                                    f"已拒绝超长部分"
                                )
                            fh.write(chunk)
                            fh.flush()
                            written += len(chunk)
                            if on_bytes:
                                on_bytes(len(chunk))
                    except DownloadCancelled:
                        raise
                    except (
                        requests.exceptions.ChunkedEncodingError,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout,
                    ) as exc:
                        # Preserve any bytes already flushed; caller will retry remainder.
                        logger.warning(
                            "传输中断，已安全落盘 %s/%s: %s",
                            format_bytes(written),
                            format_bytes(expected_len),
                            exc,
                        )
                    if written > 0:
                        os.fsync(fh.fileno())
            finally:
                resp.close()

        if written < expected_len:
            # Short body / mid-stream disconnect: keep valid prefix for retry.
            logger.warning(
                "分块响应偏短: 获得 %s/%s，保留有效前缀后重试当前分块剩余区间",
                format_bytes(written),
                format_bytes(expected_len),
            )
            return written

        final_size = dest.stat().st_size if dest.exists() else 0
        expected_file_size = already + expected_len
        if final_size != expected_file_size:
            raise CorruptPartError(
                f"落盘后分块大小异常: {final_size} != {expected_file_size}"
            )
        return written


def download_part(
    session: StrictRangeSession,
    url: str,
    local_dir: Path,
    rel_path: str,
    br: ByteRange,
    *,
    retries: int,
    progress: DurableProgress | None = None,
    cancel_event: threading.Event | None = None,
) -> int:
    """Download one part with retries. Returns newly downloaded bytes."""
    cancel_event = cancel_event or threading.Event()
    _ensure_not_cancelled(cancel_event)

    ok_path, part_path = part_paths(local_dir, rel_path, br)
    if ok_path.exists() and ok_path.stat().st_size == br.length:
        return 0

    downloaded = 0
    last_error: Exception | None = None
    range_reject_streak = 0
    for attempt in range(1, retries + 1):
        _ensure_not_cancelled(cancel_event)
        already = part_path.stat().st_size if part_path.exists() else 0
        if already > br.length:
            quarantine = part_path.with_suffix(".part.invalid")
            os.replace(part_path, quarantine)
            raise CorruptPartError(
                f"分块超长已隔离: {quarantine} ({format_bytes(already)})"
            )
        if already == br.length:
            os.replace(part_path, ok_path)
            return downloaded
        try:
            logger.info(
                "下载分块 %s bytes=%s-%s (已有 %s, attempt %s/%s)",
                rel_path,
                br.start,
                br.end,
                format_bytes(already),
                attempt,
                retries,
            )

            def _on_bytes(n: int, prog=progress) -> None:
                if prog is not None:
                    prog.add(n)

            wrote = session.fetch_range_to_file(
                url,
                part_path,
                br,
                already=already,
                on_bytes=_on_bytes,
                cancel_event=cancel_event,
            )
            downloaded += wrote
            range_reject_streak = 0
            final = part_path.stat().st_size if part_path.exists() else 0
            if final == br.length:
                os.replace(part_path, ok_path)
                return downloaded
            last_error = SafeDownloadError(
                f"分块仍不完整: {final}/{br.length}"
            )
            logger.warning(
                "分块未完成 %s %s-%s: %s/%s，将重试剩余字节",
                rel_path,
                br.start,
                br.end,
                final,
                br.length,
            )
            if attempt < retries:
                _sleep_backoff(attempt, cancel_event)
        except DownloadCancelled:
            raise
        except RangeRejectedError as exc:
            # Do not write the 200 body (already ensured by fetch). Retry same Range.
            range_reject_streak += 1
            last_error = exc
            logger.warning(
                "Range 被拒绝 %s bytes=%s-%s attempt %s/%s（连续 %s 次）: %s；"
                "已保留现有分块，不会截断；有限重试中",
                rel_path,
                br.start,
                br.end,
                attempt,
                retries,
                range_reject_streak,
                exc,
            )
            if attempt >= retries:
                break
            _sleep_backoff(attempt, cancel_event)
        except CorruptPartError as exc:
            # Validation errors (bad Content-Range / overlong body) are not cured by retry.
            if "Content-Range" in str(exc) or "长于期望范围" in str(exc):
                raise
            last_error = exc
            logger.warning(
                "分块失败 %s bytes=%s-%s attempt %s/%s: %s",
                rel_path,
                br.start,
                br.end,
                attempt,
                retries,
                exc,
            )
            if attempt < retries:
                _sleep_backoff(attempt, cancel_event)
        except (requests.RequestException, SafeDownloadError) as exc:
            last_error = exc
            logger.warning(
                "分块失败 %s bytes=%s-%s attempt %s/%s: %s",
                rel_path,
                br.start,
                br.end,
                attempt,
                retries,
                exc,
            )
            if attempt < retries:
                _sleep_backoff(attempt, cancel_event)

    if isinstance(last_error, RangeRejectedError):
        raise RangeRejectedError(
            f"分块 Range 连续被拒绝达上限 ({retries}): {rel_path} "
            f"bytes={br.start}-{br.end}: {last_error}"
        ) from last_error
    raise SafeDownloadError(
        f"分块下载失败 {rel_path} bytes={br.start}-{br.end}: {last_error}"
    )


def merge_parts(
    local_dir: Path,
    rel_path: str,
    parts: list[ByteRange],
    *,
    expected_size: int,
    expected_sha256: str | None,
) -> Path:
    final_path = local_dir / rel_path
    merge_path = local_dir / f"{rel_path}{MERGE_SUFFIX}"
    merge_path.parent.mkdir(parents=True, exist_ok=True)
    if merge_path.exists():
        merge_path.unlink()

    digest = hashlib.sha256()
    written = 0
    with merge_path.open("wb") as out:
        if expected_size == 0:
            pass
        else:
            for br in parts:
                ok_path, _ = part_paths(local_dir, rel_path, br)
                if not ok_path.exists() or ok_path.stat().st_size != br.length:
                    merge_path.unlink(missing_ok=True)
                    raise CorruptPartError(
                        f"合并前缺少完成分块: {ok_path}"
                    )
                with ok_path.open("rb") as inp:
                    while True:
                        chunk = inp.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)

    if written != expected_size:
        merge_path.unlink(missing_ok=True)
        raise IntegrityError(
            f"合并后长度不符: {written} != {expected_size}；未生成最终文件 {final_path}"
        )

    actual_sha = digest.hexdigest()
    if expected_sha256 and actual_sha.lower() != expected_sha256.lower():
        merge_path.unlink(missing_ok=True)
        raise IntegrityError(
            f"SHA256 校验失败: expected {expected_sha256}, got {actual_sha}；"
            f"未生成最终文件 {final_path}"
        )

    os.replace(merge_path, final_path)
    logger.info(
        "最终校验通过: %s size=%s sha256=%s",
        rel_path,
        format_bytes(expected_size),
        actual_sha[:16] + "...",
    )
    return final_path


def cleanup_parts_after_success(local_dir: Path, rel_path: str) -> None:
    root = parts_root_for(local_dir, rel_path)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def download_one_file(
    *,
    remote: RemoteFile,
    url: str,
    local_dir: Path,
    config: DownloadConfig,
    session: StrictRangeSession,
    progress: DurableProgress,
    cancel_event: threading.Event | None = None,
) -> FileDownloadStats:
    cancel_event = cancel_event or threading.Event()
    _ensure_not_cancelled(cancel_event)

    target = local_dir / remote.path
    if remote.size is None:
        raise SafeDownloadError(
            f"远端未提供文件大小，无法安全分块: {remote.path}"
        )

    stats = FileDownloadStats(path=remote.path, total_size=remote.size)
    if (
        target.exists()
        and target.stat().st_size == remote.size
        and (
            not remote.sha256
            or sha256_file(target).lower() == remote.sha256.lower()
        )
    ):
        stats.skipped = True
        progress.add(remote.size)
        logger.info("跳过已完成文件: %s", remote.path)
        return stats

    if target.exists():
        if not config.force_restart_file:
            raise SafeDownloadError(
                f"目标文件已存在但校验未通过: {target} "
                f"(size={target.stat().st_size}, expected={remote.size}). "
                f"请删除后重试，或使用 --force-restart-file 显式重下。"
            )
        quarantine = target.with_suffix(target.suffix + ".bad_existing")
        os.replace(target, quarantine)
        logger.warning(
            "--force-restart-file: 已将旧目标文件改名为 %s，随后重新下载",
            quarantine,
        )

    parts = plan_parts(remote.size, config.part_size)
    if remote.size == 0:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
        stats.skipped = True
        return stats

    for note in _reject_invalid_ok_parts(local_dir, remote.path, parts):
        logger.warning(note)

    reused, advisories = adopt_legacy_artifacts(
        local_dir, remote.path, parts, progress=None
    )
    for tip in advisories:
        logger.info(tip)

    # Refresh durable progress from disk before network.
    durable = inventory_durable_bytes(local_dir, remote.path, parts)
    stats.reused_bytes = durable
    progress.add(durable)

    pending = []
    for br in parts:
        ok_path, _ = part_paths(local_dir, remote.path, br)
        if not (ok_path.exists() and ok_path.stat().st_size == br.length):
            pending.append(br)

    logger.info(
        "文件 %s: size=%s parts=%s pending=%s reused=%s part_workers=%s",
        remote.path,
        format_bytes(remote.size),
        len(parts),
        len(pending),
        format_bytes(durable),
        config.part_workers,
    )

    downloaded = 0
    if pending:
        workers = max(1, min(config.part_workers, len(pending)))
        pool = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="ms-part",
        )
        futures: dict[Future, ByteRange] = {}
        fatal: Exception | None = None
        shut_down = False
        try:
            for br in pending:
                if cancel_event.is_set():
                    break
                fut = pool.submit(
                    download_part,
                    session,
                    url,
                    local_dir,
                    remote.path,
                    br,
                    retries=config.retries,
                    progress=progress,
                    cancel_event=cancel_event,
                )
                futures[fut] = br

            for fut in as_completed(list(futures.keys())):
                br = futures[fut]
                if fut.cancelled():
                    continue
                try:
                    downloaded += fut.result()
                except DownloadCancelled as exc:
                    fatal = exc
                    break
                except RangeRejectedError as exc:
                    already = inventory_durable_bytes(
                        local_dir, remote.path, parts
                    )
                    fatal = exc
                    _shutdown_cancel_futures(
                        pool,
                        futures,
                        cancel_event,
                        (
                            "停止安全下载：Range 连续失败达上限；"
                            f"已保留现有 {format_bytes(already)}，不会截断。"
                            f" 文件={remote.path} 范围={br.start}-{br.end} 原因={exc}"
                        ),
                    )
                    shut_down = True
                    break
                except Exception as exc:
                    fatal = exc
                    _shutdown_cancel_futures(
                        pool,
                        futures,
                        cancel_event,
                        (
                            "停止安全下载：文件分块致命错误；已取消未开始任务。"
                            f" 文件={remote.path} 范围={br.start}-{br.end} 原因={exc}"
                        ),
                    )
                    shut_down = True
                    break
        finally:
            if not shut_down:
                if cancel_event.is_set():
                    for fut in futures:
                        fut.cancel()
                    pool.shutdown(wait=True, cancel_futures=True)
                else:
                    pool.shutdown(wait=True, cancel_futures=False)

        if fatal is not None:
            if not cancel_event.is_set():
                already = inventory_durable_bytes(local_dir, remote.path, parts)
                _signal_cancel(
                    cancel_event,
                    (
                        "停止安全下载：已取消该文件剩余分块；"
                        f"已保留现有 {format_bytes(already)}。"
                        f" 文件={remote.path} 原因={fatal}"
                    ),
                )
            raise fatal

    _ensure_not_cancelled(cancel_event)
    stats.downloaded_bytes = downloaded
    merge_parts(
        local_dir,
        remote.path,
        parts,
        expected_size=remote.size,
        expected_sha256=remote.sha256,
    )
    cleanup_parts_after_success(local_dir, remote.path)

    if config.clean_temp:
        _clean_legacy_temps(local_dir, remote.path, parts)
    else:
        incomplete = legacy_incomplete_path(local_dir, remote.path)
        if incomplete.exists():
            logger.info(
                "仍保留旧临时文件 %s。确认最终文件无误后可手动删除:\n  rm -f %s",
                incomplete,
                incomplete,
            )
        for br in parts:
            legacy = legacy_part_path(local_dir, remote.path, br)
            if legacy.exists():
                logger.info(
                    "仍保留旧分块 %s。确认无误后可删除:\n  rm -f %s",
                    legacy,
                    legacy,
                )

    return stats


def _clean_legacy_temps(
    local_dir: Path, rel_path: str, parts: list[ByteRange]
) -> None:
    incomplete = legacy_incomplete_path(local_dir, rel_path)
    if incomplete.exists():
        incomplete.unlink()
        logger.info("已按 --clean-temp 删除 %s", incomplete)
    for br in parts:
        legacy = legacy_part_path(local_dir, rel_path, br)
        if legacy.exists():
            legacy.unlink()
            logger.info("已按 --clean-temp 删除 %s", legacy)


def advise_temp_cleanup(local_dir: Path) -> None:
    local_dir = Path(local_dir)
    tips: list[str] = []
    for path in local_dir.rglob(f"*{LEGACY_INCOMPLETE_SUFFIX}"):
        tips.append(f"rm -f {path}")
    for path in local_dir.rglob("*"):
        if not path.is_file():
            continue
        if LEGACY_PART_RE.match(path.name) and PARTS_DIR_NAME not in path.parts:
            tips.append(f"rm -f {path}")
    if tips:
        logger.info(
            "可选清理命令（仅在确认最终文件正确后执行）:\n  %s",
            "\n  ".join(tips[:50]),
        )


def snapshot_download_safe(
    repo_id: str,
    *,
    local_dir: str | Path,
    repo_type: str = "model",
    revision: str | None = None,
    token: str | None = None,
    endpoint: str | None = None,
    config: DownloadConfig | None = None,
) -> tuple[Path, list[FileDownloadStats]]:
    """Download a ModelScope repo with safe resume semantics."""
    cfg = config or DownloadConfig()
    if cfg.file_workers < 1 or cfg.part_workers < 1:
        raise ValueError("file_workers / part_workers must be >= 1")
    if cfg.part_size < 1024:
        raise ValueError("part_size must be >= 1KiB")

    ms_ver, hub_ver = require_supported_modelscope_versions()
    revision = revision or "master"
    local_path = Path(local_dir)
    local_path.mkdir(parents=True, exist_ok=True)

    max_connections = max(1, cfg.file_workers * cfg.part_workers)
    logger.info(
        "安全下载策略: modelscope=%s modelscope-hub=%s repo=%s type=%s rev=%s "
        "file_workers=%s part_workers=%s part_size=%s max_http=%s retries=%s timeout=%ss",
        ms_ver,
        hub_ver,
        repo_id,
        repo_type,
        revision,
        cfg.file_workers,
        cfg.part_workers,
        format_bytes(cfg.part_size),
        max_connections,
        cfg.retries,
        cfg.timeout,
    )

    files, url_for, cookies = _hub_api_list_and_urls(
        repo_id=repo_id,
        repo_type=repo_type,
        revision=revision,
        token=token,
        endpoint=endpoint,
    )
    if not files:
        logger.info("仓库无可下载文件: %s", repo_id)
        return local_path, []

    unknown = [f.path for f in files if f.size is None]
    if unknown:
        raise SafeDownloadError(
            "以下文件缺少 Size，无法安全分块下载: " + ", ".join(unknown[:10])
        )

    total_size = sum(int(f.size or 0) for f in files)
    progress = DurableProgress(total_size, desc=f"MS {repo_id}")
    connection_sem = threading.Semaphore(max_connections)
    headers = {
        "User-Agent": cfg.user_agent,
        # Avoid opaque compressed bodies interfering with Content-Length/Range.
        "Accept-Encoding": "identity",
    }
    session = StrictRangeSession(
        timeout=cfg.timeout,
        chunk_size=cfg.chunk_size,
        cookies=cookies,
        headers=headers,
        connection_semaphore=connection_sem,
    )

    stats: list[FileDownloadStats] = []
    cancel_event = threading.Event()
    fatal: Exception | None = None
    try:
        workers = max(1, min(cfg.file_workers, len(files)))
        pool = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="ms-file",
        )
        futures: dict[Future, RemoteFile] = {}
        shut_down = False
        try:
            for remote in files:
                if cancel_event.is_set():
                    break
                fut = pool.submit(
                    download_one_file,
                    remote=remote,
                    url=url_for(remote.path),
                    local_dir=local_path,
                    config=cfg,
                    session=session,
                    progress=progress,
                    cancel_event=cancel_event,
                )
                futures[fut] = remote

            for fut in as_completed(list(futures.keys())):
                remote = futures[fut]
                if fut.cancelled():
                    continue
                try:
                    stats.append(fut.result())
                except Exception as exc:
                    fatal = exc
                    _shutdown_cancel_futures(
                        pool,
                        futures,
                        cancel_event,
                        (
                            "停止安全下载：仓库文件级致命错误；"
                            "已取消未开始的文件任务，等待进行中的请求安全退出。"
                            f" 文件={remote.path} 原因={exc}"
                        ),
                    )
                    shut_down = True
                    break
        finally:
            if not shut_down:
                if cancel_event.is_set():
                    for fut in futures:
                        fut.cancel()
                    pool.shutdown(wait=True, cancel_futures=True)
                else:
                    pool.shutdown(wait=True, cancel_futures=False)
    finally:
        session.close()
        progress.close()

    if fatal is not None:
        advise_temp_cleanup(local_path)
        raise SafeDownloadError(
            f"下载失败（已 fail-fast 取消剩余任务）: {fatal}"
        ) from fatal

    reused = sum(s.reused_bytes for s in stats)
    downloaded = sum(s.downloaded_bytes for s in stats)
    logger.info(
        "全部完成: files=%s reused=%s downloaded=%s total=%s",
        len(stats),
        format_bytes(reused),
        format_bytes(downloaded),
        format_bytes(total_size),
    )
    advise_temp_cleanup(local_path)
    return local_path, stats

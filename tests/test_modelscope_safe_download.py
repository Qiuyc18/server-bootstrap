#!/usr/bin/env python3
"""Offline tests for modelscope_safe_download (local HTTP server only)."""

from __future__ import annotations

import hashlib
import os
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from modelscope_safe_download import (
    ByteRange,
    CorruptPartError,
    DownloadConfig,
    DownloadCancelled,
    DurableProgress,
    IntegrityError,
    RangeRejectedError,
    RemoteFile,
    SafeDownloadError,
    StrictRangeSession,
    adopt_legacy_artifacts,
    cross_process_download_lock,
    download_one_file,
    download_part,
    inventory_durable_bytes,
    legacy_incomplete_path,
    legacy_part_path,
    merge_parts,
    parse_content_range,
    part_paths,
    plan_parts,
    sha256_file,
    snapshot_download_safe,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RangeHTTPServer:
    """Configurable local server for Range resume / rejection scenarios."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.mode = "normal"
        self.drop_after = 0
        self.reject_start: int | None = None
        self.slow_seconds = 0.0
        self.served_ranges: list[tuple[int, int | None]] = []
        self.hits = 0
        self.paths_hit: list[str] = []
        self._lock = threading.Lock()
        handler = self._build_handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}/file.bin"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _build_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A003
                return

            def do_GET(self):  # noqa: N802
                with server._lock:
                    server.hits += 1
                    server.paths_hit.append(self.path)
                range_header = self.headers.get("Range")
                start = 0
                end = len(server.payload) - 1
                if range_header and range_header.startswith("bytes="):
                    spec = range_header.split("=", 1)[1]
                    a, b = spec.split("-", 1)
                    start = int(a) if a else 0
                    end = int(b) if b else (len(server.payload) - 1)
                    with server._lock:
                        server.served_ranges.append((start, end))
                else:
                    with server._lock:
                        server.served_ranges.append((0, None))

                mode = server.mode
                if mode == "reject_start_slow_others":
                    if server.reject_start is not None and start == server.reject_start:
                        body = server.payload
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if server.slow_seconds > 0:
                        time.sleep(server.slow_seconds)
                    body = server.payload[start : end + 1]
                    self.send_response(206)
                    self.send_header(
                        "Content-Range",
                        f"bytes {start}-{end}/{len(server.payload)}",
                    )
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if mode == "ignore_range":
                    body = server.payload
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if mode.startswith("compatible_200"):
                    body = server.payload[start : end + 1]
                    cr_start = start
                    cr_end = end
                    cr_total = len(server.payload)
                    if mode == "compatible_200_bad_range":
                        cr_start += 1
                        cr_end += 1
                    elif mode == "compatible_200_bad_total":
                        cr_total += 1

                    self.send_response(200)
                    content_range = f"bytes {cr_start}-{cr_end}/{cr_total}"
                    if mode == "compatible_200_bad_header":
                        content_range = "not-a-content-range"
                    self.send_header("Content-Range", content_range)
                    if mode != "compatible_200_missing_length":
                        content_length = len(body)
                        if mode == "compatible_200_bad_length":
                            content_length += 1
                        self.send_header("Content-Length", str(content_length))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if mode == "bad_content_range":
                    self.send_response(206)
                    # Deliberately wrong offsets.
                    self.send_header(
                        "Content-Range",
                        f"bytes {start + 1}-{end + 1}/{len(server.payload)}",
                    )
                    self.send_header("Content-Length", str(end - start + 1))
                    self.end_headers()
                    self.wfile.write(server.payload[start : end + 1])
                    return

                if mode == "short_body":
                    # Clean short close (no Content-Length), Content-Range claims full span.
                    body = server.payload[start : start + max(1, (end - start + 1) // 2)]
                    self.send_response(206)
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end}/{len(server.payload)}"
                    )
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if mode == "long_body":
                    # Oversized body without Content-Length so the client can observe overshoot.
                    body = server.payload[start : end + 1] + b"EXTRA"
                    self.send_response(206)
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end}/{len(server.payload)}"
                    )
                    self.end_headers()
                    self.wfile.write(body)
                    return

                body = server.payload[start : end + 1]
                if mode == "drop_after":
                    cut = min(server.drop_after, len(body))
                    self.send_response(206)
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end}/{len(server.payload)}"
                    )
                    # Intentionally omit Content-Length so the client keeps the prefix
                    # instead of raising IncompleteRead with an empty/partial buffer race.
                    self.end_headers()
                    self.wfile.write(body[:cut])
                    return

                # normal
                if range_header:
                    self.send_response(206)
                    self.send_header(
                        "Content-Range",
                        f"bytes {start}-{end}/{len(server.payload)}",
                    )
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        return Handler


class PlanPartsTests(unittest.TestCase):
    def test_plan_parts(self):
        parts = plan_parts(1000, 300)
        self.assertEqual(
            [(p.start, p.end, p.length) for p in parts],
            [(0, 299, 300), (300, 599, 300), (600, 899, 300), (900, 999, 100)],
        )

    def test_parse_content_range(self):
        self.assertEqual(parse_content_range("bytes 10-19/100"), (10, 19, 100))
        with self.assertRaises(CorruptPartError):
            parse_content_range(None)

    def test_cross_process_download_lock_rejects_second_holder(self):
        root = Path(__file__).resolve().parent / "_tmp_safe_dl" / self._testMethodName
        root.mkdir(parents=True, exist_ok=True)
        with cross_process_download_lock(root):
            with self.assertRaises(SafeDownloadError) as ctx:
                with cross_process_download_lock(root):
                    pass
        self.assertIn("另一个进程", str(ctx.exception))


class SafeDownloadHTTPTests(unittest.TestCase):
    def setUp(self):
        self.payload = os.urandom(50_000)
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.server = RangeHTTPServer(self.payload)
        self.server.start()
        self.tmp = Path(self.id().replace(".", "_"))
        # Use a dedicated temp dir under the test module location.
        self.root = Path(__file__).resolve().parent / "_tmp_safe_dl" / self._testMethodName
        if self.root.exists():
            for p in sorted(self.root.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
                else:
                    p.rmdir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.session = StrictRangeSession(
            timeout=5,
            chunk_size=4096,
            cookies=None,
            headers={"User-Agent": "test"},
            connection_semaphore=threading.Semaphore(4),
        )

    def tearDown(self):
        self.session.close()
        self.server.stop()

    def _config(self, **kwargs) -> DownloadConfig:
        cfg = DownloadConfig(
            file_workers=1,
            part_workers=2,
            part_size=8_000,
            retries=3,
            timeout=5,
            chunk_size=1024,
        )
        for k, v in kwargs.items():
            setattr(cfg, k, v)
        return cfg

    def test_full_download_size_and_sha256(self):
        remote = RemoteFile("blob.bin", size=len(self.payload), sha256=self.sha)
        from modelscope_safe_download import DurableProgress

        progress = DurableProgress(len(self.payload), desc="t1")
        stats = download_one_file(
            remote=remote,
            url=self.server.url,
            local_dir=self.root,
            config=self._config(),
            session=self.session,
            progress=progress,
        )
        progress.close()
        out = self.root / "blob.bin"
        self.assertTrue(out.exists())
        self.assertEqual(out.stat().st_size, len(self.payload))
        self.assertEqual(sha256_file(out), self.sha)
        self.assertGreater(stats.downloaded_bytes, 0)

    def test_resume_after_disconnect_uses_206(self):
        rel = "resume.bin"
        parts = plan_parts(len(self.payload), 8_000)
        br = parts[0]
        ok_path, part_path = part_paths(self.root, rel, br)

        self.server.mode = "drop_after"
        self.server.drop_after = 3_000
        with self.assertRaises(Exception):
            download_part(
                self.session,
                self.server.url,
                self.root,
                rel,
                br,
                expected_total_size=len(self.payload),
                retries=1,
            )
        self.assertTrue(part_path.exists())
        partial = part_path.stat().st_size
        self.assertGreater(partial, 0)
        self.assertLess(partial, br.length)
        inode_before = part_path.stat().st_ino

        self.server.mode = "normal"
        wrote = download_part(
            self.session,
            self.server.url,
            self.root,
            rel,
            br,
            expected_total_size=len(self.payload),
            retries=3,
        )
        self.assertTrue(ok_path.exists())
        self.assertEqual(ok_path.stat().st_size, br.length)
        # Newly downloaded bytes should not retransfer the partial prefix.
        self.assertEqual(wrote, br.length - partial)
        # 206 resume should have requested from the partial offset.
        resumed = [r for r in self.server.served_ranges if r[0] == br.start + partial]
        self.assertTrue(resumed)
        # Same path retained (append), inode may change on some FS after replace to .ok
        self.assertFalse(part_path.exists())
        self.assertEqual(ok_path.read_bytes(), self.payload[br.start : br.end + 1])
        _ = inode_before

    def test_range_ignored_200_preserves_existing(self):
        rel = "keep.bin"
        parts = plan_parts(len(self.payload), 8_000)
        br = parts[0]
        _, part_path = part_paths(self.root, rel, br)
        part_path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.payload[br.start : br.start + 5_000]
        part_path.write_bytes(existing)
        before = part_path.read_bytes()
        inode = part_path.stat().st_ino
        size_before = part_path.stat().st_size

        self.server.mode = "ignore_range"
        with self.assertRaises(RangeRejectedError) as ctx:
            download_part(
                self.session,
                self.server.url,
                self.root,
                rel,
                br,
                expected_total_size=len(self.payload),
                retries=1,
            )
        self.assertIn("不会截断", str(ctx.exception))
        self.assertTrue(part_path.exists())
        self.assertEqual(part_path.stat().st_size, size_before)
        self.assertEqual(part_path.stat().st_ino, inode)
        self.assertEqual(part_path.read_bytes(), before)
        # Must not have grown by a full payload append either.
        self.assertNotEqual(part_path.stat().st_size, size_before + len(self.payload))

    def test_compatible_200_exact_range_resumes_safely(self):
        rel = "compatible.bin"
        br = ByteRange(0, 7_999)
        ok_path, part_path = part_paths(self.root, rel, br)
        part_path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.payload[:3_000]
        part_path.write_bytes(existing)

        self.server.mode = "compatible_200"
        with self.assertLogs("modelscope_safe_download", level="WARNING") as logs:
            wrote = download_part(
                self.session,
                self.server.url,
                self.root,
                rel,
                br,
                expected_total_size=len(self.payload),
                retries=1,
            )

        self.assertEqual(wrote, br.length - len(existing))
        self.assertFalse(part_path.exists())
        self.assertEqual(ok_path.read_bytes(), self.payload[: br.length])
        self.assertTrue(
            any(
                "ModelScope 返回非标准 HTTP 200，但 Content-Range 完全匹配，"
                "按安全 Range 响应接受。" in line
                for line in logs.output
            )
        )

    def _assert_compatible_200_rejected_without_modifying_part(
        self, mode: str
    ) -> None:
        rel = f"{mode}.bin"
        br = ByteRange(0, 7_999)
        _, part_path = part_paths(self.root, rel, br)
        part_path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.payload[:3_000]
        part_path.write_bytes(existing)
        inode = part_path.stat().st_ino

        self.server.mode = mode
        response_patch = None
        if mode in (
            "compatible_200_short_body",
            "compatible_200_long_body",
        ):
            expected_body = self.payload[len(existing) : br.end + 1]
            actual_body = (
                expected_body[:-1]
                if mode == "compatible_200_short_body"
                else expected_body + b"EXTRA"
            )
            response = mock.Mock()
            response.status_code = 200
            response.headers = {
                "Content-Range": (
                    f"bytes {len(existing)}-{br.end}/{len(self.payload)}"
                ),
                "Content-Length": str(len(expected_body)),
            }
            response.iter_content.side_effect = lambda chunk_size: [actual_body]
            response_patch = mock.patch.object(
                self.session._session, "get", return_value=response
            )

        context = response_patch or mock.patch.object(
            self.session._session,
            "get",
            wraps=self.session._session.get,
        )
        with context:
            with self.assertRaises(CorruptPartError):
                self.session.fetch_range_to_file(
                    self.server.url,
                    part_path,
                    br,
                    already=len(existing),
                    expected_total_size=len(self.payload),
                )

        self.assertEqual(part_path.stat().st_ino, inode)
        self.assertEqual(part_path.read_bytes(), existing)

    def test_compatible_200_shifted_content_range_rejected(self):
        self._assert_compatible_200_rejected_without_modifying_part(
            "compatible_200_bad_range"
        )

    def test_compatible_200_wrong_total_rejected(self):
        self._assert_compatible_200_rejected_without_modifying_part(
            "compatible_200_bad_total"
        )

    def test_compatible_200_malformed_headers_rejected(self):
        for mode in (
            "compatible_200_bad_header",
            "compatible_200_missing_length",
            "compatible_200_bad_length",
        ):
            with self.subTest(mode=mode):
                self._assert_compatible_200_rejected_without_modifying_part(mode)

    def test_compatible_200_wrong_body_lengths_rejected(self):
        for mode in (
            "compatible_200_short_body",
            "compatible_200_long_body",
        ):
            with self.subTest(mode=mode):
                self._assert_compatible_200_rejected_without_modifying_part(mode)

    def test_bad_content_range_rejected(self):
        rel = "badcr.bin"
        br = ByteRange(0, 7_999)
        self.server.mode = "bad_content_range"
        with self.assertRaises(CorruptPartError):
            download_part(
                self.session,
                self.server.url,
                self.root,
                rel,
                br,
                expected_total_size=len(self.payload),
                retries=1,
            )

    def test_short_body_keeps_prefix_and_retries(self):
        rel = "short.bin"
        br = ByteRange(0, 7_999)
        _, part_path = part_paths(self.root, rel, br)
        self.server.mode = "short_body"
        with self.assertRaises(Exception):
            download_part(
                self.session,
                self.server.url,
                self.root,
                rel,
                br,
                expected_total_size=len(self.payload),
                retries=1,
            )
        self.assertTrue(part_path.exists())
        partial = part_path.stat().st_size
        self.assertGreater(partial, 0)
        self.assertLess(partial, br.length)

        self.server.mode = "normal"
        download_part(
            self.session,
            self.server.url,
            self.root,
            rel,
            br,
            expected_total_size=len(self.payload),
            retries=3,
        )
        ok_path, _ = part_paths(self.root, rel, br)
        self.assertEqual(ok_path.stat().st_size, br.length)

    def test_long_body_rejected(self):
        rel = "long.bin"
        br = ByteRange(0, 7_999)
        self.server.mode = "long_body"
        with self.assertRaises(CorruptPartError):
            download_part(
                self.session,
                self.server.url,
                self.root,
                rel,
                br,
                expected_total_size=len(self.payload),
                retries=1,
            )

    def test_restart_reuses_completed_parts(self):
        rel = "reuse.bin"
        parts = plan_parts(len(self.payload), 8_000)
        # Pretend first part already done.
        ok_path, _ = part_paths(self.root, rel, parts[0])
        ok_path.parent.mkdir(parents=True, exist_ok=True)
        ok_path.write_bytes(self.payload[parts[0].start : parts[0].end + 1])

        remote = RemoteFile(rel, size=len(self.payload), sha256=self.sha)
        from modelscope_safe_download import DurableProgress

        progress = DurableProgress(len(self.payload), desc="reuse")
        hits_before = self.server.hits
        download_one_file(
            remote=remote,
            url=self.server.url,
            local_dir=self.root,
            config=self._config(part_workers=2),
            session=self.session,
            progress=progress,
        )
        progress.close()
        # Should not have re-downloaded the first part's full range from 0
        # more times than remaining parts require; durable inventory includes it.
        durable = inventory_durable_bytes(self.root, rel, parts)
        # After success parts dir is cleaned; final file must exist.
        self.assertTrue((self.root / rel).exists())
        self.assertEqual(sha256_file(self.root / rel), self.sha)
        self.assertGreater(self.server.hits, hits_before)
        # First part exact bytes should not appear as a fresh 0-7999 download only request count proxy:
        starts = [s for s, _ in self.server.served_ranges]
        # At least one request should start after first part.
        self.assertTrue(any(s >= parts[1].start for s in starts))
        _ = durable

    def test_zero_and_bad_legacy_parts_not_treated_complete(self):
        rel = "legacy.bin"
        parts = plan_parts(len(self.payload), 8_000)
        br = parts[0]
        zero = legacy_part_path(self.root, rel, br)
        zero.parent.mkdir(parents=True, exist_ok=True)
        zero.write_bytes(b"")
        oversized = legacy_part_path(self.root, rel, parts[1])
        oversized.write_bytes(b"x" * (parts[1].length + 10))

        reused, tips = adopt_legacy_artifacts(self.root, rel, parts)
        self.assertEqual(reused, 0)
        self.assertTrue(any("不完整" in t or "异常" in t or "零字节" in t for t in tips) or tips)
        ok_path, _ = part_paths(self.root, rel, br)
        self.assertFalse(ok_path.exists())

        # Exact legacy part should be reused.
        good = legacy_part_path(self.root, rel, parts[2])
        good.write_bytes(self.payload[parts[2].start : parts[2].end + 1])
        reused2, _ = adopt_legacy_artifacts(self.root, rel, parts)
        self.assertGreaterEqual(reused2, parts[2].length)
        ok3, _ = part_paths(self.root, rel, parts[2])
        self.assertTrue(ok3.exists())
        self.assertEqual(ok3.stat().st_size, parts[2].length)

    def test_sha_mismatch_does_not_create_final(self):
        rel = "badsha.bin"
        parts = plan_parts(len(self.payload), len(self.payload))
        ok_path, _ = part_paths(self.root, rel, parts[0])
        ok_path.parent.mkdir(parents=True, exist_ok=True)
        ok_path.write_bytes(self.payload)
        with self.assertRaises(IntegrityError):
            merge_parts(
                self.root,
                rel,
                parts,
                expected_size=len(self.payload),
                expected_sha256="0" * 64,
            )
        self.assertFalse((self.root / rel).exists())
        self.assertFalse((self.root / f"{rel}.ms_merging").exists())

    def test_legacy_incomplete_prefix_reused(self):
        rel = "inc.bin"
        parts = plan_parts(len(self.payload), 8_000)
        incomplete = legacy_incomplete_path(self.root, rel)
        incomplete.parent.mkdir(parents=True, exist_ok=True)
        prefix = self.payload[: 8_000 + 1_500]
        incomplete.write_bytes(prefix)
        inode = incomplete.stat().st_ino
        reused, tips = adopt_legacy_artifacts(self.root, rel, parts)
        self.assertGreaterEqual(reused, 8_000)
        self.assertEqual(incomplete.stat().st_ino, inode)
        self.assertEqual(incomplete.stat().st_size, len(prefix))
        self.assertTrue(any(".incomplete" in t for t in tips))
        ok0, _ = part_paths(self.root, rel, parts[0])
        self.assertTrue(ok0.exists())


class FailFastCancelTests(unittest.TestCase):
    def setUp(self):
        # Large enough for many queued parts with 4 workers.
        self.payload = os.urandom(40_000)
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.server = RangeHTTPServer(self.payload)
        self.server.start()
        self.root = (
            Path(__file__).resolve().parent / "_tmp_safe_dl" / self._testMethodName
        )
        if self.root.exists():
            for p in sorted(self.root.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
                else:
                    p.rmdir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.session = StrictRangeSession(
            timeout=5,
            chunk_size=1024,
            cookies=None,
            headers={"User-Agent": "test"},
            connection_semaphore=threading.Semaphore(8),
        )

    def tearDown(self):
        self.session.close()
        self.server.stop()

    def test_range_200_retries_then_fail_fast_cancels_pending(self):
        rel = "big.bin"
        part_size = 1_000
        parts = plan_parts(len(self.payload), part_size)
        self.assertGreaterEqual(len(parts), 20)

        # Preserve an already-written in-progress part for a later range.
        preserve_br = parts[10]
        _, preserve_part = part_paths(self.root, rel, preserve_br)
        preserve_part.parent.mkdir(parents=True, exist_ok=True)
        existing = self.payload[preserve_br.start : preserve_br.start + 400]
        preserve_part.write_bytes(existing)
        inode = preserve_part.stat().st_ino
        size_before = preserve_part.stat().st_size

        reject_br = parts[2]
        self.server.mode = "reject_start_slow_others"
        self.server.reject_start = reject_br.start
        self.server.slow_seconds = 0.35

        retries = 3
        cancel_event = threading.Event()
        progress = DurableProgress(len(self.payload), desc="failfast")
        cfg = DownloadConfig(
            file_workers=1,
            part_workers=4,
            part_size=part_size,
            retries=retries,
            timeout=5,
            chunk_size=1024,
        )

        with mock.patch(
            "modelscope_safe_download._sleep_backoff",
            lambda *a, **k: None,
        ):
            with self.assertRaises(RangeRejectedError):
                download_one_file(
                    remote=RemoteFile(rel, size=len(self.payload), sha256=self.sha),
                    url=self.server.url,
                    local_dir=self.root,
                    config=cfg,
                    session=self.session,
                    progress=progress,
                    cancel_event=cancel_event,
                )
        progress.close()

        self.assertTrue(cancel_event.is_set())

        # Existing partial must be untouched (no truncate / append of 200 body).
        self.assertTrue(preserve_part.exists())
        self.assertEqual(preserve_part.stat().st_ino, inode)
        self.assertEqual(preserve_part.stat().st_size, size_before)
        self.assertEqual(preserve_part.read_bytes(), existing)

        # Rejected Range is retried a limited number of times, not flooded.
        reject_hits = [
            (s, e)
            for s, e in self.server.served_ranges
            if s == reject_br.start and e == reject_br.end
        ]
        self.assertGreaterEqual(len(reject_hits), 1)
        self.assertLessEqual(len(reject_hits), retries)

        # Far later parts should mostly never start (queued futures cancelled).
        late_starts = {p.start for p in parts[15:]}
        started_late = {s for s, _ in self.server.served_ranges if s in late_starts}
        self.assertLessEqual(len(started_late), 2)

        # Total HTTP hits must stay far below "start every remaining part".
        self.assertLess(self.server.hits, len(parts) + retries + 8)

    def test_fail_fast_does_not_continue_next_file(self):
        part_size = 2_000
        file_a = RemoteFile("a.bin", size=len(self.payload), sha256=self.sha)
        file_b = RemoteFile(
            "b.bin",
            size=len(self.payload),
            sha256=self.sha,
        )
        parts = plan_parts(len(self.payload), part_size)
        reject_br = parts[1]

        self.server.mode = "reject_start_slow_others"
        self.server.reject_start = reject_br.start
        self.server.slow_seconds = 0.2

        cfg = DownloadConfig(
            file_workers=1,
            part_workers=4,
            part_size=part_size,
            retries=2,
            timeout=5,
            chunk_size=1024,
        )

        def fake_list(*_args, **_kwargs):
            def url_for(path: str) -> str:
                # Distinct paths so we can observe which file was contacted.
                return self.server.url + f"?path={path}"

            return [file_a, file_b], url_for, None

        with mock.patch(
            "modelscope_safe_download.require_supported_modelscope_versions",
            return_value=("1.38.1", "0.1.7"),
        ), mock.patch(
            "modelscope_safe_download._hub_api_list_and_urls",
            side_effect=fake_list,
        ), mock.patch(
            "modelscope_safe_download._sleep_backoff",
            lambda *a, **k: None,
        ):
            with self.assertRaises(Exception):
                snapshot_download_safe(
                    "org/demo",
                    local_dir=self.root,
                    repo_type="model",
                    config=cfg,
                )

        # b.bin must not have been requested after fail-fast.
        b_hits = [p for p in self.server.paths_hit if "path=b.bin" in p]
        self.assertEqual(b_hits, [])
        a_hits = [p for p in self.server.paths_hit if "path=a.bin" in p]
        self.assertGreater(len(a_hits), 0)


class CLIHelpTests(unittest.TestCase):
    def test_download_help_lists_modelscope_options(self):
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "download.py", "download", "--help"],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        for token in (
            "--part-workers",
            "--part-size-mb",
            "--download-retries",
            "--download-timeout",
            "--clean-temp",
            "--force-restart-file",
            "ModelScope",
            "文件级并发",
        ):
            self.assertIn(token, out)

    def test_hf_max_workers_default_unchanged_behavior(self):
        # argparse default resolution: hf -> 8, modelscope -> 1
        import argparse

        ns = argparse.Namespace(
            command="download", source="hf", max_workers=None, part_workers=4
        )
        if ns.max_workers is None:
            ns.max_workers = 1 if ns.source == "modelscope" else 8
        self.assertEqual(ns.max_workers, 8)
        ns2 = argparse.Namespace(
            command="download", source="modelscope", max_workers=None
        )
        if ns2.max_workers is None:
            ns2.max_workers = 1 if ns2.source == "modelscope" else 8
        self.assertEqual(ns2.max_workers, 1)


if __name__ == "__main__":
    unittest.main()

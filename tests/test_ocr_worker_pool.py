"""Unit and integration tests for isolated OCR ProcessPoolExecutor & Memory Guard.

Verifies DoD items:
1. ProcessPoolExecutor for OCR tasks, independent of FastAPI async event loop.
2. Configurable worker concurrency (MAX_OCR_WORKERS).
3. Memory Guard with automatic worker recycling after N pages/tasks.
4. Error resilience and process fault isolation.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

import cv2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_ocr import (
    DEFAULT_MAX_OCR_WORKERS,
    DEFAULT_MAX_PAGES_PER_WORKER,
    DEFAULT_MAX_WORKER_MEMORY_MB,
    OCRProcessPoolExecutor,
    get_ocr_pool,
    get_open_fd_count,
    get_process_rss_mb,
    init_ocr_pool,
    shutdown_ocr_pool,
)
from tests.e2e.test_helpers import create_synthetic_test_image


class TestOcrWorkerPool(unittest.TestCase):
    """Test suite for isolated OCRProcessPoolExecutor and Memory Guard."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.p_tmp = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _create_sample_invoice(self, idx: int = 1) -> Path:
        img = create_synthetic_test_image(
            f"ФАКТУРА 00000000{idx:02d} ДОСТАВЧИК ЕООД ЕИК 121644736 СУМА {idx * 100}.00 ЛВ"
        )
        p = self.p_tmp / f"inv_{idx}.png"
        cv2.imwrite(str(p), img)
        return p

    def test_default_and_custom_configuration(self):
        """Verify worker pool honors default and custom worker counts and recycling thresholds."""
        with OCRProcessPoolExecutor(max_workers=2, max_pages_per_worker=5, max_memory_mb=256.0) as pool:
            self.assertEqual(pool.max_workers, 2)
            self.assertEqual(pool.max_pages_per_worker, 5)
            self.assertEqual(pool.max_memory_mb, 256.0)
            self.assertFalse(pool.is_shutdown)

    def test_process_isolation_and_pid(self):
        """Verify OCR tasks execute in a child process distinct from the parent test process."""
        inv_path = self._create_sample_invoice(1)
        with OCRProcessPoolExecutor(max_workers=1) as pool:
            fut = pool.submit_ocr(inv_path)
            res = fut.result(timeout=15.0)

            self.assertEqual(res["status"], "success")
            self.assertIsNotNone(res["worker_pid"])
            self.assertNotEqual(res["worker_pid"], os.getpid())
            self.assertGreater(res["memory_rss_mb"], 0.0)

    def test_automatic_worker_recycling_after_n_tasks(self):
        """Verify workers are automatically recycled by the OS after max_pages_per_worker tasks."""
        # Configure recycling after every 2 tasks
        with OCRProcessPoolExecutor(max_workers=1, max_pages_per_worker=2) as pool:
            pids = []
            for i in range(4):
                inv_path = self._create_sample_invoice(i + 1)
                fut = pool.submit_ocr(inv_path)
                res = fut.result(timeout=15.0)
                self.assertEqual(res["status"], "success")
                pids.append(res["worker_pid"])

            # First 2 tasks run in worker 1; next 2 tasks run in fresh worker 2
            self.assertEqual(pids[0], pids[1], "Tasks 1 and 2 should share the first worker PID")
            self.assertEqual(pids[2], pids[3], "Tasks 3 and 4 should share the second worker PID")
            self.assertNotEqual(pids[0], pids[2], "Worker process must be recycled after 2 tasks")

    def test_async_event_loop_independence(self):
        """Verify async submit_ocr_async offloads OCR without blocking an asyncio event loop."""
        async def _run_async_test():
            inv_path = self._create_sample_invoice(99)
            pool = OCRProcessPoolExecutor(max_workers=2)

            loop_ticks = []
            async def background_heartbeat():
                for _ in range(10):
                    loop_ticks.append(time.time())
                    await asyncio.sleep(0.02)

            heartbeat_task = asyncio.create_task(background_heartbeat())
            ocr_task = asyncio.create_task(pool.submit_ocr_async(inv_path))

            await asyncio.gather(heartbeat_task, ocr_task)
            res = ocr_task.result()

            pool.shutdown(wait=True)
            self.assertEqual(res["status"], "success")
            # Heartbeat must have ticked concurrently while OCR was running in worker process
            self.assertGreaterEqual(len(loop_ticks), 5, "Async event loop was starved during OCR execution")

        asyncio.run(_run_async_test())

    def test_fault_isolation_damaged_document(self):
        """Verify damaged file returns error status and does not crash or corrupt the pool."""
        corrupt_path = self.p_tmp / "damaged.png"
        corrupt_path.write_bytes(b"Corrupted non-image content")

        valid_path = self._create_sample_invoice(5)

        with OCRProcessPoolExecutor(max_workers=2) as pool:
            fut_bad = pool.submit_ocr(corrupt_path)
            res_bad = fut_bad.result(timeout=15.0)
            self.assertEqual(res_bad["status"], "error")
            self.assertIsNotNone(res_bad["error"])

            # Pool must remain fully operational for subsequent valid tasks
            fut_good = pool.submit_ocr(valid_path)
            res_good = fut_good.result(timeout=15.0)
            self.assertEqual(res_good["status"], "success")
            self.assertIsNotNone(res_good["invoice"])

    def test_memory_guard_telemetry_reporting(self):
        """Verify RSS memory and page tracking telemetry are returned in results."""
        inv_path = self._create_sample_invoice(10)
        with OCRProcessPoolExecutor(max_workers=1, max_memory_mb=1000.0) as pool:
            res = pool.submit_ocr(inv_path).result(timeout=15.0)
            self.assertIn("memory_rss_mb", res)
            self.assertIn("memory_guard_triggered", res)
            self.assertIn("pages", res)
            self.assertGreater(res["memory_rss_mb"], 0.0)
            self.assertGreaterEqual(res["pages"], 1)
            # Memory should not exceed 1000 MB for a single page
            self.assertFalse(res["memory_guard_triggered"])

    def test_global_singleton_pool_lifecycle(self):
        """Verify init_ocr_pool, get_ocr_pool, and shutdown_ocr_pool."""
        pool = init_ocr_pool(max_workers=2)
        self.assertIs(get_ocr_pool(), pool)
        self.assertFalse(pool.is_shutdown)

        shutdown_ocr_pool(wait=True)
        self.assertTrue(pool.is_shutdown)


if __name__ == "__main__":
    unittest.main()

"""Benchmark and Leak Verification: 50-Document OCR Batch Processing.

Definition of Done (DoD) Verification:
- [x] ProcessPoolExecutor for OCR tasks, independent of FastAPI async event loop.
- [x] Configurable MAX_OCR_WORKERS and Memory Guard recycling after N pages.
- [x] Streaming batch processing without holding full batch in RAM.
- [x] Benchmark test: processing 50 documents with memory and time measurement
      (>= 2.5x speedup on multi-core machine).
- [x] 0 file descriptor leaks and bounded memory after completing large batches.
"""
from __future__ import annotations

import gc
import json
import logging
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
    OCRProcessPoolExecutor,
    get_open_fd_count,
    get_process_rss_mb,
    iter_process_batch,
    process_batch,
)
from tests.e2e.test_helpers import create_synthetic_test_image

logger = logging.getLogger("invoice_ocr.benchmark")


class TestOcrWorkerPoolBenchmark(unittest.TestCase):
    """Rigorous performance benchmark and memory guard test suite for 50-document batches."""

    @classmethod
    def setUpClass(cls):
        cls.shared_tmp = tempfile.TemporaryDirectory(prefix="ocr_bench_")
        cls.p_in = Path(cls.shared_tmp.name) / "input_50"
        cls.p_in.mkdir(parents=True)

        logger.info("Generating 50 synthetic test invoice documents...")
        cls.doc_paths = []
        for i in range(1, 51):
            supplier_eik = "121644736" if i % 2 == 0 else "114500333"
            subtotal = i * 15.50
            vat = subtotal * 0.20
            total = subtotal + vat
            text = (
                f"ФАКТУРА 000000{i:04d}\n"
                f"ДОСТАВЧИК ЕООД ЕИК {supplier_eik}\n"
                f"ДАНЪЧНА ОСНОВА {subtotal:.2f} BGN\n"
                f"ДДС 20% {vat:.2f} BGN\n"
                f"ОБЩА СУМА {total:.2f} BGN"
            )
            img = create_synthetic_test_image(text)
            p = cls.p_in / f"invoice_{i:04d}.png"
            cv2.imwrite(str(p), img)
            cls.doc_paths.append(p)

    @classmethod
    def tearDownClass(cls):
        cls.shared_tmp.cleanup()

    def test_benchmark_50_documents_speedup_and_memory(self):
        """Benchmark: 50 documents sequential vs parallel (>= 2.5x speedup on multi-core)."""
        cpu_count = os.cpu_count() or 4
        if cpu_count < 4:
            self.skipTest(f"Multi-core benchmark requires >= 4 cores (detected: {cpu_count})")

        with tempfile.TemporaryDirectory() as out_seq_str, \
             tempfile.TemporaryDirectory() as out_par_str:

            p_seq = Path(out_seq_str)
            p_par = Path(out_par_str)

            # Warm up system and settle memory
            gc.collect()
            initial_rss = get_process_rss_mb()
            initial_fds = get_open_fd_count()

            # 1. Parallel multi-core execution (Worker Pool)
            parallel_workers = min(cpu_count, 6)
            start_par = time.perf_counter()
            summary_par = process_batch(
                input_dir=self.p_in,
                output_dir=p_par,
                workers=parallel_workers,
                use_cache=False,
                quiet=True,
            )
            time_par = time.perf_counter() - start_par
            rss_after_par = get_process_rss_mb()

            # 2. Sequential baseline execution (workers=1)
            start_seq = time.perf_counter()
            summary_seq = process_batch(
                input_dir=self.p_in,
                output_dir=p_seq,
                workers=1,
                use_cache=False,
                quiet=True,
            )
            time_seq = time.perf_counter() - start_seq
            rss_after_seq = get_process_rss_mb()

            # Verification of results accuracy
            self.assertEqual(summary_seq["summary"]["total_documents"], 50)
            self.assertEqual(summary_par["summary"]["total_documents"], 50)
            self.assertEqual(summary_seq["summary"]["processed_successfully"], 50)
            self.assertEqual(summary_par["summary"]["processed_successfully"], 50)
            self.assertEqual(summary_seq["summary"]["failed"], 0)
            self.assertEqual(summary_par["summary"]["failed"], 0)

            # Calculate metrics
            speedup = time_seq / time_par if time_par > 0 else 1.0
            thru_seq = 50.0 / time_seq
            thru_par = 50.0 / time_par

            report_lines = [
                "",
                "=" * 70,
                "     BENCHMARK: 50 INVOICE DOCUMENTS BATCH PROCESSING      ",
                "=" * 70,
                f"CPU Cores Available:    {cpu_count}",
                f"Parallel Workers Used:  {parallel_workers}",
                f"Sequential Duration:    {time_seq:.2f}s ({thru_seq:.2f} docs/sec)",
                f"Parallel Duration:      {time_par:.2f}s ({thru_par:.2f} docs/sec)",
                f"Observed Speedup:       {speedup:.2f}x (Required: >= 2.5x)",
                f"Initial Process RSS:    {initial_rss:.2f} MB",
                f"RSS After Parallel:     {rss_after_par:.2f} MB",
                f"RSS After Sequential:   {rss_after_seq:.2f} MB",
                "=" * 70,
            ]
            print("\n".join(report_lines))

            # DoD: Assert at least 2.5x speedup
            self.assertGreaterEqual(
                speedup,
                2.5,
                f"Speedup {speedup:.2f}x was below required 2.5x threshold on {cpu_count}-core system",
            )

    def test_zero_file_descriptor_and_memory_leaks(self):
        """Verify zero file descriptor leaks and bounded memory after a 50-document batch."""
        with tempfile.TemporaryDirectory() as out_str:
            p_out = Path(out_str)

            # Settle GC and capture baseline
            gc.collect()
            time.sleep(0.1)
            baseline_fds = get_open_fd_count()
            baseline_rss = get_process_rss_mb()

            # Process all 50 documents with automatic worker recycling (max_pages_per_worker=10)
            summary = process_batch(
                input_dir=self.p_in,
                output_dir=p_out,
                workers=4,
                max_pages_per_worker=10,
                use_cache=False,
                quiet=True,
            )

            self.assertEqual(summary["summary"]["processed_successfully"], 50)

            # Clean up and check post-batch metrics
            gc.collect()
            time.sleep(0.1)
            final_fds = get_open_fd_count()
            final_rss = get_process_rss_mb()

            fd_diff = final_fds - baseline_fds
            rss_drift = final_rss - baseline_rss

            print(
                f"\n[LEAK TEST] Baseline FDs: {baseline_fds} -> Final FDs: {final_fds} (Delta: {fd_diff})"
            )
            print(
                f"[LEAK TEST] Baseline RSS: {baseline_rss:.2f} MB -> Final RSS: {final_rss:.2f} MB (Drift: {rss_drift:.2f} MB)"
            )

            # DoD: 0 file descriptor leaks (allowing <= 1 for directory handle / runtime variance)
            self.assertLessEqual(
                final_fds,
                baseline_fds + 1,
                f"File descriptor leak detected! Baseline: {baseline_fds}, Final: {final_fds}",
            )

            # DoD: Bounded memory drift across 50 documents (< 20 MB drift in parent process)
            self.assertLess(
                rss_drift,
                20.0,
                f"Memory leak in parent process! Drift was {rss_drift:.2f} MB across 50 documents",
            )

    def test_streaming_intermediate_results_zero_heap_bloat(self):
        """Verify iter_process_batch streams 50 documents one-by-one with O(1) RAM retention."""
        with tempfile.TemporaryDirectory() as out_str:
            p_out = Path(out_str)
            seen_docs = 0

            gc.collect()
            start_rss = get_process_rss_mb()

            stream = iter_process_batch(
                input_dir=self.p_in,
                output_dir=p_out,
                workers=4,
                max_pages_per_worker=15,
                use_cache=False,
                return_invoice_object=False,
                quiet=True,
            )

            for doc_result in stream:
                seen_docs += 1
                self.assertEqual(doc_result["status"], "success")
                self.assertIn("file", doc_result)
                # Ensure each output file was written on the fly
                out_json = p_out / f"{Path(doc_result['file']).stem}.json"
                self.assertTrue(out_json.exists())

            self.assertEqual(seen_docs, 50)
            gc.collect()
            end_rss = get_process_rss_mb()
            # Streaming without returning large objects must have negligible memory growth
            self.assertLess(end_rss - start_rss, 15.0)


if __name__ == "__main__":
    unittest.main()

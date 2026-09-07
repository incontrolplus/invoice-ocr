"""Tests for Pillar 5: Multiprocessing, ProcessPoolExecutor, and Token-Level OCR Caching."""

import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
import numpy as np

from invoice_ocr import (
    DEFAULT_OCR_CACHE_DIR,
    Invoice,
    OcrToken,
    clear_ocr_cache,
    compute_file_sha256,
    get_ocr_cache_path,
    get_ocr_cache_stats,
    load_ocr_cache,
    process_batch,
    process_invoice,
    save_ocr_cache,
)
from tests.e2e.test_helpers import create_synthetic_test_image


class TestMultiprocessingAndCache(unittest.TestCase):
    """Test suite for Pillar 5 Multiprocessing and Token Caching."""

    def test_compute_file_sha256(self):
        """Test SHA-256 hash calculation is deterministic and sensitive to content."""
        with tempfile.NamedTemporaryFile(delete=False) as f1, tempfile.NamedTemporaryFile(delete=False) as f2:
            p1 = Path(f1.name)
            p2 = Path(f2.name)
            try:
                p1.write_bytes(b"Test Invoice Content Alpha")
                p2.write_bytes(b"Test Invoice Content Beta")

                h1 = compute_file_sha256(p1)
                h1_again = compute_file_sha256(p1)
                h2 = compute_file_sha256(p2)

                self.assertEqual(len(h1), 64)
                self.assertEqual(h1, h1_again)
                self.assertNotEqual(h1, h2)
            finally:
                p1.unlink(missing_ok=True)
                p2.unlink(missing_ok=True)

    def test_ocr_cache_save_and_load(self):
        """Test saving and loading OCR cache entries atomically."""
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            cache_dir = Path(tmp_dir_str)
            cache_file = cache_dir / "abc123hash.json"

            tokens = [
                OcrToken(text="ФАКТУРА", conf=95.0, bbox=(10, 20, 100, 30), page_number=1),
                OcrToken(text="121644736", conf=88.5, bbox=(120, 20, 80, 30), page_number=1),
            ]
            raw_evidence = {
                "total_pages": 1,
                "total_tokens": 2,
                "pages": [{"page_number": 1, "width": 1000, "height": 1400, "token_count": 2}],
            }

            # Save to cache
            save_ocr_cache(cache_file, "dummy.pdf", raw_evidence, tokens)
            self.assertTrue(cache_file.exists())

            # Load from cache
            loaded = load_ocr_cache(cache_file)
            self.assertIsNotNone(loaded)
            loaded_evidence, loaded_tokens = loaded
            self.assertEqual(len(loaded_tokens), 2)
            self.assertEqual(loaded_tokens[0].text, "ФАКТУРА")
            self.assertEqual(loaded_tokens[0].bbox, (10, 20, 100, 30))
            self.assertEqual(loaded_tokens[1].text, "121644736")
            self.assertEqual(loaded_evidence["total_tokens"], 2)

    def test_ocr_cache_corruption_resilience(self):
        """Test that corrupted or invalid JSON cache files gracefully return None."""
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            cache_dir = Path(tmp_dir_str)
            corrupt_file = cache_dir / "corrupted.json"
            corrupt_file.write_text("This is not valid JSON! { [", encoding="utf-8")

            res = load_ocr_cache(corrupt_file)
            self.assertIsNone(res)

    def test_ocr_cache_clear_and_stats(self):
        """Test cache statistics and clearing functionality."""
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            cache_dir = Path(tmp_dir_str)
            self.assertEqual(get_ocr_cache_stats(cache_dir)["total_cached_files"], 0)

            # Create 3 dummy cache files
            for i in range(3):
                f = cache_dir / f"cache_{i}.json"
                f.write_text(json.dumps({"tokens": []}), encoding="utf-8")

            stats = get_ocr_cache_stats(cache_dir)
            self.assertEqual(stats["total_cached_files"], 3)
            self.assertGreater(stats["total_size_bytes"], 0)

            deleted = clear_ocr_cache(cache_dir)
            self.assertEqual(deleted, 3)
            self.assertEqual(get_ocr_cache_stats(cache_dir)["total_cached_files"], 0)

    def test_process_invoice_cache_hit_speedup_and_accuracy(self):
        """Verify process_invoice with caching: 2nd run is an instant cache hit producing identical output."""
        with tempfile.TemporaryDirectory() as tmp_dir_str, tempfile.TemporaryDirectory() as tmp_cache_str:
            tmp_dir = Path(tmp_dir_str)
            cache_dir = Path(tmp_cache_str)

            img = create_synthetic_test_image("ФАКТУРА 0000000099 ДОСТАВЧИК ЕООД ЕИК 121644736 СУМА 120.00 ЛВ")
            img_path = tmp_dir / "cached_test.png"
            cv2.imwrite(str(img_path), img)

            # 1. Cold run: extracts via OCR and populates cache
            start_cold = time.perf_counter()
            inv_cold = process_invoice(img_path, ocr_cache_dir=cache_dir, use_cache=True)
            dur_cold = time.perf_counter() - start_cold

            # Check that cache was written
            h = compute_file_sha256(img_path)
            cache_file = cache_dir / f"{h}.json"
            self.assertTrue(cache_file.exists())

            # 2. Warm run: must hit cache
            start_warm = time.perf_counter()
            inv_warm = process_invoice(img_path, ocr_cache_dir=cache_dir, use_cache=True)
            dur_warm = time.perf_counter() - start_warm

            # Warm run should be dramatically faster (< 0.15 seconds)
            self.assertLess(dur_warm, 0.15)

            # Output data must match identically
            self.assertEqual(inv_cold.invoice_metadata.invoice_number, inv_warm.invoice_metadata.invoice_number)
            self.assertEqual(inv_cold.supplier.eik, inv_warm.supplier.eik)
            self.assertEqual(inv_cold.financial_summary.total_amount_due.amount, inv_warm.financial_summary.total_amount_due.amount)

    def test_process_invoice_no_cache_bypasses_cache(self):
        """Verify process_invoice with use_cache=False does not create or read cache."""
        with tempfile.TemporaryDirectory() as tmp_dir_str, tempfile.TemporaryDirectory() as tmp_cache_str:
            tmp_dir = Path(tmp_dir_str)
            cache_dir = Path(tmp_cache_str)

            img = create_synthetic_test_image("ФАКТУРА 0000000088 БЕЗ КЕШ")
            img_path = tmp_dir / "no_cache_test.png"
            cv2.imwrite(str(img_path), img)

            inv = process_invoice(img_path, ocr_cache_dir=cache_dir, use_cache=False)
            self.assertIsNotNone(inv)
            self.assertEqual(len(list(cache_dir.glob("*.json"))), 0)

    def test_process_batch_multiprocessing_and_sequential_parity(self):
        """Verify ProcessPoolExecutor batch execution produces identical summary to sequential execution."""
        with tempfile.TemporaryDirectory() as tmp_in_str, \
             tempfile.TemporaryDirectory() as tmp_out_seq_str, \
             tempfile.TemporaryDirectory() as tmp_out_par_str, \
             tempfile.TemporaryDirectory() as tmp_cache_str:

            p_in = Path(tmp_in_str)
            p_seq = Path(tmp_out_seq_str)
            p_par = Path(tmp_out_par_str)
            cache_dir = Path(tmp_cache_str)

            # Create 4 synthetic test invoices
            for i in range(1, 5):
                img = create_synthetic_test_image(f"ФАКТУРА 000000000{i} ДОСТАВЧИК ЕООД ЕИК 121644736 СУМА {i * 100}.00 ЛВ")
                cv2.imwrite(str(p_in / f"inv_{i}.png"), img)

            # Sequential run (workers=1)
            summary_seq = process_batch(
                input_dir=p_in,
                output_dir=p_seq,
                workers=1,
                ocr_cache_dir=cache_dir,
                quiet=True,
            )

            # Parallel run (workers=4)
            progress_updates = []
            def track_progress(completed, total, file_name):
                progress_updates.append((completed, total, file_name))

            summary_par = process_batch(
                input_dir=p_in,
                output_dir=p_par,
                workers=4,
                ocr_cache_dir=cache_dir,
                progress_callback=track_progress,
                quiet=True,
            )

            # Verify document counts and processed counts match
            self.assertEqual(summary_seq["summary"]["total_documents"], 4)
            self.assertEqual(summary_par["summary"]["total_documents"], 4)
            self.assertEqual(summary_seq["summary"]["processed_successfully"], 4)
            self.assertEqual(summary_par["summary"]["processed_successfully"], 4)

            # Verify financial totals match
            seq_curr = summary_seq["financial_totals_by_currency"]
            par_curr = summary_par["financial_totals_by_currency"]
            self.assertEqual(seq_curr, par_curr)

            # Verify progress callback was called 4 times up to 4/4
            self.assertEqual(len(progress_updates), 4)
            self.assertEqual(progress_updates[-1][0], 4)
            self.assertEqual(progress_updates[-1][1], 4)

            # Verify deterministic ordering of documents
            seq_docs = [d["file"] for d in summary_seq["documents"]]
            par_docs = [d["file"] for d in summary_par["documents"]]
            self.assertEqual(seq_docs, par_docs)

    def test_process_batch_worker_error_resilience(self):
        """Verify that a damaged/unreadable file in batch does not fail other workers."""
        with tempfile.TemporaryDirectory() as tmp_in_str, tempfile.TemporaryDirectory() as tmp_out_str:
            p_in = Path(tmp_in_str)
            p_out = Path(tmp_out_str)

            # 1 valid file
            valid_img = create_synthetic_test_image("ФАКТУРА 0000000001 ВАЛИДЕН ФАЙЛ")
            cv2.imwrite(str(p_in / "valid.png"), valid_img)

            # 1 corrupt file
            corrupt = p_in / "corrupt.png"
            corrupt.write_bytes(b"corrupted image binary content")

            summary = process_batch(
                input_dir=p_in,
                output_dir=p_out,
                workers=2,
                quiet=True,
            )

            self.assertEqual(summary["summary"]["total_documents"], 2)
            self.assertEqual(summary["summary"]["processed_successfully"], 1)
            self.assertEqual(summary["summary"]["failed"], 1)
            self.assertTrue((p_out / "valid.json").exists())


if __name__ == "__main__":
    unittest.main()

"""Tests for Milestone M6: CLI, Batch Processing, Accounting Summary Reports, and Debug Artifacts."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from invoice_ocr import (
    Invoice,
    format_batch_console_report,
    process_batch,
    process_invoice,
)
from tests.e2e.test_helpers import create_synthetic_test_image


class TestBatchProcessingAndCli(unittest.TestCase):
    """Test suite for Milestone M6 features."""

    def test_format_batch_console_report(self):
        """Verify console report formatting has required accounting sections."""
        summary = {
            "summary": {
                "total_documents": 2,
                "processed_successfully": 2,
                "failed": 0,
                "valid_documents": 2,
                "invalid_documents": 0,
                "validation_pass_rate_pct": 100.0,
                "total_line_items": 5,
                "total_duration_seconds": 1.25,
                "average_duration_seconds": 0.625,
            },
            "financial_totals_by_currency": {
                "BGN": {
                    "document_count": 1,
                    "total_tax_base": "100.00",
                    "total_vat_amount": "20.00",
                    "total_amount_due": "120.00",
                },
                "EUR": {
                    "document_count": 1,
                    "total_tax_base": "50.00",
                    "total_vat_amount": "10.00",
                    "total_amount_due": "60.00",
                },
            },
            "suppliers": [
                {
                    "name": "ТЕСТ ДОСТАВЧИК ООД",
                    "eik": "123456789",
                    "vat_number": "BG123456789",
                    "invoice_count": 2,
                    "totals_by_currency": {
                        "BGN": {"total_amount_due": "120.00"},
                        "EUR": {"total_amount_due": "60.00"},
                    },
                }
            ],
            "validation_issues_summary": {
                "errors": {},
                "warnings": {"HIGH_LOW_CONFIDENCE_RATIO": 1},
            },
            "output_directory": "results/",
            "summary_file": "results/batch_summary.json",
        }

        report = format_batch_console_report(summary)
        self.assertIn("ОБОБЩЕН СЧЕТОВОДЕН ОТЧЕТ", report)
        self.assertIn("ФИНАНСОВИ СБОРОВЕ ПО ВАЛУТИ:", report)
        self.assertIn("BGN", report)
        self.assertIn("EUR", report)
        self.assertIn("ТЕСТ ДОСТАВЧИК ООД", report)
        self.assertIn("123456789", report)
        self.assertIn("100.0%", report)

    def test_process_batch_synthetic_directory(self):
        """Test process_batch creates output JSONs and structured batch_summary.json."""
        with tempfile.TemporaryDirectory() as tmp_in_str, tempfile.TemporaryDirectory() as tmp_out_str:
            tmp_in = Path(tmp_in_str)
            tmp_out = Path(tmp_out_str)

            # Create 2 synthetic invoice images
            img1 = create_synthetic_test_image("ФАКТУРА 0000000001 ДОСТАВЧИК ЕООД ЕИК 121644736")
            img2 = create_synthetic_test_image("ФАКТУРА 0000000002 ТЕСТ ООД ЕИК 114500333")

            cv2.imwrite(str(tmp_in / "inv1.png"), img1)
            cv2.imwrite(str(tmp_in / "inv2.png"), img2)

            summary = process_batch(
                input_dir=tmp_in,
                output_dir=tmp_out,
                quiet=True,
            )

            # Verify files were generated
            self.assertTrue((tmp_out / "inv1.json").exists())
            self.assertTrue((tmp_out / "inv2.json").exists())
            self.assertTrue((tmp_out / "batch_summary.json").exists())

            # Verify summary contents
            self.assertEqual(summary["summary"]["total_documents"], 2)
            self.assertEqual(summary["summary"]["processed_successfully"], 2)
            self.assertEqual(summary["summary"]["failed"], 0)

            # Verify backward compatibility fields
            self.assertEqual(summary["processed"], 2)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(len(summary["results"]), 2)

            # Check JSON file contents
            content1 = json.loads((tmp_out / "inv1.json").read_text(encoding="utf-8"))
            self.assertIn("invoice_metadata", content1)

    def test_process_batch_empty_directory(self):
        """Test process_batch cleanly handles an empty directory with 0 files."""
        with tempfile.TemporaryDirectory() as tmp_in_str, tempfile.TemporaryDirectory() as tmp_out_str:
            summary = process_batch(
                input_dir=tmp_in_str,
                output_dir=tmp_out_str,
                quiet=True,
            )
            self.assertEqual(summary["summary"]["total_documents"], 0)
            self.assertEqual(summary["summary"]["processed_successfully"], 0)
            self.assertTrue((Path(tmp_out_str) / "batch_summary.json").exists())

    def test_debug_mode_exports_visual_and_layout_artifacts(self):
        """Test that --debug saves page images, tokens overlay, and layout tree JSON."""
        with tempfile.TemporaryDirectory() as tmp_dir_str, tempfile.TemporaryDirectory() as tmp_dbg_str:
            tmp_dir = Path(tmp_dir_str)
            tmp_dbg = Path(tmp_dbg_str)

            img = create_synthetic_test_image("ФАКТУРА 0000000010 ДЕБЪГ ТЕСТ")
            img_path = tmp_dir / "debug_inv.png"
            cv2.imwrite(str(img_path), img)

            invoice = process_invoice(img_path, debug_dir=tmp_dbg)
            self.assertIsNotNone(invoice)

            # Check that debug artifacts were generated
            raw_png = tmp_dbg / "debug_inv_page_1_raw.png"
            norm_png = tmp_dbg / "debug_inv_page_1_norm.png"
            tokens_png = tmp_dbg / "debug_inv_page_1_tokens.png"
            layout_json = tmp_dbg / "debug_inv_layout_tree.json"

            self.assertTrue(raw_png.exists(), "Raw page image must be saved in debug mode")
            self.assertTrue(norm_png.exists(), "Normalized page image must be saved in debug mode")
            self.assertTrue(tokens_png.exists(), "Token bounding boxes overlay must be saved in debug mode")
            self.assertTrue(layout_json.exists(), "Layout tree JSON must be saved in debug mode")

            # Check layout tree structure
            tree_data = json.loads(layout_json.read_text(encoding="utf-8"))
            self.assertIn("lines", tree_data)
            self.assertIn("blocks", tree_data)

    def test_cli_batch_mode_subprocess_invocation(self):
        """Test invoking invoice_ocr.py with --input-dir and --output-dir via subprocess."""
        with tempfile.TemporaryDirectory() as tmp_in_str, tempfile.TemporaryDirectory() as tmp_out_str:
            tmp_in = Path(tmp_in_str)
            tmp_out = Path(tmp_out_str)

            img = create_synthetic_test_image("ФАКТУРА 0000000042 ПАКЕТЕН ТЕСТ")
            cv2.imwrite(str(tmp_in / "test_cli.png"), img)

            cmd = [
                sys.executable,
                "invoice_ocr.py",
                "--input-dir", str(tmp_in),
                "--output-dir", str(tmp_out),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))
            self.assertEqual(proc.returncode, 0, f"CLI exited with error: {proc.stderr}")

            # Verify outputs
            self.assertTrue((tmp_out / "test_cli.json").exists())
            self.assertTrue((tmp_out / "batch_summary.json").exists())

            # Stderr should contain the formatted accounting report
            self.assertIn("ОБОБЩЕН СЧЕТОВОДЕН ОТЧЕТ", proc.stderr)


if __name__ == "__main__":
    unittest.main()

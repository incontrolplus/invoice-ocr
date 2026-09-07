"""Tests for FastAPI REST API Microservice (api_server.py)."""

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
from fastapi.testclient import TestClient

from api_server import app
from tests.e2e.test_helpers import create_synthetic_test_image


class TestApiServer(unittest.TestCase):
    """Test suite for the Invoice OCR FastAPI REST microservice."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_root_endpoint(self):
        """Root endpoint returns service name and documentation links."""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["service"], "Bulgarian Invoice OCR REST API")
        self.assertIn("docs", data)

    def test_health_endpoint(self):
        """Health endpoint returns status, version, languages and engine readiness."""
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn(data["status"], ["ok", "degraded"])
        self.assertEqual(data["version"], "1.0.0")
        self.assertIn("tesseract_ready", data)
        self.assertIn("available_languages", data)
        self.assertIn("uptime_seconds", data)
        self.assertIn("X-Process-Time-Sec", resp.headers)

    def test_languages_endpoint(self):
        """Languages endpoint lists all available OCR language packs."""
        resp = self.client.get("/api/v1/languages")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("available_languages", data)
        self.assertIn("required_ready", data)

    def test_process_single_invoice_upload(self):
        """Test uploading a single invoice image to /api/v1/invoices/process."""
        img = create_synthetic_test_image("ФАКТУРА 0000000100 ДОСТАВЧИК ЕООД ЕИК 121644736 СУМА 120.00 ЛВ")
        _, buf = cv2.imencode(".png", img)
        file_bytes = io.BytesIO(buf.tobytes())

        files = {"file": ("test_invoice.png", file_bytes, "image/png")}
        resp = self.client.post("/api/v1/invoices/process", files=files)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        # Verify structured 3-layer data
        self.assertIn("invoice_metadata", data)
        self.assertIn("supplier", data)
        self.assertIn("recipient", data)
        self.assertIn("financial_summary", data)
        self.assertIn("line_items", data)
        self.assertIn("validation", data)
        self.assertEqual(data["file_name"], "test_invoice.png")
        # By default raw_ocr_evidence is excluded for lightweight payload
        self.assertNotIn("raw_ocr_evidence", data)

    def test_process_single_invoice_with_raw_evidence(self):
        """Test including raw OCR evidence when include_raw_evidence=true."""
        img = create_synthetic_test_image("ФАКТУРА 0000000101")
        _, buf = cv2.imencode(".png", img)
        file_bytes = io.BytesIO(buf.tobytes())

        files = {"file": ("test_raw.png", file_bytes, "image/png")}
        resp = self.client.post("/api/v1/invoices/process?include_raw_evidence=true", files=files)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("raw_ocr_evidence", data)

    def test_process_unsupported_file_extension(self):
        """Test rejection of unsupported file types with HTTP 400."""
        text_bytes = io.BytesIO(b"Not an invoice image")
        files = {"file": ("document.txt", text_bytes, "text/plain")}
        resp = self.client.post("/api/v1/invoices/process", files=files)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("unsupported file extension", resp.json()["detail"].lower())

    def test_process_empty_file(self):
        """Test rejection of 0-byte file with HTTP 400."""
        empty_bytes = io.BytesIO(b"")
        files = {"file": ("empty.png", empty_bytes, "image/png")}
        resp = self.client.post("/api/v1/invoices/process", files=files)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("empty", resp.json()["detail"].lower())

    def test_process_batch_upload(self):
        """Test uploading multiple invoice files via /api/v1/invoices/batch."""
        img1 = create_synthetic_test_image("ФАКТУРА 0000000201 МЕТРО ЕООД ЕИК 121644736")
        img2 = create_synthetic_test_image("ФАКТУРА 0000000202 КАПИНА ООД ЕИК 114500333")

        _, buf1 = cv2.imencode(".png", img1)
        _, buf2 = cv2.imencode(".png", img2)

        files = [
            ("files", ("inv1.png", io.BytesIO(buf1.tobytes()), "image/png")),
            ("files", ("inv2.png", io.BytesIO(buf2.tobytes()), "image/png")),
        ]
        resp = self.client.post("/api/v1/invoices/batch", files=files)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["total_documents"], 2)
        self.assertEqual(data["summary"]["processed_successfully"], 2)
        self.assertIn("financial_totals_by_currency", data)
        self.assertIn("suppliers", data)
        self.assertIn("documents", data)

    def test_process_batch_dir_endpoint(self):
        """Test processing a server-side directory via /api/v1/invoices/batch-dir."""
        with tempfile.TemporaryDirectory() as tmp_in_str, tempfile.TemporaryDirectory() as tmp_out_str:
            tmp_in = Path(tmp_in_str)
            tmp_out = Path(tmp_out_str)

            img = create_synthetic_test_image("ФАКТУРА 0000000301 ТЕСТ ДИР")
            cv2.imwrite(str(tmp_in / "dir_inv.png"), img)

            payload = {
                "input_dir": str(tmp_in),
                "output_dir": str(tmp_out),
            }
            resp = self.client.post("/api/v1/invoices/batch-dir", json=payload)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()

            self.assertEqual(data["summary"]["total_documents"], 1)
            self.assertTrue((tmp_out / "dir_inv.json").exists())
            self.assertTrue((tmp_out / "batch_summary.json").exists())

    def test_process_batch_dir_nonexistent(self):
        """Test that batch-dir returns 404 for non-existent directory."""
        payload = {
            "input_dir": "/non/existent/path/for/invoices",
            "output_dir": "results/",
        }
        resp = self.client.post("/api/v1/invoices/batch-dir", json=payload)
        self.assertEqual(resp.status_code, 404)

    def test_process_real_pdf_invoice(self):
        """Test uploading a real PDF invoice from the dataset volume via REST API."""
        real_pdf = Path("/Volumes/NO NAME/_ФАКТУРИ/04_ВАЛБОРГЕН_ООД/валборген.pdf")
        if not real_pdf.exists():
            self.skipTest("Real dataset volume not mounted")

        with open(real_pdf, "rb") as f:
            files = {"file": ("валборген.pdf", f, "application/pdf")}
            resp = self.client.post("/api/v1/invoices/process", files=files)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["file_name"], "валборген.pdf")
        self.assertTrue(data["validation"]["is_valid"])
        self.assertEqual(data["supplier"]["eik"], "114609731")
        self.assertEqual(data["financial_summary"]["total_amount_due"]["amount"], "100.22")

    def test_validate_invoice_endpoint(self):
        """Test the POST /api/v1/invoices/validate endpoint without running OCR."""
        payload = {
            "invoice_metadata": {
                "invoice_number": "0000123456",
                "date_issued": "2026-08-15",
                "date_tax_event": "2026-08-15",
            },
            "supplier": {
                "name": "ТЕСТ ДОСТАВЧИК ЕООД",
                "eik": "121644736",
                "vat_number": "BG121644736",
            },
            "recipient": {
                "name": "ТЕСТ КЛИЕНТ ООД",
                "eik": "208380135",
                "vat_number": "BG208380135",
            },
            "financial_summary": {
                "tax_base": {"amount": "100.00", "currency": "BGN"},
                "vat_amount": {"amount": "20.00", "currency": "BGN"},
                "total_amount_due": {"amount": "120.00", "currency": "BGN"},
            },
            "line_items": [
                {
                    "index": 1,
                    "description": "Консултантски услуги",
                    "quantity": 1,
                    "unit_price_net": {"amount": "100.00"},
                    "total_price_net": {"amount": "100.00"},
                    "vat_rate_pct": 20.0,
                }
            ],
        }
        resp = self.client.post("/api/v1/invoices/validate", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("is_valid", data)
        self.assertTrue(data["is_valid"])
        self.assertEqual(len(data["errors"]), 0)


if __name__ == "__main__":
    unittest.main()

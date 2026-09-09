"""Tests for Docs Ingestion & Document Classification Endpoints:
- POST /api/v1/classify/document
- POST /api/v1/ingest/docs-email
"""

import base64
import email
from email.message import EmailMessage
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
import pymupdf as fitz

from api_server import app
from invoice_core.document_classifier import DocumentCategory, ClassificationResult


def _create_sample_pdf_bytes(text: str) -> bytes:
    """Generate a clean in-memory PDF with text and realistic size (> 10KB)."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    if not Path(font_path).exists():
        for p in ["/Library/Fonts/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
            if Path(p).exists():
                font_path = p
                break

    if Path(font_path).exists():
        page.insert_font(fontname="cyr", fontfile=font_path)
        fn = "cyr"
    else:
        fn = "helv"

    page.insert_text((50, 80), text, fontname=fn, fontsize=12)
    filler = "BULGARIAN_ACCOUNTING_DOCUMENT_PAYLOAD_" * 300
    doc.set_metadata({"title": "Commercial Document", "subject": filler})
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


class TestDocsEmailIngestion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_classify_document_upload_stokova_razpiska(self):
        content = """
        СТОКОВА РАЗПИСКА № 888999
        Дата: 14.02.2024
        Склад: Централен Склад
        Предал: Иван Иванов
        Приел: Петър Петров
        1. Артикул Тест - 10 бр.
        Подпис: ............
        """
        pdf_bytes = _create_sample_pdf_bytes(content)

        response = self.client.post(
            "/api/v1/classify/document",
            files={"file": ("stokova_888.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
            params={"sync_supabase": False},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["category"], DocumentCategory.STOKOVI_RAZPISKI.value)
        self.assertEqual(data["category_code"], DocumentCategory.STOKOVI_RAZPISKI.name)
        self.assertGreaterEqual(data["confidence"], 0.7)
        self.assertEqual(data["routing_action"], "archived_as_classified")

    def test_classify_document_upload_faktura(self):
        content = """
        ОРИГИНАЛ
        ФАКТУРА № 1000055443
        Дата на издаване: 15.01.2024
        Доставчик: ТЕСТ ДОСТАВЧИК ЕООД
        ЕИК: 123456789 ДДС: BG123456789
        Получател: ТЕСТ КЛИЕНТ ЕООД
        ЕИК: 987654321
        Данъчна основа: 100.00 лв.
        ДДС 20%: 20.00 лв.
        Обща сума за плащане: 120.00 лв.
        """
        pdf_bytes = _create_sample_pdf_bytes(content)

        response = self.client.post(
            "/api/v1/classify/document",
            files={"file": ("faktura_554.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
            params={"sync_supabase": False, "auto_process_invoice": True},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["category"], DocumentCategory.FAKTURI.value)
        self.assertEqual(data["routing_action"], "routed_to_invoices")
        self.assertIn("invoice_data", data)

    def test_docs_email_webhook_cloudflare_json(self):
        stokova_text = """
        СТОКОВА РАЗПИСКА № 999111
        Дата: 01.03.2024
        Предал: Стоян
        Приел: Димитър
        Складова наличност предадена успешно.
        """
        pdf_bytes = _create_sample_pdf_bytes(stokova_text)
        b64_pdf = base64.b64encode(pdf_bytes).decode("ascii")

        payload = {
            "from": "accounting@supplier.bg",
            "to": "docs@incontrolplus.com",
            "subject": "Документи за доставка",
            "date": "2024-03-01T10:00:00Z",
            "message_id": "<test-docs-123@supplier.bg>",
            "spf": "pass",
            "dkim": "pass",
            "attachments": [
                {
                    "filename": "razpiska_999.pdf",
                    "content_type": "application/pdf",
                    "content_base64": b64_pdf,
                    "size_bytes": len(pdf_bytes),
                }
            ],
        }

        response = self.client.post(
            "/api/v1/ingest/docs-email",
            json=payload,
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["documents_processed"], 1)
        res = data["results"][0]
        self.assertEqual(res["category"], DocumentCategory.STOKOVI_RAZPISKI.value)
        self.assertEqual(res["routing_action"], "archived_as_classified")

    def test_docs_email_webhook_filtered_attachments(self):
        """Test that small attachments (< 10KB like signatures) are filtered out."""
        small_signature = b"GIF89a" + b"\x00" * 100
        b64_sig = base64.b64encode(small_signature).decode("ascii")

        payload = {
            "from": "partner@company.bg",
            "to": "docs@incontrolplus.com",
            "subject": "Писмо без документи",
            "attachments": [
                {
                    "filename": "logo.png",
                    "content_type": "image/png",
                    "content_base64": b64_sig,
                    "size_bytes": len(small_signature),
                }
            ],
        }

        response = self.client.post(
            "/api/v1/ingest/docs-email",
            json=payload,
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "no_documents_found")
        self.assertEqual(data["documents_processed"], 0)

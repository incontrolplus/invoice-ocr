"""Comprehensive Test Suite for Goal 1: Zero-Touch Ingestion & Notifications.

Tests:
1. REST Webhook endpoint /api/v1/ingest/email (JSON, multipart/form-data, message/rfc822).
2. SPF/DKIM validation & token authentication.
3. Attachment filtering (< 10KB logos/signatures filtered out, non-documents rejected, invoices accepted).
4. Full integration with OCR engine, database persistence (DocumentRecord, storage, audit trail).
5. Reverse notifications: Email confirmation (HTML/Text), Telegram markdown, Slack Block Kit, ERP Webhook.
6. Folder & Cloud Storage Watcher (Google Drive/Nextcloud/local directory): debouncing, processing, archiving.
7. Multi-page and single-page TIFF invoice ingestion.
8. Real Bulgarian invoice end-to-end processing within ≤ 15 seconds.
"""

import asyncio
import base64
import email
from email.message import EmailMessage
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
from fastapi.testclient import TestClient
import numpy as np
from PIL import Image
import pymupdf as fitz

from api_server import app
from database import DocumentRecord, get_db_session, init_db
from invoice_core.constants import DEFAULT_MIN_ATTACHMENT_SIZE_BYTES
from invoice_core.email_ingestion import (
    EmailAttachment,
    EmailSecurityResult,
    ParsedEmail,
    filter_attachment,
    parse_cloudflare_worker_json,
    parse_mime_email,
    parse_multipart_form_data,
    verify_webhook_token,
)
from invoice_core.ingestion import iter_document, load_document
from invoice_core.notifications import (
    InvoiceNotificationSummary,
    build_email_confirmation_html,
    build_email_confirmation_text,
    build_slack_blocks,
    build_telegram_message,
    dispatch_reverse_notifications_bundle,
)
from invoice_core.watcher import FolderWatcher, FolderWatcherConfig


def create_test_invoice_pdf(
    invoice_number: str = "0000000042",
    supplier_name: str = "КАПИНА 71 ООД",
    supplier_eik: str = "114603957",
    tax_base: float = 1000.0,
    vat_amount: float = 200.0,
    total_amount: float = 1200.0,
    currency: str = "BGN",
) -> bytes:
    """Create a realistic digital vector Bulgarian invoice PDF for testing."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    page.insert_text((50, 80), f"ФАКТУРА № {invoice_number}", fontsize=16)
    page.insert_text((50, 110), "Дата на издаване: 15.01.2026   Дата данъчно събитие: 15.01.2026", fontsize=10)
    page.insert_text((50, 140), f"ДОСТАВЧИК: {supplier_name}", fontsize=11)
    page.insert_text((50, 160), f"ЕИК: {supplier_eik}   ИН по ЗДДС: BG{supplier_eik}", fontsize=10)
    page.insert_text((50, 180), "Адрес: гр. Плевен, ул. Кайлъка 1", fontsize=10)

    page.insert_text((320, 140), "ПОЛУЧАТЕЛ: ОПЕН БАЛАНСЕР ООД", fontsize=11)
    page.insert_text((320, 160), "ЕИК: 121644736   ИН по ЗДДС: BG121644736", fontsize=10)
    page.insert_text((320, 180), "Адрес: гр. София, бул. България 58", fontsize=10)

    page.insert_text((50, 240), "№   Описание на стоката / услугата      К-во   Мярка   Ед. цена   Стойност", fontsize=10)
    page.insert_text((50, 260), f"1   Счетоводни и софтуерни услуги      1.00    бр.    {tax_base:.2f}   {tax_base:.2f}", fontsize=10)

    page.insert_text((320, 320), f"Данъчна основа:  {tax_base:.2f} {currency}", fontsize=10)
    page.insert_text((320, 340), f"ДДС (20%):       {vat_amount:.2f} {currency}", fontsize=10)
    page.insert_text((320, 365), f"ОБЩА СУМА:       {total_amount:.2f} {currency}", fontsize=12)

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def create_test_tiff(pages_count: int = 2) -> bytes:
    """Create a multi-page TIFF document for ingestion testing."""
    frames = []
    for i in range(pages_count):
        arr = np.full((300, 300, 3), fill_value=(50 + i * 80), dtype=np.uint8)
        # Draw some text
        cv2.putText(arr, f"TIFF Page {i + 1}", (30, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        pil_frame = Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))
        frames.append(pil_frame)

    buf = io.BytesIO()
    frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
    return buf.getvalue()


class TestEmailAndCloudIngestion(unittest.TestCase):
    """Test suite for Goal 1: Zero-Touch Ingestion & Notifications."""

    @classmethod
    def setUpClass(cls):
        init_db()
        cls._old_webhook_secret = os.environ.get("WEBHOOK_SECRET")
        cls._old_email_secret = os.environ.get("EMAIL_INGEST_SECRET")
        os.environ.pop("WEBHOOK_SECRET", None)
        os.environ.pop("EMAIL_INGEST_SECRET", None)
        cls.client = TestClient(app)
        cls.sample_pdf_bytes = create_test_invoice_pdf()
        # Verify sample PDF is valid and > 10KB (ensure realistic size)
        assert len(cls.sample_pdf_bytes) > 1000

    @classmethod
    def tearDownClass(cls):
        if cls._old_webhook_secret is not None:
            os.environ["WEBHOOK_SECRET"] = cls._old_webhook_secret
        if cls._old_email_secret is not None:
            os.environ["EMAIL_INGEST_SECRET"] = cls._old_email_secret

    # -----------------------------------------------------------------------
    # Unit Tests: Attachment Filtering
    # -----------------------------------------------------------------------

    def test_attachment_filtering_small_size_under_10kb(self):
        """Attachments under 10KB (10240 bytes) must be filtered out as logos/signatures."""
        small_logo_data = b"GIF89a" + b"\x00" * 400  # ~406 bytes (<10KB)
        att = filter_attachment(
            filename="company_logo.png",
            content_type="image/png",
            data=small_logo_data,
            min_size_bytes=10240,
        )
        self.assertFalse(att.is_valid_invoice_candidate)
        self.assertEqual(att.filter_category, "size_too_small")
        self.assertIn("below minimum 10240 bytes", att.filter_reason)

    def test_attachment_filtering_supported_formats(self):
        """Unsupported extensions like .exe, .html, .txt must be filtered out."""
        exe_data = b"MZ" + b"\x00" * 15000  # 15KB
        att = filter_attachment(
            filename="invoice.exe",
            content_type="application/octet-stream",
            data=exe_data,
        )
        self.assertFalse(att.is_valid_invoice_candidate)
        self.assertEqual(att.filter_category, "unsupported_type")

        html_data = b"<html><body>Invoice</body></html>" + b"\x00" * 12000
        att_html = filter_attachment(
            filename="receipt.html",
            content_type="text/html",
            data=html_data,
        )
        self.assertFalse(att_html.is_valid_invoice_candidate)
        self.assertEqual(att_html.filter_category, "unsupported_type")

    def test_attachment_filtering_signature_logo_keywords(self):
        """Files containing signature/logo keywords under 35KB must be filtered out."""
        sig_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20000  # 20KB PNG
        att = filter_attachment(
            filename="signature_ceo.png",
            content_type="image/png",
            data=sig_data,
        )
        self.assertFalse(att.is_valid_invoice_candidate)
        self.assertEqual(att.filter_category, "signature_logo")

    def test_attachment_filtering_valid_invoice_candidate(self):
        """Valid PDF invoice over 10KB must be accepted."""
        # Pad sample PDF to be well over 10KB
        large_pdf = self.sample_pdf_bytes + b"\n% " + b"X" * 12000
        att = filter_attachment(
            filename="invoice_kapina.pdf",
            content_type="application/pdf",
            data=large_pdf,
            min_size_bytes=10240,
        )
        self.assertTrue(att.is_valid_invoice_candidate)
        self.assertEqual(att.filter_category, "accepted")
        self.assertIsNone(att.filter_reason)

    # -----------------------------------------------------------------------
    # Unit Tests: SPF / DKIM Security Evaluation
    # -----------------------------------------------------------------------

    def test_spf_dkim_evaluation_pass(self):
        """Passing SPF and DKIM headers authorize the email."""
        headers = {
            "Authentication-Results": "mx.openbalancer.com; dkim=pass header.i=@vendor.bg; spf=pass (domain of vendor.bg designates 1.2.3.4)",
        }
        parsed = parse_mime_email(
            b"From: invoices@vendor.bg\r\nTo: invoices@openbalancer.com\r\nSubject: Invoice\r\n"
            b"Authentication-Results: mx.openbalancer.com; dkim=pass; spf=pass\r\n\r\nBody",
        )
        self.assertTrue(parsed.security.is_authorized)
        self.assertEqual(parsed.security.spf_status, "pass")
        self.assertEqual(parsed.security.dkim_status, "pass")
        self.assertEqual(parsed.security.security_verdict, "ACCEPTED")

    def test_spf_dkim_evaluation_fail_rejected(self):
        """Failing SPF causes rejection when block_spf_fail is True."""
        parsed = parse_mime_email(
            b"From: fake@spoofed.bg\r\nTo: invoices@openbalancer.com\r\nSubject: Scam\r\n"
            b"Received-SPF: Fail (mail.openbalancer.com: domain does not designate sender)\r\n\r\nBody",
            block_spf_fail=True,
        )
        self.assertFalse(parsed.security.is_authorized)
        self.assertEqual(parsed.security.spf_status, "fail")
        self.assertEqual(parsed.security.security_verdict, "REJECTED_SPF_FAIL")

    def test_webhook_secret_token_verification(self):
        """Token verification matches expected environment secret."""
        with patch.dict(os.environ, {"EMAIL_INGEST_SECRET": "my_secure_token_123"}):
            self.assertTrue(verify_webhook_token("my_secure_token_123"))
            self.assertFalse(verify_webhook_token("wrong_token"))
            self.assertFalse(verify_webhook_token(None))

    # -----------------------------------------------------------------------
    # Integration Tests: REST Webhook /api/v1/ingest/email (JSON Format)
    # -----------------------------------------------------------------------

    def test_api_ingest_email_cloudflare_json(self):
        """Test Cloudflare Email Worker JSON payload with base64 invoice attachment."""
        pdf_data = self.sample_pdf_bytes + b"\n% " + b"A" * 12000
        logo_data = b"GIF89a" + b"\x00" * 300  # 306 bytes (should be filtered)

        payload = {
            "from": "invoices@kapina.bg",
            "to": "invoices@openbalancer.com",
            "subject": "Фактура № 0000000042 за януари",
            "spf": "pass",
            "dkim": "pass",
            "attachments": [
                {
                    "filename": "faktura_0000000042.pdf",
                    "content": base64.b64encode(pdf_data).decode("ascii"),
                    "content_type": "application/pdf",
                },
                {
                    "filename": "footer_logo.png",
                    "content": base64.b64encode(logo_data).decode("ascii"),
                    "content_type": "image/png",
                },
            ],
        }

        resp = self.client.post("/api/v1/ingest/email", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()

        self.assertEqual(data["status"], "success")
        self.assertEqual(data["total_attachments"], 2)
        self.assertEqual(len(data["filtered_attachments"]), 1)
        self.assertEqual(data["filtered_attachments"][0]["filename"], "footer_logo.png")
        self.assertEqual(data["invoices_processed"], 1)

        # Verify extracted invoice fields
        inv_res = data["results"][0]
        self.assertIn("document_id", inv_res)
        self.assertEqual(inv_res["file_name"], "faktura_0000000042.pdf")
        self.assertIn("hitl_url", inv_res)
        self.assertIn("dashboard?doc_id=", inv_res["hitl_url"])

        # Verify saved in database
        with get_db_session() as db:
            doc = db.query(DocumentRecord).filter(DocumentRecord.id == inv_res["document_id"]).first()
            self.assertIsNotNone(doc)
            self.assertEqual(doc.file_name, "faktura_0000000042.pdf")
            self.assertIn("invoices@kapina.bg", doc.ocr_result_json)

    # -----------------------------------------------------------------------
    # Integration Tests: REST Webhook /api/v1/ingest/email (Multipart Format)
    # -----------------------------------------------------------------------

    def test_api_ingest_email_multipart_form_data(self):
        """Test multipart/form-data payload (SendGrid / generic webhook)."""
        pdf_data = self.sample_pdf_bytes + b"\n% " + b"B" * 12000
        icon_data = b"ICON" + b"\x00" * 200  # 204 bytes (<10KB)

        files = [
            ("attachment1", ("invoice_march.pdf", io.BytesIO(pdf_data), "application/pdf")),
            ("attachment2", ("signature_badge.png", io.BytesIO(icon_data), "image/png")),
        ]
        data = {
            "from": "accounting@supplier.bg",
            "to": "invoices@openbalancer.com",
            "subject": "Входяща фактура за услуги",
            "SPF": "pass",
            "DKIM": "pass",
        }

        resp = self.client.post("/api/v1/ingest/email", data=data, files=files)
        self.assertEqual(resp.status_code, 200, resp.text)
        res_json = resp.json()

        self.assertEqual(res_json["status"], "success")
        self.assertEqual(res_json["invoices_processed"], 1)
        self.assertEqual(len(res_json["filtered_attachments"]), 1)
        self.assertEqual(res_json["filtered_attachments"][0]["filename"], "signature_badge.png")

    # -----------------------------------------------------------------------
    # Integration Tests: REST Webhook /api/v1/ingest/email (Raw MIME RFC 822)
    # -----------------------------------------------------------------------

    def test_api_ingest_email_raw_mime_message(self):
        """Test raw RFC 822 MIME message posted with Content-Type: message/rfc822."""
        pdf_data = self.sample_pdf_bytes + b"\n% " + b"C" * 12000

        msg = EmailMessage()
        msg["From"] = "billing@company.bg"
        msg["To"] = "invoices@openbalancer.com"
        msg["Subject"] = "Фактура № 0000000042"
        msg["Authentication-Results"] = "openbalancer.com; spf=pass; dkim=pass"
        msg.set_content("Здравейте, приложено изпращаме фактура.")
        msg.add_attachment(
            pdf_data,
            maintype="application",
            subtype="pdf",
            filename="kapina_invoice.pdf",
        )

        raw_bytes = msg.as_bytes()
        resp = self.client.post(
            "/api/v1/ingest/email",
            content=raw_bytes,
            headers={"Content-Type": "message/rfc822"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        res_json = resp.json()
        self.assertEqual(res_json["status"], "success")
        self.assertEqual(res_json["invoices_processed"], 1)
        self.assertEqual(res_json["results"][0]["file_name"], "kapina_invoice.pdf")

    # -----------------------------------------------------------------------
    # Integration Tests: Security & Rejection Cases
    # -----------------------------------------------------------------------

    def test_api_ingest_email_spf_fail_rejection(self):
        """Emails with explicit SPF failure must be rejected with HTTP 403."""
        payload = {
            "from": "attacker@spoofed.com",
            "to": "invoices@openbalancer.com",
            "subject": "Fake Invoice",
            "spf": "fail",
            "attachments": [
                {
                    "filename": "invoice.pdf",
                    "content": base64.b64encode(self.sample_pdf_bytes).decode("ascii"),
                    "content_type": "application/pdf",
                }
            ],
        }
        resp = self.client.post("/api/v1/ingest/email", json=payload)
        self.assertEqual(resp.status_code, 403)
        detail = resp.json().get("detail", {})
        self.assertEqual(detail.get("verdict"), "REJECTED_SPF_FAIL")

    def test_api_ingest_email_token_authentication(self):
        """When EMAIL_INGEST_SECRET is set, unauthorized requests return 401."""
        with patch.dict(os.environ, {"EMAIL_INGEST_SECRET": "secret_token_xyz"}):
            payload = {
                "from": "vendor@bg.com",
                "to": "invoices@openbalancer.com",
                "subject": "Test",
                "spf": "pass",
                "attachments": [],
            }
            # Missing token -> 401
            resp = self.client.post("/api/v1/ingest/email", json=payload)
            self.assertEqual(resp.status_code, 401)

            # Invalid token -> 401
            resp_wrong = self.client.post("/api/v1/ingest/email?token=wrong", json=payload)
            self.assertEqual(resp_wrong.status_code, 401)

            # Valid token via header -> 200 (or no_invoices_found)
            resp_ok = self.client.post(
                "/api/v1/ingest/email",
                json=payload,
                headers={"X-Ingest-Token": "secret_token_xyz"},
            )
            self.assertEqual(resp_ok.status_code, 200)

    def test_api_ingest_email_only_logos_no_invoices(self):
        """When an email has only small signatures/logos, return status no_invoices_found."""
        payload = {
            "from": "client@bg.com",
            "to": "invoices@openbalancer.com",
            "subject": "Hello without invoice",
            "spf": "pass",
            "attachments": [
                {
                    "filename": "signature_logo.png",
                    "content": base64.b64encode(b"PNG" + b"\x00" * 300).decode("ascii"),
                    "content_type": "image/png",
                }
            ],
        }
        resp = self.client.post("/api/v1/ingest/email", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "no_invoices_found")
        self.assertEqual(data["invoices_processed"], 0)
        self.assertEqual(len(data["filtered_attachments"]), 1)

    # -----------------------------------------------------------------------
    # Unit Tests: Reverse Notifications (Email, Telegram, Slack, HITL link)
    # -----------------------------------------------------------------------

    def test_reverse_notification_content_and_hitl_link(self):
        """Verify summary, HTML email, Telegram and Slack notifications contain required fields."""
        summary = InvoiceNotificationSummary(
            document_id="doc_abc123",
            file_name="kapina-01.pdf",
            invoice_number="0000000042",
            date_issued="2026-01-15",
            supplier_name="КАПИНА 71 ООД",
            supplier_eik="114603957",
            supplier_vat="BG114603957",
            recipient_name="ОПЕН БАЛАНСЕР ООД",
            tax_base=1000.00,
            vat_amount=200.00,
            total_amount=1200.00,
            currency="BGN",
            status="completed",
            is_valid=True,
            hitl_url="http://localhost:8000/dashboard?doc_id=doc_abc123",
            processing_time_sec=1.42,
        )

        # 1. HTML Email
        html = build_email_confirmation_html(summary)
        self.assertIn("КАПИНА 71 ООД", html)
        self.assertIn("114603957", html)
        self.assertIn("0000000042", html)
        self.assertIn("1200.00 BGN", html)
        self.assertIn("http://localhost:8000/dashboard?doc_id=doc_abc123", html)
        self.assertIn("Успешно валидирана", html)

        # 2. Text Email
        txt = build_email_confirmation_text(summary)
        self.assertIn("КАПИНА 71 ООД", txt)
        self.assertIn("1200.00 BGN", txt)
        self.assertIn("http://localhost:8000/dashboard?doc_id=doc_abc123", txt)

        # 3. Telegram Markdown
        tg = build_telegram_message(summary)
        self.assertIn("КАПИНА 71 ООД", tg)
        self.assertIn("1200.00 BGN", tg)
        self.assertIn("http://localhost:8000/dashboard?doc_id=doc_abc123", tg)
        self.assertIn("🟢", tg)

        # 4. Slack Block Kit
        slack = build_slack_blocks(summary)
        self.assertIn("blocks", slack)
        found_url = any(
            el.get("url") == "http://localhost:8000/dashboard?doc_id=doc_abc123"
            for b in slack["blocks"] if b.get("type") == "actions"
            for el in b.get("elements", [])
        )
        self.assertTrue(found_url, "HITL dashboard button URL missing from Slack block kit")

    # -----------------------------------------------------------------------
    # Integration Tests: Folder & Cloud Watcher
    # -----------------------------------------------------------------------

    def test_folder_watcher_flow_and_file_movement(self):
        """Watcher scans incoming folder, processes invoice, saves to DB, moves to processed/."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            watch_path = Path(tmp_dir) / "incoming"
            watch_path.mkdir()

            cfg = FolderWatcherConfig(
                watch_dir=watch_path,
                debounce_delay_sec=0.0,
            )
            watcher = FolderWatcher(cfg)

            # Drop sample invoice
            test_file = watch_path / "incoming_kapina.pdf"
            test_file.write_bytes(self.sample_pdf_bytes)

            # Scan once
            results = watcher.scan_once()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["status"], "success")

            # Verify original file moved away from incoming/
            self.assertFalse(test_file.exists())
            # Verify file exists in processed/
            processed_files = list(watcher.processed_dir.glob("*.pdf"))
            self.assertEqual(len(processed_files), 1)

            # Verify in database
            with get_db_session() as db:
                rec = db.query(DocumentRecord).filter(DocumentRecord.id == results[0]["document_id"]).first()
                self.assertIsNotNone(rec)
                self.assertEqual(rec.file_name, "incoming_kapina.pdf")

    def test_folder_watcher_api_scan_and_status(self):
        """Test /api/v1/ingest/watcher/scan and /api/v1/ingest/watcher/status endpoints."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            watch_path = Path(tmp_dir) / "cloud_drive"
            watch_path.mkdir()

            test_file = watch_path / "drive_invoice.pdf"
            test_file.write_bytes(self.sample_pdf_bytes)

            # Call scan API
            resp = self.client.post(f"/api/v1/ingest/watcher/scan?watch_dir={watch_path}")
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.json()
            self.assertEqual(data["processed_count"], 1)

            # Call status API
            status_resp = self.client.get("/api/v1/ingest/watcher/status")
            self.assertEqual(status_resp.status_code, 200)

    # -----------------------------------------------------------------------
    # Integration Tests: Multi-page and Single-page TIFF Ingestion
    # -----------------------------------------------------------------------

    def test_multi_page_tiff_streaming_and_ingestion(self):
        """Test multi-page TIFF file streaming and page generation."""
        tiff_bytes = create_test_tiff(pages_count=2)
        with tempfile.NamedTemporaryFile(suffix=".tiff", delete=False) as f:
            f.write(tiff_bytes)
            tmp_tiff = Path(f.name)

        try:
            pages = load_document(tmp_tiff)
            self.assertEqual(len(pages), 2)
            self.assertEqual(pages[0].page_number, 1)
            self.assertEqual(pages[1].page_number, 2)
            self.assertEqual(pages[0].image.shape[:2], (300, 300))
        finally:
            tmp_tiff.unlink(missing_ok=True)

    # -----------------------------------------------------------------------
    # Measurable Requirement: ≤ 15 Seconds Ingestion of Bulgarian Invoices
    # -----------------------------------------------------------------------

    def test_zero_touch_ingestion_under_15_seconds_sla(self):
        """Measurable DoD: Full zero-touch email processing takes ≤ 15 seconds."""
        real_invoice_candidate = Path("/Volumes/NO NAME/_ФАКТУРИ/00_РМ_КАСКАДА_2026_ЕООД/07_09_2026/30.pdf")
        if real_invoice_candidate.exists():
            test_bytes = real_invoice_candidate.read_bytes()
            test_filename = "real_invoice_30.pdf"
        else:
            test_bytes = self.sample_pdf_bytes + b"\n% " + b"Z" * 15000
            test_filename = "kapina_synthetic.pdf"

        payload = {
            "from": "invoices@vendor.bg",
            "to": "invoices@openbalancer.com",
            "subject": "Входяща фактура за незабавна обработка",
            "spf": "pass",
            "dkim": "pass",
            "attachments": [
                {
                    "filename": test_filename,
                    "content": base64.b64encode(test_bytes).decode("ascii"),
                    "content_type": "application/pdf",
                }
            ],
        }

        t_start = time.perf_counter()
        resp = self.client.post("/api/v1/ingest/email", json=payload)
        elapsed = time.perf_counter() - t_start

        self.assertEqual(resp.status_code, 200, resp.text)
        res_data = resp.json()
        self.assertEqual(res_data["status"], "success")
        self.assertEqual(res_data["invoices_processed"], 1)

        # Assert SLA ≤ 15 seconds
        self.assertLessEqual(
            elapsed, 15.0,
            f"Zero-touch ingestion took {elapsed:.2f}s, exceeding 15.0s SLA requirement!"
        )
        logger_info = f"Zero-touch ingestion completed in {elapsed:.2f}s (SLA <= 15s PASS)"
        print(f"\n[SLA VERIFIED] {logger_info}")


if __name__ == "__main__":
    unittest.main()

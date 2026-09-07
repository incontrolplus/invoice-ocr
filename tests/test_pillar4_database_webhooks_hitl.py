"""Comprehensive Test Suite for Pillar 4 (P1): Database, Webhook Architecture, and HITL Dashboard.

Verifies:
1. Persistent SQLAlchemy Database & Audit Trail:
   - Complete document lifecycle (upload, OCR persistence, field updates, approval)
   - Execution timing, error/warning counts, validation status
   - Audit trail diff tracking (actor, old_value, new_value, timestamp)
   - Persistent JobManager lifecycle surviving simulated server restarts
2. Webhook Architecture & ERP Integration:
   - Rich ERP notification payload with double-entry journal entries (контировки: Д-т 304/602, Д-т 4531, К-т 401)
   - Real-time debit == credit balance check
   - Statutory НАП POKUPKI.TXT ledger record formatting
   - HMAC-SHA256 payload signing (X-Webhook-Signature)
   - End-to-end delivery to Mock ERP receiver with audit log recording
3. Human-in-the-Loop (HITL) Web Dashboard & APIs:
   - HTML5 Canvas dashboard serving (/dashboard & /hitl)
   - Document filtering and search
   - Page rasterization to PNG (/pages/1/image)
   - Field editing diff logging
   - Single-click approval and webhook dispatch
   - Direct document exports (НАП POKUPKI & CSV journal entries)
4. Production Containerization & Dockerfile Validation:
   - Multi-stage Dockerfile verification (builder & runtime)
   - Tesseract 5 & Bulgarian/English language packs configuration
   - OpenCV headless runtime libraries and non-root security user
"""

from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api_server import (
    JobManager,
    JobStatus,
    MOCK_ERP_RECEIVED,
    app,
    job_manager,
)
from database import (
    AuditTrailRecord,
    Base,
    DocumentRecord,
    PersistentJobRecord,
    WebhookLogRecord,
    add_audit_entry,
    approve_document_in_db,
    init_db,
    save_document_to_db,
    storage_manager,
    update_document_corrections,
)
from tests.e2e.test_helpers import create_synthetic_test_image
from webhooks import (
    compute_signature,
    prepare_webhook_payload,
    send_webhook_async,
)


class TestPillar4DatabaseWebhooksHitl(unittest.TestCase):
    """Test suite for persistent DB, Webhooks, HITL web dashboard, and containerization."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        init_db()

    def setUp(self):
        MOCK_ERP_RECEIVED.clear()

    # =======================================================================
    # 1. Persistent Database & Audit Trail Tests
    # =======================================================================

    def test_document_persistence_and_fields(self):
        """Test persisting an OCR result with full 3-layer data and metadata indexing."""
        sample_ocr = {
            "normalized_data": {
                "invoice_metadata": {
                    "invoice_number": "0000005555",
                    "date_issued": "2026-08-15",
                    "date_tax_event": "2026-08-15",
                    "currency": "BGN",
                },
                "supplier": {
                    "name": "КАПИНА 71 ООД",
                    "eik": "121644736",
                    "vat_number": "BG121644736",
                    "address": "гр. София",
                },
                "recipient": {
                    "name": "КЛИЕНТ АД",
                    "eik": "205123456",
                    "vat_number": "BG205123456",
                    "address": "гр. Пловдив",
                },
                "financial_summary": {
                    "tax_base": {"amount": "1000.00", "currency": "BGN"},
                    "vat_amount": {"amount": "200.00", "currency": "BGN"},
                    "total_amount_due": {"amount": "1200.00", "currency": "BGN"},
                },
                "line_items": [
                    {
                        "index": 1,
                        "description": "Консултантски услуги",
                        "quantity": 1,
                        "unit_price_net": {"amount": 1000.00, "currency": "BGN"},
                        "total_price_net": {"amount": 1000.00, "currency": "BGN"},
                        "vat_rate_pct": 20.0,
                    }
                ],
            },
            "validation_results": {
                "is_valid": True,
                "errors": [],
                "warnings": [],
            },
        }

        # Create temporary dummy image file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(b"dummy image data")
            dummy_path = Path(tf.name)

        doc_id = "test_doc_pers_01"
        from database import get_db_session
        with get_db_session() as db:
            doc = save_document_to_db(
                db=db,
                doc_id=doc_id,
                file_name="test_invoice.png",
                source_path=dummy_path,
                ocr_result=sample_ocr,
                processing_time=0.452,
                webhook_url="http://erp.local/webhook",
            )
            self.assertEqual(doc.id, doc_id)
            self.assertEqual(doc.invoice_number, "0000005555")
            self.assertEqual(doc.supplier_eik, "121644736")
            self.assertEqual(doc.tax_base, 1000.0)
            self.assertEqual(doc.vat_amount, 200.0)
            self.assertEqual(doc.total_amount, 1200.0)
            self.assertEqual(doc.status, "completed")
            self.assertTrue(doc.is_valid)

        dummy_path.unlink(missing_ok=True)

    def test_audit_trail_field_corrections(self):
        """Test that updating document fields logs an exact diff in AuditTrailRecord."""
        from database import get_db_session
        doc_id = "test_audit_diff_01"
        sample_ocr = {
            "normalized_data": {
                "invoice_metadata": {"invoice_number": "1111111111"},
                "financial_summary": {"tax_base": 100.0, "vat_amount": 20.0, "total_amount_due": 120.0},
            },
            "validation_results": {"is_valid": True},
        }

        with get_db_session() as db:
            save_document_to_db(
                db=db,
                doc_id=doc_id,
                file_name="invoice_111.png",
                source_path=None,
                ocr_result=sample_ocr,
                processing_time=0.12,
            )

            # Apply corrections
            corrections = {
                "invoice_number": "2222222222",
                "tax_base": 150.0,
                "vat_amount": 30.0,
                "total_amount": 180.0,
            }
            updated = update_document_corrections(
                db=db,
                doc_id=doc_id,
                corrections=corrections,
                actor="ivan_accountant",
            )
            self.assertIsNotNone(updated)
            self.assertEqual(updated.invoice_number, "2222222222")
            self.assertEqual(updated.tax_base, 150.0)

            # Query audit trail
            audits = db.query(AuditTrailRecord).filter(AuditTrailRecord.document_id == doc_id).all()
            actions = [a.action for a in audits]
            self.assertIn("uploaded_and_processed", actions)
            self.assertIn("field_corrected", actions)

            # Check diff details
            field_audits = {a.field_name: (a.old_value, a.new_value) for a in audits if a.action == "field_corrected"}
            self.assertIn("invoice_number", field_audits)
            self.assertEqual(field_audits["invoice_number"], ("1111111111", "2222222222"))
            self.assertEqual(field_audits["tax_base"], ("100.0", "150.0"))

    def test_persistent_job_manager_across_restarts(self):
        """Test that JobManager jobs survive restart by loading from PersistentJobRecord."""
        # Create a new job manager instance and create a job
        jm1 = JobManager()
        job_id = jm1.create_job(request_type="batch-test", metadata={"test_key": "val123"})
        jm1.set_started(job_id, total_documents=5)
        jm1.update_progress(job_id, processed=3, total=5, current_file="file3.pdf")

        # Verify job status in jm1
        job1 = jm1.get_job(job_id)
        self.assertIsNotNone(job1)
        self.assertEqual(job1["status"], JobStatus.PROCESSING.value)
        self.assertEqual(job1["progress"]["percent"], 60.0)

        # Simulate microservice restart by instantiating a fresh JobManager
        # Startup reconciliation marks in-flight jobs as INTERRUPTED so they don't hang indefinitely
        jm2 = JobManager()
        job2 = jm2.get_job(job_id)
        self.assertIsNotNone(job2)
        self.assertEqual(job2["job_id"], job_id)
        self.assertEqual(job2["status"], JobStatus.INTERRUPTED.value)
        self.assertEqual(job2["progress"]["percent"], 60.0)
        self.assertEqual(job2["metadata"]["test_key"], "val123")

        # Complete job in jm2
        jm2.complete_job(job_id, result={"summary": "all done"})
        job2_completed = jm2.get_job(job_id)
        self.assertEqual(job2_completed["status"], JobStatus.COMPLETED.value)
        self.assertEqual(job2_completed["result"]["summary"], "all done")

        # Simulate another restart and check completed status
        jm3 = JobManager()
        job3 = jm3.get_job(job_id)
        self.assertEqual(job3["status"], JobStatus.COMPLETED.value)

    # =======================================================================
    # 2. Webhook Architecture & ERP Integration Tests
    # =======================================================================

    def test_prepare_webhook_payload_with_balanced_journal_entries(self):
        """Test preparing ERP webhook payload with double-entry journal entries and NAP record."""
        doc_dict = {
            "id": "doc_wh_01",
            "file_name": "inv_service.pdf",
            "status": "completed",
            "is_valid": True,
            "processing_time_sec": 0.35,
            "normalized_data": {
                "invoice_metadata": {
                    "invoice_number": "0000009999",
                    "date_issued": "2026-08-16",
                    "date_tax_event": "2026-08-16",
                    "currency": "BGN",
                },
                "supplier": {
                    "name": "МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД",
                    "eik": "121644736",
                    "vat_number": "BG121644736",
                },
                "recipient": {
                    "name": "ТЕСТ КЛИЕНТ ЕООД",
                    "eik": "114500333",
                },
                "financial_summary": {
                    "tax_base": {"amount": "500.00", "currency": "BGN"},
                    "vat_amount": {"amount": "100.00", "currency": "BGN"},
                    "total_amount_due": {"amount": "600.00", "currency": "BGN"},
                },
                "line_items": [
                    {
                        "index": 1,
                        "description": "Търговски стоки",
                        "quantity": 10,
                        "unit_price_net": {"amount": 50.00, "currency": "BGN"},
                        "total_price_net": {"amount": 500.00, "currency": "BGN"},
                        "vat_rate_pct": 20.0,
                    }
                ],
            },
            "validation_results": {"is_valid": True, "errors": [], "warnings": []},
        }

        payload = prepare_webhook_payload(doc_dict, event_type="invoice.processed")
        self.assertEqual(payload["event"], "invoice.processed")
        self.assertEqual(payload["document_id"], "doc_wh_01")
        self.assertEqual(payload["file_name"], "inv_service.pdf")
        self.assertTrue(payload["is_valid"])

        # Verify Double-Entry Journal Entries
        acc = payload["accounting_entries"]
        self.assertIn("entries", acc)
        self.assertTrue(len(acc["entries"]) > 0)
        self.assertTrue(acc["is_balanced"])

        # Check entry accounts: Debit 304/602 + Debit 4531 = Credit 401
        entry = acc["entries"][0]
        self.assertEqual(entry["supplier"]["eik"], "121644736")
        self.assertEqual(entry["vat_account"], "4531")
        self.assertEqual(entry["payable_account"], "401")
        self.assertTrue(entry["is_balanced"])

        # Verify Statutory НАП POKUPKI Record is populated
        nap = payload["statutory_nap_pokupki"]
        self.assertIn("tsv_line", nap)
        self.assertIn("fixed_width_line", nap)
        self.assertTrue(len(nap["tsv_line"]) > 0)
        self.assertTrue(len(nap["fixed_width_line"]) > 0)

    def test_hmac_signature_generation_and_verification(self):
        """Test HMAC-SHA256 signature calculation and verification."""
        secret = "secret_key_12345"
        payload_bytes = b'{"event":"invoice.processed","doc_id":"123"}'
        sig = compute_signature(payload_bytes, secret)

        self.assertTrue(sig.startswith("sha256="))
        expected_hash = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
        self.assertEqual(sig, f"sha256={expected_hash}")

    def test_webhook_dispatch_to_mock_erp(self):
        """Test asynchronous webhook dispatch to /mock-erp/webhook with signature and audit log."""
        # 1. Upload a synthetic invoice to /api/v1/invoices/process specifying mock ERP webhook
        img = create_synthetic_test_image("ФАКТУРА 0000007777 ДОСТАВЧИК ЕИК 121644736 СУМА 60.00 ЛВ")
        _, buf = cv2.imencode(".png", img)
        file_bytes = io.BytesIO(buf.tobytes())

        files = {"file": ("webhook_test_inv.png", file_bytes, "image/png")}
        resp = self.client.post(
            "/api/v1/invoices/process?webhook_url=http://testserver/mock-erp/webhook",
            files=files,
        )
        self.assertEqual(resp.status_code, 200)
        doc_data = resp.json()
        doc_id = doc_data["id"]

        # 2. Check that the mock ERP endpoint received the webhook notification
        mock_resp = self.client.get("/mock-erp/webhook/received")
        self.assertEqual(mock_resp.status_code, 200)
        received_data = mock_resp.json()
        self.assertGreater(received_data["count"], 0)

        latest = received_data["webhooks"][0]
        self.assertEqual(latest["event"], "invoice.processed")
        self.assertEqual(latest["document_id"], doc_id)
        self.assertIn("accounting_entries", latest["payload"])

        # 3. Check Webhook Logs API
        logs_resp = self.client.get("/api/v1/webhooks/logs?limit=5")
        self.assertEqual(logs_resp.status_code, 200)
        logs = logs_resp.json()
        self.assertGreater(len(logs), 0)
        self.assertEqual(logs[0]["target_url"], "http://testserver/mock-erp/webhook")
        self.assertTrue(logs[0]["success"])

    def test_webhook_test_ping_endpoint(self):
        """Test /api/v1/webhooks/test diagnostic ping endpoint."""
        payload = {"target_url": "http://testserver/mock-erp/webhook", "secret": "test_sec"}
        resp = self.client.post("/api/v1/webhooks/test", json=payload)
        self.assertEqual(resp.status_code, 200)
        res = resp.json()
        self.assertTrue(res["success"])
        self.assertEqual(res["status_code"], 200)

    # =======================================================================
    # 3. Human-in-the-Loop (HITL) Web Dashboard Tests
    # =======================================================================

    def test_dashboard_and_hitl_routes(self):
        """Test that /dashboard and /hitl serve the HTML5 HITL interface."""
        resp1 = self.client.get("/dashboard")
        self.assertEqual(resp1.status_code, 200)
        self.assertIn("Счетоводен OCR & HITL Портал", resp1.text)
        self.assertIn("overlay-canvas", resp1.text)
        self.assertIn("Одобри и изпрати към НАП / Счетоводство", resp1.text)

        resp2 = self.client.get("/hitl")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp1.text, resp2.text)

    def test_hitl_document_flow(self):
        """Test full HITL flow: upload -> get -> render page -> correct -> approve -> export."""
        # 1. Upload
        img = create_synthetic_test_image("ФАКТУРА 0000008888 ДОСТАВЧИК ЕИК 121644736 СУМА 120.00 ЛВ")
        _, buf = cv2.imencode(".png", img)
        files = {"file": ("hitl_flow.png", io.BytesIO(buf.tobytes()), "image/png")}

        upload_resp = self.client.post("/api/v1/invoices/process", files=files)
        self.assertEqual(upload_resp.status_code, 200)
        doc = upload_resp.json()
        doc_id = doc["id"]

        # 2. List documents
        list_resp = self.client.get("/api/v1/documents?limit=10")
        self.assertEqual(list_resp.status_code, 200)
        docs = list_resp.json()["documents"]
        self.assertTrue(any(d["id"] == doc_id for d in docs))

        # 3. Get document details with raw evidence
        detail_resp = self.client.get(f"/api/v1/documents/{doc_id}?include_raw_evidence=true")
        self.assertEqual(detail_resp.status_code, 200)
        det = detail_resp.json()
        self.assertIn("raw_ocr_evidence", det)
        self.assertIn("pages", det["raw_ocr_evidence"])

        # 4. Render document page as PNG
        page_img_resp = self.client.get(f"/api/v1/documents/{doc_id}/pages/1/image")
        self.assertEqual(page_img_resp.status_code, 200)
        self.assertEqual(page_img_resp.headers["content-type"], "image/png")
        self.assertGreater(len(page_img_resp.content), 100)

        # 5. Apply corrections via HITL
        corrections = {
            "invoice_number": "0000008888",
            "supplier_name": "МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД",
            "tax_base": 100.0,
            "vat_amount": 20.0,
            "total_amount": 120.0,
            "line_items": [
                {
                    "index": 1,
                    "description": "Коригиран артикул",
                    "quantity": 2,
                    "unit_price_net": 50.0,
                    "total_price_net": 100.0,
                    "vat_rate_pct": 20,
                }
            ],
        }
        correct_resp = self.client.post(
            f"/api/v1/documents/{doc_id}/correct?actor=chief_accountant",
            json=corrections,
        )
        self.assertEqual(correct_resp.status_code, 200)
        cor_data = correct_resp.json()
        self.assertEqual(cor_data["invoice_number"], "0000008888")
        self.assertEqual(cor_data["supplier"]["name"], "МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД")

        # 6. Check Audit Trail
        audit_resp = self.client.get(f"/api/v1/documents/{doc_id}/audit-trail")
        self.assertEqual(audit_resp.status_code, 200)
        audits = audit_resp.json()
        actors = [a["actor"] for a in audits]
        self.assertIn("chief_accountant", actors)

        # 7. Approve Document with Webhook
        approve_resp = self.client.post(
            f"/api/v1/documents/{doc_id}/approve",
            json={
                "actor": "chief_accountant",
                "webhook_url": "http://testserver/mock-erp/webhook",
            },
        )
        self.assertEqual(approve_resp.status_code, 200)
        appr_data = approve_resp.json()
        self.assertEqual(appr_data["status"], "approved")

        # 8. Check that approval webhook was dispatched to mock ERP
        mock_resp = self.client.get("/mock-erp/webhook/received")
        latest = mock_resp.json()["webhooks"][0]
        self.assertEqual(latest["event"], "invoice.approved")
        self.assertEqual(latest["document_id"], doc_id)

        # 9. Export НАП POKUPKI.TXT for this single document
        pokupki_resp = self.client.get(f"/api/v1/documents/{doc_id}/export/pokupki")
        self.assertEqual(pokupki_resp.status_code, 200)
        self.assertIn("POKUPKI", pokupki_resp.headers["content-disposition"])

        # 10. Export Journal Entries (CSV) for this document
        je_resp = self.client.get(f"/api/v1/documents/{doc_id}/export/journal-entries?format=universal")
        self.assertEqual(je_resp.status_code, 200)
        self.assertIn("journal_entries", je_resp.headers["content-disposition"])

    # =======================================================================
    # 4. Production Containerization & Dockerfile Validation
    # =======================================================================

    def test_dockerfile_and_compose_configuration(self):
        """Validate multi-stage Dockerfile, docker-compose, and .dockerignore syntax."""
        dockerfile_path = Path("Dockerfile")
        self.assertTrue(dockerfile_path.exists(), "Dockerfile must exist")
        content = dockerfile_path.read_text(encoding="utf-8")

        # Verify multi-stage build
        self.assertIn("AS builder", content)
        self.assertIn("AS runtime", content)
        self.assertIn("COPY --from=builder", content)

        # Verify Tesseract 5 and Bulgarian/English models
        self.assertIn("tesseract-ocr", content)
        self.assertIn("tesseract-ocr-bul", content)
        self.assertIn("tesseract-ocr-eng", content)
        self.assertIn("tesseract-ocr-osd", content)

        # Verify headless graphics & OpenCV runtime libs
        self.assertIn("libgl1", content)
        self.assertIn("libglib2.0-0", content)

        # Verify security hardening: non-root user and healthcheck
        self.assertIn("useradd", content)
        self.assertIn("USER appuser", content)
        self.assertIn("HEALTHCHECK", content)

        # Verify docker-compose.yml
        compose_path = Path("docker-compose.yml")
        self.assertTrue(compose_path.exists(), "docker-compose.yml must exist")
        compose_text = compose_path.read_text(encoding="utf-8")
        self.assertIn("postgres:16-alpine", compose_text)
        self.assertIn("DATABASE_URL", compose_text)
        self.assertIn("healthcheck", compose_text)

        # Verify .dockerignore
        dockerignore_path = Path(".dockerignore")
        self.assertTrue(dockerignore_path.exists(), ".dockerignore must exist")
        dign_text = dockerignore_path.read_text(encoding="utf-8")
        self.assertIn(".venv", dign_text)
        self.assertIn("__pycache__", dign_text)


if __name__ == "__main__":
    unittest.main()

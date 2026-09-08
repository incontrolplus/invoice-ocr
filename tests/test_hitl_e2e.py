"""End-to-End Test Suite for Goal 2: HITL (Human-in-the-Loop) Interface and Workflow (P1).

Validates:
1. Web interface on /hitl:
   - Split-screen layout: high-resolution canvas with colored bounding boxes on left,
     editable form with real-time recalculation on right.
   - Zoom and pan controls, page navigation, legend, active field tracking.
2. Visual discrepancy indication (Red highlight):
   - Broken mathematical control under Art. 26 ZDDS (Tax base + VAT != Total amount).
   - Missing or invalid Bulgarian EIK under Art. 114 ZDDS (Modulo 11).
   - Real-time transition to green (Debet == Credit) upon manual correction.
3. Click-to-fill / Click-to-correct:
   - Clicking on a bounding box in the document populates/corrects the active form field.
   - Quick assignment popover supports single-click field binding.
4. Button "Одобри и експортирай":
   - Saves manual corrections in database.py with field-level diff audit trail.
   - Approves document status in database.py.
   - Dispatches outbound ERP webhook with balanced double-entry journal entries.
   - Generates and returns statutory НАП POKUPKI.TXT and journal entries (CSV/JSON).
   - Logs "exported" and "approved_by_accountant" in the immutable audit log.
5. Complete E2E cycle:
   - Upload invoice with error -> Open in HITL -> Manual correction -> Confirm/Approve -> Generated export.
"""

from datetime import datetime, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
from fastapi.testclient import TestClient

from api_server import (
    MOCK_ERP_RECEIVED,
    app,
)
from database import (
    AuditTrailRecord,
    DocumentRecord,
    get_db_session,
    init_db,
    save_document_to_db,
    update_document_corrections,
    approve_document_in_db,
)
from tests.e2e.test_helpers import create_synthetic_test_image


class TestHitlInterfaceAndWorkflowE2E(unittest.TestCase):
    """Rigorous E2E test suite covering the full HITL lifecycle."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        init_db()

    def setUp(self):
        MOCK_ERP_RECEIVED.clear()

    def _create_test_error_invoice(self, doc_id: str) -> DocumentRecord:
        """Helper to create a document with intentional math and EIK discrepancies."""
        error_ocr = {
            "normalized_data": {
                "invoice_metadata": {
                    "invoice_number": "0000001234",
                    "date_issued": "2026-08-16",
                    "date_tax_event": "2026-08-16",
                    "currency": "BGN",
                },
                "supplier": {
                    "name": "ТЕСТОВ ДОСТАВЧИК ЕООД",
                    "eik": None,  # Missing EIK!
                    "vat_number": None,
                    "address": "гр. София, бул. България 1",
                },
                "recipient": {
                    "name": "КАПИНА 71 ООД",
                    "eik": "100000086",
                    "vat_number": "BG100000086",
                },
                "financial_summary": {
                    "tax_base": {"amount": "100.00", "currency": "BGN"},
                    "vat_amount": {"amount": "20.00", "currency": "BGN"},
                    "total_amount_due": {"amount": "150.00", "currency": "BGN"},  # 100 + 20 != 150!
                },
                "line_items": [
                    {
                        "index": 1,
                        "description": "Счетоводни консултации",
                        "quantity": 1,
                        "unit_price_net": {"amount": 100.00, "currency": "BGN"},
                        "total_price_net": {"amount": 100.00, "currency": "BGN"},
                        "vat_rate_pct": 20.0,
                    }
                ],
            },
            "validation_results": {
                "is_valid": False,
                "errors": [
                    {"code": "TOTAL_SUM_MISMATCH", "message": "Tax base (100.00) + VAT (20.00) = 120.00, but total is 150.00", "severity": "error"},
                    {"code": "MISSING_SUPPLIER_EIK", "message": "Supplier EIK was not detected (Art. 114 ЗДДС)", "severity": "error"},
                ],
                "warnings": [],
            },
            "raw_ocr_evidence": {
                "total_pages": 1,
                "total_tokens": 4,
                "pages": [
                    {
                        "page_number": 1,
                        "width": 800,
                        "height": 1100,
                        "tokens": [
                            {"text": "ФАКТУРА", "conf": 95.0, "bbox": [50, 50, 120, 30], "page_number": 1, "is_low_confidence": False},
                            {"text": "0000001234", "conf": 92.0, "bbox": [180, 50, 140, 30], "page_number": 1, "is_low_confidence": False},
                            {"text": "121644736", "conf": 94.0, "bbox": [100, 150, 110, 25], "page_number": 1, "is_low_confidence": False},
                            {"text": "120.00", "conf": 89.0, "bbox": [600, 450, 90, 25], "page_number": 1, "is_low_confidence": False},
                        ],
                    }
                ],
            },
        }

        with get_db_session() as db:
            doc = save_document_to_db(
                db=db,
                doc_id=doc_id,
                file_name=f"{doc_id}.png",
                source_path=None,
                ocr_result=error_ocr,
                processing_time=0.25,
            )
            return doc

    # =======================================================================
    # 1. HITL Web Interface & Split-Screen Structure Tests
    # =======================================================================

    def test_hitl_split_screen_html_structure(self):
        """Verify that /hitl serves full split-screen UI with canvas, zoom, and balance controls."""
        resp = self.client.get("/hitl")
        self.assertEqual(resp.status_code, 200)
        html = resp.text

        # 1. Side-by-side split screen structure
        self.assertIn("class=\"workspace\"", html)
        self.assertIn("class=\"left-panel\"", html)
        self.assertIn("class=\"right-panel\"", html)

        # 2. Canvas & Bounding boxes overlay
        self.assertIn("id=\"overlay-canvas\"", html)
        self.assertIn("id=\"doc-image\"", html)
        self.assertIn("id=\"canvas-tooltip\"", html)
        self.assertIn("id=\"toggle-boxes\"", html)

        # 3. Zoom and Pan Controls
        self.assertIn("zoomChange", html)
        self.assertIn("zoomReset", html)
        self.assertIn("zoomFitWidth", html)
        self.assertIn("id=\"zoom-display\"", html)

        # 4. Real-time balance and validation
        self.assertIn("id=\"balance-card\"", html)
        self.assertIn("id=\"validation-banner\"", html)
        self.assertIn("id=\"balance-status\"", html)
        self.assertIn("чл. 26 ЗДДС", html)
        self.assertIn("id=\"eik-checksum-indicator\"", html)

        # 5. Click-to-fill and Popover
        self.assertIn("id=\"box-popover\"", html)
        self.assertIn("id=\"active-target-bar\"", html)
        self.assertIn("assignPopoverText", html)

        # 6. "Одобри и експортирай" button
        self.assertIn("id=\"btn-approve-export\"", html)
        self.assertIn("Одобри и експортирай", html)

    # =======================================================================
    # 2. Visual Discrepancy & Mathematical Control Tests
    # =======================================================================

    def test_math_and_eik_discrepancy_detection_on_upload(self):
        """Uploading invoice with math mismatch and missing EIK flags errors and sets status to needs_review."""
        doc_id = "hitl_test_discrepancy_01"
        self._create_test_error_invoice(doc_id)

        # Retrieve document via API with include_raw_evidence=true
        resp = self.client.get(f"/api/v1/documents/{doc_id}?include_raw_evidence=true")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "needs_review")
        self.assertFalse(data["is_valid"])
        self.assertIn("raw_ocr_evidence", data)
        self.assertEqual(len(data["raw_ocr_evidence"]["pages"][0]["tokens"]), 4)

        # Verify validation error codes reflect Art. 26 and Art. 114 ZDDS
        val = data.get("validation", {})
        err_codes = [e["code"] for e in val.get("errors", [])]
        self.assertIn("TOTAL_SUM_MISMATCH", err_codes)
        self.assertIn("MISSING_SUPPLIER_EIK", err_codes)

    # =======================================================================
    # 3. Manual Correction & Re-Validation in database.py
    # =======================================================================

    def test_manual_field_correction_updates_validity_and_audit_diff(self):
        """Manual field correction of total_amount and supplier_eik restores is_valid and records diff."""
        doc_id = "hitl_test_correct_01"
        self._create_test_error_invoice(doc_id)

        corrections = {
            "supplier_eik": "121644736",      # Valid Bulgarian EIK (Modulo 11)
            "total_amount": 120.00,           # Restores Art. 26 ZDDS math: 100.00 + 20.00 == 120.00
            "supplier_name": "МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД",
        }

        resp = self.client.post(
            f"/api/v1/documents/{doc_id}/correct?actor=chief_accountant",
            json=corrections,
        )
        self.assertEqual(resp.status_code, 200)
        updated = resp.json()

        self.assertEqual(updated["supplier"]["eik"], "121644736")
        self.assertEqual(updated["total_amount"], 120.00)
        self.assertTrue(updated["is_valid"], "Document must be valid after correcting math and EIK")
        self.assertEqual(updated["error_count"], 0)
        self.assertEqual(updated["status"], "completed")

        # Verify exact diff in AuditTrailRecord
        with get_db_session() as db:
            audits = db.query(AuditTrailRecord).filter(
                AuditTrailRecord.document_id == doc_id,
                AuditTrailRecord.action == "field_corrected",
            ).all()
            diff_map = {a.field_name: (a.old_value, a.new_value) for a in audits}
            self.assertIn("supplier_eik", diff_map)
            self.assertEqual(diff_map["supplier_eik"][1], "121644736")
            self.assertIn("total_amount", diff_map)
            self.assertEqual(diff_map["total_amount"][1], "120.0")

    # =======================================================================
    # 4. "Одобри и експортирай" Endpoint Tests
    # =======================================================================

    def test_approve_and_export_atomic_workflow(self):
        """Test POST /api/v1/documents/{doc_id}/approve-and-export atomic endpoint."""
        doc_id = "hitl_test_atomic_01"
        self._create_test_error_invoice(doc_id)

        # Apply correction and approve in single atomic call
        approve_req = {
            "actor": "senior_accountant_petrova",
            "webhook_url": "http://testserver/mock-erp/webhook",
            "corrections": {
                "supplier_eik": "121644736",
                "supplier_name": "МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД",
                "total_amount": 120.00,
            },
        }

        resp = self.client.post(
            f"/api/v1/documents/{doc_id}/approve-and-export",
            json=approve_req,
        )
        self.assertEqual(resp.status_code, 200)
        res = resp.json()

        # 1. Verification of Approval Status
        self.assertEqual(res["status"], "approved")
        self.assertTrue(res["webhook_queued"])
        self.assertEqual(res["document"]["status"], "approved")
        self.assertEqual(res["document"]["supplier"]["eik"], "121644736")
        self.assertTrue(res["document"]["is_valid"])

        # 2. Verification of Generated Accounting Exports
        exports = res["exports"]
        self.assertIn("pokupki", exports)
        self.assertIn("journal_entries_csv", exports)
        self.assertIn("journal_entries_json", exports)

        # Check НАП POKUPKI text structure
        pokupki_txt = exports["pokupki"]["content"]
        self.assertIn("0000001234", pokupki_txt)
        self.assertIn("121644736", pokupki_txt)

        # Check Double-Entry Journal Entries (Контировки: 602/304, 4531, 401)
        journal_csv = exports["journal_entries_csv"]["content"]
        self.assertIn("4531", journal_csv)
        self.assertIn("401", journal_csv)

        # 3. Verification of Audit Trail Entries in DB
        with get_db_session() as db:
            audits = db.query(AuditTrailRecord).filter(
                AuditTrailRecord.document_id == doc_id
            ).order_by(AuditTrailRecord.timestamp.asc()).all()

            actions = [a.action for a in audits]
            self.assertIn("field_corrected", actions)
            self.assertIn("approved_by_accountant", actions)
            self.assertIn("exported", actions)

            # Check actors
            actors = [a.actor for a in audits]
            self.assertIn("senior_accountant_petrova", actors)

        # 4. Verify Outbound Webhook received by Mock ERP
        mock_resp = self.client.get("/mock-erp/webhook/received")
        self.assertEqual(mock_resp.status_code, 200)
        received = mock_resp.json()
        self.assertGreater(received["count"], 0)
        webhook_item = next(w for w in received["webhooks"] if w["document_id"] == doc_id)
        self.assertEqual(webhook_item["event"], "invoice.approved")
        self.assertTrue(webhook_item["payload"]["accounting_entries"]["is_balanced"])

    # =======================================================================
    # 5. Complete End-to-End Cycle Test (DoD Cycle)
    # =======================================================================

    def test_full_hitl_cycle_upload_review_correct_approve_export(self):
        """Full cycle: upload error invoice -> inspect in HITL -> correct -> approve -> export."""
        # 1. Upload Synthetic Invoice
        img = create_synthetic_test_image("ФАКТУРА 0000009999 СУМА 999.00 ЛВ")
        _, buf = cv2.imencode(".png", img)
        file_bytes = io.BytesIO(buf.tobytes())

        files = {"file": ("degraded_invoice.png", file_bytes, "image/png")}
        upload_resp = self.client.post("/api/v1/invoices/process", files=files)
        self.assertEqual(upload_resp.status_code, 200)
        doc = upload_resp.json()
        doc_id = doc["id"]

        # 2. Verify Document is Ingested & requires review
        get_resp = self.client.get(f"/api/v1/documents/{doc_id}?include_raw_evidence=true")
        self.assertEqual(get_resp.status_code, 200)
        det = get_resp.json()
        self.assertIn("raw_ocr_evidence", det)

        # 3. Simulate Accountant Correction in HITL
        # Set proper supplier, valid EIK, balanced numbers under Art. 26 ZDDS
        corrections = {
            "invoice_number": "0000009999",
            "date_issued": "2026-08-16",
            "date_tax_event": "2026-08-16",
            "supplier_name": "МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД",
            "supplier_eik": "121644736",
            "supplier_vat": "BG121644736",
            "supplier_address": "гр. София, бул. Цариградско шосе 7-11",
            "recipient_name": "КАПИНА 71 ООД",
            "recipient_eik": "100000086",
            "recipient_vat": "BG100000086",
            "recipient_address": "гр. София, ул. Централна 5",
            "tax_base": 832.50,
            "vat_amount": 166.50,
            "total_amount": 999.00,  # 832.50 + 166.50 == 999.00 (Balanced!)
            "line_items": [
                {
                    "index": 1,
                    "description": "Хардуерни компоненти",
                    "quantity": 1,
                    "unit_price_net": {"amount": 832.50, "currency": "BGN"},
                    "total_price_net": {"amount": 832.50, "currency": "BGN"},
                    "vat_rate_pct": 20.0,
                }
            ],
        }

        # 4. Accountant clicks "Одобри и експортирай"
        approve_resp = self.client.post(
            f"/api/v1/documents/{doc_id}/approve-and-export",
            json={
                "actor": "glaven_schetovoditel",
                "webhook_url": "http://testserver/mock-erp/webhook",
                "corrections": corrections,
            },
        )
        self.assertEqual(approve_resp.status_code, 200)
        appr = approve_resp.json()
        self.assertEqual(appr["status"], "approved")
        self.assertEqual(appr["document"]["total_amount"], 999.00)
        self.assertEqual(appr["document"]["supplier"]["eik"], "121644736")
        self.assertTrue(appr["document"]["is_valid"])

        # 5. Verify Exports Generated & Audit Log
        exports = appr["exports"]
        self.assertIn("pokupki", exports)
        self.assertIn("0000009999", exports["pokupki"]["content"])

        # 6. Verify Direct Export Endpoint also logs audit entry
        pok_resp = self.client.get(f"/api/v1/documents/{doc_id}/export/pokupki")
        self.assertEqual(pok_resp.status_code, 200)
        self.assertIn("POKUPKI", pok_resp.headers["content-disposition"])

        je_resp = self.client.get(f"/api/v1/documents/{doc_id}/export/journal-entries?format=universal")
        self.assertEqual(je_resp.status_code, 200)
        self.assertIn("journal_entries", je_resp.headers["content-disposition"])

        # 7. Check Audit Trail History
        audit_resp = self.client.get(f"/api/v1/documents/{doc_id}/audit-trail")
        self.assertEqual(audit_resp.status_code, 200)
        audits = audit_resp.json()
        actions = [a["action"] for a in audits]
        self.assertIn("uploaded_and_processed", actions)
        self.assertIn("field_corrected", actions)
        self.assertIn("approved_by_accountant", actions)
        self.assertIn("exported", actions)


if __name__ == "__main__":
    unittest.main()

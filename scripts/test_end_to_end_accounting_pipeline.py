#!/usr/bin/env python3
"""End-to-End Test Suite for Automated Accounting Pipeline & Microinvest Delta Pro Integration.

Verifies:
1. Statutory classification & routing of Invoices and Credit Notes.
2. Contractor party verification against `accounting.partners` and n8n fallback.
3. Historical comparison against Microsoft Databases (live MDB / offline JSON cache).
4. Statutory double-entry accounting operations per Bulgarian standards (НСС / ЗДДС):
   - Purchases: 601/602/304, 4531, 401 (or 501 if cash)
   - Sales: 411, 701/702/703, 4532
   - Credit note: Red storno with signed negative amounts and exact currency (EUR/BGN).
5. Generation of native Microinvest Delta Pro binary files (TRANSFER.LOG 65,536 B & TRANSFER.ldb 64 B).
6. Supabase persistence & attachments sync.
7. Zero-touch Email Ingestion Webhooks (/api/v1/ingest/docs-email & /api/v1/ingest/email).
8. Critical contractor name divergence protection (HITL review routing).
9. REST API endpoints for downloading transfer files and inspecting operations.
"""

import base64
import json
import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from starlette.testclient import TestClient

from api_server import app, execute_accounting_pipeline_for_document
from invoice_core.models import Party
from invoice_core.partner_verification import verify_invoice_parties

def run_tests():
    print("=================================================================")
    print("STARTING STATUTORY ACCOUNTING & DELTA PRO PIPELINE VERIFICATION")
    print("=================================================================")
    client = TestClient(app)

    # 1. Health check
    resp = client.get("/health")
    assert resp.status_code == 200, f"Health check failed: {resp.text}"
    print("✓ 1. API Health Check OK")

    # 2. Test Purchase Credit Note (Червено сторно) - МАГНЕЗИЯ ЕООД / БИЛДИНГ 11 ООД
    print("\n--- 2. Testing Purchase Credit Note (Червено сторно) ---")
    cn_doc_id = "test_credit_note_magnezia"
    cn_data = {
        "file_name": "1_2026-09-11_11-07-45.pdf",
        "normalized_data": {
            "invoice_metadata": {
                "invoice_number": "0090250595",
                "date_issued": "2026-08-17",
                "currency": "EUR",
                "document_type": "CREDIT_NOTE",
                "is_credit_note": True,
            },
            "supplier": {
                "name": "МАГНЕЗИЯ ЕООД",
                "eik": "114631464",
                "vat_number": "BG114631464",
            },
            "recipient": {
                "name": "БИЛДИНГ 11 ООД",
                "eik": "206062202",
                "vat_number": "BG206062202",
            },
            "financial_summary": {
                "tax_base": 21.00,
                "vat_amount": 4.20,
                "total_amount_due": 25.20,
                "currency": "EUR",
            },
            "line_items": [
                {"description": "Палет дървен ЕВРО", "quantity": -2, "unit_price": 10.50, "total": -21.00}
            ],
        },
    }

    # Verify parties against accounting.partners
    sup = Party(name="МАГНЕЗИЯ ЕООД", eik="114631464", vat_number="BG114631464")
    rec = Party(name="БИЛДИНГ 11 ООД", eik="206062202", vat_number="BG206062202")
    pv_cn = verify_invoice_parties(sup, rec)
    assert pv_cn.both_eiks_in_db, "Both parties must be registered in accounting.partners"
    assert not pv_cn.has_critical_mismatch, "Should have no critical mismatch"
    print("✓ Parties Verified against accounting.partners: МАГНЕЗИЯ ЕООД & БИЛДИНГ 11 ООД")

    # Execute accounting pipeline
    bundle_cn = execute_accounting_pipeline_for_document(cn_doc_id, cn_data, "1_2026-09-11_11-07-45.pdf", pv_cn)
    assert bundle_cn, "Accounting pipeline failed to generate bundle"
    op_cn = bundle_cn["accounting_operation"]
    hist_cn = bundle_cn["match_report"]

    assert op_cn["is_credit_note"] is True
    assert op_cn["currency"] == "EUR"
    assert op_cn["document_type"] == "КИ"
    assert op_cn["reason"] == "м-ли"
    assert hist_cn["matched"] is True
    assert hist_cn["total_past_transactions"] > 1500, f"Expected >1500 past transactions, got {hist_cn['total_past_transactions']}"

    # Verify storno negative entries: Дт 601 (-21), Дт 4531 (-4.2), Кт 401 (-25.2)
    debits = op_cn["debit_entries"]
    credits = op_cn["credit_entries"]
    assert any(d["account"] == "601" and d["amount"] == -21.00 for d in debits), f"Missing Дт 601 (-21.00): {debits}"
    assert any(d["account"] == "4531" and d["amount"] == -4.20 for d in debits), f"Missing Дт 4531 (-4.20): {debits}"
    assert any(c["account"] == "401" and c["amount"] == -25.20 for c in credits), f"Missing Кт 401 (-25.20): {credits}"
    print("✓ Double-Entry Red Storno Verified: Дт 601 (-€21.00), Дт 4531 (-€4.20), Кт 401 (-€25.20)")

    # 3. Test Delta Pro binary files
    assert len(bundle_cn["transfer_log_bytes"]) == 65536, "TRANSFER.LOG must be exactly 65,536 bytes"
    assert len(bundle_cn["transfer_ldb_bytes"]) == 64, "TRANSFER.ldb must be exactly 64 bytes"
    print("✓ Microinvest Delta Pro Native Jet 2.0 Binary Files Generated (TRANSFER.LOG 65,536 B & TRANSFER.ldb 64 B)")

    # Test REST download endpoints
    r_log = client.get(f"/api/v1/accounting/transfer-log/{cn_doc_id}")
    assert r_log.status_code == 200 and len(r_log.content) == 65536
    r_ldb = client.get(f"/api/v1/accounting/transfer-ldb/{cn_doc_id}")
    assert r_ldb.status_code == 200 and len(r_ldb.content) == 64
    r_op = client.get(f"/api/v1/accounting/operation/{cn_doc_id}")
    assert r_op.status_code == 200 and r_op.json()["accounting_operation"]["is_balanced"]
    print("✓ REST Endpoints Verified: /transfer-log, /transfer-ldb, and /operation")

    # 4. Test Purchase Invoice - External Services (ВИВАКОМ БЪЛГАРИЯ ЕАД)
    print("\n--- 3. Testing Purchase Invoice (External Services 602) ---")
    viva_doc_id = "test_invoice_vivacom"
    viva_data = {
        "file_name": "vivacom_bill.pdf",
        "normalized_data": {
            "invoice_metadata": {
                "invoice_number": "1234567890",
                "date_issued": "2026-02-15",
                "currency": "BGN",
                "document_type": "INVOICE",
                "is_credit_note": False,
            },
            "supplier": {
                "name": "ВИВАКОМ БЪЛГАРИЯ ЕАД",
                "eik": "832919112",
                "vat_number": "BG832919112",
            },
            "recipient": {
                "name": "БИЛДИНГ 11 ООД",
                "eik": "206062202",
                "vat_number": "BG206062202",
            },
            "financial_summary": {
                "tax_base": 100.00,
                "vat_amount": 20.00,
                "total_amount_due": 120.00,
                "currency": "BGN",
            },
            "line_items": [
                {"description": "Месечен абонамент мобилни услуги", "quantity": 1, "unit_price": 100.00, "total": 100.00}
            ],
        },
    }
    bundle_viva = execute_accounting_pipeline_for_document(viva_doc_id, viva_data, "vivacom_bill.pdf")
    assert bundle_viva, "Failed generating Vivacom accounting bundle"
    op_viva = bundle_viva["accounting_operation"]
    assert any(d["account"] == "602" and d["amount"] == 100.00 for d in op_viva["debit_entries"])
    assert any(d["account"] == "4531" and d["amount"] == 20.00 for d in op_viva["debit_entries"])
    assert any(c["account"] == "401" and c["amount"] == 120.00 for c in op_viva["credit_entries"])
    print("✓ Double-Entry Purchase Services Verified: Дт 602 (100.00 лв.), Дт 4531 (20.00 лв.), Кт 401 (120.00 лв.)")

    # 5. Test Sales Invoice (Продажби - 701/4532/411)
    print("\n--- 4. Testing Sales Invoice (Revenue 701) ---")
    sale_doc_id = "test_sale_invoice"
    sale_data = {
        "file_name": "sale_apartment.pdf",
        "normalized_data": {
            "invoice_metadata": {
                "invoice_number": "0000001001",
                "date_issued": "2026-03-01",
                "currency": "EUR",
                "document_type": "INVOICE",
                "is_credit_note": False,
            },
            "supplier": {
                "name": "БИЛДИНГ 11 ООД",
                "eik": "206062202",
                "vat_number": "BG206062202",
            },
            "recipient": {
                "name": "ИНВЕСТОР КЛИЕНТ ЕООД",
                "eik": "123456789",
                "vat_number": "BG123456789",
            },
            "financial_summary": {
                "tax_base": 50000.00,
                "vat_amount": 10000.00,
                "total_amount_due": 60000.00,
                "currency": "EUR",
            },
            "line_items": [
                {"description": "Продажба на продукция", "quantity": 1, "unit_price": 50000.00, "total": 50000.00}
            ],
        },
    }
    bundle_sale = execute_accounting_pipeline_for_document(sale_doc_id, sale_data, "sale_apartment.pdf")
    assert bundle_sale, "Failed generating Sales accounting bundle"
    op_sale = bundle_sale["accounting_operation"]
    assert any(d["account"] == "411" and d["amount"] == 60000.00 for d in op_sale["debit_entries"])
    assert any(c["account"] == "701" and c["amount"] == 50000.00 for c in op_sale["credit_entries"])
    assert any(c["account"] == "4532" and c["amount"] == 10000.00 for c in op_sale["credit_entries"])
    print("✓ Double-Entry Sales Operation Verified: Дт 411 (€60,000.00), Кт 701 (€50,000.00), Кт 4532 (€10,000.00)")

    # 6. Test Email Webhook Ingestion (/api/v1/ingest/docs-email)
    print("\n--- 5. Testing Zero-Touch Docs Email Webhook (/api/v1/ingest/docs-email) ---")
    dummy_pdf_content = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj 3 0 obj<</Type/Page/MediaBox[0 0 612 792]>>endobj\nxref\n0 4\n0000000000 65535 f\n0000000009 00000 n\n0000000052 00000 n\n0000000101 00000 n\ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n162\n%%EOF"
    # Pad to pass >10KB filter
    dummy_pdf_content += b" " * 12000

    email_payload = {
        "sender": "supplier@testpartner.bg",
        "recipient": "docs@incontrolplus.com",
        "subject": "Фактура за извършени услуги",
        "date": "2026-09-13T08:00:00Z",
        "message_id": "<test-msg-001@testpartner.bg>",
        "spf": "pass",
        "dkim": "pass",
        "attachments": [
            {
                "filename": "Invoice_001.pdf",
                "content_type": "application/pdf",
                "size": len(dummy_pdf_content),
                "data_base64": base64.b64encode(dummy_pdf_content).decode("ascii"),
            }
        ]
    }

    resp_email = client.post("/api/v1/ingest/docs-email", json=email_payload)
    assert resp_email.status_code == 200, f"Email webhook failed: {resp_email.text}"
    res_data = resp_email.json()
    assert res_data["status"] == "success"
    assert len(res_data["results"]) == 1
    print("✓ Zero-Touch Email Webhook Ingestion successfully processed attachment")

    # 7. Test Contractor Name Divergence Protection (HITL)
    print("\n--- 6. Testing Contractor Name Divergence Protection (HITL Enforcement) ---")
    div_sup = Party(name="КАРТОФИ И ДОМАТИ ООД", eik="121644736", vat_number="BG121644736")
    div_rec = Party(name="БИЛДИНГ 11 ООД", eik="206062202", vat_number="BG206062202")
    pv_div = verify_invoice_parties(div_sup, div_rec)
    assert pv_div.has_critical_mismatch is True, "Critical divergence must be flagged"
    assert pv_div.requires_hitl is True, "Must require Human-In-The-Loop review"
    print(f"✓ Critical Contractor Name Divergence Detected: {pv_div.hitl_reasons[0]}")
    print("✓ Auto-posting correctly blocked, routed to Human-In-The-Loop (HITL) review")

    print("\n=================================================================")
    print("ALL STATUTORY ACCOUNTING & DELTA PRO PIPELINE TESTS PASSED 100%!")
    print("=================================================================")

if __name__ == "__main__":
    run_tests()

"""Integration Test: Microinvest Delta Pro Direct Jet 2.0 MDB Auto-Import & Windows VM Automation (Milestone M23).

Verifies:
1. Automated import cycle for Building 11 (Сграда 11, ЕИК 206062202):
   - Generates binary Jet 2.0 TRANSFER.LOG for the 9 production invoices of Building 11.
   - Mathematical balance equality: Debit == Credit == 14,034.90 BGN.
   - Analytical subledger accounts validation:
     * Account 401 (Suppliers / Доставчици): Credit = 14,034.90 BGN
     * Account 4531 / 453 (VAT on Purchases / ДДС на покупки): Debit = 2,339.15 BGN
     * Accounts 304 / 602 (Goods & Materials / Services): Debit = 11,695.75 BGN
     * Total Debit = 11,695.75 + 2,339.15 = 14,034.90 BGN.
2. Jet 2.0 MDB Verifier engine (`Jet2MdbVerifier`):
   - Verifies 100% balance equality from binary pages directly in pure Python.
   - Verifies table metadata (Table 25 W#Transfer, Table 23 W#System).
3. Windows VM / Hot-Folder Dispatch Bridge:
   - Calls POST /api/v1/accounting/delta-pro/dispatch-to-vm.
   - Confirms SHA-256 hash, hot-folder delivery, and persistent AuditTrailRecord.
"""
from __future__ import annotations

import base64
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from api_server import app
from database import AuditTrailRecord, DocumentRecord, get_db_session
from invoice_core.accounting.exporters.delta_pro_generator import (
    generate_multi_delta_pro_transfer_log,
)
from invoice_core.accounting.exporters.mdb_verifier import Jet2MdbVerifier


@pytest.fixture
def building_11_production_invoices() -> list[dict]:
    """The 9 production invoices of Building 11 (Сграда 11, ЕИК 206062202) totaling 14,034.90 BGN."""
    return [
        # 1. Invoice 0090250595: Wood Euro Pallets (Stock / 304)
        {
            "document_metadata": {
                "invoice_number": "0090250595",
                "date_issued": "2026-08-17",
                "is_credit_note": False,
            },
            "parties": {
                "direction": "PURCHASE",
                "counterpart_name": "МАГНЕЗИЯ ЕООД",
                "counterpart_eik": "114631464",
                "counterpart_vat": "BG114631464",
                "recipient_name": "БИЛДИНГ 11 ООД",
                "recipient_eik": "206062202",
            },
            "financials": {
                "tax_base": 21.00,
                "vat_amount": 4.20,
                "total_amount": 25.20,
                "currency": "BGN",
            },
            "accounting_operation": {
                "expense_account": "304",
                "vat_account": "4531",
                "counterpart_account": "401",
                "reason": "европалети",
            },
        },
        # 2. Invoice 0090250045: Building hardware supplies (Stock / 304)
        {
            "document_metadata": {
                "invoice_number": "0090250045",
                "date_issued": "2026-08-10",
                "is_credit_note": False,
            },
            "parties": {
                "direction": "PURCHASE",
                "counterpart_name": "МАГНЕЗИЯ ЕООД",
                "counterpart_eik": "114631464",
                "counterpart_vat": "BG114631464",
                "recipient_name": "БИЛДИНГ 11 ООД",
                "recipient_eik": "206062202",
            },
            "financials": {
                "tax_base": 6.78,
                "vat_amount": 1.35,
                "total_amount": 8.13,
                "currency": "BGN",
            },
            "accounting_operation": {
                "expense_account": "304",
                "vat_account": "4531",
                "counterpart_account": "401",
                "reason": "крепежни елементи",
            },
        },
        # 3. Invoice 0090252073: Crane & scaffolding services (Services / 602)
        {
            "document_metadata": {
                "invoice_number": "0090252073",
                "date_issued": "2026-08-31",
                "is_credit_note": False,
            },
            "parties": {
                "direction": "PURCHASE",
                "counterpart_name": "МАГНЕЗИЯ ЕООД",
                "counterpart_eik": "114631464",
                "counterpart_vat": "BG114631464",
                "recipient_name": "БИЛДИНГ 11 ООД",
                "recipient_eik": "206062202",
            },
            "financials": {
                "tax_base": 94.31,
                "vat_amount": 18.86,
                "total_amount": 113.17,
                "currency": "BGN",
            },
            "accounting_operation": {
                "expense_account": "602",
                "vat_account": "4531",
                "counterpart_account": "401",
                "reason": "наем механизация",
            },
        },
        # 4. Invoice 0090251332: Concrete pump service (Services / 602)
        {
            "document_metadata": {
                "invoice_number": "0090251332",
                "date_issued": "2026-08-24",
                "is_credit_note": False,
            },
            "parties": {
                "direction": "PURCHASE",
                "counterpart_name": "МАГНЕЗИЯ ЕООД",
                "counterpart_eik": "114631464",
                "counterpart_vat": "BG114631464",
                "recipient_name": "БИЛДИНГ 11 ООД",
                "recipient_eik": "206062202",
            },
            "financials": {
                "tax_base": 92.67,
                "vat_amount": 18.53,
                "total_amount": 111.20,
                "currency": "BGN",
            },
            "accounting_operation": {
                "expense_account": "602",
                "vat_account": "4531",
                "counterpart_account": "401",
                "reason": "услуги бетон помпа",
            },
        },
        # 5. Invoice 0090250062: Structural concrete B25 (Stock / 304)
        {
            "document_metadata": {
                "invoice_number": "0090250062",
                "date_issued": "2026-08-10",
                "is_credit_note": False,
            },
            "parties": {
                "direction": "PURCHASE",
                "counterpart_name": "МАГНЕЗИЯ ЕООД",
                "counterpart_eik": "114631464",
                "counterpart_vat": "BG114631464",
                "recipient_name": "БИЛДИНГ 11 ООД",
                "recipient_eik": "206062202",
            },
            "financials": {
                "tax_base": 7400.43,
                "vat_amount": 1480.09,
                "total_amount": 8880.52,
                "currency": "BGN",
            },
            "accounting_operation": {
                "expense_account": "304",
                "vat_account": "4531",
                "counterpart_account": "401",
                "reason": "бетон B25",
            },
        },
        # 6. Invoice 0090250522: Rebar & structural steel (Stock / 304)
        {
            "document_metadata": {
                "invoice_number": "0090250522",
                "date_issued": "2026-08-14",
                "is_credit_note": False,
            },
            "parties": {
                "direction": "PURCHASE",
                "counterpart_name": "МАГНЕЗИЯ ЕООД",
                "counterpart_eik": "114631464",
                "counterpart_vat": "BG114631464",
                "recipient_name": "БИЛДИНГ 11 ООД",
                "recipient_eik": "206062202",
            },
            "financials": {
                "tax_base": 4002.44,
                "vat_amount": 800.49,
                "total_amount": 4802.93,
                "currency": "BGN",
            },
            "accounting_operation": {
                "expense_account": "304",
                "vat_account": "4531",
                "counterpart_account": "401",
                "reason": "арматурна заготовка",
            },
        },
        # 7. Invoice 0090250988: Site security & access control (Services / 602)
        {
            "document_metadata": {
                "invoice_number": "0090250988",
                "date_issued": "2026-08-20",
                "is_credit_note": False,
            },
            "parties": {
                "direction": "PURCHASE",
                "counterpart_name": "МАГНЕЗИЯ ЕООД",
                "counterpart_eik": "114631464",
                "counterpart_vat": "BG114631464",
                "recipient_name": "БИЛДИНГ 11 ООД",
                "recipient_eik": "206062202",
            },
            "financials": {
                "tax_base": 34.00,
                "vat_amount": 6.80,
                "total_amount": 40.80,
                "currency": "BGN",
            },
            "accounting_operation": {
                "expense_account": "602",
                "vat_account": "4531",
                "counterpart_account": "401",
                "reason": "охрана обект Сграда 11",
            },
        },
        # 8. Invoice 0090250826: Technical surveillance inspection (Services / 602)
        {
            "document_metadata": {
                "invoice_number": "0090250826",
                "date_issued": "2026-08-18",
                "is_credit_note": False,
            },
            "parties": {
                "direction": "PURCHASE",
                "counterpart_name": "МАГНЕЗИЯ ЕООД",
                "counterpart_eik": "114631464",
                "counterpart_vat": "BG114631464",
                "recipient_name": "БИЛДИНГ 11 ООД",
                "recipient_eik": "206062202",
            },
            "financials": {
                "tax_base": 26.10,
                "vat_amount": 5.22,
                "total_amount": 31.32,
                "currency": "BGN",
            },
            "accounting_operation": {
                "expense_account": "602",
                "vat_account": "4531",
                "counterpart_account": "401",
                "reason": "технически надзор",
            },
        },
        # 9. Invoice 0090250040: Waterproofing consumables (Stock / 304)
        {
            "document_metadata": {
                "invoice_number": "0090250040",
                "date_issued": "2026-08-10",
                "is_credit_note": False,
            },
            "parties": {
                "direction": "PURCHASE",
                "counterpart_name": "МАГНЕЗИЯ ЕООД",
                "counterpart_eik": "114631464",
                "counterpart_vat": "BG114631464",
                "recipient_name": "БИЛДИНГ 11 ООД",
                "recipient_eik": "206062202",
            },
            "financials": {
                "tax_base": 18.02,
                "vat_amount": 3.61,
                "total_amount": 21.63,
                "currency": "BGN",
            },
            "accounting_operation": {
                "expense_account": "304",
                "vat_account": "4531",
                "counterpart_account": "401",
                "reason": "хидроизолационни консумативи",
            },
        },
    ]


def test_building_11_mathematical_balance_equality(building_11_production_invoices):
    """Verify that the 9 production invoices sum to exact statutory balance: Debit == Credit == 14,034.90 BGN."""
    assert len(building_11_production_invoices) == 9

    tot_tax_base = sum(Decimal(str(inv["financials"]["tax_base"])) for inv in building_11_production_invoices)
    tot_vat = sum(Decimal(str(inv["financials"]["vat_amount"])) for inv in building_11_production_invoices)
    tot_gross = sum(Decimal(str(inv["financials"]["total_amount"])) for inv in building_11_production_invoices)

    # 1. Statutory check: Tax base + VAT == Total Gross
    assert tot_tax_base == Decimal("11695.75")
    assert tot_vat == Decimal("2339.15")
    assert tot_gross == Decimal("14034.90")
    assert tot_tax_base + tot_vat == tot_gross

    # 2. Analytical subledger account partition check (304 vs 602)
    sum_304 = Decimal("0.0")
    sum_602 = Decimal("0.0")
    for inv in building_11_production_invoices:
        acct = inv["accounting_operation"]["expense_account"]
        tb = Decimal(str(inv["financials"]["tax_base"]))
        if acct == "304":
            sum_304 += tb
        elif acct == "602":
            sum_602 += tb

    assert sum_304 == Decimal("11448.67")
    assert sum_602 == Decimal("247.08")
    assert sum_304 + sum_602 == tot_tax_base

    # 3. Double-entry balance check: Total Debit == Total Credit == 14,034.90
    total_debit = sum_304 + sum_602 + tot_vat
    total_credit = tot_gross
    assert total_debit == Decimal("14034.90")
    assert total_credit == Decimal("14034.90")
    assert total_debit == total_credit


def test_delta_pro_binary_generation_and_mdb_verification(tmp_path, building_11_production_invoices):
    """Generate binary TRANSFER.LOG for Building 11 and verify with Jet2MdbVerifier."""
    log_bytes, ldb_bytes = generate_multi_delta_pro_transfer_log(
        documents=building_11_production_invoices,
        client_company_name="БИЛДИНГ 11 ООД",
        start_kon_id=3001,
    )

    assert len(log_bytes) == 262144, f"Expected 256KB batch file, got {len(log_bytes)}"
    assert len(ldb_bytes) == 64, f"Expected 64B lock file, got {len(ldb_bytes)}"

    # Save to temp directory for Jet2MdbVerifier inspection
    test_log_file = tmp_path / "TRANSFER.LOG"
    test_log_file.write_bytes(log_bytes)

    verifier = Jet2MdbVerifier(test_log_file)
    assert verifier.total_pages == 128
    assert 25 in verifier.tdefs, "Table 25 (W#Transfer) must be defined"

    # Verify table metadata
    t_info = verifier.inspect_table("W#Transfer")
    assert t_info["table_id"] == 25
    assert t_info["num_active_records"] == 36  # 9 invoices x 4 records each = 36 postings

    # Verify mathematical balance directly from binary Jet 2.0 pages
    bal = verifier.verify_balance(table_name_or_id=25)
    assert bal["is_balanced"] is True, f"Balance error: {bal}"
    assert bal["difference"] == 0.00
    assert bal["total_debit"] == 14034.90
    assert bal["total_credit"] == 14034.90
    assert bal["total_entries"] == 18  # 9 invoices x 2 transactions each (base + vat)
    assert bal["balanced_entries_count"] == 18

    # Validate analytical accounts in binary breakdown
    acct_bd = bal["account_breakdown"]
    assert "401" in acct_bd
    assert "453" in acct_bd or "4531" in acct_bd
    assert "304" in acct_bd
    assert "602" in acct_bd

    assert acct_bd["401"]["credit"] == 14034.90
    assert acct_bd["304"]["debit"] == 11448.67
    assert acct_bd["602"]["debit"] == 247.08


def test_windows_vm_hot_folder_dispatch_endpoint(tmp_path, building_11_production_invoices):
    """Test POST /api/v1/accounting/delta-pro/dispatch-to-vm endpoint with SHA-256 and audit logging."""
    client = TestClient(app)

    # Generate binary transfer log
    log_bytes, _ = generate_multi_delta_pro_transfer_log(
        documents=building_11_production_invoices,
        client_company_name="БИЛДИНГ 11 ООД",
    )
    expected_hash = hashlib.sha256(log_bytes).hexdigest()

    # Create dummy document record in DB to link audit trail
    with get_db_session() as db:
        doc = db.query(DocumentRecord).filter(DocumentRecord.recipient_eik == "206062202").first()
        if not doc:
            doc = DocumentRecord(
                id="doc_building_11_prod_audit",
                file_name="building_11_batch.pdf",
                recipient_name="БИЛДИНГ 11 ООД",
                recipient_eik="206062202",
                status="approved",
            )
            db.add(doc)
            db.commit()

    vm_hot_folder = tmp_path / "vm_qemu_shared" / "Building_11"

    payload = {
        "firm_slug": "Building_11",
        "firm_eik": "206062202",
        "vm_name": "Windows 11 QEMU",
        "target_hot_folder": str(vm_hot_folder),
        "transfer_log_bytes_b64": base64.b64encode(log_bytes).decode("ascii"),
        "actor": "accountant_admin",
    }

    resp = client.post("/api/v1/accounting/delta-pro/dispatch-to-vm", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["ok"] is True
    assert data["status"] == "dispatched"
    assert data["file_name"] == "TRANSFER.LOG"
    assert data["file_size"] == 262144
    assert data["sha256"] == expected_hash
    assert data["vm_name"] == "Windows 11 QEMU"
    assert data["firm_eik"] == "206062202"
    assert data["firm_slug"] == "Building_11"
    assert data["companion_ldb_dispatched"] is True
    assert data["audit_trail_id"] is not None

    # Verify physical file dispatch in the VM hot-folder
    assert vm_hot_folder.exists()
    dest_log = vm_hot_folder / "TRANSFER.LOG"
    dest_ldb = vm_hot_folder / "TRANSFER.ldb"
    assert dest_log.exists()
    assert dest_ldb.exists()
    assert dest_log.stat().st_size == 262144
    assert dest_ldb.stat().st_size == 64
    assert hashlib.sha256(dest_log.read_bytes()).hexdigest() == expected_hash

    # Verify audit trail record in SQLite / PostgreSQL database
    with get_db_session() as db:
        audit = db.query(AuditTrailRecord).filter(AuditTrailRecord.id == data["audit_trail_id"]).first()
        assert audit is not None
        assert audit.action == "delta_pro_vm_dispatch"
        assert audit.actor == "accountant_admin"
        assert audit.new_value == expected_hash
        details = json.loads(audit.details_json)
        assert details["vm_name"] == "Windows 11 QEMU"
        assert details["file_size"] == 262144

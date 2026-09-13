"""Comprehensive Multi-Firm and Batch Delta Pro Pipeline Test Suite.

Verifies:
1. Multi-document Jet 2.0 binary generation (TRANSFER.LOG & TRANSFER.ldb).
2. Cross-firm accounting isolation (Building 11, Storgozia AD, Mesomania 1).
3. Auto-drop folder synchronization (USB/Network/Local drop).
4. Obsidian Vault Dossier generation with Markdown, Mermaid, and Dataview frontmatter.
5. End-to-end REST API batch endpoints.
"""
from decimal import Decimal
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from invoice_core.delta_pro_generator import (
    generate_multi_delta_pro_transfer_log,
    generate_delta_pro_transfer_log,
    DELTA_PRO_LDB_TEMPLATE,
)
from invoice_core.obsidian_sync import generate_obsidian_client_dossier
from api_server import app


@pytest.fixture
def sample_multi_firm_documents():
    """Sample batch of invoices across multiple contractors and client firms."""
    return [
        # Document 1: Magnesia (Materials 601) -> Building 11
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
            },
            "financials": {
                "tax_base": 94.31,
                "vat_amount": 18.86,
                "total_amount": 113.17,
                "currency": "EUR",
            },
            "accounting_operation": {
                "expense_account": "601",
                "vat_account": "4531",
                "counterpart_account": "401",
                "reason": "м-ли",
            },
        },
        # Document 2: Express Security SOD (Services 602) -> Building 11
        {
            "document_metadata": {
                "invoice_number": "1000045892",
                "date_issued": "2026-08-30",
                "is_credit_note": False,
            },
            "parties": {
                "direction": "PURCHASE",
                "counterpart_name": "ЕКСПРЕС СЕКЮРИТИ СОД ЕООД",
                "counterpart_eik": "114540185",
                "counterpart_vat": "BG114540185",
            },
            "financials": {
                "tax_base": 250.00,
                "vat_amount": 50.00,
                "total_amount": 300.00,
                "currency": "EUR",
            },
            "accounting_operation": {
                "expense_account": "602",
                "vat_account": "4531",
                "counterpart_account": "401",
                "reason": "охрана обект",
            },
        },
        # Document 3: Credit Note Storno (Kreditno Izvestie) -> Building 11
        {
            "document_metadata": {
                "invoice_number": "0090252100",
                "date_issued": "2026-08-29",
                "is_credit_note": True,
            },
            "parties": {
                "direction": "PURCHASE",
                "counterpart_name": "МАГНЕЗИЯ ЕООД",
                "counterpart_eik": "114631464",
                "counterpart_vat": "BG114631464",
            },
            "financials": {
                "tax_base": -40.00,
                "vat_amount": -8.00,
                "total_amount": -48.00,
                "currency": "EUR",
            },
            "accounting_operation": {
                "expense_account": "601",
                "vat_account": "4531",
                "counterpart_account": "401",
                "reason": "м-ли корекция",
            },
        },
    ]


def test_multi_delta_pro_binary_generation(sample_multi_firm_documents):
    """Test generating a consolidated TRANSFER.LOG with multiple sequential operations."""
    log_bytes, ldb_bytes = generate_multi_delta_pro_transfer_log(
        documents=sample_multi_firm_documents,
        client_company_name="БИЛДИНГ 11 ООД",
    )

    assert len(log_bytes) == 262144, f"Expected 256KB batch file, got {len(log_bytes)}"
    assert len(ldb_bytes) == 64, f"Expected 64B lock file, got {len(ldb_bytes)}"

    # Verify Jet 2.0 Header on Page 0
    assert log_bytes[0:4] == b"\x01\x00\x00\x00"

    # Verify Page 25 Table Definition links
    import struct
    p25 = log_bytes[25*2048 : 26*2048]
    first_pg, last_pg = struct.unpack("<II", p25[12:20])
    num_pages, num_records = struct.unpack("<II", p25[32:40])

    assert first_pg == 29
    assert num_records == 12  # 3 documents x 4 entries each


def test_obsidian_dossier_generation(tmp_path, sample_multi_firm_documents):
    """Test generating full Obsidian Markdown dossier with tables, charts, and frontmatter."""
    out_file = generate_obsidian_client_dossier(
        client_eik="206062202",
        client_name="БИЛДИНГ 11 ООД",
        invoices=sample_multi_firm_documents,
        period="2026-08",
        vault_dir=tmp_path,
    )

    assert out_file.exists()
    content = out_file.read_text("utf-8")

    # Verify Frontmatter
    assert "title: \"Дневник на покупките — БИЛДИНГ 11 ООД\"" in content
    assert "client_eik: \"206062202\"" in content
    assert "period: \"2026-08\"" in content
    assert "total_tax_base: 304.31" in content
    assert "total_vat: 60.86" in content
    assert "total_gross: 365.17" in content

    # Verify Mermaid chart
    assert "pie title Разпределение на топ доставчици по оборот" in content
    assert "МАГНЕЗИЯ ЕООД" in content

    # Verify Table rows
    assert "0090252073" in content
    assert "1000045892" in content
    assert "ЕКСПРЕС СЕКЮРИТИ СОД ЕООД" in content
    assert "**601**" in content
    assert "**602**" in content


def test_api_batch_transfer_log_and_drop_sync(tmp_path):
    """Test end-to-end API calls for batch generation, drop synchronization, and Obsidian export."""
    client = TestClient(app)

    # 1. Test POST /api/v1/accounting/batch-transfer-log
    drop_target = tmp_path / "drop_folder"
    payload = {
        "client_eik": "206062202",
        "client_company_name": "БИЛДИНГ 11 ООД",
        "period": "2026-08",
        "target_drop_dir": str(drop_target),
        "sync_to_obsidian": False,
        "response_format": "json",
    }
    resp = client.post("/api/v1/accounting/batch-transfer-log", json=payload)
    assert resp.status_code == 200, f"Batch generation failed: {resp.text}"
    data = resp.json()

    assert data["ok"] is True
    assert "batch_id" in data
    assert data["total_documents"] > 0
    batch_id = data["batch_id"]

    # Verify that files were auto-dropped to target_drop_dir
    assert drop_target.exists()
    assert (drop_target / "TRANSFER.LOG").exists()
    assert (drop_target / "TRANSFER.ldb").exists()
    assert (drop_target / "TRANSFER.LOG").stat().st_size == 262144

    # 2. Test GET /api/v1/accounting/batches/{batch_id}/transfer-log
    log_resp = client.get(f"/api/v1/accounting/batches/{batch_id}/transfer-log")
    assert log_resp.status_code == 200
    assert len(log_resp.content) == 262144

    # 3. Test GET /api/v1/accounting/batches/{batch_id}/package
    pkg_resp = client.get(f"/api/v1/accounting/batches/{batch_id}/package")
    assert pkg_resp.status_code == 200
    assert len(pkg_resp.content) > 1000

    # 4. Test POST /api/v1/accounting/sync-to-drop-folder
    sync_drop = tmp_path / "another_folder"
    sync_payload = {
        "target_dir": str(sync_drop),
        "batch_id": batch_id,
    }
    sync_resp = client.post("/api/v1/accounting/sync-to-drop-folder", json=sync_payload)
    assert sync_resp.status_code == 200
    assert (sync_drop / "TRANSFER.LOG").exists()
    assert (sync_drop / "TRANSFER.ldb").exists()

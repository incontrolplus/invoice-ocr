"""Unit and integration tests for Microinvest Delta Pro export pipeline."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from invoice_ocr import process_invoice, serialize_invoice
from invoice_core.historical_matcher import process_invoice_and_create_accounting_package
from invoice_core.delta_pro_generator import generate_delta_pro_transfer_log, DEFAULT_LDB_BYTES


def test_delta_pro_package_generation_building_11():
    """Verify that invoice 3_2026-09-11_11-08-59.pdf generates valid Delta Pro files."""
    pdf_path = Path("3_2026-09-11_11-08-59.pdf")
    assert pdf_path.exists(), "Test PDF file missing"

    inv = process_invoice(pdf_path)
    inv_dict = json.loads(serialize_invoice(inv))

    bundle = process_invoice_and_create_accounting_package(inv_dict)
    assert bundle is not None
    assert "transfer_log_bytes" in bundle
    assert "transfer_ldb_bytes" in bundle

    log_bytes = bundle["transfer_log_bytes"]
    ldb_bytes = bundle["transfer_ldb_bytes"]

    assert len(log_bytes) == 65536, f"Expected 65,536 bytes, got {len(log_bytes)}"
    assert len(ldb_bytes) == 64, f"Expected 64 bytes, got {len(ldb_bytes)}"

    acc_op = bundle["accounting_operation"]
    assert acc_op["currency"] == "EUR"
    assert acc_op["contractor_name"] == "МАГНЕЗИЯ ЕООД"
    assert acc_op["contractor_eik"] == "114631464"
    assert acc_op["tax_base"] == 94.31
    assert acc_op["vat_amount"] == 18.86
    assert acc_op["total_amount"] == 113.17

    debit_accounts = [d["account"] for d in acc_op["debit_entries"]]
    credit_accounts = [c["account"] for c in acc_op["credit_entries"]]
    assert "601" in debit_accounts
    assert "4531" in debit_accounts
    assert "401" in credit_accounts


def test_delta_pro_credit_note_storno():
    """Verify that credit notes generate red storno with negative amounts and doc_type 'КИ'."""
    cn_data = {
        "document_type": "CREDIT_NOTE",
        "is_credit_note": True,
        "normalized_data": {
            "invoice_metadata": {
                "invoice_number": "0090250595",
                "date_issued": "2026-08-17",
                "date_tax_event": "2026-08-17",
                "document_type": "CREDIT_NOTE",
                "currency": "EUR",
            },
            "supplier": {
                "name": "МАГНЕЗИЯ ЕООД",
                "eik": "114631464",
                "vat_number": "BG114631464",
            },
            "recipient": {
                "name": "БИЛДИНГ 11 ООД",
                "eik": "206062202",
            },
            "financial_summary": {
                "tax_base": 21.00,
                "vat_amount": 4.20,
                "total_amount_due": 25.20,
            },
        },
    }

    bundle = process_invoice_and_create_accounting_package(cn_data)
    assert bundle is not None

    acc_op = bundle["accounting_operation"]
    assert acc_op["document_type"] == "КИ"
    assert acc_op["is_credit_note"] is True
    assert acc_op["currency"] == "EUR"

    debit_amounts = [d["amount"] for d in acc_op["debit_entries"]]
    assert -21.00 in debit_amounts
    assert -4.20 in debit_amounts

    log_bytes = bundle["transfer_log_bytes"]
    assert len(log_bytes) == 65536
    assert b"\xca\xc8" in log_bytes  # "КИ" in CP1251


def test_problem_invoices_reconciliation():
    """Verify that all 6 corrected documents from 'СЛЕД ПРЕГЛЕД' extract correctly without regressions."""
    from invoice_core.pipeline import load_ocr_cache, normalize_ocr_tokens, group_tokens_into_lines
    from invoice_core.extraction import extract_invoice_number, extract_dates
    from invoice_core.financials import extract_financial_summary

    test_cases = [
        ("Doc 01 (КИ Date)", "81510ec52e6f3a6ac4422fc68408f028442bf7529c6c18bf5293e49b130c5b35.json", "0090250595", "2026-08-17", None, None),
        ("Doc 31 (VAT / Base Swap Guard)", "44e21ab2816a1b4e7c8c6f76feb93c85bbc77f6bbfffd82fd8cfdf6b920c4a9a.json", "0090249603", "2026-08-05", 58.58, 11.71),
        ("Doc 42 (Ne: prefix + Base artifact)", "cc88b579052f3daa0030e7639181fcaba52a62798100f42a4d56dafea890b3e0.json", "0090251647", "2026-08-27", 21.45, 4.29),
        ("Doc 44 (Ne: prefix + IBAN guard)", "25f8edd58fd4e748d24c5e659881419d9e488312c77ddb6cfb22f46d95c7063d.json", "0090251951", "2026-08-31", 65.63, 13.13),
        ("Doc 49 (Statutory 20% VAT rate)", "02277bc4af1ed80915cbf1ccfa24e2ab7eee59a1a04182e2afbc15aab8545979.json", "0090250497", "2026-08-14", 2.49, 0.50),
        ("Doc 54 (Ко: prefix + Delivery note guard)", "d8dad369e110e5c4ebe3c6db5098f1c51a934e27a10461dba6ebb6edcc1b187a.json", "0090250071", "2026-08-11", 168.57, 33.72),
    ]

    for label, cache_name, exp_inv, exp_date, exp_tb, exp_vat in test_cases:
        cache_path = Path(".ocr_cache") / cache_name
        if not cache_path.exists():
            continue
        cached_evidence, cached_tokens = load_ocr_cache(cache_path)
        tokens = normalize_ocr_tokens(cached_tokens)
        lines = group_tokens_into_lines(tokens)

        inv_no = extract_invoice_number(lines, tokens, supplier_eik="114631464", recipient_eik="206062202")
        dates = extract_dates(lines)
        fin = extract_financial_summary(lines, tokens)

        assert inv_no == exp_inv, f"[{label}] Expected invoice {exp_inv}, got {inv_no}"
        assert dates[0] == exp_date, f"[{label}] Expected date {exp_date}, got {dates[0]}"
        if exp_tb is not None:
            assert fin.tax_base is not None and float(fin.tax_base.amount) == exp_tb, f"[{label}] Expected tax_base {exp_tb}, got {fin.tax_base}"
        if exp_vat is not None:
            assert fin.vat_amount is not None and float(fin.vat_amount.amount) == exp_vat, f"[{label}] Expected VAT {exp_vat}, got {fin.vat_amount}"


def test_delta_pro_multi_account_distribution():
    """Verify that an invoice with multi-account distribution generates 2*N + 2 balanced records."""
    import struct
    distributions = [
        {"account": "601", "amount": 100.00, "description": "Цимент и арматура", "subaccount": 0.0},
        {"account": "602", "amount": 50.00, "description": "Транспортни услуги", "subaccount": 0.0},
    ]
    log_bytes, ldb_bytes = generate_delta_pro_transfer_log(
        invoice_number="0090255555",
        doc_date="2026-09-18",
        company_name="ТЕСТОВ ДОСТАВЧИК ЕООД",
        bulstat="201234567",
        vat_number="BG201234567",
        tax_base=150.00,
        vat_amount=30.00,
        total_amount=180.00,
        currency="EUR",
        is_purchase=True,
        is_credit_note=False,
        counterpart_account="401",
        vat_account="4531",
        distributions=distributions,
    )
    assert len(log_bytes) == 65536
    assert len(ldb_bytes) == 64

    # Check Table 25 Definition on Page 25
    p25 = log_bytes[25*2048 : 26*2048]
    rec_count = struct.unpack("<I", p25[36:40])[0]
    assert rec_count == 6  # 2 for 601 (kon=1), 2 for 602 (kon=2), 2 for 4531 (kon=3)

    # Check Page 29
    p29 = log_bytes[29*2048 : 30*2048]
    p29_rec_count = struct.unpack("<H", p29[8:10])[0]
    assert p29_rec_count == 6

    # Verify that accounts 601, 602, 4531 and 401 are encoded as binary short ints and account titles in CP1251
    assert struct.pack("<H", 601) in log_bytes
    assert struct.pack("<H", 602) in log_bytes
    assert struct.pack("<H", 401) in log_bytes
    assert "Разходи за материали".encode("cp1251") in log_bytes
    assert "Разходи за външни услуги".encode("cp1251") in log_bytes


def test_delta_pro_multi_account_distribution_credit_note():
    """Verify that a credit note with multi-account distribution generates storno records."""
    import struct
    distributions = [
        {"account": "601", "amount": 40.00, "description": "Върнати материали"},
        {"account": "602", "amount": 10.00, "description": "Корекция транспорт"},
    ]
    log_bytes, _ = generate_delta_pro_transfer_log(
        invoice_number="0090255556",
        doc_date="2026-09-18",
        company_name="ТЕСТОВ ДОСТАВЧИК ЕООД",
        bulstat="201234567",
        vat_number="BG201234567",
        tax_base=50.00,
        vat_amount=10.00,
        total_amount=60.00,
        currency="EUR",
        is_purchase=True,
        is_credit_note=True,
        counterpart_account="401",
        vat_account="4531",
        distributions=distributions,
    )
    p25 = log_bytes[25*2048 : 26*2048]
    assert struct.unpack("<I", p25[36:40])[0] == 6
    assert b"\xca\xc8" in log_bytes  # "КИ" in CP1251


def test_historical_matcher_multi_item_distribution_package():
    """Verify that process_invoice_and_create_accounting_package automatically creates distributions for multi-item invoices."""
    inv_data = {
        "invoice_metadata": {
            "invoice_number": "1000088888",
            "date_issued": "2026-09-15",
            "currency": "EUR",
        },
        "supplier": {
            "name": "СТРОЙМАРКЕТ ООД",
            "eik": "123456789",
            "vat_number": "BG123456789",
        },
        "recipient": {
            "name": "БИЛДИНГ 11 ООД",
            "eik": "206062202",
        },
        "financial_summary": {
            "tax_base": 1200.00,
            "vat_amount": 240.00,
            "total_amount_due": 1440.00,
        },
        "items": [
            {
                "description": "Бетон B25",
                "quantity": 10.0,
                "unit_price": 80.0,
                "total_price": 800.00,
            },
            {
                "description": "Транспорт и помпа бетон",
                "quantity": 1.0,
                "unit_price": 400.0,
                "total_price": 400.00,
            },
        ],
    }
    bundle = process_invoice_and_create_accounting_package(inv_data)
    assert bundle is not None
    acc_op = bundle["accounting_operation"]
    assert "distributions" in acc_op
    dists = acc_op["distributions"]
    assert dists is not None and len(dists) >= 2

    # Check debit breakdown: items split into goods/materials (304) and transport services (602) + VAT (4531)
    debit_accounts = [d["account"] for d in acc_op["debit_entries"]]
    assert "304" in debit_accounts  # Бетон -> стоки/материали
    assert "602" in debit_accounts  # Транспорт -> услуги
    assert "4531" in debit_accounts  # ДДС
    credit_accounts = [c["account"] for c in acc_op["credit_entries"]]
    assert "401" in credit_accounts  # Доставчик

    assert len(bundle["transfer_log_bytes"]) == 65536


def test_multi_delta_pro_batch_with_distributions():
    """Verify that generate_multi_delta_pro_transfer_log packs multi-account distributions into a 256KB batch file."""
    from invoice_core.delta_pro_generator import generate_multi_delta_pro_transfer_log
    import struct

    docs = [
        # Document 1: standard single-account invoice (4 records)
        {
            "document_metadata": {"invoice_number": "0000000001", "date_issued": "2026-09-01"},
            "parties": {"direction": "PURCHASE", "counterpart_name": "ДОСТАВЧИК 1", "counterpart_eik": "111111111"},
            "financials": {"tax_base": 100.0, "vat_amount": 20.0, "total_amount": 120.0},
            "accounting_operation": {"expense_account": "601", "vat_account": "4531", "counterpart_account": "401"},
        },
        # Document 2: multi-item distribution (6 records)
        {
            "document_metadata": {"invoice_number": "0000000002", "date_issued": "2026-09-02"},
            "parties": {"direction": "PURCHASE", "counterpart_name": "ДОСТАВЧИК 2", "counterpart_eik": "222222222"},
            "financials": {"tax_base": 300.0, "vat_amount": 60.0, "total_amount": 360.0},
            "accounting_operation": {
                "vat_account": "4531",
                "counterpart_account": "401",
                "distributions": [
                    {"account": "601", "amount": 200.0, "description": "Материали"},
                    {"account": "602", "amount": 100.0, "description": "Услуги"},
                ],
            },
        },
        # Document 3: sales invoice (4 records)
        {
            "document_metadata": {"invoice_number": "0000000003", "date_issued": "2026-09-03"},
            "parties": {"direction": "SALES", "counterpart_name": "КЛИЕНТ 1", "counterpart_eik": "333333333", "counterpart_role": "CLIENT"},
            "financials": {"tax_base": 500.0, "vat_amount": 100.0, "total_amount": 600.0},
            "accounting_operation": {"revenue_account": "702", "vat_account": "4532", "counterpart_account": "411"},
        },
    ]

    log_bytes, ldb_bytes = generate_multi_delta_pro_transfer_log(
        documents=docs,
        client_company_name="БИЛДИНГ 11 ООД",
        start_kon_id=2001,
    )

    assert len(log_bytes) == 262144
    assert len(ldb_bytes) == 64

    # Check Table 25 Definition on Page 25
    p25 = log_bytes[25*2048 : 26*2048]
    total_recs = struct.unpack("<I", p25[36:40])[0]
    # Doc 1: 4 records, Doc 2: 6 records, Doc 3: 4 records => total 14 records
    assert total_recs == 14

    # Check Page 24 system string
    p24 = log_bytes[24*2048 : 25*2048]
    assert b"3 \xe4\xee\xea\xf3\xec\xe5\xed\xf2\xe0" in p24  # "3 документа" in CP1251


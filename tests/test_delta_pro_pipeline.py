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

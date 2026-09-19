#!/usr/bin/env python3
"""Process all 9 Building 11 invoices (6 Sales + 3 Purchases) through full OCR Pipeline,
generate double-entry statutory accounting operations, build byte-perfect Jet 2.0 TRANSFER.LOG,
and stage for Microinvest Delta Pro import.
"""
import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import sys

# Ensure root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from invoice_core.accounting_engine import AccountingEngine, DELTA_PRO_DEBIT, DELTA_PRO_CREDIT
from invoice_core.delta_pro_generator import (
    generate_multi_delta_pro_transfer_log,
    PAGE_SIZE,
)
from invoice_core.pipeline import process_invoice

SUPPLIER_DIR = Path("/Volumes/NO NAME/Building_11/ФАКТУРИ_БИЛДИНГ_11_ДОСТАВЧИК")
RECIPIENT_DIR = Path("/Volumes/NO NAME/Building_11/ФАКТУРИ_БИЛДИНГ_11_ПОЛУЧАТЕЛ")

DEST_DIRS = [
    Path("/Volumes/NO NAME/Building_11"),
    Path("/Volumes/NO NAME"),
    PROJECT_ROOT / "Building_11",
    Path("/Users/diokarabaz/.gemini/antigravity-cli/brain/0a7f4473-555c-4082-b34f-d1f3912dfbd5/Building_11"),
    Path("/Users/diokarabaz/.gemini/antigravity-cli/brain/0a7f4473-555c-4082-b34f-d1f3912dfbd5"),
]


def main():
    print("=" * 80)
    print("STARTING FULL OCR PIPELINE FOR BUILDING 11 INVOICES (SALES & PURCHASES)")
    print("=" * 80)

    engine = AccountingEngine()

    # Collect files
    supplier_files = sorted(SUPPLIER_DIR.glob("*.pdf"))
    recipient_files = sorted(RECIPIENT_DIR.glob("*.pdf"))

    print(f"Found {len(supplier_files)} Sales invoices (Building 11 as Supplier)")
    print(f"Found {len(recipient_files)} Purchase invoices (Building 11 as Recipient)")

    all_docs_for_transfer = []
    summary_report = []

    # 1. Process Sales Invoices (Building 11 is Supplier: 206062202)
    # Order: We want chronological order or sequential invoice numbers 5000000082 .. 5000000087
    print("\n--- PROCESSING SALES INVOICES (ПРОДАЖБИ) ---")
    processed_sales = []
    for pdf_path in supplier_files:
        print(f"\nProcessing {pdf_path.name}...")
        inv = process_invoice(str(pdf_path))
        op = engine.create_operation(inv, operation_id=len(all_docs_for_transfer) + 1)

        doc_num = inv.invoice_metadata.invoice_number
        is_ann = getattr(inv.invoice_metadata, "is_annulled", False)
        partner_name = inv.recipient.name if inv.recipient else ""
        partner_eik = inv.recipient.eik if inv.recipient else ""
        partner_vat = inv.recipient.vat_number if (inv.recipient and inv.recipient.vat_number) else f"BG{partner_eik}"
        
        tax_base = float(op.tax_base)
        vat_amt = float(op.vat_amount)
        tot_amt = float(op.total_amount)

        processed_sales.append({
            "pdf_file": pdf_path.name,
            "invoice": inv,
            "operation": op,
            "doc_num": doc_num,
            "is_annulled": is_ann,
            "direction": "SALES",
            "partner_name": partner_name,
            "partner_eik": partner_eik,
            "partner_vat": partner_vat,
            "tax_base": tax_base,
            "vat_amount": vat_amt,
            "total_amount": tot_amt,
        })

    # Sort sales by invoice number (5000000082 .. 5000000087)
    processed_sales.sort(key=lambda x: str(x["doc_num"]))

    for item in processed_sales:
        inv = item["invoice"]
        op = item["operation"]
        doc_num = item["doc_num"]
        is_ann = item["is_annulled"]
        
        print(f"  ✓ Doc #{doc_num} | Date: {inv.invoice_metadata.date_issued} | Client: {item['partner_name']} ({item['partner_eik']})")
        print(f"    Base: {item['tax_base']:,.2f} | VAT: {item['vat_amount']:,.2f} | Total: {item['total_amount']:,.2f} EUR {'(Анулирана)' if is_ann else ''}")
        print(f"    Balanced: {op.is_balanced}")

        doc_dict = {
            "document_metadata": {
                "invoice_number": doc_num,
                "date_issued": inv.invoice_metadata.date_issued,
                "document_type": "ФАК",
                "is_credit_note": False,
                "is_annulled": is_ann,
            },
            "parties": {
                "counterpart_name": item["partner_name"],
                "counterpart_eik": item["partner_eik"],
                "counterpart_vat": item["partner_vat"],
                "direction": "SALES",
            },
            "financials": {
                "tax_base": item["tax_base"],
                "vat_amount": item["vat_amount"],
                "total_amount": item["total_amount"],
            },
            "accounting_operation": {
                "revenue_account": "702",
                "vat_account": "4532",
                "counterpart_account": "411",
                "reason": "Анулирана фактура" if is_ann else "продажби",
            }
        }
        all_docs_for_transfer.append(doc_dict)
        summary_report.append({
            "direction": "ПРОДАЖБИ (Дневник Продажби)",
            "file": item["pdf_file"],
            "invoice_number": doc_num,
            "date": inv.invoice_metadata.date_issued,
            "partner": item["partner_name"],
            "eik": item["partner_eik"],
            "tax_base": item["tax_base"],
            "vat": item["vat_amount"],
            "total": item["total_amount"],
            "debit_account": "411 (Клиенти)",
            "credit_account": "702 (Приходи от стоки) / 4532 (ДДС продажби)",
            "is_annulled": is_ann,
        })

    # 2. Process Purchase Invoices (Building 11 is Recipient: 206062202)
    print("\n--- PROCESSING PURCHASE INVOICES (ПОКУПКИ) ---")
    processed_purchases = []
    for pdf_path in recipient_files:
        print(f"\nProcessing {pdf_path.name}...")
        inv = process_invoice(str(pdf_path))
        op = engine.create_operation(inv, operation_id=len(all_docs_for_transfer) + 1)

        doc_num = inv.invoice_metadata.invoice_number
        is_cn = bool(inv.invoice_metadata.is_credit_note or inv.invoice_metadata.document_type == "CREDIT_NOTE")
        partner_name = inv.supplier.name if inv.supplier else ""
        partner_eik = inv.supplier.eik if inv.supplier else ""
        partner_vat = inv.supplier.vat_number if (inv.supplier and inv.supplier.vat_number) else f"BG{partner_eik}"
        
        tax_base = float(op.tax_base)
        vat_amt = float(op.vat_amount)
        tot_amt = float(op.total_amount)

        processed_purchases.append({
            "pdf_file": pdf_path.name,
            "invoice": inv,
            "operation": op,
            "doc_num": doc_num,
            "is_credit_note": is_cn,
            "direction": "PURCHASE",
            "partner_name": partner_name,
            "partner_eik": partner_eik,
            "partner_vat": partner_vat,
            "tax_base": tax_base,
            "vat_amount": vat_amt,
            "total_amount": tot_amt,
        })

    # Sort purchases by date or doc number
    processed_purchases.sort(key=lambda x: str(x["doc_num"]))

    for item in processed_purchases:
        inv = item["invoice"]
        op = item["operation"]
        doc_num = item["doc_num"]
        
        # Determine expense account: Stroyrent (services/rent 602), Atila Agro (security/services 602), Alpha Mix (materials 601)
        expense_acc = "602"
        reason = "услуги"
        if "алфа микс" in item["partner_name"].lower() or "бетон" in str(inv.line_items).lower():
            expense_acc = "601"
            reason = "материали"
        elif "стройрент" in item["partner_name"].lower():
            expense_acc = "602"
            reason = "наем/услуги"
        elif "атила" in item["partner_name"].lower():
            expense_acc = "602"
            reason = "охрана/услуги"

        print(f"  ✓ Doc #{doc_num} | Date: {inv.invoice_metadata.date_issued} | Supplier: {item['partner_name']} ({item['partner_eik']})")
        print(f"    Base: {item['tax_base']:,.2f} | VAT: {item['vat_amount']:,.2f} | Total: {item['total_amount']:,.2f} EUR")
        print(f"    Expense Acc: {expense_acc} | Reason: {reason} | Balanced: {op.is_balanced}")

        doc_dict = {
            "document_metadata": {
                "invoice_number": doc_num,
                "date_issued": inv.invoice_metadata.date_issued,
                "document_type": "ФАК",
                "is_credit_note": False,
                "is_annulled": False,
            },
            "parties": {
                "counterpart_name": item["partner_name"],
                "counterpart_eik": item["partner_eik"],
                "counterpart_vat": item["partner_vat"],
                "direction": "PURCHASE",
            },
            "financials": {
                "tax_base": item["tax_base"],
                "vat_amount": item["vat_amount"],
                "total_amount": item["total_amount"],
            },
            "accounting_operation": {
                "expense_account": expense_acc,
                "vat_account": "4531",
                "counterpart_account": "401",
                "reason": reason,
            }
        }
        all_docs_for_transfer.append(doc_dict)
        summary_report.append({
            "direction": "ПОКУПКИ (Дневник Покупки)",
            "file": item["pdf_file"],
            "invoice_number": doc_num,
            "date": inv.invoice_metadata.date_issued,
            "partner": item["partner_name"],
            "eik": item["partner_eik"],
            "tax_base": item["tax_base"],
            "vat": item["vat_amount"],
            "total": item["total_amount"],
            "debit_account": f"{expense_acc} ({reason}) / 4531 (ДДС покупки)",
            "credit_account": "401 (Доставчици)",
            "is_annulled": False,
        })

    # 3. Generate Consolidated TRANSFER.LOG
    print(f"\n--- GENERATING CONSOLIDATED TRANSFER.LOG FOR {len(all_docs_for_transfer)} DOCUMENTS ---")
    log_bytes, ldb_bytes = generate_multi_delta_pro_transfer_log(
        documents=all_docs_for_transfer,
        client_company_name="БИЛДИНГ 11 ООД",
        start_kon_id=1001,
    )

    print(f"Generated TRANSFER.LOG: {len(log_bytes)} bytes (128 pages)")
    print(f"Generated TRANSFER.ldb: {len(ldb_bytes)} bytes")
    assert len(log_bytes) == 262144, f"Invalid LOG size: {len(log_bytes)}"
    assert len(ldb_bytes) == 64, f"Invalid LDB size: {len(ldb_bytes)}"

    # 4. Deploy to target directories
    for d in DEST_DIRS:
        if not d.parent.exists():
            continue
        d.mkdir(parents=True, exist_ok=True)
        t_log = d / "TRANSFER.LOG"
        t_ldb = d / "TRANSFER.ldb"
        t_log.write_bytes(log_bytes)
        t_ldb.write_bytes(ldb_bytes)
        print(f"  ✓ Deployed to {t_log} ({t_log.stat().st_size} B)")

    # 5. Save JSON summary manifest
    manifest_path = PROJECT_ROOT / "Building_11" / "BUILDING_11_BATCH_SUMMARY.json"
    manifest_path.write_text(json.dumps(summary_report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved batch summary manifest to {manifest_path}")

    print("\n" + "=" * 80)
    print(f"SUMMARY OF ALL {len(summary_report)} DOCUMENTS READY FOR IMPORT:")
    print("=" * 80)
    for idx, r in enumerate(summary_report, 1):
        status = "[Анулиран]" if r["is_annulled"] else f"{r['total']:,.2f} EUR"
        print(f"{idx:02d}. {r['direction'][:8]} | № {r['invoice_number']:>10} | {r['date']} | {r['partner']:<25} | {status}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import concurrent.futures
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ["PROJECT_ROOT"] = str(PROJECT_ROOT)

from invoice_ocr import process_invoice, serialize_invoice
from invoice_core.historical_matcher import HistoricalAccountingMatcher
from contractor_verification import verify_contractor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("batch_kingston")

INPUT_DIR = Path("/Volumes/KINGSTON/_02_БИЛДИНГ_11_ООД")
CACHE_OUT = PROJECT_ROOT / "scratch_kingston_analysis.json"

def process_single_file(pdf_path: Path) -> dict[str, Any]:
    start_t = time.time()
    file_name = pdf_path.name
    
    try:
        inv = process_invoice(str(pdf_path))
        data_str = serialize_invoice(inv)
        data = json.loads(data_str)
    except Exception as exc:
        logger.error("Failed processing %s: %s", file_name, exc, exc_info=True)
        return {
            "file_name": file_name,
            "error": str(exc),
            "status": "FAILED",
            "elapsed_sec": round(time.time() - start_t, 2),
        }

    norm = data.get("normalized_data", {})
    meta = norm.get("invoice_metadata", {})
    sup = norm.get("supplier", {})
    rec = norm.get("recipient", {})
    fin = norm.get("financial_summary", {})
    line_items = norm.get("line_items", [])

    raw_tokens = []
    for page in data.get("raw_ocr_evidence", {}).get("pages", []):
        for tok in page.get("tokens", []):
            raw_tokens.append(tok.get("text", ""))
    full_text = " ".join(raw_tokens)

    # Handwritten account notes detection (e.g. 601 / 401, 602 / 401, 304, etc.)
    handwritten_account = None
    hw_matches = re.findall(r"(60[123456789]|304|609)\s*[-/]?\s*/?\s*401", full_text)
    if hw_matches:
        handwritten_account = hw_matches[0]

    # Document type
    is_credit_note = meta.get("is_credit_note", False)
    if "КРЕДИТНО" in full_text.upper() or "ИЗВЕСТИЕ" in full_text.upper() or " 03" in full_text:
        is_credit_note = True
        doc_type_label = "КИ"
    elif "ДЕБИТНО" in full_text.upper():
        doc_type_label = "ДИ"
    else:
        doc_type_label = "КИ" if is_credit_note else "ФАК"

    client_eik = "206062202"
    client_name = "БИЛДИНГ 11 ООД"

    supp_eik = re.sub(r"[^0-9]", "", str(sup.get("eik") or ""))
    rec_eik = re.sub(r"[^0-9]", "", str(rec.get("eik") or ""))
    supp_name = sup.get("name") or ""
    rec_name = rec.get("name") or ""

    eik_matches = re.findall(r"\b\d{9,13}\b", full_text)
    inv_num_clean = re.sub(r"[^0-9]", "", str(meta.get("invoice_number") or ""))
    found_eiks = set(e for e in eik_matches if e != inv_num_clean and len(e) in (9, 10, 13))

    # Disambiguate parties:
    is_purchase = True
    counterpart_eik = None
    counterpart_name = None

    matcher = HistoricalAccountingMatcher.get_instance()

    # If supplier or recipient mentions МАГНЕЗИЯ, resolve directly to МАГНЕЗИЯ ЕООД (114631464)
    if "МАГНЕЗИЯ" in str(supp_name).upper() or "МАГНЕЗИЯ" in str(rec_name).upper() or "114631464" in full_text:
        counterpart_eik = "114631464"
        counterpart_name = "МАГНЕЗИЯ ЕООД"
        is_purchase = True
    elif supp_eik and supp_eik != client_eik and supp_eik in matcher.purchase_contractors:
        counterpart_eik = supp_eik
        counterpart_name = matcher.purchase_contractors[supp_eik].get("canonical_name", "")
        is_purchase = True
    elif rec_eik and rec_eik != client_eik and rec_eik in matcher.purchase_contractors:
        counterpart_eik = rec_eik
        counterpart_name = matcher.purchase_contractors[rec_eik].get("canonical_name", "")
        is_purchase = True
    else:
        known_suppliers_in_text = [e for e in found_eiks if e in matcher.purchase_contractors]
        if known_suppliers_in_text:
            counterpart_eik = known_suppliers_in_text[0]
            counterpart_name = matcher.purchase_contractors[counterpart_eik].get("canonical_name", "")
            is_purchase = True
        elif supp_eik == client_eik and rec_eik and rec_eik != client_eik:
            counterpart_eik = rec_eik
            counterpart_name = rec_name
            is_purchase = True
        elif rec_eik == client_eik and supp_eik and supp_eik != client_eik:
            counterpart_eik = supp_eik
            counterpart_name = supp_name
            is_purchase = True
        else:
            other_eiks = [e for e in found_eiks if e != client_eik]
            counterpart_eik = other_eiks[0] if other_eiks else (supp_eik if supp_eik != client_eik else rec_eik)

    reg_contractor = verify_contractor(counterpart_eik) if counterpart_eik else None
    if reg_contractor and reg_contractor.company_name:
        counterpart_canonical_name = reg_contractor.company_name
    elif counterpart_eik and counterpart_eik in matcher.purchase_contractors:
        counterpart_canonical_name = matcher.purchase_contractors[counterpart_eik].get("canonical_name", "Не е зададен")
    else:
        counterpart_canonical_name = counterpart_name or "Не е зададен"

    tax_base_obj = fin.get("tax_base") or {}
    vat_obj = fin.get("vat_amount") or {}
    total_obj = fin.get("total_amount_due") or {}
    currency = fin.get("currency") or "EUR"

    tax_base = Decimal(str(tax_base_obj.get("amount") or 0.0))
    vat_amount = Decimal(str(vat_obj.get("amount") or 0.0))
    total_amount = Decimal(str(total_obj.get("amount") or 0.0))

    # Match against historical knowledge base
    match_report = matcher.match_document(
        recipient_eik=client_eik if is_purchase else counterpart_eik,
        recipient_name=client_name if is_purchase else counterpart_canonical_name,
        supplier_eik=counterpart_eik if is_purchase else client_eik,
        supplier_name=counterpart_canonical_name if is_purchase else client_name,
        line_items=line_items,
        total_amount=float(total_amount),
        tax_base=float(tax_base),
        vat_amount=float(vat_amount),
        is_credit_note=is_credit_note,
        currency=currency,
        doc_type_raw=doc_type_label,
    )

    expense_account = handwritten_account or (match_report.recommended_expense_account if match_report else "601")
    vat_account = "4531" if is_purchase else "4532"
    counterpart_account = "401" if is_purchase else "411"
    reason = match_report.recommended_reason if match_report else "м-ли"

    calc_total = tax_base + vat_amount
    diff = abs(calc_total - total_amount)
    math_valid = diff <= Decimal("0.02")

    hitl_reasons = []
    requires_hitl = False

    if total_amount == Decimal("0.0"):
        requires_hitl = True
        hitl_reasons.append("Липсва или нулева тотална сума от OCR")

    if not math_valid and total_amount != Decimal("0.0"):
        requires_hitl = True
        hitl_reasons.append(f"Математическо несъответствие: данъчна основа ({tax_base}) + ДДС ({vat_amount}) != тотал ({total_amount})")

    if not counterpart_eik:
        requires_hitl = True
        hitl_reasons.append("Неразпознат ЕИК на контрагента от документа")

    if not meta.get("invoice_number"):
        requires_hitl = True
        hitl_reasons.append("Липсва номер на фактура/документ")

    if not meta.get("date_issued"):
        requires_hitl = True
        hitl_reasons.append("Липсва дата на издаване на документа")

    return {
        "file_name": file_name,
        "pdf_path": str(pdf_path),
        "status": "NEEDS_HITL" if requires_hitl else "READY",
        "requires_hitl": requires_hitl,
        "hitl_reasons": hitl_reasons,
        "document_metadata": {
            "invoice_number": meta.get("invoice_number"),
            "date_issued": meta.get("date_issued"),
            "date_tax_event": meta.get("date_tax_event"),
            "document_type": doc_type_label,
            "is_credit_note": is_credit_note,
            "currency": currency,
            "ocr_confidence": meta.get("ocr_confidence_score"),
        },
        "parties": {
            "direction": "PURCHASE" if is_purchase else "SALE",
            "client_company": client_name,
            "client_eik": client_eik,
            "counterpart_eik": counterpart_eik,
            "counterpart_name": counterpart_canonical_name,
            "counterpart_vat": f"BG{counterpart_eik}" if counterpart_eik else None,
            "supplier_raw": sup,
            "recipient_raw": rec,
        },
        "financials": {
            "tax_base": float(tax_base),
            "vat_amount": float(vat_amount),
            "total_amount": float(total_amount),
            "currency": currency,
            "math_valid": math_valid,
        },
        "line_items_count": len(line_items),
        "line_items_sample": [item.get("description") for item in line_items[:5]],
        "accounting_operation": {
            "expense_account": expense_account,
            "vat_account": vat_account,
            "counterpart_account": counterpart_account,
            "reason": reason,
            "handwritten_detected": handwritten_account,
            "direction": "PURCHASE" if is_purchase else "SALE",
            "is_credit_note": is_credit_note,
        },
        "historical_match": match_report.to_dict() if match_report else None,
        "elapsed_sec": round(time.time() - start_t, 2),
    }

def main():
    files = [p for p in INPUT_DIR.glob("*.pdf") if not p.name.startswith("._")]
    files = sorted(files, key=lambda p: int(p.name.split("_")[0]) if p.name.split("_")[0].isdigit() else 999)
    print(f"Starting batch analysis on {len(files)} files...")

    results = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(process_single_file, f): f for f in files}
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            results.append(res)
            print(f"Finished {res["file_name"]} -> {res.get("status")} ({res.get("elapsed_sec")}s)")

    results.sort(key=lambda r: int(r["file_name"].split("_")[0]) if r["file_name"].split("_")[0].isdigit() else 999)

    with open(CACHE_OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    ready_count = sum(1 for r in results if r.get("status") == "READY")
    hitl_count = sum(1 for r in results if r.get("status") == "NEEDS_HITL")
    print(f"\nBatch analysis complete: {len(results)} files, {ready_count} READY, {hitl_count} NEEDS_HITL")

if __name__ == "__main__":
    main()

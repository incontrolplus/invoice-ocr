"""Microinvest ERP Export Engine for Bulgarian Invoices.

Supports:
1. Microinvest Sklad Pro (Warehouse Pro) XML import for purchases (<Invoice> / <Items>).
2. Microinvest Delta Pro TransferData XML (<TransferData xmlns="urn:Transfer">)
   with balanced double-entry accounting postings (Debit 601/602/304, Debit 4531, Credit 401),
   analytical subaccounts, and multi-item line distribution.
3. Microinvest Delta Pro CSV import format for double-entry postings (контировки)
   with CP1251 / UTF-8 encoding and analytical subledgers.
"""
from __future__ import annotations

import csv
from decimal import Decimal, ROUND_HALF_UP
import io
import logging
from pathlib import Path
import re
from typing import Any, Optional, Sequence
import xml.etree.ElementTree as ET
import zipfile

from .account_mapping import DEFAULT_MAPPING_ENGINE, AccountMappingEngine
from .models import Invoice, LineItem, MoneyAmount, Party

logger = logging.getLogger("invoice_ocr.microinvest")

TRANSFER_NS = "urn:Transfer"
MONEY = Decimal("0.01")
QTY = Decimal("0.001")

DOC_TYPE_INVOICE = 1
DOC_TYPE_DEBIT_NOTE = 2
DOC_TYPE_CREDIT_NOTE = 3

PURCHASE_VAT_TERMS = {Decimal("20"): 1, Decimal("9"): 11, Decimal("0"): 12}
SALE_VAT_TERMS = {Decimal("20"): 7, Decimal("9"): 11, Decimal("0"): 12}


def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _to_dec(val: Any) -> Decimal:
    if val is None or val == "":
        return Decimal("0.00")
    if isinstance(val, MoneyAmount):
        return val.amount if val.amount is not None else Decimal("0.00")
    if isinstance(val, Decimal):
        return val
    if hasattr(val, "amount"):
        amt = getattr(val, "amount")
        return _to_dec(amt) if amt is not None else Decimal("0.00")
    if isinstance(val, dict):
        if "amount" in val:
            return _to_dec(val.get("amount"))
        if "value" in val:
            return _to_dec(val.get("value"))
        return Decimal("0.00")
    try:
        clean = str(val).replace(" ", "").replace(",", ".")
        if not clean or clean.lower() == "none" or clean.lower() == "null":
            return Decimal("0.00")
        return Decimal(clean)
    except Exception:
        return Decimal("0.00")



# ============================================================================
# 1. Microinvest Sklad Pro (Warehouse Pro) XML Generator
# ============================================================================

def generate_microinvest_sklad_xml(invoice: Invoice | dict[str, Any], encoding: str = "utf-8") -> str:
    """Generate Microinvest Sklad Pro (Warehouse Pro) Purchase XML.

    Formatted according to Microinvest import specifications:
    - Document type: Purchase (Покупка / Приход)
    - 2 decimal places for amounts, 4 decimal places for quantities.
    - Standard Bulgarian VAT rates (0%, 9%, 20%).
    """
    if isinstance(invoice, dict):
        inv_no = _clean_str(invoice.get("invoice_number") or invoice.get("invoiceNumber"))
        inv_dt = _clean_str(invoice.get("date_issued") or invoice.get("invoiceDate"))
        cur = _clean_str(invoice.get("currency") or "BGN")

        sup = invoice.get("supplier") or {}
        sup_name = _clean_str(sup.get("name") or invoice.get("vendorName"))
        sup_eik = _clean_str(sup.get("eik") or invoice.get("vendorEik"))
        sup_vat = _clean_str(sup.get("vat_number") or invoice.get("vendorVatNumber") or f"BG{sup_eik}")
        sup_addr = _clean_str(sup.get("address") or invoice.get("vendorAddress"))

        rec = invoice.get("recipient") or {}
        rec_name = _clean_str(rec.get("name") or invoice.get("customerName"))
        rec_eik = _clean_str(rec.get("eik") or invoice.get("customerEik"))

        fin = invoice.get("financial_summary") or {}
        tax_base = _to_dec(fin.get("tax_base") or invoice.get("subtotal"))
        vat_amount = _to_dec(fin.get("vat_amount") or invoice.get("taxAmount"))
        total_amount = _to_dec(fin.get("total_amount_due") or invoice.get("totalAmount"))

        raw_items = invoice.get("line_items") or invoice.get("items") or []
        items_data = []
        for it in raw_items:
            qty = _to_dec(it.get("quantity") or 1)
            tot = _to_dec(it.get("total_price_net") or it.get("totalPrice"))
            u_pr = _to_dec(it.get("unit_price_net") or it.get("unitPrice"))
            if u_pr == Decimal("0.00") and tot > 0 and qty > 0:
                u_pr = (tot / qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            items_data.append({
                "code": _clean_str(it.get("sku") or it.get("code") or f"ITEM-{it.get('index', 1)}"),
                "name": _clean_str(it.get("description") or it.get("name") or "Стока/Услуга"),
                "quantity": qty,
                "unit": _clean_str(it.get("unit") or "бр."),
                "unit_price": u_pr,
                "vat_rate": _to_dec(it.get("vat_rate_pct") or it.get("vatRate") or Decimal("20.00")),
                "discount": _to_dec(it.get("discount_pct") or it.get("discount")),
                "total": tot,
            })
    else:
        inv_no = _clean_str(invoice.invoice_metadata.invoice_number)
        inv_dt = _clean_str(invoice.invoice_metadata.date_issued)
        cur = _clean_str(
            getattr(invoice.invoice_metadata, "currency", None)
            or (invoice.financial_summary.total_amount_due.currency if invoice.financial_summary.total_amount_due else None)
            or "BGN"
        )

        sup_name = _clean_str(invoice.supplier.name)
        sup_eik = _clean_str(invoice.supplier.eik)
        sup_vat = _clean_str(invoice.supplier.vat_number or f"BG{sup_eik}")
        sup_addr = _clean_str(invoice.supplier.address)

        rec_name = _clean_str(invoice.recipient.name)
        rec_eik = _clean_str(invoice.recipient.eik)

        tax_base = _to_dec(invoice.financial_summary.tax_base)
        vat_amount = _to_dec(invoice.financial_summary.vat_amount)
        total_amount = _to_dec(invoice.financial_summary.total_amount_due)

        items_data = []
        for it in invoice.line_items:
            qty = _to_dec(it.quantity or 1)
            tot = _to_dec(it.total_price_net)
            u_pr = _to_dec(it.unit_price_net)
            if u_pr == Decimal("0.00") and tot > 0 and qty > 0:
                u_pr = (tot / qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            items_data.append({
                "code": _clean_str(it.sku or f"ITEM-{it.index or 1}"),
                "name": _clean_str(it.description or "Стока/Услуга"),
                "quantity": qty,
                "unit": _clean_str(it.unit or "бр."),
                "unit_price": u_pr,
                "vat_rate": _to_dec(it.vat_rate_pct or Decimal("20.00")),
                "discount": _to_dec(getattr(it, "discount_pct", Decimal("0.00"))),
                "total": tot,
            })

    # If no line items, synthesize a general line item
    if not items_data and total_amount > 0:
        items_data.append({
            "code": "ITEM-1",
            "name": "Доставка / Услуга по фактура",
            "quantity": Decimal("1.0000"),
            "unit": "бр.",
            "unit_price": tax_base if tax_base > 0 else total_amount,
            "vat_rate": Decimal("20.00") if vat_amount > 0 else Decimal("0.00"),
            "discount": Decimal("0.00"),
            "total": tax_base if tax_base > 0 else total_amount,
        })

    root = ET.Element("Invoice")
    ET.SubElement(root, "DocumentNumber").text = inv_no
    ET.SubElement(root, "DocumentDate").text = inv_dt
    ET.SubElement(root, "DocumentType").text = "Purchase"

    vendor_el = ET.SubElement(root, "Vendor")
    ET.SubElement(vendor_el, "Name").text = sup_name
    ET.SubElement(vendor_el, "TaxNumber").text = sup_eik
    ET.SubElement(vendor_el, "VATNumber").text = sup_vat
    if sup_addr:
        ET.SubElement(vendor_el, "Address").text = sup_addr

    recip_el = ET.SubElement(root, "Recipient")
    ET.SubElement(recip_el, "Name").text = rec_name
    ET.SubElement(recip_el, "TaxNumber").text = rec_eik

    ET.SubElement(root, "Currency").text = cur

    items_el = ET.SubElement(root, "Items")
    for it in items_data:
        item_el = ET.SubElement(items_el, "Item")
        ET.SubElement(item_el, "Code").text = it["code"]
        ET.SubElement(item_el, "Name").text = it["name"]
        ET.SubElement(item_el, "Quantity").text = f"{it['quantity']:.4f}"
        ET.SubElement(item_el, "Unit").text = it["unit"]
        ET.SubElement(item_el, "UnitPrice").text = f"{it['unit_price']:.2f}"
        ET.SubElement(item_el, "VATRate").text = f"{it['vat_rate']:.2f}"
        if it["discount"] > 0:
            ET.SubElement(item_el, "Discount").text = f"{it['discount']:.2f}"
        ET.SubElement(item_el, "Total").text = f"{it['total']:.2f}"

    totals_el = ET.SubElement(root, "Totals")
    ET.SubElement(totals_el, "Subtotal").text = f"{tax_base:.2f}"
    ET.SubElement(totals_el, "VATAmount").text = f"{vat_amount:.2f}"
    ET.SubElement(totals_el, "TotalAmount").text = f"{total_amount:.2f}"

    xml_str = ET.tostring(root, encoding=encoding, xml_declaration=True).decode(encoding)
    return xml_str


# ============================================================================
# 2. Microinvest Delta Pro (<TransferData xmlns="urn:Transfer">) Generator
# ============================================================================

def generate_microinvest_delta_xml(
    invoices: list[Invoice | dict[str, Any]] | Invoice | dict[str, Any],
    default_expense_account: str = "602",
    default_goods_account: str = "304",
    default_vat_account: str = "453/1",
    default_supplier_account: str = "401",
    operation_prefix: str = "OCR",
    encoding: str = "utf-8",
    mapping_engine: AccountMappingEngine | None = None,
) -> str:
    """Generate official Microinvest Delta Pro TransferData XML.

    Emits <TransferData xmlns="urn:Transfer"> with balanced double-entry accounting
    records (<Accounting>) balancing:
      Σ Debit (Expense 601/602/304 + VAT 453/1) == Σ Credit (Supplier 401).

    Credit notes automatically reverse debit/credit directions.
    Supports multi-item accounting distributions across multiple accounts.
    """
    if not isinstance(invoices, list):
        inv_list = [invoices]
    else:
        inv_list = invoices

    engine = mapping_engine or DEFAULT_MAPPING_ENGINE

    ET.register_namespace("", TRANSFER_NS)
    root = ET.Element(f"{{{TRANSFER_NS}}}TransferData", {"Version": "1.0"})
    accountings_el = ET.SubElement(root, f"{{{TRANSFER_NS}}}Accountings")

    for idx, inv in enumerate(inv_list, start=1):
        if isinstance(inv, dict):
            inv_no = _clean_str(inv.get("invoice_number") or inv.get("invoiceNumber"))
            inv_dt = _clean_str(inv.get("date_issued") or inv.get("invoiceDate"))
            is_credit = bool(inv.get("is_credit_note") or inv.get("document_type") == "CREDIT_NOTE")

            sup = inv.get("supplier") or {}
            sup_name = _clean_str(sup.get("name") or inv.get("vendorName"))
            sup_eik = _clean_str(sup.get("eik") or inv.get("vendorEik"))
            sup_vat = _clean_str(sup.get("vat_number") or inv.get("vendorVatNumber") or f"BG{sup_eik}")

            fin = inv.get("financial_summary") or {}
            tax_base = _to_dec(fin.get("tax_base") or inv.get("subtotal"))
            vat_amount = _to_dec(fin.get("vat_amount") or inv.get("taxAmount"))
            total_amount = _to_dec(fin.get("total_amount_due") or inv.get("totalAmount"))
        else:
            inv_no = _clean_str(invoice_number := inv.invoice_metadata.invoice_number)
            inv_dt = _clean_str(inv.invoice_metadata.date_issued)
            is_credit = bool(inv.invoice_metadata.is_credit_note or inv.invoice_metadata.document_type == "CREDIT_NOTE")

            sup_name = _clean_str(inv.supplier.name)
            sup_eik = _clean_str(inv.supplier.eik)
            sup_vat = _clean_str(inv.supplier.vat_number or f"BG{sup_eik}")

            tax_base = _to_dec(inv.financial_summary.tax_base)
            vat_amount = _to_dec(inv.financial_summary.vat_amount)
            total_amount = _to_dec(inv.financial_summary.total_amount_due)

        # Ensure balanced totals
        if total_amount == Decimal("0.00") and (tax_base > 0 or vat_amount > 0):
            total_amount = tax_base + vat_amount
        elif tax_base == Decimal("0.00") and total_amount > 0:
            tax_base = (total_amount - vat_amount).quantize(MONEY, ROUND_HALF_UP)

        doc_type_code = DOC_TYPE_CREDIT_NOTE if is_credit else DOC_TYPE_INVOICE
        term_label = "Кредитно известие" if is_credit else "Покупка"

        # Direction signs:
        # Standard Purchase: Debit Expense, Debit VAT, Credit Supplier
        # Credit Note: Reverses directions (Credit Expense, Credit VAT, Debit Supplier)
        exp_dir = "Credit" if is_credit else "Debit"
        vat_dir = "Credit" if is_credit else "Debit"
        sup_dir = "Debit" if is_credit else "Credit"

        # TransferData elements
        acc_attrs = {
            "Number": f"{operation_prefix}-{idx:04d}",
            "AccountingDate": inv_dt,
            "Term": term_label,
            "VatTerm": "1" if vat_amount > 0 else "12",
        }
        acc_el = ET.SubElement(accountings_el, f"{{{TRANSFER_NS}}}Accounting", acc_attrs)

        doc_attrs = {
            "DocumentType": str(doc_type_code),
            "Number": inv_no,
            "Date": inv_dt,
        }
        ET.SubElement(acc_el, f"{{{TRANSFER_NS}}}Document", doc_attrs)

        comp_attrs = {
            "Name": sup_name,
            "Bulstat": sup_eik,
            "VatNumber": sup_vat,
        }
        ET.SubElement(acc_el, f"{{{TRANSFER_NS}}}Company", comp_attrs)

        details_el = ET.SubElement(acc_el, f"{{{TRANSFER_NS}}}AccountingDetails")

        # Multi-line or single-line distributions
        raw_items = []
        if isinstance(inv, dict):
            raw_items = inv.get("line_items") or inv.get("items") or []
        elif hasattr(inv, "line_items"):
            raw_items = getattr(inv, "line_items", []) or []

        if raw_items and default_expense_account in ("602", "304"):
            distributions = engine.split_invoice_by_accounts(inv, prefer_subaccounts=False)
        else:
            distributions = [{
                "account": default_expense_account,
                "amount": tax_base,
                "subledger": sup_eik,
                "vat_rate": Decimal("20.00") if vat_amount > 0 else Decimal("0.00"),
            }]

        # 1. Expense Debit rows (one per distributed account)
        for dist in distributions:
            acc_code = dist["account"]
            amt = dist["amount"]
            sub_acc = dist.get("subledger") or sup_eik
            vat_term_code = "1" if dist.get("vat_rate", Decimal("20")) == Decimal("20") and vat_amount > 0 else ("11" if dist.get("vat_rate") == Decimal("9") else "12")

            detail_dict = {
                "Account": acc_code,
                "Amount": f"{abs(amt):.2f}",
                "Direction": exp_dir,
                "VatTerm": vat_term_code,
            }
            if sub_acc:
                detail_dict["SubAccount"] = sub_acc
                detail_dict["AnalyticCode"] = sub_acc
            ET.SubElement(details_el, f"{{{TRANSFER_NS}}}AccountingDetail", detail_dict)

        # 2. VAT Debit row (if VAT exists)
        if vat_amount > 0:
            vat_detail = {
                "Account": default_vat_account,
                "Amount": f"{abs(vat_amount):.2f}",
                "Direction": vat_dir,
                "VatTerm": "1",
            }
            ET.SubElement(details_el, f"{{{TRANSFER_NS}}}AccountingDetail", vat_detail)

        # 3. Supplier Credit row (Gross amount balancing the record to maintain double-entry balance)
        exp_sum = sum(dist["amount"] for dist in distributions)
        balanced_total = (exp_sum + (vat_amount if vat_amount > 0 else Decimal("0.00"))).quantize(MONEY, ROUND_HALF_UP)

        sup_detail = {
            "Account": default_supplier_account,
            "Amount": f"{abs(balanced_total):.2f}",
            "Direction": sup_dir,
            "VatTerm": "0",
        }
        if sup_eik:
            sup_detail["SubAccount"] = sup_eik
            sup_detail["AnalyticCode"] = sup_eik
        ET.SubElement(details_el, f"{{{TRANSFER_NS}}}AccountingDetail", sup_detail)

    xml_str = ET.tostring(root, encoding=encoding, xml_declaration=True).decode(encoding)
    return xml_str


# ============================================================================
# 3. Microinvest Delta Pro CSV Postings Generator
# ============================================================================

def generate_microinvest_delta_csv(
    invoices: Sequence[Invoice | dict[str, Any]] | Invoice | dict[str, Any],
    default_expense_account: str = "602",
    default_goods_account: str = "304",
    default_vat_account: str = "453/1",
    default_supplier_account: str = "401",
    delimiter: str = ";",
    encoding: str = "windows-1251",
    mapping_engine: AccountMappingEngine | None = None,
) -> bytes | str:
    """Generate Microinvest Delta Pro CSV import format for double-entry postings (контировки).

    Format: Semicolon-delimited 2-sided postings with analytical subledgers.
    Columns:
      Дата;ВидДокумент;НомерДокумент;СметкаДт;АналитичностДт;СметкаКт;АналитичностКт;Сума;Валута;Основание
    """
    if not isinstance(invoices, (list, tuple)):
        inv_list = [invoices]
    else:
        inv_list = list(invoices)

    engine = mapping_engine or DEFAULT_MAPPING_ENGINE
    output = io.StringIO()
    writer = csv.writer(output, delimiter=delimiter, lineterminator="\r\n")

    # Header
    writer.writerow([
        "Дата",
        "ВидДокумент",
        "НомерДокумент",
        "СметкаДт",
        "АналитичностДт",
        "СметкаКт",
        "АналитичностКт",
        "Сума",
        "Валута",
        "Основание",
    ])

    for inv in inv_list:
        if isinstance(inv, dict):
            meta = inv.get("invoice_metadata") or inv
            inv_no = _clean_str(meta.get("invoice_number") or meta.get("invoiceNumber"))
            inv_dt = _clean_str(meta.get("date_issued") or meta.get("invoiceDate"))
            is_credit = bool(meta.get("is_credit_note") or meta.get("document_type") == "CREDIT_NOTE")

            sup = inv.get("supplier") or {}
            sup_name = _clean_str(sup.get("name") or inv.get("vendorName") or "ДОСТАВЧИК")
            sup_eik = _clean_str(sup.get("eik") or inv.get("vendorEik") or "")

            fin = inv.get("financial_summary") or {}
            tax_base = _to_dec(fin.get("tax_base") or inv.get("subtotal"))
            vat_amount = _to_dec(fin.get("vat_amount") or inv.get("taxAmount"))
            total_amount = _to_dec(fin.get("total_amount_due") or inv.get("totalAmount"))
            currency = _clean_str(inv.get("currency") or "BGN")
        else:
            inv_no = _clean_str(inv.invoice_metadata.invoice_number)
            inv_dt = _clean_str(inv.invoice_metadata.date_issued)
            is_credit = bool(inv.invoice_metadata.is_credit_note or inv.invoice_metadata.document_type == "CREDIT_NOTE")

            sup_name = _clean_str(inv.supplier.name)
            sup_eik = _clean_str(inv.supplier.eik)

            tax_base = _to_dec(inv.financial_summary.tax_base)
            vat_amount = _to_dec(inv.financial_summary.vat_amount)
            total_amount = _to_dec(inv.financial_summary.total_amount_due)
            currency = _clean_str(getattr(inv.invoice_metadata, "currency", "BGN") or "BGN")

        doc_type_name = "Кредитно известие" if is_credit else "Фактура"
        sign = Decimal("-1") if is_credit else Decimal("1")

        distributions = engine.split_invoice_by_accounts(inv, prefer_subaccounts=False)

        # 1. Expense Debit Postings (Д-т 601/602/304 / К-т 401)
        for dist in distributions:
            amt = sign * abs(dist["amount"])
            writer.writerow([
                inv_dt,
                doc_type_name,
                inv_no,
                dist["account"],
                dist.get("subledger") or sup_eik,
                default_supplier_account,
                sup_eik,
                f"{amt:.2f}",
                currency,
                f"{doc_type_name} № {inv_no} от {sup_name}",
            ])

        # 2. VAT Debit Posting (Д-т 453/1 / К-т 401)
        if vat_amount > 0:
            vat_amt = sign * abs(vat_amount)
            clean_vat_acc = default_vat_account.replace("/", "")
            writer.writerow([
                inv_dt,
                doc_type_name,
                inv_no,
                clean_vat_acc,
                "",
                default_supplier_account,
                sup_eik,
                f"{vat_amt:.2f}",
                currency,
                f"ДДС 20% по {doc_type_name} № {inv_no}",
            ])

    content_str = output.getvalue()
    if encoding:
        return content_str.encode(encoding, errors="replace")
    return content_str


# ============================================================================
# 4. Microinvest Complete Export Package
# ============================================================================

def export_microinvest_package(
    invoices: list[Invoice] | Sequence[Any],
    output_dir: Path | str,
    create_zip: bool = True,
) -> dict[str, str]:
    """Generate Sklad Pro XML, Delta Pro XML, Delta Pro CSV, and ZIP package.

    Returns dict of {filename_key: absolute_path}.
    """
    out_p = Path(output_dir)
    out_p.mkdir(parents=True, exist_ok=True)
    generated = {}

    inv_list = list(invoices)

    # 1. Delta Pro TransferData XML
    delta_xml = generate_microinvest_delta_xml(inv_list)
    delta_file = out_p / "Microinvest_Delta_TransferData.xml"
    delta_file.write_text(delta_xml, encoding="utf-8")
    generated["delta_xml"] = str(delta_file)

    # 2. Delta Pro Postings CSV (CP1251)
    delta_csv_bytes = generate_microinvest_delta_csv(inv_list, encoding="windows-1251")
    assert isinstance(delta_csv_bytes, bytes)
    delta_csv_file = out_p / "Microinvest_Delta_Postings.csv"
    delta_csv_file.write_bytes(delta_csv_bytes)
    generated["delta_csv"] = str(delta_csv_file)

    # 3. Sklad Pro XML (per document)
    for i, inv in enumerate(inv_list, start=1):
        sklad_xml = generate_microinvest_sklad_xml(inv)
        if hasattr(inv, "invoice_metadata"):
            doc_num = inv.invoice_metadata.invoice_number or f"doc_{i}"
        elif isinstance(inv, dict):
            doc_num = inv.get("invoice_number") or inv.get("invoiceNumber") or f"doc_{i}"
        else:
            doc_num = f"doc_{i}"
        sklad_file = out_p / f"Microinvest_Sklad_{doc_num}.xml"
        sklad_file.write_text(sklad_xml, encoding="utf-8")
        generated[f"sklad_xml_{doc_num}"] = str(sklad_file)

    # 4. ZIP Package
    if create_zip:
        zip_path = out_p / "Microinvest_Export_Package.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(delta_file, arcname="Microinvest_Delta_TransferData.xml")
            zf.write(delta_csv_file, arcname="Microinvest_Delta_Postings.csv")
            for k, f_path in generated.items():
                if k.startswith("sklad_xml_"):
                    p = Path(f_path)
                    zf.write(p, arcname=p.name)
        generated["microinvest_zip"] = str(zip_path)

    return generated

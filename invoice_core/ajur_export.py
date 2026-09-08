"""Ajur ERP (Ажур 7 / Ажур-L) Export Engine for Bulgarian Invoices.

Generates structured import files for Ajur accounting software:
- Semicolon-delimited CSV / TXT import format with complete accounting entries (контировки).
- Analytical subledgers for supplier (401 with supplier EIK) and expense accounts.
- CP1251 Cyrillic encoding with CRLF endings.
"""
from __future__ import annotations

import csv
from decimal import Decimal, ROUND_HALF_UP
import io
import logging
from pathlib import Path
import re
from typing import Any, Sequence
import zipfile

from .account_mapping import DEFAULT_MAPPING_ENGINE, AccountMappingEngine
from .models import Invoice, LineItem, MoneyAmount, Party

logger = logging.getLogger("invoice_ocr.ajur")

MONEY = Decimal("0.01")


def _clean_str(val: Any, max_len: int | None = None) -> str:
    if val is None:
        return ""
    if isinstance(val, dict):
        val = val.get("name") or val.get("text") or val.get("value") or ""
    s = re.sub(r"\s+", " ", str(val)).strip()
    if max_len and len(s) > max_len:
        return s[:max_len]
    return s


def _clean_digits(val: Any, max_len: int = 10) -> str:
    if val is None:
        return "0" * max_len
    if isinstance(val, dict):
        val = val.get("number") or val.get("value") or val.get("text") or ""
    digits = re.sub(r"[^\d]", "", str(val))
    if not digits:
        return "0" * max_len
    if len(digits) > max_len:
        return digits[-max_len:]
    return digits.zfill(max_len)


def _to_dec(val: Any) -> Decimal:
    if val is None or val == "":
        return Decimal("0.00")
    if isinstance(val, MoneyAmount):
        return (val.amount or Decimal("0.00")).quantize(MONEY, rounding=ROUND_HALF_UP)
    if isinstance(val, Decimal):
        return val.quantize(MONEY, rounding=ROUND_HALF_UP)
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
        return Decimal(clean).quantize(MONEY, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")



def generate_ajur_csv(
    invoices: Sequence[Any] | Any,
    default_supplier_account: str = "401",
    default_vat_account: str = "4531",
    delimiter: str = ";",
    encoding: str = "windows-1251",
    mapping_engine: AccountMappingEngine | None = None,
) -> bytes | str:
    """Generate official Ajur-L / Ajur 7 CSV import file.
    
    Columns:
      Дата;ВидДокумент;НомерДокумент;ЕИК;ИмеКонтрагент;СметкаДт;ПодсметкаДт;СметкаКт;ПодсметкаКт;ДанъчнаОснова;ДДС;Общо;Валута;Основание;КлеткаДДС
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
        "ЕИК",
        "ИмеКонтрагент",
        "СметкаДт",
        "ПодсметкаДт",
        "СметкаКт",
        "ПодсметкаКт",
        "ДанъчнаОснова",
        "ДДС",
        "Общо",
        "Валута",
        "Основание",
        "КлеткаДДС",
    ])

    for inv in inv_list:
        if isinstance(inv, dict):
            meta = inv.get("invoice_metadata") or inv
            inv_no = _clean_digits(meta.get("invoice_number") or meta.get("invoiceNumber"))
            inv_dt = _clean_str(meta.get("date_issued") or meta.get("invoiceDate") or "2026-01-01")
            is_credit = bool(meta.get("is_credit_note") or meta.get("document_type") == "CREDIT_NOTE")
            is_debit = bool(meta.get("is_debit_note") or meta.get("document_type") == "DEBIT_NOTE")

            sup = inv.get("supplier") or {}
            sup_name = _clean_str(sup.get("name") or inv.get("vendorName") or "ДОСТАВЧИК", max_len=50)
            sup_eik = _clean_str(sup.get("eik") or inv.get("vendorEik") or "999999999", max_len=15)

            fin = inv.get("financial_summary") or {}
            tax_base = _to_dec(fin.get("tax_base") or inv.get("subtotal"))
            vat_amount = _to_dec(fin.get("vat_amount") or inv.get("taxAmount"))
            total_amount = _to_dec(fin.get("total_amount_due") or inv.get("totalAmount"))
            currency = _clean_str(inv.get("currency") or "BGN", max_len=3)
        else:
            inv_no = _clean_digits(inv.invoice_metadata.invoice_number)
            inv_dt = _clean_str(inv.invoice_metadata.date_issued or "2026-01-01")
            is_credit = bool(inv.invoice_metadata.is_credit_note or inv.invoice_metadata.document_type == "CREDIT_NOTE")
            is_debit = bool(inv.invoice_metadata.is_debit_note or inv.invoice_metadata.document_type == "DEBIT_NOTE")

            sup_name = _clean_str(inv.supplier.name, max_len=50)
            sup_eik = _clean_str(inv.supplier.eik, max_len=15)

            tax_base = _to_dec(inv.financial_summary.tax_base)
            vat_amount = _to_dec(inv.financial_summary.vat_amount)
            total_amount = _to_dec(inv.financial_summary.total_amount_due)
            currency = _clean_str(getattr(inv.invoice_metadata, "currency", "BGN") or "BGN", max_len=3)

        if total_amount == Decimal("0.00") and (tax_base > 0 or vat_amount > 0):
            total_amount = tax_base + vat_amount
        elif tax_base == Decimal("0.00") and total_amount > 0:
            tax_base = total_amount - vat_amount

        doc_type = "03" if is_credit else ("02" if is_debit else "01")
        sign = Decimal("-1") if is_credit else Decimal("1")
        vat_cell = "10" if vat_amount > 0 else "16"

        distributions = engine.split_invoice_by_accounts(inv, prefer_subaccounts=True)
        for dist in distributions:
            if dist.get("vat_rate") == Decimal("9.00"):
                vat_cell = "12"
                break

        # 1. Expense distributions
        for dist in distributions:
            sub_base = sign * abs(dist["amount"])
            sub_vat = sign * abs(dist.get("vat_amount") or Decimal("0.00"))
            sub_tot = sub_base + sub_vat
            writer.writerow([
                inv_dt,
                doc_type,
                inv_no,
                sup_eik,
                sup_name,
                dist["account"],
                dist.get("subledger") or "",
                default_supplier_account,
                sup_eik,
                f"{sub_base:.2f}",
                f"{sub_vat:.2f}",
                f"{sub_tot:.2f}",
                currency,
                dist["description"][:60],
                vat_cell,
            ])

    content_str = output.getvalue()
    if encoding:
        return content_str.encode(encoding, errors="replace")
    return content_str


def export_ajur_package(
    invoices: Sequence[Any] | Any,
    output_dir: Path | str,
    create_zip: bool = True,
) -> dict[str, str]:
    """Export complete Ajur package to output directory."""
    out_p = Path(output_dir)
    out_p.mkdir(parents=True, exist_ok=True)
    generated = {}

    csv_bytes = generate_ajur_csv(invoices, encoding="windows-1251")
    assert isinstance(csv_bytes, bytes)
    csv_file = out_p / "AJUR_IMPORT.csv"
    csv_file.write_bytes(csv_bytes)
    generated["ajur_csv"] = str(csv_file)

    if create_zip:
        zip_path = out_p / "Ajur_Import_Package.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(csv_file, arcname="AJUR_IMPORT.csv")
        generated["ajur_zip"] = str(zip_path)

    return generated

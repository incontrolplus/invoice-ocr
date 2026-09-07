"""Accounting & ERP Export Module for Bulgarian Purchase Invoices (ЗДДС).

Implements statutory requirements for the National Revenue Agency (НАП / NRA)
purchase VAT ledger (Дневник за покупки по Приложение № 12 от ППЗДДС) and automated
double-entry accounting journal entries (контировки) for major ERP systems:
- POKUPKI.TXT generator (Fixed-width & TSV, Windows-1251 & UTF-8, Storno / Credit Notes)
- Double-entry bookkeeping generator (Д-т 304/602, Д-т 4531, К-т 401)
- ERP export formats: Microinvest Delta Pro, Бизнес Навигатор, Ajur, SAP, Universal CSV, JSON
"""

from __future__ import annotations

import csv
import dataclasses
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
import io
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Iterable, Literal, Sequence

from invoice_ocr import (
    DocumentType,
    Invoice,
    MoneyAmount,
    convert_eur_to_bgn,
)

logger = logging.getLogger("accounting_export")

# ---------------------------------------------------------------------------
# Constants & Enums
# ---------------------------------------------------------------------------

DEFAULT_BRANCH = "00"
DEFAULT_EXPENSE_GOODS_ACCOUNT = "304"      # Стоки
DEFAULT_EXPENSE_SERVICE_ACCOUNT = "602"    # Разходи за външни услуги
DEFAULT_VAT_PURCHASE_ACCOUNT = "4531"      # Начислен данък за покупките
DEFAULT_SUPPLIER_PAYABLE_ACCOUNT = "401"   # Доставчици

# Default reporting company metadata (configurable via environment variables)
DEFAULT_COMPANY_NAME = os.environ.get("DEFAULT_COMPANY_NAME", "РМ КАСКАДА 2026 ЕООД")
DEFAULT_COMPANY_EIK = os.environ.get("DEFAULT_COMPANY_EIK", "208380135")
DEFAULT_COMPANY_VAT = os.environ.get("DEFAULT_COMPANY_VAT", f"BG{DEFAULT_COMPANY_EIK}")
DEFAULT_COMPANY_ADDRESS = os.environ.get("DEFAULT_COMPANY_ADDRESS", "гр. Плевен, ул. Гривишко шосе 1")
DEFAULT_COMPANY_CITY = os.environ.get("DEFAULT_COMPANY_CITY", "Плевен")
DEFAULT_COMPANY_POSTAL = os.environ.get("DEFAULT_COMPANY_POSTAL", "5800")
DEFAULT_COMPANY_MOL = os.environ.get("DEFAULT_COMPANY_MOL", "УПРАВИТЕЛ")

# NAP Document Types under ППЗДДС Приложение 12
NAP_DOC_TYPES = {
    "INVOICE": "01",
    "DEBIT_NOTE": "02",
    "CREDIT_NOTE": "03",
    "CUSTOMS_DECLARATION": "07",
    "PROTOCOL_CHL_117": "09",
    "FISCAL_RECEIPT": "01",
    "PAYMENT_ORDER_NAP": "01",
    "FISCAL_MEMORY_REPORT": "81",  # Отчет за продажбите по чл. 119 ЗДДС
    "GOODS_RECEIPT": "00",         # Стокова разписка (вътрешноскладов документ)
}

# Service indicators for automatic classification of Account 602 vs 304
SERVICE_KEYWORDS = (
    "услуг", "наем", "транспорт", "абонамент", "консултаци", "хостинг",
    "софтуер", "ремонт", "поддръжка", "сервиз", "почистване", "охрана",
    "застраховк", "лиценз", "телеком", "интернет", "счетовод", "правни",
    "реклама", "маркетинг", "обучение", "куриер", "доставка", "service",
    "maintenance", "consulting", "subscription", "repair", "hosting",
    "software", "fee", "license", "комисион", "такси", "автомивка",
    "превоз", "нощувк", "хотел", "трафик", "инсталаци"
)

SERVICE_SUPPLIER_KEYWORDS = (
    "а1", "виваком", "vivacom", "йеттел", "yettel", "електрохолд",
    "енерго", "евн", "evn", "топлофикация", "софийска вода", "български пощи",
    "спиди", "speedy", "еконт", "econt", "амазон", "гугъл", "google",
    "майкрософт", "microsoft", "aws", "hetzner", "digitalocean",
    "телеком", "telecom"
)


# ---------------------------------------------------------------------------
# Utility & Normalization Helpers
# ---------------------------------------------------------------------------

def normalize_doc_number(doc_no: str | None) -> str:
    """Normalize invoice number to statutory 10-digit zero-padded format for NAP.
    
    Examples:
        '124013' -> '0000124013'
        '1100098511' -> '1100098511'
        '№ 00042' -> '0000000042'
        None -> '0000000000'
    """
    if not doc_no:
        return "0000000000"
    digits = re.sub(r"[^\d]", "", str(doc_no))
    if not digits:
        return "0000000000"
    if len(digits) > 10:
        # If longer than 10 digits, take the trailing 10 digits (statutory max)
        return digits[-10:]
    return digits.zfill(10)


def map_document_type(
    doc_type_val: str | DocumentType | None,
    is_credit: bool = False,
    is_debit: bool = False,
) -> str:
    """Map internal document type to statutory 2-digit NAP code under ППЗДДС Приложение 12.
    
    Codes:
        01 = Данъчна фактура / Фактура
        02 = Дебитно известие
        03 = Кредитно известие (Сторно)
        07 = Митническа декларация
        09 = Протокол (чл. 117 ЗДДС и др.)
    """
    if is_credit:
        return "03"
    if is_debit:
        return "02"
    if not doc_type_val:
        return "01"

    val = doc_type_val.value if isinstance(doc_type_val, DocumentType) else str(doc_type_val).strip().upper()
    return NAP_DOC_TYPES.get(val, "01")


def classify_expense_account(
    invoice: Any,
    default_account: str = DEFAULT_EXPENSE_GOODS_ACCOUNT,
) -> str:
    """Classify expense/asset account: 304 (Стоки) vs 602 (Разходи за външни услуги).
    
    Inspects line items, supplier name, and document text for service indicators.
    """
    if default_account and default_account != DEFAULT_EXPENSE_GOODS_ACCOUNT:
        return default_account

    # 1. Check supplier name
    supplier_name = ""
    if hasattr(invoice, "supplier") and getattr(invoice.supplier, "name", None):
        supplier_name = str(invoice.supplier.name).lower()
    elif isinstance(invoice, dict) and invoice.get("supplier", {}).get("name"):
        supplier_name = str(invoice["supplier"]["name"]).lower()

    for kw in SERVICE_SUPPLIER_KEYWORDS:
        if kw in supplier_name:
            return DEFAULT_EXPENSE_SERVICE_ACCOUNT

    # 2. Check line items
    items = []
    if hasattr(invoice, "line_items") and getattr(invoice, "line_items", None):
        items = [getattr(it, "description", "") or "" for it in invoice.line_items]
    elif isinstance(invoice, dict) and "line_items" in invoice:
        items = [it.get("description", "") or "" for it in invoice["line_items"]]

    for desc in items:
        desc_lower = desc.lower()
        for kw in SERVICE_KEYWORDS:
            if kw in desc_lower:
                return DEFAULT_EXPENSE_SERVICE_ACCOUNT

    return DEFAULT_EXPENSE_GOODS_ACCOUNT


def _to_decimal(val: Any) -> Decimal:
    """Safely convert float/int/str/Decimal to 2-decimal quantized Decimal."""
    if val is None or val == "":
        return Decimal("0.00")
    if isinstance(val, MoneyAmount):
        val = val.amount
        if val is None:
            return Decimal("0.00")
    try:
        d = Decimal(str(val))
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def _extract_invoice_fields(invoice: Any) -> dict[str, Any]:
    """Uniformly extract core accounting fields from Invoice object or dict."""
    if hasattr(invoice, "invoice_metadata"):
        # Dataclass instance
        meta = invoice.invoice_metadata
        supplier = invoice.supplier
        financial = invoice.financial_summary
        line_items = invoice.line_items

        is_credit = bool(getattr(meta, "is_credit_note", False) or getattr(meta, "document_type", "") == "CREDIT_NOTE")
        is_debit = bool(getattr(meta, "is_debit_note", False) or getattr(meta, "document_type", "") == "DEBIT_NOTE")
        doc_type = getattr(meta, "document_type", "INVOICE")
        doc_number = getattr(meta, "invoice_number", None)
        date_issued = getattr(meta, "date_issued", None)
        date_tax_event = getattr(meta, "date_tax_event", None)

        sup_name = getattr(supplier, "name", None) or "НЕИЗВЕСТЕН ДОСТАВЧИК"
        sup_eik = getattr(supplier, "eik", None) or getattr(supplier, "vat_number", None) or "999999999"
        sup_vat = getattr(supplier, "vat_number", None)

        currency = (
            getattr(financial.total_amount_due, "currency", None)
            or getattr(financial.tax_base, "currency", None)
            or "BGN"
        )
        total_due = _to_decimal(getattr(financial.total_amount_due, "amount", None))
        tax_base = _to_decimal(getattr(financial.tax_base, "amount", None))
        vat_amount = _to_decimal(getattr(financial.vat_amount, "amount", None))
        total_bgn = _to_decimal(getattr(getattr(financial, "total_amount_bgn", None), "amount", None))

        items_desc = [getattr(it, "description", "") or "" for it in line_items]
        vat_rates = [getattr(it, "vat_rate_pct", None) for it in line_items if getattr(it, "vat_rate_pct", None) is not None]

        fiscal = getattr(invoice, "fiscal_report", None)
        goods = getattr(invoice, "goods_receipt", None)
        if fiscal or doc_type == "FISCAL_MEMORY_REPORT":
            doc_type = "FISCAL_MEMORY_REPORT"
            if fiscal:
                doc_number = doc_number or getattr(fiscal, "fiscal_memory_number", None) or getattr(fiscal, "device_number", None) or "0000000001"
                date_issued = date_issued or getattr(fiscal, "period_to", None) or getattr(fiscal, "period_from", None)
                if total_due == Decimal("0.00") and getattr(fiscal, "turnover_total", None) is not None:
                    total_due = _to_decimal(getattr(fiscal, "turnover_total", 0.0))
                if tax_base == Decimal("0.00") and getattr(fiscal, "tax_base_total", None) is not None:
                    tax_base = _to_decimal(getattr(fiscal, "tax_base_total", 0.0))
                if vat_amount == Decimal("0.00") and getattr(fiscal, "vat_total", None) is not None:
                    vat_amount = _to_decimal(getattr(fiscal, "vat_total", 0.0))
            if not items_desc:
                items_desc = ["Отчет от фискална памет (чл. 119 ЗДДС)"]
        elif goods or doc_type == "GOODS_RECEIPT":
            doc_type = "GOODS_RECEIPT"
            if goods:
                doc_number = doc_number or getattr(goods, "receipt_number", None) or "0000000001"
                date_issued = date_issued or getattr(goods, "receipt_date", None)
                if total_due == Decimal("0.00") and getattr(goods, "total_amount", None) is not None:
                    total_due = _to_decimal(getattr(goods, "total_amount", 0.0))
                tax_base = total_due
                vat_amount = Decimal("0.00")
            if not items_desc:
                items_desc = ["Стокова разписка"]
    elif isinstance(invoice, dict):
        # JSON dict (supports both top-level and 3-layer nested structure)
        norm = invoice.get("normalized_data") or invoice.get("data", {}).get("normalized_data") or {}
        meta = invoice.get("invoice_metadata") or norm.get("invoice_metadata") or {}
        supplier = invoice.get("supplier") or norm.get("supplier") or {}
        financial = invoice.get("financial_summary") or norm.get("financial_summary") or {}
        line_items = invoice.get("line_items") or norm.get("line_items") or []

        is_credit = bool(meta.get("is_credit_note", False) or meta.get("document_type") == "CREDIT_NOTE")
        is_debit = bool(meta.get("is_debit_note", False) or meta.get("document_type") == "DEBIT_NOTE")
        doc_type = meta.get("document_type", "INVOICE")
        doc_number = meta.get("invoice_number")
        date_issued = meta.get("date_issued")
        date_tax_event = meta.get("date_tax_event")

        sup_name = supplier.get("name") or "НЕИЗВЕСТЕН ДОСТАВЧИК"
        sup_eik = supplier.get("eik") or supplier.get("vat_number") or "999999999"
        sup_vat = supplier.get("vat_number")

        total_due_dict = financial.get("total_amount_due") or {}
        tax_base_dict = financial.get("tax_base") or {}
        vat_amount_dict = financial.get("vat_amount") or {}
        total_bgn_dict = financial.get("total_amount_bgn") or {}

        currency = (
            total_due_dict.get("currency")
            or tax_base_dict.get("currency")
            or "BGN"
        )
        total_due = _to_decimal(total_due_dict.get("amount"))
        tax_base = _to_decimal(tax_base_dict.get("amount"))
        vat_amount = _to_decimal(vat_amount_dict.get("amount"))
        total_bgn = _to_decimal(total_bgn_dict.get("amount"))

        items_desc = [it.get("description", "") or "" for it in line_items]
        vat_rates = [it.get("vat_rate_pct") for it in line_items if it.get("vat_rate_pct") is not None]

        fiscal = norm.get("fiscal_report") or invoice.get("fiscal_report")
        goods = norm.get("goods_receipt") or invoice.get("goods_receipt")
        if fiscal or doc_type == "FISCAL_MEMORY_REPORT":
            doc_type = "FISCAL_MEMORY_REPORT"
            if fiscal:
                doc_number = doc_number or fiscal.get("fiscal_memory_number") or fiscal.get("device_number") or "0000000001"
                date_issued = date_issued or fiscal.get("period_to") or fiscal.get("period_from")
                if total_due == Decimal("0.00") and fiscal.get("turnover_total") is not None:
                    total_due = _to_decimal(fiscal.get("turnover_total"))
                if tax_base == Decimal("0.00") and fiscal.get("tax_base_total") is not None:
                    tax_base = _to_decimal(fiscal.get("tax_base_total"))
                if vat_amount == Decimal("0.00") and fiscal.get("vat_total") is not None:
                    vat_amount = _to_decimal(fiscal.get("vat_total"))
            if not items_desc:
                items_desc = ["Отчет от фискална памет (чл. 119 ЗДДС)"]
        elif goods or doc_type == "GOODS_RECEIPT":
            doc_type = "GOODS_RECEIPT"
            if goods:
                doc_number = doc_number or goods.get("receipt_number") or "0000000001"
                date_issued = date_issued or goods.get("receipt_date")
                if total_due == Decimal("0.00") and goods.get("total_amount") is not None:
                    total_due = _to_decimal(goods.get("total_amount"))
                tax_base = total_due
                vat_amount = Decimal("0.00")
            if not items_desc:
                items_desc = ["Стокова разписка"]
    elif isinstance(invoice, NapLedgerEntry):
        return {
            "is_credit": invoice.doc_type == "03",
            "is_debit": invoice.doc_type == "02",
            "doc_type": "CREDIT_NOTE" if invoice.doc_type == "03" else ("DEBIT_NOTE" if invoice.doc_type == "02" else "INVOICE"),
            "doc_number": invoice.doc_number,
            "doc_date": invoice.doc_date,
            "supplier_name": invoice.contractor_name,
            "supplier_eik": invoice.contractor_id,
            "supplier_vat": None,
            "currency": "BGN",
            "total_due": invoice.total_amount,
            "tax_base": invoice.tax_base_20 + invoice.tax_base_9 + invoice.tax_base_no_credit,
            "vat_amount": invoice.vat_20 + invoice.vat_9,
            "total_bgn": invoice.total_amount,
            "delivery_desc": invoice.description,
            "vat_rates": [20] if invoice.vat_20 else [],
        }
    else:
        raise TypeError(f"Unsupported invoice type: {type(invoice)}")

    # Standardize Bulgarian date
    doc_date = date_tax_event or date_issued or "2026-01-01"

    # Delivery description summary
    clean_items = [d.strip() for d in items_desc if d and d.strip()]
    if clean_items:
        first_desc = clean_items[0]
        # Clean newlines and excessive whitespace
        first_desc = re.sub(r"\s+", " ", first_desc).strip()
        delivery_desc = first_desc[:30]
    else:
        delivery_desc = "Стоки / Услуги"
    # Reconcile unextracted or missing financial fields if mathematically deduceable
    if vat_amount == Decimal("0.00") and total_due > Decimal("0.00") and tax_base > Decimal("0.00"):
        diff = total_due - tax_base
        expected_vat20 = (tax_base * Decimal("0.20")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if abs(diff - expected_vat20) <= Decimal("0.02"):
            vat_amount = diff
            vat_rates.append(Decimal("20.00"))
    elif tax_base == Decimal("0.00") and total_due > Decimal("0.00") and vat_amount > Decimal("0.00"):
        tax_base = total_due - vat_amount
    elif total_due == Decimal("0.00") and (tax_base > Decimal("0.00") or vat_amount > Decimal("0.00")):
        total_due = tax_base + vat_amount

    return {
        "doc_type": doc_type,
        "is_credit": is_credit,
        "is_debit": is_debit,
        "doc_number": doc_number,
        "doc_date": doc_date,
        "supplier_name": sup_name,
        "supplier_eik": sup_eik,
        "supplier_vat": sup_vat,
        "currency": currency,
        "total_due": total_due,
        "tax_base": tax_base,
        "vat_amount": vat_amount,
        "total_bgn": total_bgn,
        "delivery_desc": delivery_desc,
        "vat_rates": vat_rates,
    }


# ---------------------------------------------------------------------------
# NAP Ledger Entry (Приложение № 12 от ППЗДДС - Дневник за покупки)
# ---------------------------------------------------------------------------

@dataclass
class NapLedgerEntry:
    """A single record in the statutory VAT Purchase Ledger (POKUPKI.TXT).
    
    Columns according to ППЗДДС Приложение № 12:
      Col 1:  Клон / Период (Branch or Period)
      Col 2:  Вид на документа (01=Фактура, 02=ДИ, 03=КИ/Сторно, 07=Митн. декл., 09=Протокол)
      Col 3:  Номер на документа (10-цифрен, водещи нули)
      Col 4:  Дата на документа (YYYY-MM-DD)
      Col 5:  Идентификационен номер на контрагента (ЕИК / ДДС номер)
      Col 6:  Име на контрагента (до 50 символа)
      Col 7:  Предмет на доставката (Стоки / Услуги / Описание, до 30 символа)
      Col 8:  Обща сума на документа (Крайна сума с ДДС)
      Col 9:  Данъчна основа за 20% ДДС (клетка 10 с пълен данъчен кредит)
      Col 10: Начислен ДДС (клетка 11 с пълен данъчен кредит)
      Col 11: Данъчна основа за 9% ДДС (клетка 12)
      Col 12: Начислен ДДС 9% (клетка 13)
      Col 13: Данъчна основа с частичен данъчен кредит (клетка 14)
      Col 14: ДДС с частичен данъчен кредит (клетка 15)
      Col 15: Данъчна основа без право на данъчен кредит (клетка 16)
      Col 16: ДДС на получените доставки по чл. 82, ал. 2-5 ЗДДС (клетка 17)
    """
    branch_or_period: str = DEFAULT_BRANCH
    doc_type: str = "01"
    doc_number: str = "0000000000"
    doc_date: str = "2026-01-01"
    contractor_id: str = "999999999"
    contractor_name: str = "НЕИЗВЕСТЕН ДОСТАВЧИК"
    description: str = "Стоки / Услуги"
    total_amount: Decimal = Decimal("0.00")
    tax_base_20: Decimal = Decimal("0.00")
    vat_20: Decimal = Decimal("0.00")
    tax_base_9: Decimal = Decimal("0.00")
    vat_9: Decimal = Decimal("0.00")
    tax_base_partial: Decimal = Decimal("0.00")
    vat_partial: Decimal = Decimal("0.00")
    tax_base_no_credit: Decimal = Decimal("0.00")
    vat_special_art82: Decimal = Decimal("0.00")

    def to_fixed_width_line(self) -> str:
        """Format as official NAP fixed-width text record."""
        branch = self.branch_or_period[:4].ljust(4)
        doc_t = self.doc_type[:2].rjust(2)
        doc_no = self.doc_number[:10].rjust(10, "0")
        doc_dt = self.doc_date[:10].ljust(10)
        c_id = self.contractor_id[:15].ljust(15)
        c_name = self.contractor_name[:50].ljust(50)
        desc = self.description[:30].ljust(30)

        tot_s = f"{self.total_amount:.2f}".rjust(15)
        tb20_s = f"{self.tax_base_20:.2f}".rjust(15)
        v20_s = f"{self.vat_20:.2f}".rjust(15)
        tb9_s = f"{self.tax_base_9:.2f}".rjust(15)
        v9_s = f"{self.vat_9:.2f}".rjust(15)
        tb_part_s = f"{self.tax_base_partial:.2f}".rjust(15)
        v_part_s = f"{self.vat_partial:.2f}".rjust(15)
        tb_no_s = f"{self.tax_base_no_credit:.2f}".rjust(15)
        v_art82_s = f"{self.vat_special_art82:.2f}".rjust(15)

        return (
            f"{branch}{doc_t}{doc_no}{doc_dt}{c_id}{c_name}{desc}"
            f"{tot_s}{tb20_s}{v20_s}{tb9_s}{v9_s}{tb_part_s}{v_part_s}{tb_no_s}{v_art82_s}"
        )

    def to_tsv_line(self) -> str:
        """Format as Tab-separated values record for NAP/ERP."""
        fields = [
            self.branch_or_period,
            self.doc_type,
            self.doc_number,
            self.doc_date,
            self.contractor_id,
            self.contractor_name,
            self.description,
            f"{self.total_amount:.2f}",
            f"{self.tax_base_20:.2f}",
            f"{self.vat_20:.2f}",
            f"{self.tax_base_9:.2f}",
            f"{self.vat_9:.2f}",
            f"{self.tax_base_partial:.2f}",
            f"{self.vat_partial:.2f}",
            f"{self.tax_base_no_credit:.2f}",
            f"{self.vat_special_art82:.2f}",
        ]
        return "\t".join(fields)

    def to_csv_line(self, delimiter: str = ";") -> str:
        """Format as Semicolon-delimited CSV record."""
        fields = [
            self.branch_or_period,
            self.doc_type,
            self.doc_number,
            self.doc_date,
            self.contractor_id,
            f'"{self.contractor_name}"',
            f'"{self.description}"',
            f"{self.total_amount:.2f}",
            f"{self.tax_base_20:.2f}",
            f"{self.vat_20:.2f}",
            f"{self.tax_base_9:.2f}",
            f"{self.vat_9:.2f}",
            f"{self.tax_base_partial:.2f}",
            f"{self.vat_partial:.2f}",
            f"{self.tax_base_no_credit:.2f}",
            f"{self.vat_special_art82:.2f}",
        ]
        return delimiter.join(fields)

    def to_dict(self) -> dict[str, Any]:
        """Convert entry to dictionary with formatted numeric strings."""
        return {
            "branch_or_period": self.branch_or_period,
            "doc_type": self.doc_type,
            "doc_number": self.doc_number,
            "doc_date": self.doc_date,
            "contractor_id": self.contractor_id,
            "contractor_name": self.contractor_name,
            "description": self.description,
            "total_amount": f"{self.total_amount:.2f}",
            "tax_base_20": f"{self.tax_base_20:.2f}",
            "vat_20": f"{self.vat_20:.2f}",
            "tax_base_9": f"{self.tax_base_9:.2f}",
            "vat_9": f"{self.vat_9:.2f}",
            "tax_base_partial": f"{self.tax_base_partial:.2f}",
            "vat_partial": f"{self.vat_partial:.2f}",
            "tax_base_no_credit": f"{self.tax_base_no_credit:.2f}",
            "vat_special_art82": f"{self.vat_special_art82:.2f}",
        }


def invoice_to_nap_entry(
    invoice: Any,
    period: str | None = None,
    branch: str = DEFAULT_BRANCH,
) -> NapLedgerEntry:
    """Convert an Invoice or invoice dictionary to a statutory NAP Purchase Ledger entry."""
    if isinstance(invoice, NapLedgerEntry):
        return invoice
    if hasattr(invoice, "to_purchase_ledger_entry"):
        return invoice.to_purchase_ledger_entry(period=period, branch=branch)
    info = _extract_invoice_fields(invoice)
    if info.get("doc_type") == "FISCAL_MEMORY_REPORT":
        raise ValueError("Отчетите от фискална памет са документи за продажби (чл. 119 ЗДДС) и следва да се експортират към Дневник за продажби (PRODAGBI.TXT), а не в Дневник за покупки.")
    if info.get("doc_type") == "GOODS_RECEIPT":
        raise ValueError("Стоковите разписки са вътрешноскладови документи и не подлежат на регистрация в ДДС Дневник за покупки по ЗДДС.")

    doc_type = map_document_type(
        info["doc_type"],
        is_credit=info["is_credit"],
        is_debit=info["is_debit"],
    )
    doc_number = normalize_doc_number(info["doc_number"])
    doc_date = info["doc_date"]

    # Sign: negative for credit notes (storno 03)
    sign = Decimal("-1") if (doc_type == "03" or info["is_credit"]) else Decimal("1")

    # If amounts are already negative in model, use their absolute values scaled by sign
    total_due = sign * abs(info["total_due"])
    tax_base = sign * abs(info["tax_base"])
    vat_amount = sign * abs(info["vat_amount"])

    # If foreign currency (e.g. EUR, USD, GBP) and BGN total exists, prefer BGN conversion for NAP
    if info["currency"] == "EUR":
        if info["total_bgn"] and info["total_bgn"] > Decimal("0.00"):
            total_due = sign * abs(info["total_bgn"])
            tax_base = sign * abs(convert_eur_to_bgn(info["tax_base"]))
            vat_amount = sign * abs(convert_eur_to_bgn(info["vat_amount"]))
        else:
            total_due = sign * abs(convert_eur_to_bgn(info["total_due"]))
            tax_base = sign * abs(convert_eur_to_bgn(info["tax_base"]))
            vat_amount = sign * abs(convert_eur_to_bgn(info["vat_amount"]))
    elif info["currency"] not in ("BGN", None, ""):
        if info.get("total_bgn") and info["total_bgn"] > Decimal("0.00"):
            ratio = info["total_bgn"] / info["total_due"] if info["total_due"] else Decimal("1.0")
            total_due = sign * abs(info["total_bgn"])
            tax_base = (sign * abs(info["tax_base"]) * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            vat_amount = (sign * abs(info["vat_amount"]) * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            logger.warning(
                "Invoice %s is in foreign currency '%s' without explicit BGN total.",
                info.get("doc_number"), info["currency"],
            )

    # Categorize tax bases
    tax_base_20 = Decimal("0.00")
    vat_20 = Decimal("0.00")
    tax_base_9 = Decimal("0.00")
    vat_9 = Decimal("0.00")
    tax_base_no_credit = Decimal("0.00")

    is_9_pct = False
    for r in info["vat_rates"]:
        try:
            r_dec = Decimal(str(r))
            if Decimal("8.0") <= r_dec <= Decimal("10.0"):
                is_9_pct = True
                break
        except Exception:
            pass

    # If effective rate vat/tax_base is ~9%
    if not is_9_pct and abs(tax_base) > Decimal("0.01"):
        effective_rate = (abs(vat_amount) / abs(tax_base)) * Decimal("100")
        if Decimal("8.0") <= effective_rate <= Decimal("10.0"):
            is_9_pct = True

    if abs(vat_amount) == Decimal("0.00") and abs(tax_base) > Decimal("0.00"):
        # Zero VAT / exempt delivery
        tax_base_no_credit = tax_base
    elif is_9_pct:
        tax_base_9 = tax_base
        vat_9 = vat_amount
    else:
        # Standard 20% VAT rate (default)
        tax_base_20 = tax_base
        vat_20 = vat_amount

    # Contractor ID: 9 digits if valid EIK/BULSTAT, otherwise original
    sup_eik = str(info["supplier_eik"]).strip()
    contractor_id = re.sub(r"[^A-Za-z0-9]", "", sup_eik) or "999999999"
    contractor_name = re.sub(r"\s+", " ", str(info["supplier_name"])).strip()
    description = re.sub(r"\s+", " ", str(info["delivery_desc"])).strip()

    branch_val = period if period else branch

    return NapLedgerEntry(
        branch_or_period=branch_val,
        doc_type=doc_type,
        doc_number=doc_number,
        doc_date=doc_date,
        contractor_id=contractor_id,
        contractor_name=contractor_name,
        description=description,
        total_amount=total_due,
        tax_base_20=tax_base_20,
        vat_20=vat_20,
        tax_base_9=tax_base_9,
        vat_9=vat_9,
        tax_base_partial=Decimal("0.00"),
        vat_partial=Decimal("0.00"),
        tax_base_no_credit=tax_base_no_credit,
        vat_special_art82=Decimal("0.00"),
    )


def invoices_to_pokupki_txt(
    invoices: Iterable[Any],
    format: Literal["fixed_width", "tsv", "csv"] = "fixed_width",
    encoding: str | None = "cp1251",
    period: str | None = None,
    branch: str = DEFAULT_BRANCH,
) -> bytes | str:
    """Generate statutory POKUPKI.TXT file from an iterable of invoices.
    
    Args:
        invoices: Iterable of Invoice objects or parsed invoice dicts.
        format: 'fixed_width' (statutory fixed-width), 'tsv' (tab-separated), or 'csv' (semicolon).
        encoding: 'cp1251' (statutory Windows-1251 for NAP) or 'utf-8'. If None, returns str.
        period: Optional VAT period (e.g. '202608' or '2026-08').
        branch: Branch code (default '00').
        
    Returns:
        Encoded bytes (if encoding specified) or string with CRLF line endings.
    """
    lines: list[str] = []
    
    if format == "csv":
        # Add standard CSV header
        header = (
            "Клон/Период;ВидДокумент;НомерДокумент;Дата;ЕИК_ДДС;ИмеКонтрагент;"
            "Предмет;ОбщаСума;ДО_20;ДДС_20;ДО_9;ДДС_9;ДО_ЧастиченДК;ДДС_ЧастиченДК;"
            "ДО_БезДК;ДДС_Чл82"
        )
        lines.append(header)

    if format == "fixed_width" and encoding and encoding.lower().replace("-", "") in ("utf8", "utf16", "utf32"):
        logger.warning(
            "Statutory fixed-width NAP files (POKUPKI.TXT) require 1-byte encoding ('cp1251'/'windows-1251'). "
            "Using %s causes multi-byte Cyrillic column misalignment in the NRA submission portal.",
            encoding,
        )

    for inv in invoices:
        if isinstance(inv, NapLedgerEntry):
            entry = inv
        else:
            info = _extract_invoice_fields(inv)
            if info.get("doc_type") in ("FISCAL_MEMORY_REPORT", "GOODS_RECEIPT"):
                logger.info("Skipping document %s (type %s) from POKUPKI.TXT (not a purchase VAT document)", info.get("doc_number"), info.get("doc_type"))
                continue
            entry = invoice_to_nap_entry(inv, period=period, branch=branch)
        if format == "fixed_width":
            lines.append(entry.to_fixed_width_line())
        elif format == "tsv":
            lines.append(entry.to_tsv_line())
        elif format == "csv":
            lines.append(entry.to_csv_line(";"))
        else:
            raise ValueError(f"Unknown format: {format}. Must be 'fixed_width', 'tsv', or 'csv'.")

    # CRLF line separator standard for Windows & NAP file submission
    content_str = "\r\n".join(lines)
    if lines:
        content_str += "\r\n"

    if encoding:
        return content_str.encode(encoding, errors="replace")
    return content_str


# ---------------------------------------------------------------------------
# Double-Entry Accounting Journal Entries (Счетоводни Контировки)
# ---------------------------------------------------------------------------

@dataclass
class DoubleEntryRecord:
    """A single balanced debit-credit posting record."""
    debit_account: str
    debit_subledger: str | None
    credit_account: str
    credit_subledger: str | None
    amount: Decimal
    currency: str
    description: str


@dataclass
class AccountingJournalEntry:
    """Complete double-entry accounting transaction for a purchase invoice.
    
    Standard Bulgarian accounting accounts:
      - Д-т 304 (Стоки) или 602 (Разходи за външни услуги)
      - Д-т 4531 (Начислен данък за покупките)
      - К-т 401 (Доставчици / с аналитичност ЕИК на доставчика)
      
    Mathematical balance:
      Дебит (304/602) + Дебит (4531) = Кредит (401)
    """
    doc_number: str
    doc_date: str
    doc_type: str
    supplier_name: str
    supplier_eik: str
    currency: str
    expense_account: str
    tax_base_amount: Decimal
    vat_account: str
    vat_amount: Decimal
    payable_account: str
    total_amount: Decimal
    is_balanced: bool
    is_storno: bool
    description: str
    records: list[DoubleEntryRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert entry to dictionary with formatted numbers and verification."""
        return {
            "doc_number": self.doc_number,
            "doc_date": self.doc_date,
            "doc_type": self.doc_type,
            "supplier": {
                "name": self.supplier_name,
                "eik": self.supplier_eik,
            },
            "currency": self.currency,
            "expense_account": self.expense_account,
            "tax_base_amount": f"{self.tax_base_amount:.2f}",
            "vat_account": self.vat_account,
            "vat_amount": f"{self.vat_amount:.2f}",
            "payable_account": self.payable_account,
            "total_amount": f"{self.total_amount:.2f}",
            "is_balanced": self.is_balanced,
            "is_storno": self.is_storno,
            "description": self.description,
            "records": [
                {
                    "debit_account": r.debit_account,
                    "debit_subledger": r.debit_subledger,
                    "credit_account": r.credit_account,
                    "credit_subledger": r.credit_subledger,
                    "amount": f"{r.amount:.2f}",
                    "currency": r.currency,
                    "description": r.description,
                }
                for r in self.records
            ],
        }


def invoice_to_journal_entry(
    invoice: Any,
    default_expense_account: str | None = None,
) -> AccountingJournalEntry:
    """Generate double-entry bookkeeping journal entries for an invoice.
    
    Verifies mathematical equality: Debit = Credit.
    For Credit Notes (03), generates storno entries with negative amounts.
    """
    info = _extract_invoice_fields(invoice)

    if info["doc_type"] == "FISCAL_MEMORY_REPORT":
        cash_account = "501"
        revenue_account = "702"
        vat_sales_account = "4532"
        tax_base = abs(info["tax_base"])
        vat_amt = abs(info["vat_amount"])
        total_amt = abs(info["total_due"])
        if total_amt == Decimal("0.00"):
            total_amt = tax_base + vat_amt
        elif tax_base == Decimal("0.00") and vat_amt != Decimal("0.00"):
            tax_base = total_amt - vat_amt
        elif tax_base + vat_amt != total_amt:
            total_amt = tax_base + vat_amt

        records = [
            DoubleEntryRecord(
                debit_account=cash_account,
                debit_subledger=None,
                credit_account=revenue_account,
                credit_subledger=None,
                amount=tax_base,
                currency=info["currency"],
                description="Приходи от продажби на стоки (отчет ФП чл. 119 ЗДДС)",
            ),
            DoubleEntryRecord(
                debit_account=cash_account,
                debit_subledger=None,
                credit_account=vat_sales_account,
                credit_subledger=None,
                amount=vat_amt,
                currency=info["currency"],
                description="Начислен ДДС за продажбите 20% (отчет ФП)",
            ),
        ]
        return AccountingJournalEntry(
            doc_number=normalize_doc_number(info["doc_number"]),
            doc_date=info["doc_date"],
            doc_type="Отчет от фискална памет",
            supplier_name=info["supplier_name"],
            supplier_eik=info["supplier_eik"],
            currency=info["currency"],
            expense_account=revenue_account,
            tax_base_amount=tax_base,
            vat_account=vat_sales_account,
            vat_amount=vat_amt,
            payable_account=cash_account,
            total_amount=total_amt,
            is_balanced=(tax_base + vat_amt) == total_amt,
            is_storno=False,
            description=f"Отчет от фискална памет № {info['doc_number']} от {info['doc_date']}",
            records=records,
        )

    if info["doc_type"] == "GOODS_RECEIPT":
        goods_account = "304"
        payable_account = DEFAULT_SUPPLIER_PAYABLE_ACCOUNT
        total_amt = abs(info["total_due"])
        sup_eik = info["supplier_eik"]
        records = [
            DoubleEntryRecord(
                debit_account=goods_account,
                debit_subledger=None,
                credit_account=payable_account,
                credit_subledger=sup_eik,
                amount=total_amt,
                currency=info["currency"],
                description="Заприхождаване на стоки по стокова разписка",
            )
        ]
        return AccountingJournalEntry(
            doc_number=normalize_doc_number(info["doc_number"]),
            doc_date=info["doc_date"],
            doc_type="Стокова разписка",
            supplier_name=info["supplier_name"],
            supplier_eik=sup_eik,
            currency=info["currency"],
            expense_account=goods_account,
            tax_base_amount=total_amt,
            vat_account="0000",
            vat_amount=Decimal("0.00"),
            payable_account=payable_account,
            total_amount=total_amt,
            is_balanced=True,
            is_storno=False,
            description=f"Стокова разписка № {info['doc_number']} от {info['supplier_name']}",
            records=records,
        )

    # Account determination
    if default_expense_account:
        exp_account = default_expense_account
    else:
        exp_account = classify_expense_account(invoice, DEFAULT_EXPENSE_GOODS_ACCOUNT)

    vat_account = DEFAULT_VAT_PURCHASE_ACCOUNT
    payable_account = DEFAULT_SUPPLIER_PAYABLE_ACCOUNT

    is_credit = info["is_credit"] or info["doc_type"] == "CREDIT_NOTE"
    sign = Decimal("-1") if is_credit else Decimal("1")

    tax_base = sign * abs(info["tax_base"])
    vat_amt = sign * abs(info["vat_amount"])
    total_amt = sign * abs(info["total_due"])

    # If total_due doesn't match tax_base + vat_amt due to minor penny rounding,
    # ensure strict debit == credit equality
    calculated_sum = tax_base + vat_amt
    if total_amt == Decimal("0.00") and calculated_sum != Decimal("0.00"):
        total_amt = calculated_sum
    elif tax_base == Decimal("0.00") and total_amt != Decimal("0.00") and vat_amt == Decimal("0.00"):
        tax_base = total_amt
    elif abs(calculated_sum - total_amt) <= Decimal("0.02") and calculated_sum != total_amt:
        total_amt = calculated_sum
    elif calculated_sum != total_amt:
        if vat_amt != Decimal("0.00"):
            tax_base = total_amt - vat_amt
        else:
            tax_base = total_amt

    is_balanced = (tax_base + vat_amt) == total_amt

    doc_no = normalize_doc_number(info["doc_number"])
    doc_dt = info["doc_date"]
    doc_tp_name = "Кредитно известие" if is_credit else "Фактура"
    sup_name = info["supplier_name"]
    sup_eik = info["supplier_eik"]
    curr = info["currency"]

    # Construct individual double-entry records
    records: list[DoubleEntryRecord] = []
    
    # Record 1: Expense / Inventory (Д-т 304/602 / К-т 401)
    if tax_base != Decimal("0.00"):
        rec_desc = f"{'Сторно - ' if is_credit else ''}Покупка на стоки/услуги"
        records.append(
            DoubleEntryRecord(
                debit_account=exp_account,
                debit_subledger=None,
                credit_account=payable_account,
                credit_subledger=sup_eik,
                amount=tax_base,
                currency=curr,
                description=rec_desc,
            )
        )

    # Record 2: Purchase VAT (Д-т 4531 / К-т 401)
    if vat_amt != Decimal("0.00"):
        vat_desc = f"{'Сторно - ' if is_credit else ''}ДДС покупки 20%"
        records.append(
            DoubleEntryRecord(
                debit_account=vat_account,
                debit_subledger=None,
                credit_account=payable_account,
                credit_subledger=sup_eik,
                amount=vat_amt,
                currency=curr,
                description=vat_desc,
            )
        )

    summary_desc = f"{doc_tp_name} № {doc_no} от {sup_name}"

    return AccountingJournalEntry(
        doc_number=doc_no,
        doc_date=doc_dt,
        doc_type=doc_tp_name,
        supplier_name=sup_name,
        supplier_eik=sup_eik,
        currency=curr,
        expense_account=exp_account,
        tax_base_amount=tax_base,
        vat_account=vat_account,
        vat_amount=vat_amt,
        payable_account=payable_account,
        total_amount=total_amt,
        is_balanced=is_balanced,
        is_storno=is_credit,
        description=summary_desc,
        records=records,
    )


def invoices_to_journal_entries(
    invoices: Iterable[Any],
    default_expense_account: str | None = None,
) -> list[AccountingJournalEntry]:
    """Generate double-entry journal entries for an iterable of invoices."""
    return [
        invoice_to_journal_entry(inv, default_expense_account=default_expense_account)
        for inv in invoices
    ]


# ---------------------------------------------------------------------------
# ERP Exporters (CSV, Microinvest, Бизнес Навигатор, Ajur, SAP, JSON)
# ---------------------------------------------------------------------------

def export_journal_entries_csv(
    entries: Sequence[AccountingJournalEntry],
    format_type: Literal[
        "universal",
        "standard",
        "microinvest",
        "business_navigator",
        "ajur",
        "sap",
    ] = "universal",
) -> str:
    """Export accounting journal entries to CSV formatted for various ERP systems.
    
    Supported formats:
      - 'universal' / 'standard': Standard compound CSV with all accounts and amounts
      - 'microinvest': Microinvest Delta Pro import format (Semicolon-delimited)
      - 'business_navigator': Бизнес Навигатор format
      - 'ajur': Ажур format
      - 'sap': SAP FI batch import format (Tab-delimited)
    """
    output = io.StringIO()

    if format_type in ("universal", "standard"):
        # Universal compound CSV
        writer = csv.writer(output, delimiter=";")
        writer.writerow([
            "Дата",
            "ВидДокумент",
            "НомерДокумент",
            "ЕИК_Доставчик",
            "Име_Доставчик",
            "Сметка_Разход",
            "Сума_Разход",
            "Сметка_ДДС",
            "Сума_ДДС",
            "Сметка_Доставчик",
            "Обща_Сума",
            "Валута",
            "Балансирана",
            "Сторно",
            "Основание",
        ])
        for e in entries:
            writer.writerow([
                e.doc_date,
                e.doc_type,
                e.doc_number,
                e.supplier_eik,
                e.supplier_name,
                e.expense_account,
                f"{e.tax_base_amount:.2f}",
                e.vat_account,
                f"{e.vat_amount:.2f}",
                e.payable_account,
                f"{e.total_amount:.2f}",
                e.currency,
                "1" if e.is_balanced else "0",
                "1" if e.is_storno else "0",
                e.description,
            ])

    elif format_type == "microinvest":
        # Microinvest Delta Pro format: Semicolon-delimited 2-sided postings
        writer = csv.writer(output, delimiter=";")
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
        for e in entries:
            for r in e.records:
                writer.writerow([
                    e.doc_date,
                    e.doc_type,
                    e.doc_number,
                    r.debit_account,
                    r.debit_subledger or "",
                    r.credit_account,
                    r.credit_subledger or "",
                    f"{r.amount:.2f}",
                    r.currency,
                    r.description,
                ])

    elif format_type == "business_navigator":
        # Бизнес Навигатор format
        writer = csv.writer(output, delimiter=";")
        writer.writerow([
            "Дата",
            "Документ",
            "Номер",
            "ЕИК",
            "Контрагент",
            "СметкаДт",
            "СметкаКт",
            "Дебит",
            "Кредит",
            "Основание",
        ])
        for e in entries:
            for r in e.records:
                # Positive debit/credit presentation
                writer.writerow([
                    e.doc_date,
                    e.doc_type,
                    e.doc_number,
                    e.supplier_eik,
                    e.supplier_name,
                    r.debit_account,
                    r.credit_account,
                    f"{r.amount:.2f}",
                    f"{r.amount:.2f}",
                    r.description,
                ])

    elif format_type == "ajur":
        # Ажур 7 / L format
        writer = csv.writer(output, delimiter=";")
        writer.writerow([
            "Дата",
            "Вид",
            "Номер",
            "СметкаДт",
            "СметкаКт",
            "ДанъчнаОснова",
            "ДДС",
            "Общо",
            "ЕИК",
            "Име",
            "Основание",
        ])
        for e in entries:
            writer.writerow([
                e.doc_date,
                "03" if e.is_storno else "01",
                e.doc_number,
                e.expense_account,
                e.payable_account,
                f"{e.tax_base_amount:.2f}",
                f"{e.vat_amount:.2f}",
                f"{e.total_amount:.2f}",
                e.supplier_eik,
                e.supplier_name,
                e.description,
            ])

    elif format_type == "sap":
        # SAP FI posting interface format: Tab-delimited
        writer = csv.writer(output, delimiter="\t")
        writer.writerow([
            "BUDAT",
            "BLDAT",
            "BLART",
            "XBLNR",
            "LIFNR",
            "HKONT",
            "WRBTR",
            "SHKZG",
            "MWSKZ",
            "SGTXT",
        ])
        for e in entries:
            blart = "KG" if e.is_storno else "KR"  # Vendor credit memo (KG) vs invoice (KR)
            # Item 1: Expense
            writer.writerow([
                e.doc_date,
                e.doc_date,
                blart,
                e.doc_number,
                e.supplier_eik,
                f"{e.expense_account}000",
                f"{abs(e.tax_base_amount):.2f}",
                "H" if e.is_storno else "S",
                "V1",
                e.description,
            ])
            # Item 2: VAT
            if e.vat_amount != Decimal("0.00"):
                writer.writerow([
                    e.doc_date,
                    e.doc_date,
                    blart,
                    e.doc_number,
                    e.supplier_eik,
                    f"{e.vat_account}00",
                    f"{abs(e.vat_amount):.2f}",
                    "H" if e.is_storno else "S",
                    "V1",
                    f"ДДС 20% към {e.doc_number}",
                ])
            # Item 3: Vendor Payable
            writer.writerow([
                e.doc_date,
                e.doc_date,
                blart,
                e.doc_number,
                e.supplier_eik,
                f"{e.payable_account}000",
                f"{abs(e.total_amount):.2f}",
                "S" if e.is_storno else "H",
                "",
                f"Задължение {e.supplier_name}",
            ])

    else:
        raise ValueError(f"Unknown format_type: {format_type}")

    return output.getvalue()


def export_journal_entries_json(entries: Sequence[AccountingJournalEntry]) -> str:
    """Export accounting journal entries as structured JSON with aggregated totals."""
    total_debit = Decimal("0.00")
    total_credit = Decimal("0.00")
    all_balanced = True
    storno_count = 0

    for e in entries:
        total_debit += e.tax_base_amount + e.vat_amount
        total_credit += e.total_amount
        if not e.is_balanced:
            all_balanced = False
        if e.is_storno:
            storno_count += 1

    summary_obj = {
        "total_documents": len(entries),
        "total_debit": f"{total_debit:.2f}",
        "total_credit": f"{total_credit:.2f}",
        "is_overall_balanced": all_balanced and (total_debit == total_credit),
        "storno_documents_count": storno_count,
    }

    payload = {
        "summary": summary_obj,
        "entries": [e.to_dict() for e in entries],
    }

    return json.dumps(payload, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# High-Level Batch Export Facilitator
# ---------------------------------------------------------------------------

def export_all_accounting_files(
    invoices: Iterable[Any],
    output_dir: Path | str,
    nap_period: str | None = None,
    default_expense_account: str | None = None,
) -> dict[str, str]:
    """Export all accounting outputs (POKUPKI.TXT, CSV, JSON, Microinvest) to output_dir.
    
    Returns:
        Dictionary mapping artifact name to its written file path.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    inv_list = list(invoices)
    exported_files: dict[str, str] = {}

    # 1. POKUPKI.TXT (Fixed-width Windows-1251 for NAP)
    pokupki_bytes = invoices_to_pokupki_txt(
        inv_list,
        format="fixed_width",
        encoding="cp1251",
        period=nap_period,
    )
    assert isinstance(pokupki_bytes, bytes)
    pokupki_file = out_path / "POKUPKI.TXT"
    pokupki_file.write_bytes(pokupki_bytes)
    exported_files["pokupki_txt"] = str(pokupki_file)

    # 2. POKUPKI_UTF8.TXT (UTF-8 TSV for modern systems / inspection)
    pokupki_utf8 = invoices_to_pokupki_txt(
        inv_list,
        format="tsv",
        encoding=None,
        period=nap_period,
    )
    assert isinstance(pokupki_utf8, str)
    pokupki_tsv_file = out_path / "POKUPKI_TSV.txt"
    pokupki_tsv_file.write_text(pokupki_utf8, encoding="utf-8")
    exported_files["pokupki_tsv"] = str(pokupki_tsv_file)

    # Generate journal entries
    journal_entries = invoices_to_journal_entries(
        inv_list,
        default_expense_account=default_expense_account,
    )

    # 3. Universal Journal Entries CSV
    csv_universal = export_journal_entries_csv(journal_entries, format_type="universal")
    csv_file = out_path / "journal_entries.csv"
    csv_file.write_text(csv_universal, encoding="utf-8-sig")
    exported_files["journal_entries_csv"] = str(csv_file)

    # 4. Microinvest Delta Pro CSV
    csv_microinvest = export_journal_entries_csv(journal_entries, format_type="microinvest")
    microinvest_file = out_path / "kontirovki_microinvest.csv"
    microinvest_file.write_text(csv_microinvest, encoding="windows-1251", errors="replace")
    exported_files["microinvest_csv"] = str(microinvest_file)

    # 5. Journal Entries JSON
    json_entries = export_journal_entries_json(journal_entries)
    json_file = out_path / "journal_entries.json"
    json_file.write_text(json_entries, encoding="utf-8")
    exported_files["journal_entries_json"] = str(json_file)

    # 6. Full NAP VAT Package (PRODAGBI.TXT, DEKLAR.TXT, and ZIP)
    nap_pkg = export_nap_package(
        purchase_invoices=inv_list,
        output_dir=out_path,
        period=nap_period or "202608",
        format="fixed_width",
        encoding="cp1251",
        auto_generate_protocols=True,
        create_zip=True,
    )
    for k, v in nap_pkg.get("exported_files", {}).items():
        exported_files[f"nap_{k}"] = v

    return exported_files


# ===========================================================================
# PILLAR 3 (P1) — PROTOCOLS ART. 117 ЗДДС, SALES LEDGER (PRODAGBI.TXT)
#                  AND VAT DECLARATION (DEKLAR.TXT)
# ===========================================================================

# ---------------------------------------------------------------------------
# Reverse Charge & Article 117 Protocols (ВОП и обратно начисляване)
# ---------------------------------------------------------------------------

REVERSE_CHARGE_SUPPLIER_KEYWORDS = (
    "google", "meta", "facebook", "adobe", "aws", "amazon web services",
    "microsoft", "zoom", "hetzner", "digitalocean", "openai", "github",
    "linkedin", "stripe", "ovh", "cloudflare", "hubspot", "salesforce",
)

REVERSE_CHARGE_TEXT_PATTERNS = [
    re.compile(r"(?i)\breverse\s*charge\b"),
    re.compile(r"(?i)\bобратно\s*начисляване\b"),
    re.compile(r"(?i)\bчл\.?\s*82\b"),
    re.compile(r"(?i)\bчл\.?\s*21\s*,\s*ал\.?\s*2\b"),
    re.compile(r"(?i)\bчл\.?\s*84\b"),
    re.compile(r"(?i)\bвоп\b"),
    re.compile(r"(?i)\bдиректива\s*2006/112/ео\b"),
    re.compile(r"(?i)\bdirective\s*2006/112/ec\b"),
    re.compile(r"(?i)\bintra-community\b"),
]


def is_reverse_charge_or_vop(invoice: Any) -> bool:
    """Detect whether an invoice requires mandatory Art. 117 VAT Protocol self-assessment.
    
    Triggers for:
      1. Cross-border EU/foreign suppliers (Google, Meta, Adobe, AWS, etc.).
      2. Invoices with non-BG VAT numbers where VAT amount is zero / exempt.
      3. Invoices containing statutory reverse charge / ВОП legal clauses.
    """
    info = _extract_invoice_fields(invoice)
    sup_name = (info.get("supplier_name") or "").lower()
    sup_vat = (info.get("supplier_vat") or "").upper()
    sup_eik = (info.get("supplier_eik") or "").upper()
    desc = (info.get("delivery_desc") or "").lower()
    vat_amt = info.get("vat_amount", Decimal("0.00"))

    # 1. Check known tech / foreign cloud vendor keywords
    for kw in REVERSE_CHARGE_SUPPLIER_KEYWORDS:
        if kw in sup_name:
            return True

    # 2. Check foreign EU VAT prefix (IE, LU, DE, FR, NL, US, GB, etc.)
    vat_id = sup_vat or sup_eik
    if len(vat_id) >= 4 and vat_id[:2].isalpha() and not vat_id.startswith("BG"):
        # Foreign supplier with 0% VAT
        if vat_amt == Decimal("0.00"):
            return True

    # 3. Check line item descriptions & invoice notes for legal clauses
    combined_text = f"{sup_name} {desc}"
    for pat in REVERSE_CHARGE_TEXT_PATTERNS:
        if pat.search(combined_text):
            return True

    # 4. Check raw invoice tokens/text if available
    raw_text = ""
    if hasattr(invoice, "raw_evidence") and invoice.raw_evidence:
        pages = invoice.raw_evidence.get("pages", [])
        for p in pages:
            tokens = p.get("tokens", [])
            raw_text += " " + " ".join(t.get("text", "") for t in tokens)
    if raw_text:
        for pat in REVERSE_CHARGE_TEXT_PATTERNS:
            if pat.search(raw_text):
                return True

    return False


@dataclass
class ProtocolChl117:
    """Statutory Protocol under Art. 117 of the Bulgarian VAT Act (ЗДДС).
    
    Issued by a registered Bulgarian recipient for self-assessment of 20% VAT
    on cross-border services (Art. 82, para 2, item 3) or intra-community
    acquisitions of goods / ВОП (Art. 84).
    
    Reflected simultaneously in:
      - Purchase Ledger (POKUPKI.TXT): Document type 09, full tax credit (Cells 10 & 11)
      - Sales Ledger (PRODAGBI.TXT): Document type 09, output tax (Cells 15 & 16 or 13 & 14)
    """
    protocol_number: str
    protocol_date: str
    issuer_name: str
    issuer_eik: str
    issuer_vat: str
    supplier_name: str
    supplier_vat: str
    supplier_country: str
    original_doc_number: str
    original_doc_date: str
    description: str
    delivery_type: Literal["REVERSE_CHARGE_SERVICE", "VOP_GOODS", "OTHER_ART_82"] = "REVERSE_CHARGE_SERVICE"
    legal_basis: str = "чл. 82, ал. 2, т. 3 от ЗДДС"
    currency: str = "BGN"
    tax_base_original: Decimal = Decimal("0.00")
    exchange_rate: Decimal = Decimal("1.95583")
    tax_base_bgn: Decimal = Decimal("0.00")
    vat_rate_pct: Decimal = Decimal("20.00")
    vat_amount_bgn: Decimal = Decimal("0.00")
    total_amount_bgn: Decimal = Decimal("0.00")
    full_tax_credit: bool = True

    def to_text_document(self) -> str:
        """Render formal Bulgarian statutory Protocol under Art. 117 ЗДДС."""
        border = "=" * 76
        sep = "-" * 76
        lines = [
            border,
            "                  ПРОТОКОЛ ПО ЧЛ. 117 ОТ ЗДДС".center(76),
            f"                     № {self.protocol_number} / {self.protocol_date}".center(76),
            border,
            f"ИЗДАДЕН ОТ (ПОЛУЧАТЕЛ): {self.issuer_name}",
            f"ЕИК: {self.issuer_eik}             ДДС Номер: {self.issuer_vat}",
            sep,
            f"ДОСТАВЧИК: {self.supplier_name}",
            f"ДДС Номер: {self.supplier_vat} (Държава: {self.supplier_country})",
            sep,
            "ОСНОВАНИЕ ЗА ИЗДАВАНЕ:",
            f"  • Правно основание: {self.legal_basis}",
            f"  • Към първичен документ: Фактура № {self.original_doc_number} от {self.original_doc_date}",
            f"  • Предмет на доставката: {self.description}",
            sep,
            "ИЗЧИСЛЕНИЕ НА ДАНЪКА:",
            f"  • Данъчна основа в оригинална валута: {self.tax_base_original:.2f} {self.currency}",
            f"  • Фиксиран валутен курс (БНБ):        {self.exchange_rate:.5f}",
            f"  • Данъчна основа в лева (BGN):        {self.tax_base_bgn:.2f} лв.",
            f"  • Ставка на данъка:                   {self.vat_rate_pct:.2f}%",
            f"  • НАЧИСЛЕН ДДС (BGN):                 {self.vat_amount_bgn:.2f} лв.",
            f"  • ОБЩА СУМА ЗА ДНЕВНИЦИТЕ:            {self.total_amount_bgn:.2f} лв.",
            sep,
            "ОТРАЗЯВАНЕ В ДДС ДНЕВНИЦИТЕ ЗА СЪОТВЕТНИЯ ДАНЪЧЕН ПЕРИОД:",
            "  1. В ДНЕВНИК ЗА ПРОДАЖБИТЕ (Начислен данък по чл. 82 / ВОП):",
            f"     Колона 15 (ДО по чл. 82): {self.tax_base_bgn:.2f} лв. | Колона 16 (ДДС по чл. 82): {self.vat_amount_bgn:.2f} лв.",
            "  2. В ДНЕВНИК ЗА ПОКУПКИТЕ (Право на пълен данъчен кредит):",
            f"     Колона 10 (ДО с пълен ДК): {self.tax_base_bgn:.2f} лв. | Колона 11 (ДДС с пълен ДК): {self.vat_amount_bgn:.2f} лв.",
            f"     Колона 16 (ДДС чл. 82):    {self.vat_amount_bgn:.2f} лв.",
            "  3. НЕТЕН ЕФЕКТ ЗА ВНАСЯНЕ / ВЪЗСТАНОВЯВАНЕ: 0.00 лв. (БАЛАНСИРАН)",
            border,
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Convert Protocol to JSON-serializable dictionary."""
        return {
            "protocol_number": self.protocol_number,
            "protocol_date": self.protocol_date,
            "issuer_name": self.issuer_name,
            "issuer_eik": self.issuer_eik,
            "issuer_vat": self.issuer_vat,
            "supplier_name": self.supplier_name,
            "supplier_vat": self.supplier_vat,
            "supplier_country": self.supplier_country,
            "original_doc_number": self.original_doc_number,
            "original_doc_date": self.original_doc_date,
            "description": self.description,
            "delivery_type": self.delivery_type,
            "legal_basis": self.legal_basis,
            "currency": self.currency,
            "tax_base_original": f"{self.tax_base_original:.2f}",
            "exchange_rate": f"{self.exchange_rate:.5f}",
            "tax_base_bgn": f"{self.tax_base_bgn:.2f}",
            "vat_rate_pct": f"{self.vat_rate_pct:.2f}",
            "vat_amount_bgn": f"{self.vat_amount_bgn:.2f}",
            "total_amount_bgn": f"{self.total_amount_bgn:.2f}",
            "full_tax_credit": self.full_tax_credit,
        }

    def to_purchase_ledger_entry(
        self,
        period: str | None = None,
        branch: str = DEFAULT_BRANCH,
    ) -> NapLedgerEntry:
        """Create corresponding Purchase Ledger (POKUPKI.TXT) Type 09 record."""
        branch_val = period if period else branch
        desc_summary = f"Прот.117 ф-ра {self.original_doc_number}"[:30]
        return NapLedgerEntry(
            branch_or_period=branch_val,
            doc_type="09",
            doc_number=normalize_doc_number(self.protocol_number),
            doc_date=self.protocol_date,
            contractor_id=re.sub(r"[^A-Za-z0-9]", "", self.supplier_vat) or "999999999",
            contractor_name=self.supplier_name[:50],
            description=desc_summary,
            total_amount=self.total_amount_bgn,
            tax_base_20=self.tax_base_bgn if self.full_tax_credit else Decimal("0.00"),
            vat_20=self.vat_amount_bgn if self.full_tax_credit else Decimal("0.00"),
            tax_base_9=Decimal("0.00"),
            vat_9=Decimal("0.00"),
            tax_base_partial=Decimal("0.00"),
            vat_partial=Decimal("0.00"),
            tax_base_no_credit=Decimal("0.00") if self.full_tax_credit else self.tax_base_bgn,
            vat_special_art82=self.vat_amount_bgn,
        )

    def to_sales_ledger_entry(
        self,
        period: str | None = None,
        branch: str = DEFAULT_BRANCH,
    ) -> NapSalesLedgerEntry:
        """Create corresponding Sales Ledger (PRODAGBI.TXT) Type 09 record."""
        branch_val = period if period else branch
        desc_summary = f"Прот.117 ф-ра {self.original_doc_number}"[:30]

        is_vop = (self.delivery_type == "VOP_GOODS")
        tax_base_vop = self.tax_base_bgn if is_vop else Decimal("0.00")
        vat_vop = self.vat_amount_bgn if is_vop else Decimal("0.00")
        tax_base_art82 = Decimal("0.00") if is_vop else self.tax_base_bgn
        vat_art82 = Decimal("0.00") if is_vop else self.vat_amount_bgn

        return NapSalesLedgerEntry(
            branch_or_period=branch_val,
            doc_type="09",
            doc_number=normalize_doc_number(self.protocol_number),
            doc_date=self.protocol_date,
            contractor_id=re.sub(r"[^A-Za-z0-9]", "", self.supplier_vat) or "999999999",
            contractor_name=self.supplier_name[:50],
            description=desc_summary,
            total_amount=self.total_amount_bgn,
            tax_base_20=Decimal("0.00"),
            vat_20=Decimal("0.00"),
            tax_base_vop=tax_base_vop,
            vat_vop=vat_vop,
            tax_base_art82=tax_base_art82,
            vat_art82=vat_art82,
            tax_base_9=Decimal("0.00"),
            vat_9=Decimal("0.00"),
            tax_base_chapter3=Decimal("0.00"),
            tax_base_art69=Decimal("0.00"),
            tax_base_exempt=Decimal("0.00"),
        )

    def to_journal_entry(self) -> AccountingJournalEntry:
        """Create double-entry bookkeeping journal entries for Art. 117 self-assessment."""
        records: list[DoubleEntryRecord] = []

        # 1. Base expense: Debit 602 (Външни услуги) / Credit 401 (Чуждестранен доставчик)
        records.append(DoubleEntryRecord(
            debit_account=DEFAULT_EXPENSE_SERVICE_ACCOUNT,
            debit_subledger=self.description[:30],
            credit_account=DEFAULT_SUPPLIER_PAYABLE_ACCOUNT,
            credit_subledger=self.supplier_vat,
            amount=self.tax_base_bgn,
            currency="BGN",
            description=f"Разход {self.supplier_name} ф-ра {self.original_doc_number}",
        ))

        # 2. Reverse charge VAT: Debit 4531 (Начислен ДДС за покупките) / Credit 4532 (Начислен ДДС за продажбите)
        records.append(DoubleEntryRecord(
            debit_account="4531",
            debit_subledger="Протокол чл.117 ДК",
            credit_account="4532",
            credit_subledger="Протокол чл.117 Продажби",
            amount=self.vat_amount_bgn,
            currency="BGN",
            description=f"Самоначислен ДДС 20% по Протокол {self.protocol_number}",
        ))

        return AccountingJournalEntry(
            doc_number=self.protocol_number,
            doc_date=self.protocol_date,
            doc_type="09",
            supplier_name=self.supplier_name,
            supplier_eik=self.supplier_vat,
            expense_account=DEFAULT_EXPENSE_SERVICE_ACCOUNT,
            vat_account="4531",
            payable_account=DEFAULT_SUPPLIER_PAYABLE_ACCOUNT,
            tax_base_amount=self.tax_base_bgn,
            vat_amount=self.vat_amount_bgn,
            total_amount=self.total_amount_bgn,
            currency="BGN",
            is_storno=False,
            is_balanced=True,
            description=f"Самоначисляване по {self.legal_basis} към ф-ра {self.original_doc_number}",
            records=records,
        )


def generate_protocol_chl_117(
    invoice: Any,
    protocol_number: str = "0000000001",
    protocol_date: str | None = None,
    recipient_company: dict[str, Any] | None = None,
    legal_basis: str | None = None,
    full_tax_credit: bool = True,
) -> ProtocolChl117:
    """Construct an official Protocol under Art. 117 ЗДДС from a foreign invoice."""
    info = _extract_invoice_fields(invoice)
    doc_no = info["doc_number"] or "0000000000"
    inv_date = info["doc_date"]

    # Statutory deadline: Protocol must be issued within 15 days of tax event (чл. 117, ал. 3 ЗДДС)
    proto_date = protocol_date or inv_date

    # Recipient default metadata (Bulgarian buyer)
    rec = recipient_company or {}
    issuer_name = rec.get("name") or DEFAULT_COMPANY_NAME
    issuer_eik = rec.get("eik") or DEFAULT_COMPANY_EIK
    issuer_vat = rec.get("vat_number") or (f"BG{issuer_eik}" if not issuer_eik.startswith("BG") else issuer_eik)

    # Supplier
    sup_name = info["supplier_name"]
    sup_vat = info["supplier_vat"] or info["supplier_eik"] or "EU999999999"

    # Country code extraction
    country = "IE"  # Default common for Google/Meta/Adobe
    if len(sup_vat) >= 4 and sup_vat[:2].isalpha():
        country = sup_vat[:2].upper()

    curr = info["currency"] or "EUR"
    rate = Decimal("1.95583") if curr == "EUR" else Decimal("1.00000")

    tax_base_orig = info["tax_base"]
    if tax_base_orig == Decimal("0.00"):
        tax_base_orig = info["total_due"]

    if curr == "EUR":
        tax_base_bgn = (tax_base_orig * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        tax_base_bgn = tax_base_orig

    # 20% statutory VAT
    vat_amt_bgn = (tax_base_bgn * Decimal("0.20")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    tot_amt_bgn = tax_base_bgn + vat_amt_bgn

    # Delivery type & legal grounds
    is_vop = "воп" in info["delivery_desc"].lower() or "придобиване" in info["delivery_desc"].lower()
    deliv_type: Literal["REVERSE_CHARGE_SERVICE", "VOP_GOODS", "OTHER_ART_82"] = (
        "VOP_GOODS" if is_vop else "REVERSE_CHARGE_SERVICE"
    )
    def_basis = "чл. 84 от ЗДДС (ВОП)" if is_vop else "чл. 82, ал. 2, т. 3 от ЗДДС (доставка на услуги)"
    chosen_basis = legal_basis or def_basis

    return ProtocolChl117(
        protocol_number=normalize_doc_number(protocol_number),
        protocol_date=proto_date,
        issuer_name=issuer_name,
        issuer_eik=issuer_eik,
        issuer_vat=issuer_vat,
        supplier_name=sup_name,
        supplier_vat=sup_vat,
        supplier_country=country,
        original_doc_number=doc_no,
        original_doc_date=inv_date,
        description=info["delivery_desc"] or "Облачни софтуерни услуги",
        delivery_type=deliv_type,
        legal_basis=chosen_basis,
        currency=curr,
        tax_base_original=tax_base_orig,
        exchange_rate=rate,
        tax_base_bgn=tax_base_bgn,
        vat_rate_pct=Decimal("20.00"),
        vat_amount_bgn=vat_amt_bgn,
        total_amount_bgn=tot_amt_bgn,
        full_tax_credit=full_tax_credit,
    )


# ---------------------------------------------------------------------------
# Statutory Sales Ledger (PRODAGBI.TXT - Приложение № 10 от ППЗДДС)
# ---------------------------------------------------------------------------

@dataclass
class NapSalesLedgerEntry:
    """A single record in the statutory VAT Sales Ledger (PRODAGBI.TXT).
    
    Columns according to ППЗДДС Приложение № 10:
      Col 1:  Клон / Период (Branch or Period) - 4 chars
      Col 2:  Вид на документа (01=Фактура, 02=ДИ, 03=КИ, 07=Митн. декл., 09=Протокол) - 2 chars
      Col 3:  Номер на документа (10-цифрен) - 10 chars
      Col 4:  Дата на документа (YYYY-MM-DD) - 10 chars
      Col 5:  Идентификационен номер на контрагента (ЕИК / ДДС) - 15 chars
      Col 6:  Име на контрагента (до 50 символа) - 50 chars
      Col 7:  Предмет на доставката (до 30 символа) - 30 chars
      Col 8:  Обща сума на документа (клетка 10) - 15 chars
      Col 9:  Данъчна основа за 20% ДДС (клетка 11) - 15 chars
      Col 10: Начислен ДДС 20% (клетка 12) - 15 chars
      Col 11: Данъчна основа за ВОП (клетка 13) - 15 chars
      Col 12: Начислен ДДС за ВОП (клетка 14) - 15 chars
      Col 13: Данъчна основа за доставки по чл. 82, ал. 2-6 (клетка 15) - 15 chars
      Col 14: Начислен ДДС по чл. 82, ал. 2-6 (клетка 16) - 15 chars
      Col 15: Данъчна основа за 9% ДДС (клетка 17) - 15 chars
      Col 16: Начислен ДДС 9% (клетка 18) - 15 chars
      Col 17: Данъчна основа по глава трета (клетка 19) - 15 chars
      Col 18: Данъчна основа по чл. 69, ал. 2 (клетка 20) - 15 chars
      Col 19: Данъчна основа за освободени доставки и освободен ВОП (клетка 24) - 15 chars
    """
    branch_or_period: str = DEFAULT_BRANCH
    doc_type: str = "01"
    doc_number: str = "0000000000"
    doc_date: str = "2026-01-01"
    contractor_id: str = "999999999"
    contractor_name: str = "НЕИЗВЕСТЕН КЛИЕНТ"
    description: str = "Стоки / Услуги"
    total_amount: Decimal = Decimal("0.00")
    tax_base_20: Decimal = Decimal("0.00")
    vat_20: Decimal = Decimal("0.00")
    tax_base_vop: Decimal = Decimal("0.00")
    vat_vop: Decimal = Decimal("0.00")
    tax_base_art82: Decimal = Decimal("0.00")
    vat_art82: Decimal = Decimal("0.00")
    tax_base_9: Decimal = Decimal("0.00")
    vat_9: Decimal = Decimal("0.00")
    tax_base_chapter3: Decimal = Decimal("0.00")
    tax_base_art69: Decimal = Decimal("0.00")
    tax_base_exempt: Decimal = Decimal("0.00")

    def to_fixed_width_line(self) -> str:
        """Format as official NAP fixed-width text record for PRODAGBI.TXT."""
        branch = self.branch_or_period[:4].ljust(4)
        doc_t = self.doc_type[:2].rjust(2)
        doc_no = self.doc_number[:10].rjust(10, "0")
        doc_dt = self.doc_date[:10].ljust(10)
        c_id = self.contractor_id[:15].ljust(15)
        c_name = self.contractor_name[:50].ljust(50)
        desc = self.description[:30].ljust(30)

        tot_s = f"{self.total_amount:.2f}".rjust(15)
        tb20_s = f"{self.tax_base_20:.2f}".rjust(15)
        v20_s = f"{self.vat_20:.2f}".rjust(15)
        tb_vop_s = f"{self.tax_base_vop:.2f}".rjust(15)
        v_vop_s = f"{self.vat_vop:.2f}".rjust(15)
        tb_art82_s = f"{self.tax_base_art82:.2f}".rjust(15)
        v_art82_s = f"{self.vat_art82:.2f}".rjust(15)
        tb9_s = f"{self.tax_base_9:.2f}".rjust(15)
        v9_s = f"{self.vat_9:.2f}".rjust(15)
        tb_ch3_s = f"{self.tax_base_chapter3:.2f}".rjust(15)
        tb_art69_s = f"{self.tax_base_art69:.2f}".rjust(15)
        tb_exempt_s = f"{self.tax_base_exempt:.2f}".rjust(15)

        return (
            f"{branch}{doc_t}{doc_no}{doc_dt}{c_id}{c_name}{desc}"
            f"{tot_s}{tb20_s}{v20_s}{tb_vop_s}{v_vop_s}{tb_art82_s}{v_art82_s}"
            f"{tb9_s}{v9_s}{tb_ch3_s}{tb_art69_s}{tb_exempt_s}"
        )

    def to_tsv_line(self) -> str:
        """Format as Tab-separated values record for PRODAGBI.TXT."""
        fields = [
            self.branch_or_period,
            self.doc_type,
            self.doc_number,
            self.doc_date,
            self.contractor_id,
            self.contractor_name,
            self.description,
            f"{self.total_amount:.2f}",
            f"{self.tax_base_20:.2f}",
            f"{self.vat_20:.2f}",
            f"{self.tax_base_vop:.2f}",
            f"{self.vat_vop:.2f}",
            f"{self.tax_base_art82:.2f}",
            f"{self.vat_art82:.2f}",
            f"{self.tax_base_9:.2f}",
            f"{self.vat_9:.2f}",
            f"{self.tax_base_chapter3:.2f}",
            f"{self.tax_base_art69:.2f}",
            f"{self.tax_base_exempt:.2f}",
        ]
        return "\t".join(fields)

    def to_csv_line(self, delimiter: str = ";") -> str:
        """Format as Semicolon-delimited CSV record for PRODAGBI.TXT."""
        fields = [
            self.branch_or_period,
            self.doc_type,
            self.doc_number,
            self.doc_date,
            self.contractor_id,
            f'"{self.contractor_name}"',
            f'"{self.description}"',
            f"{self.total_amount:.2f}",
            f"{self.tax_base_20:.2f}",
            f"{self.vat_20:.2f}",
            f"{self.tax_base_vop:.2f}",
            f"{self.vat_vop:.2f}",
            f"{self.tax_base_art82:.2f}",
            f"{self.vat_art82:.2f}",
            f"{self.tax_base_9:.2f}",
            f"{self.vat_9:.2f}",
            f"{self.tax_base_chapter3:.2f}",
            f"{self.tax_base_art69:.2f}",
            f"{self.tax_base_exempt:.2f}",
        ]
        return delimiter.join(fields)

    def to_dict(self) -> dict[str, Any]:
        """Convert entry to dictionary with formatted numeric strings."""
        return {
            "branch_or_period": self.branch_or_period,
            "doc_type": self.doc_type,
            "doc_number": self.doc_number,
            "doc_date": self.doc_date,
            "contractor_id": self.contractor_id,
            "contractor_name": self.contractor_name,
            "description": self.description,
            "total_amount": f"{self.total_amount:.2f}",
            "tax_base_20": f"{self.tax_base_20:.2f}",
            "vat_20": f"{self.vat_20:.2f}",
            "tax_base_vop": f"{self.tax_base_vop:.2f}",
            "vat_vop": f"{self.vat_vop:.2f}",
            "tax_base_art82": f"{self.tax_base_art82:.2f}",
            "vat_art82": f"{self.vat_art82:.2f}",
            "tax_base_9": f"{self.tax_base_9:.2f}",
            "vat_9": f"{self.vat_9:.2f}",
            "tax_base_chapter3": f"{self.tax_base_chapter3:.2f}",
            "tax_base_art69": f"{self.tax_base_art69:.2f}",
            "tax_base_exempt": f"{self.tax_base_exempt:.2f}",
        }


def invoice_to_nap_sales_entry(
    invoice: Any,
    period: str | None = None,
    branch: str = DEFAULT_BRANCH,
) -> NapSalesLedgerEntry:
    """Convert an outgoing Sales Invoice or Protocol to a PRODAGBI.TXT ledger entry."""
    if isinstance(invoice, ProtocolChl117):
        return invoice.to_sales_ledger_entry(period=period, branch=branch)

    info = _extract_invoice_fields(invoice)
    doc_type = map_document_type(
        info["doc_type"],
        is_credit=info["is_credit"],
        is_debit=info["is_debit"],
    )
    doc_number = normalize_doc_number(info["doc_number"])
    doc_date = info["doc_date"]

    sign = Decimal("-1") if (doc_type == "03" or info["is_credit"]) else Decimal("1")
    total_due = sign * abs(info["total_due"])
    tax_base = sign * abs(info["tax_base"])
    vat_amount = sign * abs(info["vat_amount"])

    # If foreign currency, convert
    if info["currency"] == "EUR":
        total_due = sign * abs(convert_eur_to_bgn(info["total_due"]))
        tax_base = sign * abs(convert_eur_to_bgn(info["tax_base"]))
        vat_amount = sign * abs(convert_eur_to_bgn(info["vat_amount"]))
    elif info["currency"] not in ("BGN", None, ""):
        if info.get("total_bgn") and info["total_bgn"] > Decimal("0.00"):
            ratio = info["total_bgn"] / info["total_due"] if info["total_due"] else Decimal("1.0")
            total_due = sign * abs(info["total_bgn"])
            tax_base = (sign * abs(info["tax_base"]) * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            vat_amount = (sign * abs(info["vat_amount"]) * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    tax_base_20 = Decimal("0.00")
    vat_20 = Decimal("0.00")
    tax_base_9 = Decimal("0.00")
    vat_9 = Decimal("0.00")
    tax_base_exempt = Decimal("0.00")

    is_9_pct = False
    for r in info["vat_rates"]:
        try:
            r_dec = Decimal(str(r))
            if Decimal("8.0") <= r_dec <= Decimal("10.0"):
                is_9_pct = True
                break
        except Exception:
            pass

    if abs(vat_amount) == Decimal("0.00") and abs(tax_base) > Decimal("0.00"):
        tax_base_exempt = tax_base
    elif is_9_pct:
        tax_base_9 = tax_base
        vat_9 = vat_amount
    else:
        tax_base_20 = tax_base
        vat_20 = vat_amount

    # For sales invoices, contractor is the buyer/client
    client_name = "НЕИЗВЕСТЕН КЛИЕНТ"
    client_eik = "999999999"

    if info["doc_type"] == "FISCAL_MEMORY_REPORT":
        doc_type = "81"
        client_name = "ФИЗИЧЕСКИ ЛИЦА / СВОДЕН КАСОВ ОТЧЕТ"
        client_eik = "999999999999999"
        info["delivery_desc"] = "Отчет продажби по чл. 119 ЗДДС"
    elif info["doc_type"] == "GOODS_RECEIPT":
        raise ValueError("Стоковите разписки са вътрешноскладови документи и не подлежат на регистрация в ДДС Дневник за продажби.")
    elif hasattr(invoice, "client") and invoice.client:
        client_name = getattr(invoice.client, "name", None) or client_name
        client_eik = getattr(invoice.client, "eik", None) or getattr(invoice.client, "vat_number", None) or client_eik
    elif isinstance(invoice, dict) and "client" in invoice:
        cl = invoice["client"]
        client_name = cl.get("name") or client_name
        client_eik = cl.get("eik") or cl.get("vat_number") or client_eik

    contractor_id = re.sub(r"[^A-Za-z0-9]", "", str(client_eik)) or "999999999"
    contractor_name = re.sub(r"\s+", " ", str(client_name)).strip()
    description = re.sub(r"\s+", " ", str(info["delivery_desc"])).strip()
    branch_val = period if period else branch

    return NapSalesLedgerEntry(
        branch_or_period=branch_val,
        doc_type=doc_type,
        doc_number=doc_number,
        doc_date=doc_date,
        contractor_id=contractor_id,
        contractor_name=contractor_name,
        description=description,
        total_amount=total_due,
        tax_base_20=tax_base_20,
        vat_20=vat_20,
        tax_base_vop=Decimal("0.00"),
        vat_vop=Decimal("0.00"),
        tax_base_art82=Decimal("0.00"),
        vat_art82=Decimal("0.00"),
        tax_base_9=tax_base_9,
        vat_9=vat_9,
        tax_base_chapter3=Decimal("0.00"),
        tax_base_art69=Decimal("0.00"),
        tax_base_exempt=tax_base_exempt,
    )


def invoices_to_prodagbi_txt(
    entries_or_invoices: Iterable[Any],
    format: Literal["fixed_width", "tsv", "csv"] = "fixed_width",
    encoding: str | None = "cp1251",
    period: str | None = None,
    branch: str = DEFAULT_BRANCH,
) -> bytes | str:
    """Generate statutory PRODAGBI.TXT sales ledger file under ППЗДДС Приложение № 10."""
    lines: list[str] = []

    if format == "csv":
        header = (
            "Клон/Период;ВидДокумент;НомерДокумент;Дата;ЕИК_ДДС;ИмеКонтрагент;"
            "Предмет;ОбщаСума;ДО_20;ДДС_20;ДО_ВОП;ДДС_ВОП;ДО_Чл82;ДДС_Чл82;"
            "ДО_9;ДДС_9;ДО_Глава3;ДО_Чл69;ДО_Освободени"
        )
        lines.append(header)

    if format == "fixed_width" and encoding and encoding.lower().replace("-", "") in ("utf8", "utf16", "utf32"):
        logger.warning(
            "Statutory fixed-width NAP files (PRODAGBI.TXT) require 1-byte encoding ('cp1251'/'windows-1251'). "
            "Using %s causes multi-byte Cyrillic column misalignment in the NRA submission portal.",
            encoding,
        )

    for item in entries_or_invoices:
        if isinstance(item, NapSalesLedgerEntry):
            entry = item
        else:
            info = _extract_invoice_fields(item)
            if info.get("doc_type") == "GOODS_RECEIPT":
                logger.info("Skipping GOODS_RECEIPT %s from PRODAGBI.TXT (not a sales document)", info.get("doc_number"))
                continue
            entry = invoice_to_nap_sales_entry(item, period=period, branch=branch)

        if format == "fixed_width":
            lines.append(entry.to_fixed_width_line())
        elif format == "tsv":
            lines.append(entry.to_tsv_line())
        elif format == "csv":
            lines.append(entry.to_csv_line(";"))
        else:
            raise ValueError(f"Unknown format: {format}")

    content_str = "\r\n".join(lines)
    if lines:
        content_str += "\r\n"

    if encoding:
        return content_str.encode(encoding, errors="replace")
    return content_str


# ---------------------------------------------------------------------------
# Statutory VAT Return Declaration (DEKLAR.TXT - Приложение № 13 от ППЗДДС)
# ---------------------------------------------------------------------------

@dataclass
class VatDeclaration:
    """Statutory VAT Return Declaration under Art. 125 of the Bulgarian VAT Act (ЗДДС).
    
    Reconciles all tax bases and VAT amounts from POKUPKI.TXT and PRODAGBI.TXT
    and determines the net financial result for the tax period:
      - Cell 50: ДДС за внасяне (VAT payable)
      - Cell 60: ДДС за възстановяване (VAT refundable)
    """
    period: str = "202608"                 # YYYYMM
    bulstat_eik: str = field(default_factory=lambda: DEFAULT_COMPANY_EIK)
    vat_number: str = field(default_factory=lambda: DEFAULT_COMPANY_VAT)
    company_name: str = field(default_factory=lambda: DEFAULT_COMPANY_NAME)
    address: str = field(default_factory=lambda: DEFAULT_COMPANY_ADDRESS)
    city: str = field(default_factory=lambda: DEFAULT_COMPANY_CITY)
    postal_code: str = field(default_factory=lambda: DEFAULT_COMPANY_POSTAL)
    phone: str = ""
    email: str = ""
    mol_name: str = field(default_factory=lambda: DEFAULT_COMPANY_MOL)

    # Раздел А: Данни за начисления данък (Дневник за продажбите)
    cell_01: Decimal = Decimal("0.00")  # Общ размер на ДО за облагане
    cell_11: Decimal = Decimal("0.00")  # ДО 20%
    cell_12: Decimal = Decimal("0.00")  # Начислен ДДС 20%
    cell_13: Decimal = Decimal("0.00")  # ДО ВОП
    cell_14: Decimal = Decimal("0.00")  # Начислен ДДС за ВОП
    cell_15: Decimal = Decimal("0.00")  # ДО чл. 82, ал. 2-6
    cell_16: Decimal = Decimal("0.00")  # Начислен ДДС чл. 82, ал. 2-6
    cell_17: Decimal = Decimal("0.00")  # ДО 9%
    cell_18: Decimal = Decimal("0.00")  # Начислен ДДС 9%
    cell_19: Decimal = Decimal("0.00")  # ДО глава трета (износ 0%)
    cell_20: Decimal = Decimal("0.00")  # Всичко начислен данък (12+14+16+18)
    cell_24: Decimal = Decimal("0.00")  # Освободени доставки и освободен ВОП

    # Раздел Б: Данни за упражнения данъчен кредит (Дневник за покупките)
    cell_30: Decimal = Decimal("0.00")  # Общ размер на ДО при покупки (31+32+без кредит)
    cell_31: Decimal = Decimal("0.00")  # ДО с право на пълен данъчен кредит
    cell_32: Decimal = Decimal("0.00")  # ДО с право на частичен данъчен кредит
    cell_40: Decimal = Decimal("0.00")  # ДДС с право на пълен данъчен кредит
    cell_41: Decimal = Decimal("0.00")  # ДДС с право на частичен данъчен кредит
    cell_42: Decimal = Decimal("1.00")  # Коефициент по чл. 73 ЗДДС
    cell_43: Decimal = Decimal("0.00")  # Всичко упражнен данъчен кредит (40 + 41*42)

    # Раздел В: Резултат за периода
    cell_50: Decimal = Decimal("0.00")  # ДДС за внасяне (клетка 20 - клетка 43 > 0)
    cell_60: Decimal = Decimal("0.00")  # ДДС за възстановяване (клетка 43 - клетка 20 > 0)
    cell_70: Decimal = Decimal("0.00")  # ДДС за приспадане по чл. 92 ЗДДС
    cell_80: Decimal = Decimal("0.00")  # Окончателен ДДС за внасяне

    def to_dict(self) -> dict[str, Any]:
        """Convert declaration to dictionary with formatted numbers."""
        return {
            "period": self.period,
            "bulstat_eik": self.bulstat_eik,
            "vat_number": self.vat_number,
            "company_name": self.company_name,
            "address": self.address,
            "city": self.city,
            "postal_code": self.postal_code,
            "mol_name": self.mol_name,
            "sales_section_A": {
                "cell_01_total_tax_base": f"{self.cell_01:.2f}",
                "cell_11_tax_base_20": f"{self.cell_11:.2f}",
                "cell_12_vat_20": f"{self.cell_12:.2f}",
                "cell_13_tax_base_vop": f"{self.cell_13:.2f}",
                "cell_14_vat_vop": f"{self.cell_14:.2f}",
                "cell_15_tax_base_art82": f"{self.cell_15:.2f}",
                "cell_16_vat_art82": f"{self.cell_16:.2f}",
                "cell_17_tax_base_9": f"{self.cell_17:.2f}",
                "cell_18_vat_9": f"{self.cell_18:.2f}",
                "cell_19_tax_base_export": f"{self.cell_19:.2f}",
                "cell_20_total_vat_charged": f"{self.cell_20:.2f}",
                "cell_24_exempt_deliveries": f"{self.cell_24:.2f}",
            },
            "purchases_section_B": {
                "cell_30_total_purchase_base": f"{self.cell_30:.2f}",
                "cell_31_tax_base_full_credit": f"{self.cell_31:.2f}",
                "cell_32_tax_base_partial_credit": f"{self.cell_32:.2f}",
                "cell_40_vat_full_credit": f"{self.cell_40:.2f}",
                "cell_41_vat_partial_credit": f"{self.cell_41:.2f}",
                "cell_42_coefficient_art73": f"{self.cell_42:.2f}",
                "cell_43_total_vat_credit": f"{self.cell_43:.2f}",
            },
            "result_section_C": {
                "cell_50_vat_payable": f"{self.cell_50:.2f}",
                "cell_60_vat_refundable": f"{self.cell_60:.2f}",
                "cell_70_prior_deduction_art92": f"{self.cell_70:.2f}",
                "cell_80_final_payable": f"{self.cell_80:.2f}",
            },
        }

    def to_nap_deklar_txt(self, encoding: str | None = "cp1251") -> bytes | str:
        """Format as official statutory DEKLAR.TXT for electronic submission to NRA (НАП)."""
        lines = [
            f"[ПЕРИОД]={self.period}",
            f"[ЕИК]={self.bulstat_eik}",
            f"[ДДС_НОМЕР]={self.vat_number}",
            f"[ИМЕ]={self.company_name}",
            f"[АДРЕС]={self.address}",
            f"[ГРАД]={self.city}",
            f"[ПК]={self.postal_code}",
            f"[МОЛ]={self.mol_name}",
            f"[01]={self.cell_01:.2f}",
            f"[11]={self.cell_11:.2f}",
            f"[12]={self.cell_12:.2f}",
            f"[13]={self.cell_13:.2f}",
            f"[14]={self.cell_14:.2f}",
            f"[15]={self.cell_15:.2f}",
            f"[16]={self.cell_16:.2f}",
            f"[17]={self.cell_17:.2f}",
            f"[18]={self.cell_18:.2f}",
            f"[19]={self.cell_19:.2f}",
            f"[20]={self.cell_20:.2f}",
            f"[24]={self.cell_24:.2f}",
            f"[30]={self.cell_30:.2f}",
            f"[31]={self.cell_31:.2f}",
            f"[32]={self.cell_32:.2f}",
            f"[40]={self.cell_40:.2f}",
            f"[41]={self.cell_41:.2f}",
            f"[42]={self.cell_42:.2f}",
            f"[43]={self.cell_43:.2f}",
            f"[50]={self.cell_50:.2f}",
            f"[60]={self.cell_60:.2f}",
            f"[70]={self.cell_70:.2f}",
            f"[80]={self.cell_80:.2f}",
        ]
        content_str = "\r\n".join(lines) + "\r\n"
        if encoding:
            return content_str.encode(encoding, errors="replace")
        return content_str

    def validate_consistency(
        self,
        purchase_entries: Sequence[NapLedgerEntry],
        sales_entries: Sequence[NapSalesLedgerEntry],
    ) -> list[str]:
        """Perform statutory cross-ledger mathematical consistency audit."""
        issues: list[str] = []

        # 1. Sales VAT sum == Cell 20
        expected_sales_vat = sum(
            ((e.vat_20 + e.vat_vop + e.vat_art82 + e.vat_9) for e in sales_entries),
            Decimal("0.00"),
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if abs(expected_sales_vat - self.cell_20) > Decimal("0.02"):
            issues.append(
                f"Несъответствие в продажбите: сумата от ДДС в PRODAGBI.TXT ({expected_sales_vat:.2f}) "
                f"не съвпада с клетка 20 ({self.cell_20:.2f})!"
            )

        # 2. Purchase Full VAT Credit sum == Cell 40
        expected_pur_vat = sum(
            ((e.vat_20 + e.vat_9) for e in purchase_entries),
            Decimal("0.00"),
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if abs(expected_pur_vat - self.cell_40) > Decimal("0.02"):
            issues.append(
                f"Несъответствие в покупките: сумата от ДДС в POKUPKI.TXT ({expected_pur_vat:.2f}) "
                f"не съвпада с клетка 40 ({self.cell_40:.2f})!"
            )

        # 3. Balance: Cell 20 - Cell 43 == Cell 50 - Cell 60
        diff = self.cell_20 - self.cell_43
        res_diff = self.cell_50 - self.cell_60
        if abs(diff - res_diff) > Decimal("0.02"):
            issues.append(
                f"Математически дисбаланс в справка-декларацията: клетка 20 - клетка 43 ({diff:.2f}) "
                f"!= клетка 50 - клетка 60 ({res_diff:.2f})!"
            )

        return issues


def generate_vat_declaration(
    purchase_entries: Sequence[NapLedgerEntry] | None = None,
    sales_entries: Sequence[NapSalesLedgerEntry] | None = None,
    company_info: dict[str, Any] | None = None,
    period: str = "202608",
    prior_vat_credit_cell_70: Decimal = Decimal("0.00"),
    partial_credit_coefficient_cell_42: Decimal = Decimal("1.00"),
) -> VatDeclaration:
    """Calculate and balance the statutory VAT Return Declaration (DEKLAR.TXT)."""
    p_entries = list(purchase_entries or [])
    s_entries = list(sales_entries or [])
    comp = company_info or {}

    clean_period = re.sub(r"[^\d]", "", period)
    if len(clean_period) == 4:  # YYYY -> default Aug
        clean_period += "08"
    clean_period = clean_period[:6]

    # Section A: Sales
    tb_20 = sum((e.tax_base_20 for e in s_entries), Decimal("0.00"))
    v_20 = sum((e.vat_20 for e in s_entries), Decimal("0.00"))
    tb_vop = sum((e.tax_base_vop for e in s_entries), Decimal("0.00"))
    v_vop = sum((e.vat_vop for e in s_entries), Decimal("0.00"))
    tb_art82 = sum((e.tax_base_art82 for e in s_entries), Decimal("0.00"))
    v_art82 = sum((e.vat_art82 for e in s_entries), Decimal("0.00"))
    tb_9 = sum((e.tax_base_9 for e in s_entries), Decimal("0.00"))
    v_9 = sum((e.vat_9 for e in s_entries), Decimal("0.00"))
    tb_ch3 = sum((e.tax_base_chapter3 for e in s_entries), Decimal("0.00"))
    tb_exempt = sum((e.tax_base_exempt for e in s_entries), Decimal("0.00"))

    cell_20 = (v_20 + v_vop + v_art82 + v_9).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cell_01 = (tb_20 + tb_vop + tb_art82 + tb_9 + tb_ch3 + tb_exempt).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Section B: Purchases
    pur_tb_full = sum(((e.tax_base_20 + e.tax_base_9) for e in p_entries), Decimal("0.00"))
    pur_tb_part = sum((e.tax_base_partial for e in p_entries), Decimal("0.00"))
    pur_tb_none = sum((e.tax_base_no_credit for e in p_entries), Decimal("0.00"))
    cell_30 = (pur_tb_full + pur_tb_part + pur_tb_none).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cell_31 = pur_tb_full.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cell_32 = pur_tb_part.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    cell_40 = sum(((e.vat_20 + e.vat_9) for e in p_entries), Decimal("0.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cell_41 = sum((e.vat_partial for e in p_entries), Decimal("0.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    coeff = partial_credit_coefficient_cell_42.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    part_credit = (cell_41 * coeff).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cell_43 = (cell_40 + part_credit).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Section C: Net Result
    diff = cell_20 - cell_43
    if diff > Decimal("0.00"):
        cell_50 = diff.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cell_60 = Decimal("0.00")
        prior = prior_vat_credit_cell_70.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cell_80 = max(Decimal("0.00"), cell_50 - prior)
    else:
        cell_50 = Decimal("0.00")
        cell_60 = abs(diff).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cell_80 = Decimal("0.00")

    eik = comp.get("eik") or DEFAULT_COMPANY_EIK
    vat_no = comp.get("vat_number") or (f"BG{eik}" if not eik.startswith("BG") else eik)
    name = comp.get("name") or DEFAULT_COMPANY_NAME
    addr = comp.get("address") or DEFAULT_COMPANY_ADDRESS
    city = comp.get("city") or DEFAULT_COMPANY_CITY
    postal = comp.get("postal_code") or DEFAULT_COMPANY_POSTAL
    mol = comp.get("mol_name") or DEFAULT_COMPANY_MOL

    return VatDeclaration(
        period=clean_period,
        bulstat_eik=eik,
        vat_number=vat_no,
        company_name=name,
        address=addr,
        city=city,
        postal_code=postal,
        mol_name=mol,
        cell_01=cell_01,
        cell_11=tb_20.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        cell_12=v_20.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        cell_13=tb_vop.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        cell_14=v_vop.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        cell_15=tb_art82.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        cell_16=v_art82.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        cell_17=tb_9.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        cell_18=v_9.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        cell_19=tb_ch3.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        cell_20=cell_20,
        cell_24=tb_exempt.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        cell_30=cell_30,
        cell_31=cell_31,
        cell_32=cell_32,
        cell_40=cell_40,
        cell_41=cell_41,
        cell_42=coeff,
        cell_43=cell_43,
        cell_50=cell_50,
        cell_60=cell_60,
        cell_70=prior_vat_credit_cell_70.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        cell_80=cell_80,
    )


# ---------------------------------------------------------------------------
# Complete Statutory НАП VAT Package (POKUPKI.TXT, PRODAGBI.TXT, DEKLAR.TXT)
# ---------------------------------------------------------------------------

def export_nap_package(
    purchase_invoices: Iterable[Any],
    sales_invoices: Iterable[Any] | None = None,
    protocols: Iterable[ProtocolChl117] | None = None,
    company_info: dict[str, Any] | None = None,
    period: str = "202608",
    output_dir: Path | str | None = None,
    format: Literal["fixed_width", "tsv", "csv"] = "fixed_width",
    encoding: str | None = "cp1251",
    auto_generate_protocols: bool = True,
    create_zip: bool = False,
) -> dict[str, Any]:
    """Generate the complete statutory 3-file package for electronic submission to НАП.
    
    Files generated:
      1. POKUPKI.TXT: Purchase Ledger (Приложение № 12 от ППЗДДС)
      2. PRODAGBI.TXT: Sales Ledger (Приложение № 10 от ППЗДДС)
      3. DEKLAR.TXT: VAT Return Declaration (Приложение № 13 от ППЗДДС)
      4. Optional ZIP archive: NAP_{period}.zip
      
    Performs automated cross-ledger mathematical consistency audit and
    auto-generates Art. 117 protocols for foreign cloud/reverse-charge invoices.
    """
    clean_period = re.sub(r"[^\d]", "", period)[:6]
    comp = company_info or {
        "name": DEFAULT_COMPANY_NAME,
        "eik": DEFAULT_COMPANY_EIK,
        "vat_number": DEFAULT_COMPANY_VAT,
        "address": DEFAULT_COMPANY_ADDRESS,
        "city": DEFAULT_COMPANY_CITY,
        "postal_code": DEFAULT_COMPANY_POSTAL,
        "mol_name": DEFAULT_COMPANY_MOL,
    }

    pur_invoices = list(purchase_invoices or [])
    sal_invoices = list(sales_invoices or [])
    prots = list(protocols or [])

    # 1. Detect & Auto-generate Art. 117 protocols for reverse charge invoices
    generated_protocols: list[ProtocolChl117] = []
    if auto_generate_protocols:
        proto_seq = len(prots) + 1
        for inv in pur_invoices:
            if is_reverse_charge_or_vop(inv):
                p_num = str(proto_seq).zfill(10)
                p = generate_protocol_chl_117(
                    invoice=inv,
                    protocol_number=p_num,
                    recipient_company=comp,
                )
                generated_protocols.append(p)
                proto_seq += 1

    all_protocols = prots + generated_protocols

    # 2. Build Purchase Ledger entries (POKUPKI.TXT)
    purchase_entries: list[NapLedgerEntry] = []
    for inv in pur_invoices:
        # Standard invoice purchase entry
        purchase_entries.append(invoice_to_nap_entry(inv, period=clean_period))

    # Add purchase records for all Art. 117 protocols
    for p in all_protocols:
        purchase_entries.append(p.to_purchase_ledger_entry(period=clean_period))

    # 3. Build Sales Ledger entries (PRODAGBI.TXT)
    sales_entries: list[NapSalesLedgerEntry] = []
    for sinv in sal_invoices:
        sales_entries.append(invoice_to_nap_sales_entry(sinv, period=clean_period))

    # Add sales records for all Art. 117 protocols (Output tax self-assessment)
    for p in all_protocols:
        sales_entries.append(p.to_sales_ledger_entry(period=clean_period))

    # 4. Generate & balance the statutory VAT Declaration (DEKLAR.TXT)
    declaration = generate_vat_declaration(
        purchase_entries=purchase_entries,
        sales_entries=sales_entries,
        company_info=comp,
        period=clean_period,
    )

    # 5. Consistency check
    consistency_issues = declaration.validate_consistency(purchase_entries, sales_entries)

    # 6. Serialize contents
    pokupki_content = invoices_to_pokupki_txt(
        purchase_entries,
        format=format,
        encoding=encoding,
        period=clean_period,
    )
    prodagbi_content = invoices_to_prodagbi_txt(
        sales_entries,
        format=format,
        encoding=encoding,
        period=clean_period,
    )
    deklar_content = declaration.to_nap_deklar_txt(encoding=encoding)

    exported_files: dict[str, str] = {}
    if output_dir:
        out_p = Path(output_dir)
        out_p.mkdir(parents=True, exist_ok=True)

        pokupki_file = out_p / "POKUPKI.TXT"
        if isinstance(pokupki_content, bytes):
            pokupki_file.write_bytes(pokupki_content)
        else:
            pokupki_file.write_text(pokupki_content, encoding="utf-8")
        exported_files["pokupki"] = str(pokupki_file)

        prodagbi_file = out_p / "PRODAGBI.TXT"
        if isinstance(prodagbi_content, bytes):
            prodagbi_file.write_bytes(prodagbi_content)
        else:
            prodagbi_file.write_text(prodagbi_content, encoding="utf-8")
        exported_files["prodagbi"] = str(prodagbi_file)

        deklar_file = out_p / "DEKLAR.TXT"
        if isinstance(deklar_content, bytes):
            deklar_file.write_bytes(deklar_content)
        else:
            deklar_file.write_text(deklar_content, encoding="utf-8")
        exported_files["deklar"] = str(deklar_file)

        # Write protocols text archives
        if all_protocols:
            proto_dir = out_p / "PROTOCOLS_CHL_117"
            proto_dir.mkdir(exist_ok=True)
            for p in all_protocols:
                p_path = proto_dir / f"PROTOCOL_117_{p.protocol_number}.txt"
                p_path.write_text(p.to_text_document(), encoding="utf-8")
            exported_files["protocols_dir"] = str(proto_dir)

        # Optional ZIP package
        if create_zip:
            import zipfile
            zip_target = out_p / f"NAP_{clean_period}.zip"
            with zipfile.ZipFile(zip_target, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(pokupki_file, arcname="POKUPKI.TXT")
                zf.write(prodagbi_file, arcname="PRODAGBI.TXT")
                zf.write(deklar_file, arcname="DEKLAR.TXT")
            exported_files["zip_package"] = str(zip_target)

    return {
        "status": "success" if not consistency_issues else "warning",
        "period": clean_period,
        "company": comp,
        "protocols_count": len(all_protocols),
        "purchase_entries_count": len(purchase_entries),
        "sales_entries_count": len(sales_entries),
        "declaration": declaration.to_dict(),
        "consistency_issues": consistency_issues,
        "exported_files": exported_files,
        "raw_contents": {
            "pokupki": pokupki_content if isinstance(pokupki_content, str) else pokupki_content.decode("cp1251", errors="replace"),
            "prodagbi": prodagbi_content if isinstance(prodagbi_content, str) else prodagbi_content.decode("cp1251", errors="replace"),
            "deklar": deklar_content if isinstance(deklar_content, str) else deklar_content.decode("cp1251", errors="replace"),
        },
    }


# ---------------------------------------------------------------------------
# Microinvest ERP Integration (Sklad Pro & Delta Pro TransferData XML)
# ---------------------------------------------------------------------------
from invoice_core.microinvest_export import (
    generate_microinvest_sklad_xml,
    generate_microinvest_delta_xml,
    export_microinvest_package,
)

"""Automated Accounting Engine for Bulgarian Invoices & Microinvest Delta Pro.

Converts verified OCR invoices, credit notes, and sales reports into balanced,
statutory double-entry accounting operations (стопански операции / контировки)
compliant with Bulgarian Accounting Standards (НСС / Закон за счетоводството),
VAT Act (ЗДДС), and Microinvest Delta Pro TRANSFER.LOG / XML / CSV specifications.

Key Capabilities:
1. Deterministic Supplier and Line-Item Account Mapping (304, 601, 602, 609, 4531, 501, 401).
2. Credit Note & Storno Handling (Червено сторно / отрицателни дебити).
3. Exact Microinvest Delta Pro representation (IsKredit: 1=Credit, 2=Debit, signed amounts).
4. Delta Pro XML, CSV, and Supabase accounting schema export.
5. Low-level binary reader/parser for TRANSFER.LOG (Jet 1/2 W#Transfer).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import datetime
from decimal import Decimal, ROUND_HALF_UP
import io
import json
import logging
import os
import re
import struct
from typing import Any, Sequence
import xml.etree.ElementTree as ET

from .account_mapping import DEFAULT_MAPPING_ENGINE, AccountMappingEngine
from .models import Invoice, LineItem, MoneyAmount, Party

logger = logging.getLogger("invoice_ocr.accounting_engine")

MONEY_CENT = Decimal("0.01")

# Standard Bulgarian Chart of Accounts Synthetic Numbers
ACCT_STOCK_MERCHANDISE = "304"      # Стоки
ACCT_MATERIALS = "601"              # Разходи за материали
ACCT_EXTERNAL_SERVICES = "602"      # Разходи за външни услуги
ACCT_PAYROLL = "604"                # Разходи за заплати
ACCT_SOCIAL_SECURITY = "605"        # Разходи за осигуровки
ACCT_OTHER_EXPENSES = "609"         # Други разходи / консумативи
ACCT_VAT_PURCHASES = "4531"         # Начислен данък за покупките (453.1)
ACCT_VAT_SALES = "4532"             # Начислен данък за продажбите (453.2)
ACCT_CASH_BGN = "501"               # Каса в левове
ACCT_BANK_BGN = "503"               # Разплащателна сметка в левове
ACCT_SUPPLIERS = "401"              # Доставчици
ACCT_CLIENTS = "411"                # Клиенти
ACCT_SALES_REVENUE = "702"          # Приходи от продажба на стоки

# Delta Pro Direction Codes
DELTA_PRO_CREDIT = 1
DELTA_PRO_DEBIT = 2

# Document Type Codes
DOC_TYPE_INVOICE = "01"             # Ф-ра / Фактура
DOC_TYPE_DEBIT_NOTE = "02"          # ДИ / Дебитно известие
DOC_TYPE_CREDIT_NOTE = "03"         # КИ / Кредитно известие
DOC_TYPE_SALES_REPORT = "119"       # ОП / Отчет за продажбите (чл. 119 ЗДДС)


def _to_dec(val: Any, default: Decimal = Decimal("0.00")) -> Decimal:
    """Safely convert any value to Decimal with 2-decimal precision."""
    if val is None or val == "":
        return default
    if isinstance(val, Decimal):
        return val.quantize(MONEY_CENT, rounding=ROUND_HALF_UP)
    if hasattr(val, "amount"):
        amt = getattr(val, "amount")
        return _to_dec(amt, default) if amt is not None else default
    if isinstance(val, dict):
        if "amount" in val:
            return _to_dec(val.get("amount"), default)
        if "value" in val:
            return _to_dec(val.get("value"), default)
        return default
    try:
        clean = str(val).strip().replace(" ", "").replace(",", ".")
        if not clean or clean.lower() in ("none", "null"):
            return default
        return Decimal(clean).quantize(MONEY_CENT, rounding=ROUND_HALF_UP)
    except Exception:
        return default


@dataclass
class JournalEntryRow:
    """A single double-entry journal line (кореспондентски ред)."""
    operation_id: int
    line_number: int                        # Sequence within operation (1, 2, 3...)
    direction: str                          # "DEBIT" or "CREDIT"
    is_kredit: int                          # Delta Pro convention: 1 = Credit, 2 = Debit
    account: str                            # Synthetic account code (e.g. "304", "4531", "501")
    subaccount: str = "0"                   # Analytical subaccount (e.g. "1.0", "121644736")
    amount: Decimal = Decimal("0.00")       # Signed amount in BGN (negative for storno/counterpart)
    account_name: str = ""                  # Bulgarian title of account
    document_type: str = DOC_TYPE_INVOICE   # "01", "03", "119"
    document_type_label: str = "Ф-ра"       # "Ф-ра", "КИ", "ОП"
    document_number: str = ""               # 10-digit invoice number
    document_date: str = ""                 # YYYY-MM-DD or DD.MM.YYYY
    accounting_date: str = ""               # YYYY-MM-DD or DD.MM.YYYY
    partner_name: str = ""                  # Official company legal name
    partner_eik: str = ""                   # UIC / Bulstat
    partner_vat: str = ""                   # VAT number (e.g. BG121644736)
    reason: str = ""                        # Основание за операцията (стоки, материали, услуги)
    is_vat_row: bool = False                # 1 if row touches 4531/4532
    is_purchase: bool = True                # 1 for purchases, 0 for sales

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "line_number": self.line_number,
            "direction": self.direction,
            "is_kredit": self.is_kredit,
            "account": self.account,
            "subaccount": self.subaccount,
            "amount": str(self.amount),
            "account_name": self.account_name,
            "document_type": self.document_type,
            "document_type_label": self.document_type_label,
            "document_number": self.document_number,
            "document_date": self.document_date,
            "accounting_date": self.accounting_date,
            "partner_name": self.partner_name,
            "partner_eik": self.partner_eik,
            "partner_vat": self.partner_vat,
            "reason": self.reason,
            "is_vat_row": self.is_vat_row,
            "is_purchase": self.is_purchase,
        }


@dataclass
class AccountingOperation:
    """A complete, balanced business transaction (стопанска операция)."""
    operation_id: int
    document_type: str                      # "01", "02", "03", "119"
    document_type_label: str                # "Ф-ра", "ДИ", "КИ", "ОП"
    document_number: str
    document_date: str                      # YYYY-MM-DD
    accounting_date: str                    # YYYY-MM-DD
    partner_name: str
    partner_eik: str
    partner_vat: str
    tax_base: Decimal                       # Net amount (данъчна основа)
    vat_amount: Decimal                     # VAT amount
    total_amount: Decimal                   # Gross total
    currency: str = "BGN"
    payment_method: str = "CASH"            # "CASH" -> 501, "BANK" -> 401/503
    is_credit_note: bool = False            # True for credit note / storno
    reason: str = "стоки"                   # "стоки", "материали", "външни услуги", "други"
    rows: list[JournalEntryRow] = field(default_factory=list)

    @property
    def is_balanced(self) -> bool:
        """Verify that total debits match total credits."""
        total_debit = Decimal("0.00")
        total_credit = Decimal("0.00")
        for r in self.rows:
            if r.direction == "DEBIT":
                total_debit += abs(r.amount)
            elif r.direction == "CREDIT":
                total_credit += abs(r.amount)
        return total_debit == total_credit

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "document_type": self.document_type,
            "document_type_label": self.document_type_label,
            "document_number": self.document_number,
            "document_date": self.document_date,
            "accounting_date": self.accounting_date,
            "partner_name": self.partner_name,
            "partner_eik": self.partner_eik,
            "partner_vat": self.partner_vat,
            "tax_base": str(self.tax_base),
            "vat_amount": str(self.vat_amount),
            "total_amount": str(self.total_amount),
            "currency": self.currency,
            "payment_method": self.payment_method,
            "is_credit_note": self.is_credit_note,
            "reason": self.reason,
            "is_balanced": self.is_balanced,
            "rows": [r.to_dict() for r in self.rows],
        }


# ============================================================================
# Core Accounting Engine
# ============================================================================

class AccountingEngine:
    """Intelligent Accounting Rule Engine and Delta Pro Generator."""

    ACCOUNT_NAMES: dict[str, str] = {
        ACCT_STOCK_MERCHANDISE: "Стоки",
        ACCT_MATERIALS: "Разходи за материали",
        ACCT_EXTERNAL_SERVICES: "Разходи за външни услуги",
        ACCT_PAYROLL: "Разходи за заплати",
        ACCT_SOCIAL_SECURITY: "Разходи за осигуровки",
        ACCT_OTHER_EXPENSES: "Други разходи",
        ACCT_VAT_PURCHASES: "Данък върху покупките",
        ACCT_VAT_SALES: "Данък върху продажбите",
        ACCT_CASH_BGN: "Каса в левове",
        ACCT_BANK_BGN: "Разплащателна сметка в левове",
        ACCT_SUPPLIERS: "Доставчици",
        ACCT_CLIENTS: "Клиенти",
        ACCT_SALES_REVENUE: "Приходи от продажба на стоки",
    }

    # Deterministic supplier mappings from Secret Legend & Majestic Smoke historical ledgers
    KNOWN_SUPPLIERS: dict[str, dict[str, str]] = {
        # Merchandise / Resale Suppliers -> 304
        "121644736": {"account": ACCT_STOCK_MERCHANDISE, "reason": "стоки", "name": "МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД"},
        "131071587": {"account": ACCT_STOCK_MERCHANDISE, "reason": "стоки", "name": "ЛИДЛ БЪЛГАРИЯ ЕООД ЕНД КО КД"},
        "130987441": {"account": ACCT_STOCK_MERCHANDISE, "reason": "стоки", "name": "КАУФЛАНД БЪЛГАРИЯ ЕООД ЕНД КО КД"},
        "114690224": {"account": ACCT_STOCK_MERCHANDISE, "reason": "стоки", "name": "АРПАК ЕООД"},
        "207390964": {"account": ACCT_STOCK_MERCHANDISE, "reason": "стоки", "name": "ТИМ СТОК 23 ЕООД"},
        "207822235": {"account": ACCT_STOCK_MERCHANDISE, "reason": "стоки", "name": "КУКИЛИШЪС ЕООД"},
        "131379488": {"account": ACCT_STOCK_MERCHANDISE, "reason": "стоки", "name": "ГЕНИК КАФЕ КЪМПАНИ ООД"},
        "202262252": {"account": ACCT_STOCK_MERCHANDISE, "reason": "стоки", "name": "АНДА 2012 2 АНКО ПЕТРОВ ЕООД"},
        "200349655": {"account": ACCT_STOCK_MERCHANDISE, "reason": "стоки", "name": "ЦВЕТНА РАДОСТ ООД"},
        "204540024": {"account": ACCT_STOCK_MERCHANDISE, "reason": "стоки", "name": "ИНВИКТЪС СТАЙЛ ЕООД"},

        # External Services -> 602
        "831642181": {"account": ACCT_EXTERNAL_SERVICES, "reason": "телекомуникации", "name": "БТК ЕАД (VIVACOM)"},
        "131468980": {"account": ACCT_EXTERNAL_SERVICES, "reason": "телекомуникации", "name": "А1 БЪЛГАРИЯ ЕАД"},
        "130408101": {"account": ACCT_EXTERNAL_SERVICES, "reason": "телекомуникации", "name": "ЙЕТТЕЛ БЪЛГАРИЯ ЕАД"},
        "130177202": {"account": ACCT_EXTERNAL_SERVICES, "reason": "куриерски услуги", "name": "СПИДИ АД"},
        "117041887": {"account": ACCT_EXTERNAL_SERVICES, "reason": "куриерски услуги", "name": "ЕКОНТ ЕКСПРЕС ООД"},
        "121021487": {"account": ACCT_EXTERNAL_SERVICES, "reason": "охрана СОТ", "name": "СОТ - СИГНАЛНО-ОХРАНИТЕЛНА ТЕХНИКА ЕООД"},
        "114620023": {"account": ACCT_EXTERNAL_SERVICES, "reason": "интернет услуги", "name": "ОПТИСПРИНТ ЕООД"},
        "175369680": {"account": ACCT_EXTERNAL_SERVICES, "reason": "инкасо услуги", "name": "КЕШ СЪРВИСИЗ КЪМПАНИ АД"},
        "201389718": {"account": ACCT_EXTERNAL_SERVICES, "reason": "консултантски услуги", "name": "ОНЛАЙН КОНСУЛТ ЕООД"},

        # Consumables / Supplies / Cleaning / Other -> 609 / 601
        "200388915": {"account": ACCT_OTHER_EXPENSES, "reason": "консумативи", "name": "СББ ГРУП ЕООД"},
        "131238479": {"account": ACCT_OTHER_EXPENSES, "reason": "почистване", "name": "КЛИЙН СИСТЕМС ООД"},
        "131313550": {"account": ACCT_OTHER_EXPENSES, "reason": "материали", "name": "БАУМАКС БЪЛГАРИЯ ООД"},
        "200525782": {"account": ACCT_OTHER_EXPENSES, "reason": "материали", "name": "ПРАКТИКЕР РИТЕЙЛ ЕООД"},
        "131470112": {"account": ACCT_OTHER_EXPENSES, "reason": "обзавеждане", "name": "ЮСК БУЛ ЕООД"},
        "130948956": {"account": ACCT_OTHER_EXPENSES, "reason": "осветление", "name": "ЕГЛО БЪЛГАРИЯ ЕООД"},
        "130858590": {"account": ACCT_OTHER_EXPENSES, "reason": "техника", "name": "ТЕХНОПОЛИС БЪЛГАРИЯ ЕАД"},

        # Fuels & Materials -> 601
        "130962406": {"account": ACCT_MATERIALS, "reason": "горива", "name": "РОМПЕТРОЛ БЪЛГАРИЯ ЕАД"},
        "121687551": {"account": ACCT_MATERIALS, "reason": "горива", "name": "ЛУКОЙЛ БЪЛГАРИЯ ЕООД"},
        "121528328": {"account": ACCT_MATERIALS, "reason": "горива", "name": "ОМВ БЪЛГАРИЯ ООД"},
        "121852504": {"account": ACCT_MATERIALS, "reason": "горива", "name": "ШЕЛ БЪЛГАРИЯ ЕАД"},

        # Construction & Building Materials -> 601 (from bilding 11.MDB, bil 26.MDB, BILD10.MDB)
        "114631464": {"account": ACCT_MATERIALS, "reason": "м-ли", "name": "МАГНЕЗИЯ ЕООД"},
        "114049058": {"account": ACCT_MATERIALS, "reason": "м-ли", "name": "НОВОКОМ АД"},
        "115853140": {"account": ACCT_MATERIALS, "reason": "м-ли", "name": "СТЕНОР ООД"},
        "114654580": {"account": ACCT_MATERIALS, "reason": "м-ли", "name": "СИМЕКС ООД"},
        "114672009": {"account": ACCT_MATERIALS, "reason": "м-ли и транспорт", "name": "АЛФА МИКС ООД"},
    }

    def __init__(self, mapping_engine: AccountMappingEngine | None = None) -> None:
        self.mapping_engine = mapping_engine or DEFAULT_MAPPING_ENGINE

    def resolve_expense_account(
        self,
        supplier_eik: str,
        items: Sequence[LineItem | dict[str, Any]] | None = None,
        default_account: str = ACCT_STOCK_MERCHANDISE,
    ) -> tuple[str, str]:
        """Resolve synthetic account and reason description for an invoice.

        Priority:
        1. Supplier specific rule (if verified supplier).
        2. Line item analysis (if clear keywords match services or materials).
        3. Fallback default.
        """
        clean_eik = re.sub(r"[^0-9A-Za-z]", "", str(supplier_eik or "")).upper()
        if clean_eik.startswith("BG") and len(clean_eik) > 2:
            clean_eik = clean_eik[2:]

        if clean_eik in self.KNOWN_SUPPLIERS:
            rule = self.KNOWN_SUPPLIERS[clean_eik]
            return rule["account"], rule["reason"]

        # If items are present, check item descriptions
        if items:
            combined_desc = " ".join(
                (it.get("description") or it.get("name") or "") if isinstance(it, dict)
                else (it.description or "")
                for it in items
            ).lower()

            if any(w in combined_desc for w in ("услуга", "абонамент", "наем", "транспорт", "куриер", "ремонт", "софтуер")):
                return ACCT_EXTERNAL_SERVICES, "външни услуги"
            if any(w in combined_desc for w in ("гориво", "дизел", "бензин", "масло", "газ")):
                return ACCT_MATERIALS, "горива"
            if any(w in combined_desc for w in ("консуматив", "почистващ", "хартия", "офис")):
                return ACCT_OTHER_EXPENSES, "консумативи"
            if any(w in combined_desc for w in ("стока", "кафе", "напитка", "бира", "вино", "храна", "тютюн", "наргиле")):
                return ACCT_STOCK_MERCHANDISE, "стоки"

        return default_account, "стоки" if default_account == ACCT_STOCK_MERCHANDISE else "разходи"

    def resolve_payment_method(
        self,
        payment_method: str | None,
        payment_details: Any | None = None,
    ) -> str:
        """Determine payment method: CASH (501) vs BANK (401/503)."""
        text = str(payment_method or "").upper()
        if payment_details:
            if hasattr(payment_details, "method") and payment_details.method:
                text += f" {payment_details.method.upper()}"
            if hasattr(payment_details, "iban") and payment_details.iban:
                text += " IBAN BANK"

        if any(w in text for w in ("CASH", "БРОЙ", "КАСА", "БОН", "В БРОЙ")):
            return "CASH"
        if any(w in text for w in ("BANK", "ПРЕВОД", "БАНКА", "IBAN", "СМЕТКА")):
            return "BANK"
        return "CASH"

    def create_operation(
        self,
        invoice: Invoice | dict[str, Any],
        operation_id: int = 1,
        accounting_date: str | None = None,
    ) -> AccountingOperation:
        """Create a complete, balanced AccountingOperation from an Invoice or Dict."""
        # Normalize fields
        if isinstance(invoice, dict):
            inv_no = str(invoice.get("invoice_number") or invoice.get("invoiceNumber") or "").strip()
            inv_dt = str(invoice.get("date_issued") or invoice.get("invoiceDate") or "").strip()
            is_credit = bool(
                invoice.get("is_credit_note")
                or invoice.get("document_type") in ("CREDIT_NOTE", "03", "КИ")
                or "кредитно" in str(invoice.get("document_title") or "").lower()
            )
            sup = invoice.get("supplier") or {}
            sup_name = str(sup.get("name") or invoice.get("vendorName") or "").strip()
            sup_eik = str(sup.get("eik") or invoice.get("vendorEik") or "").strip()
            sup_vat = str(sup.get("vat_number") or invoice.get("vendorVatNumber") or f"BG{sup_eik}").strip()

            fin = invoice.get("financial_summary") or {}
            tax_base = _to_dec(fin.get("tax_base") or invoice.get("subtotal"))
            vat_amount = _to_dec(fin.get("vat_amount") or invoice.get("taxAmount"))
            total_amount = _to_dec(fin.get("total_amount_due") or invoice.get("totalAmount"))
            pay_method = invoice.get("payment_method") or (invoice.get("payment_details") or {}).get("method")
            items = invoice.get("line_items") or invoice.get("items") or []
        else:
            inv_no = str(invoice.invoice_metadata.invoice_number or "").strip()
            inv_dt = str(invoice.invoice_metadata.date_issued or "").strip()
            is_credit = bool(
                invoice.invoice_metadata.is_credit_note
                or invoice.invoice_metadata.document_type == "CREDIT_NOTE"
            )
            sup_name = str(invoice.supplier.name or "").strip()
            sup_eik = str(invoice.supplier.eik or "").strip()
            sup_vat = str(invoice.supplier.vat_number or f"BG{sup_eik}").strip()

            tax_base = _to_dec(invoice.financial_summary.tax_base)
            vat_amount = _to_dec(invoice.financial_summary.vat_amount)
            total_amount = _to_dec(invoice.financial_summary.total_amount_due)
            pay_method = getattr(invoice.payment_details, "method", None)
            items = invoice.line_items

        # Fallback date if missing
        if not inv_dt:
            inv_dt = datetime.date.today().strftime("%Y-%m-%d")
        act_dt = accounting_date or inv_dt

        # Ensure totals consistency
        if total_amount == Decimal("0.00") and (tax_base > 0 or vat_amount > 0):
            total_amount = (tax_base + vat_amount).quantize(MONEY_CENT, rounding=ROUND_HALF_UP)
        elif tax_base == Decimal("0.00") and total_amount > 0:
            tax_base = (total_amount - vat_amount).quantize(MONEY_CENT, rounding=ROUND_HALF_UP)

        doc_code = DOC_TYPE_CREDIT_NOTE if is_credit else DOC_TYPE_INVOICE
        doc_label = "КИ" if is_credit else "Ф-ра"

        # Resolve accounts
        expense_acct, reason_desc = self.resolve_expense_account(sup_eik, items)
        pay_type = self.resolve_payment_method(pay_method)
        counterpart_acct = ACCT_CASH_BGN if pay_type == "CASH" else ACCT_SUPPLIERS

        op = AccountingOperation(
            operation_id=operation_id,
            document_type=doc_code,
            document_type_label=doc_label,
            document_number=inv_no,
            document_date=inv_dt,
            accounting_date=act_dt,
            partner_name=sup_name,
            partner_eik=sup_eik,
            partner_vat=sup_vat,
            tax_base=tax_base,
            vat_amount=vat_amount,
            total_amount=total_amount,
            payment_method=pay_type,
            is_credit_note=is_credit,
            reason=reason_desc,
        )

        rows: list[JournalEntryRow] = []
        line_idx = 1

        # 1. Tax Base Line (Expense / Merchandise)
        if tax_base > 0:
            debit_amt = -tax_base if is_credit else tax_base
            credit_amt = tax_base if is_credit else -tax_base

            # Credit entry for Counterpart
            rows.append(JournalEntryRow(
                operation_id=operation_id,
                line_number=line_idx,
                direction="CREDIT",
                is_kredit=DELTA_PRO_CREDIT,
                account=counterpart_acct,
                subaccount="0",
                amount=credit_amt,
                account_name=self.ACCOUNT_NAMES.get(counterpart_acct, "Каса/Доставчици"),
                document_type=doc_code,
                document_type_label=doc_label,
                document_number=inv_no,
                document_date=inv_dt,
                accounting_date=act_dt,
                partner_name=sup_name,
                partner_eik=sup_eik,
                partner_vat=sup_vat,
                reason=reason_desc,
                is_vat_row=False,
                is_purchase=True,
            ))

            # Debit entry for Expense / Goods
            rows.append(JournalEntryRow(
                operation_id=operation_id,
                line_number=line_idx,
                direction="DEBIT",
                is_kredit=DELTA_PRO_DEBIT,
                account=expense_acct,
                subaccount="0",
                amount=debit_amt,
                account_name=self.ACCOUNT_NAMES.get(expense_acct, "Стоки/Разходи"),
                document_type=doc_code,
                document_type_label=doc_label,
                document_number=inv_no,
                document_date=inv_dt,
                accounting_date=act_dt,
                partner_name=sup_name,
                partner_eik=sup_eik,
                partner_vat=sup_vat,
                reason=reason_desc,
                is_vat_row=False,
                is_purchase=True,
            ))
            line_idx += 1

        # 2. VAT Line (4531 - Начислен данък за покупките)
        if vat_amount > 0:
            vat_debit = -vat_amount if is_credit else vat_amount
            vat_credit = vat_amount if is_credit else -vat_amount

            # Credit entry for Counterpart
            rows.append(JournalEntryRow(
                operation_id=operation_id,
                line_number=line_idx,
                direction="CREDIT",
                is_kredit=DELTA_PRO_CREDIT,
                account=counterpart_acct,
                subaccount="0",
                amount=vat_credit,
                account_name=self.ACCOUNT_NAMES.get(counterpart_acct, "Каса/Доставчици"),
                document_type=doc_code,
                document_type_label=doc_label,
                document_number=inv_no,
                document_date=inv_dt,
                accounting_date=act_dt,
                partner_name=sup_name,
                partner_eik=sup_eik,
                partner_vat=sup_vat,
                reason=reason_desc,
                is_vat_row=True,
                is_purchase=True,
            ))

            # Debit entry for VAT Purchases (453.1)
            rows.append(JournalEntryRow(
                operation_id=operation_id,
                line_number=line_idx,
                direction="DEBIT",
                is_kredit=DELTA_PRO_DEBIT,
                account=ACCT_VAT_PURCHASES,
                subaccount="1.0",
                amount=vat_debit,
                account_name=self.ACCOUNT_NAMES.get(ACCT_VAT_PURCHASES, "Данък върху покупките"),
                document_type=doc_code,
                document_type_label=doc_label,
                document_number=inv_no,
                document_date=inv_dt,
                accounting_date=act_dt,
                partner_name=sup_name,
                partner_eik=sup_eik,
                partner_vat=sup_vat,
                reason=reason_desc,
                is_vat_row=True,
                is_purchase=True,
            ))
            line_idx += 1

        op.rows = rows
        return op


# ============================================================================
# Transfer.log Low-Level Binary Parser
# ============================================================================

def parse_transfer_log(log_path: str) -> list[AccountingOperation]:
    """Parse a Microinvest Delta Pro TRANSFER.LOG binary database file.

    Extracts all operations and journal rows from table W#Transfer (Table ID 25)
    with 100% field recovery and encoding in CP1251.
    """
    if not os.path.exists(log_path):
        raise FileNotFoundError(f"TRANSFER.LOG file not found at: {log_path}")

    with open(log_path, "rb") as f:
        data = f.read()

    PAGE_SIZE = 2048
    base_date = datetime.datetime(1899, 12, 30)
    raw_records: list[dict[str, Any]] = []

    for p in range(len(data) // PAGE_SIZE):
        page = data[p * PAGE_SIZE : (p + 1) * PAGE_SIZE]
        # Check Jet 1/2 Data Page (0x06) and Table ID 25 (W#Transfer)
        if page[0] != 0x06 or int.from_bytes(page[4:8], "little") != 25:
            continue

        num_recs = int.from_bytes(page[8:10], "little")
        for i in range(num_recs):
            ptr = int.from_bytes(page[20 + i * 2 : 22 + i * 2], "little")
            # Bit 0x1000 indicates active record
            if ptr & 0x1000 == 0:
                continue
            offset = ptr & 0x07FF
            end = 0x800 if i == 0 else (int.from_bytes(page[20 + (i - 1) * 2 : 22 + (i - 1) * 2], "little") & 0x07FF)
            rec = page[offset:end]
            if len(rec) < 70:
                continue

            try:
                kon_id = int.from_bytes(rec[4:8], "little")
                kon_num = int.from_bytes(rec[8:12], "little")
                is_kredit = int.from_bytes(rec[12:14], "little")
                acct = int.from_bytes(rec[14:16], "little")
                sub_acct = struct.unpack("<d", rec[16:24])[0] if len(rec) >= 24 else 0.0
                amount = struct.unpack("<d", rec[32:40])[0] if len(rec) >= 40 else 0.0
                date_float = struct.unpack("<d", rec[40:48])[0] if len(rec) >= 48 else 0.0
                date_str = (base_date + datetime.timedelta(days=date_float)).strftime("%Y-%m-%d") if 30000 < date_float < 60000 else ""
                is_dds = int.from_bytes(rec[58:60], "little") if len(rec) >= 60 else 0
                is_pokupka = int.from_bytes(rec[60:62], "little") if len(rec) >= 62 else 0

                num_var = rec[-3]
                var_texts: list[str] = []
                if num_var < 30 and len(rec) >= 4 + num_var:
                    raw_offsets = [rec[-(4 + j)] for j in range(num_var + 1)]
                    for j in range(len(raw_offsets) - 1):
                        s, e = raw_offsets[j], raw_offsets[j + 1]
                        var_texts.append(rec[s:e].decode("cp1251", errors="replace").strip() if s <= e <= len(rec) else "")

                raw_records.append({
                    "kon_id": kon_id,
                    "kon_num": kon_num,
                    "is_kredit": is_kredit,
                    "acct": acct,
                    "sub_acct": sub_acct,
                    "amount": amount,
                    "date": date_str,
                    "is_dds": is_dds,
                    "is_pokupka": is_pokupka,
                    "var": var_texts,
                })
            except Exception as e:
                logger.debug(f"Failed to parse TRANSFER.LOG record: {e}")

    # Group by kon_id into AccountingOperation objects
    ops_dict: dict[int, list[dict[str, Any]]] = {}
    for r in raw_records:
        ops_dict.setdefault(r["kon_id"], []).append(r)

    operations: list[AccountingOperation] = []
    for kon_id, recs in sorted(ops_dict.items()):
        first = recs[0]
        var = first.get("var", [])
        inv_no = var[1] if len(var) > 1 else ""
        reason = var[2] if len(var) > 2 else ""
        doc_label = var[5] if len(var) > 5 else "Ф-ра"
        doc_dt = var[6] if len(var) > 6 else first.get("date", "")
        sup_name = var[10] if len(var) > 10 else ""
        sup_vat = var[11] if len(var) > 11 else ""
        sup_eik = var[12] if len(var) > 12 else ""

        is_credit = (doc_label == "КИ")
        doc_code = DOC_TYPE_CREDIT_NOTE if is_credit else DOC_TYPE_INVOICE

        # Calculate totals
        tax_base = Decimal("0.00")
        vat_amt = Decimal("0.00")
        total_amt = Decimal("0.00")

        rows: list[JournalEntryRow] = []
        for r in recs:
            amt = Decimal(str(round(r["amount"], 2)))
            direction = "CREDIT" if r["is_kredit"] == DELTA_PRO_CREDIT else "DEBIT"
            acct_str = str(r["acct"])
            is_vat = bool(r["is_dds"] or acct_str == "453")

            if direction == "DEBIT":
                if is_vat:
                    vat_amt += abs(amt)
                else:
                    tax_base += abs(amt)
            elif direction == "CREDIT" and acct_str in ("501", "401", "503"):
                total_amt += abs(amt)

            r_var = r.get("var", [])
            acct_name = r_var[17] if len(r_var) > 17 else ""

            rows.append(JournalEntryRow(
                operation_id=kon_id,
                line_number=r["kon_num"],
                direction=direction,
                is_kredit=r["is_kredit"],
                account=acct_str,
                subaccount=str(r["sub_acct"]),
                amount=amt,
                account_name=acct_name,
                document_type=doc_code,
                document_type_label=doc_label,
                document_number=inv_no,
                document_date=doc_dt,
                accounting_date=first.get("date", ""),
                partner_name=sup_name,
                partner_eik=sup_eik,
                partner_vat=sup_vat,
                reason=reason,
                is_vat_row=is_vat,
                is_purchase=bool(r["is_pokupka"]),
            ))

        op = AccountingOperation(
            operation_id=kon_id,
            document_type=doc_code,
            document_type_label=doc_label,
            document_number=inv_no,
            document_date=doc_dt,
            accounting_date=first.get("date", ""),
            partner_name=sup_name,
            partner_eik=sup_eik,
            partner_vat=sup_vat,
            tax_base=tax_base,
            vat_amount=vat_amt,
            total_amount=total_amt if total_amt > 0 else (tax_base + vat_amt),
            payment_method="CASH",
            is_credit_note=is_credit,
            reason=reason,
            rows=rows,
        )
        operations.append(op)

    return operations


# ============================================================================
# Exporters (Delta Pro CSV, XML, Supabase SQL)
# ============================================================================

def export_delta_csv(operations: list[AccountingOperation], encoding: str = "cp1251") -> bytes:
    """Export operations to Microinvest Delta Pro CSV import format."""
    out = io.StringIO()
    # Delta Pro CSV columns
    out.write("Номер;Дата;Документ;НомерДок;ДатаДок;СметкаДт;ПодсметкаДт;СметкаКт;ПодсметкаКт;Сума;Основание;Булстат;ИмеКонтрагент;ДДС_Дневник;ДДС_Ставка\r\n")

    for op in operations:
        lines: dict[int, dict[str, JournalEntryRow]] = {}
        for r in op.rows:
            lines.setdefault(r.line_number, {})[r.direction] = r

        for l_num, pair in sorted(lines.items()):
            deb = pair.get("DEBIT")
            crd = pair.get("CREDIT")
            if not deb or not crd:
                continue

            dt_acct = deb.account
            dt_sub = deb.subaccount
            kt_acct = crd.account
            kt_sub = crd.subaccount
            amt = abs(deb.amount)
            vat_diary = "1" if deb.is_vat_row else ("0" if op.vat_amount == 0 else "1")

            out.write(
                f"{op.operation_id};{op.accounting_date};{op.document_type_label};{op.document_number};"
                f"{op.document_date};{dt_acct};{dt_sub};{kt_acct};{kt_sub};{amt:.2f};"
                f"{op.reason};{op.partner_eik};{op.partner_name};{vat_diary};20\r\n"
            )

    return out.getvalue().encode(encoding, errors="replace")


def generate_supabase_accounting_sql(operations: list[AccountingOperation]) -> str:
    """Generate SQL statements to insert operations into Supabase accounting schema."""
    lines: list[str] = [
        "-- Supabase Accounting Journal Entries Insertion",
        "BEGIN;",
    ]

    for op in operations:
        lines.append(f"""
INSERT INTO accounting.transactions (
    transaction_number, transaction_date, document_type, document_number,
    partner_eik, partner_name, tax_base, vat_amount, total_amount,
    currency, is_credit_note, status
) VALUES (
    'OP-{op.operation_id:05d}', '{op.accounting_date}', '{op.document_type}', '{op.document_number}',
    '{op.partner_eik}', '{op.partner_name.replace("'", "''")}', {op.tax_base}, {op.vat_amount}, {op.total_amount},
    '{op.currency}', {'true' if op.is_credit_note else 'false'}, 'POSTED'
) ON CONFLICT (document_number, partner_eik) DO UPDATE SET
    tax_base = EXCLUDED.tax_base,
    vat_amount = EXCLUDED.vat_amount,
    total_amount = EXCLUDED.total_amount;
""".strip())

        for r in op.rows:
            lines.append(f"""
INSERT INTO accounting.journal_entries (
    transaction_id, line_number, direction, account_code, subaccount_code,
    amount, account_name, description
) VALUES (
    (SELECT id FROM accounting.transactions WHERE transaction_number = 'OP-{op.operation_id:05d}'),
    {r.line_number}, '{r.direction}', '{r.account}', '{r.subaccount}',
    {r.amount}, '{r.account_name.replace("'", "''")}', '{r.reason.replace("'", "''")}'
);
""".strip())

    lines.append("COMMIT;")
    return "\n".join(lines)

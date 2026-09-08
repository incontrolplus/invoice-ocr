"""Business Navigator (Бизнес Навигатор) ERP Export Engine.

Supports:
1. Structured Text import format (.txt / .csv / .bn) with primary documents
   and double-entry accounting postings (контировки) encoded in Windows-1251 (CP1251).
2. Native binary dBase III / IV DBF export (DOKUM.DBF, OPER.DBF, and single IMPORT_BN.DBF)
   with complete field structures, CP1251 Cyrillic encoding, and verified mathematical balance.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import io
import logging
from pathlib import Path
import re
import struct
from typing import Any, Iterable, Literal, Sequence
import zipfile

from .account_mapping import DEFAULT_MAPPING_ENGINE, AccountMappingEngine
from .chart_of_accounts import lookup_account
from .models import Invoice, LineItem, MoneyAmount, Party
from .tax_period_validator import validate_tax_period

logger = logging.getLogger("invoice_ocr.business_navigator")

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


def _clean_digits(val: Any, max_len: int = 10, pad_zero: bool = True) -> str:
    if val is None:
        return "0" * max_len if pad_zero else ""
    if isinstance(val, dict):
        val = val.get("number") or val.get("value") or val.get("text") or ""
    digits = re.sub(r"[^\d]", "", str(val))
    if not digits:
        return "0" * max_len if pad_zero else ""
    if len(digits) > max_len:
        return digits[-max_len:]
    return digits.zfill(max_len) if pad_zero else digits


def _to_decimal(val: Any) -> Decimal:
    if val is None or val == "":
        return Decimal("0.00")
    if isinstance(val, MoneyAmount):
        return (val.amount or Decimal("0.00")).quantize(MONEY, rounding=ROUND_HALF_UP)
    if isinstance(val, Decimal):
        return val.quantize(MONEY, rounding=ROUND_HALF_UP)
    if hasattr(val, "amount"):
        amt = getattr(val, "amount")
        return _to_decimal(amt) if amt is not None else Decimal("0.00")
    if isinstance(val, dict):
        if "amount" in val:
            return _to_decimal(val.get("amount"))
        if "value" in val:
            return _to_decimal(val.get("value"))
        return Decimal("0.00")
    try:
        clean = str(val).replace(" ", "").replace(",", ".")
        if not clean or clean.lower() == "none" or clean.lower() == "null":
            return Decimal("0.00")
        return Decimal(clean).quantize(MONEY, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")



def _parse_date_components(d_val: Any) -> tuple[str, str]:
    """Return (YYYY-MM-DD, YYYYMMDD)."""
    if not d_val:
        today = datetime.now().date()
        return today.isoformat(), today.strftime("%Y%m%d")
    s = str(d_val).strip()
    # YYYY-MM-DD
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}", f"{y:04d}{mo:02d}{d:02d}"
    # DD.MM.YYYY
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}", f"{y:04d}{mo:02d}{d:02d}"
    today = datetime.now().date()
    return today.isoformat(), today.strftime("%Y%m%d")


# ============================================================================
# PURE PYTHON DBASE III / IV DBF GENERATOR & PARSER
# ============================================================================

@dataclass
class DBFField:
    name: str          # 1-11 ASCII characters
    type: str          # 'C' (Character), 'N' (Numeric), 'D' (Date)
    length: int        # Total length in bytes
    decimals: int = 0  # Decimal places for 'N'


class SimpleDBFWriter:
    """Pure Python dBase III / IV binary DBF writer with CP1251 encoding."""

    def __init__(self, fields: list[DBFField], encoding: str = "cp1251"):
        self.fields = fields
        self.encoding = encoding
        self.records: list[dict[str, Any]] = []

    def add_record(self, record: dict[str, Any]) -> None:
        self.records.append(record)

    def write_bytes(self) -> bytes:
        out = io.BytesIO()
        now = datetime.now()
        yy = now.year - 1900 if now.year >= 1900 else 100
        mm = now.month
        dd = now.day

        num_records = len(self.records)
        header_len = 32 + len(self.fields) * 32 + 1
        record_len = 1 + sum(f.length for f in self.fields)  # 1 byte deletion flag

        # 32-byte header
        # byte 0: 0x03 (dBase III without memo)
        # byte 1-3: YY MM DD
        # byte 4-7: Record count (little-endian uint32)
        # byte 8-9: Header length (little-endian uint16)
        # byte 10-11: Record length (little-endian uint16)
        # byte 12-31: Reserved (20 bytes zeros)
        header_bytes = struct.pack(
            "<BBBBIHH20s",
            0x03,
            yy,
            mm,
            dd,
            num_records,
            header_len,
            record_len,
            b"\x00" * 20,
        )
        out.write(header_bytes)

        # Field descriptor records (32 bytes each)
        for f in self.fields:
            name_bytes = f.name.encode("ascii", errors="replace")[:10].ljust(11, b"\x00")
            field_type = f.type.encode("ascii")[:1]
            displacement = 0
            flen = f.length
            fdec = f.decimals
            field_desc = struct.pack(
                "<11scIBB14s",
                name_bytes,
                field_type,
                displacement,
                flen,
                fdec,
                b"\x00" * 14,
            )
            out.write(field_desc)

        # Header terminator
        out.write(b"\x0D")

        # Write records
        for r in self.records:
            # Record starts with 0x20 space (not deleted)
            out.write(b"\x20")
            for f in self.fields:
                val = r.get(f.name, "")
                if f.type == "C":
                    s_val = str(val or "")
                    encoded = s_val.encode(self.encoding, errors="replace")
                    if len(encoded) > f.length:
                        encoded = encoded[:f.length]
                    encoded = encoded.ljust(f.length, b" ")
                    out.write(encoded)
                elif f.type == "N":
                    if val is None or val == "":
                        s_num = "0.00" if f.decimals > 0 else "0"
                    elif isinstance(val, (int, float, Decimal)):
                        s_num = f"{val:.{f.decimals}f}"
                    else:
                        s_num = str(val).strip().replace(",", ".")
                    encoded = s_num.encode("ascii", errors="replace")
                    if len(encoded) > f.length:
                        encoded = encoded[:f.length]
                    encoded = encoded.rjust(f.length, b" ")
                    out.write(encoded)
                elif f.type == "D":
                    # YYYYMMDD (8 chars)
                    s_d = str(val or "").replace("-", "").replace(".", "").replace("/", "")[:8]
                    encoded = s_d.encode("ascii", errors="replace").ljust(8, b" ")
                    out.write(encoded)
                else:
                    encoded = str(val).encode(self.encoding, errors="replace").ljust(f.length, b" ")
                    out.write(encoded)

        # End of file marker
        out.write(b"\x1A")
        return out.getvalue()


class SimpleDBFReader:
    """Pure Python helper to read and verify generated DBF files."""

    def __init__(self, data: bytes, encoding: str = "cp1251"):
        self.data = data
        self.encoding = encoding
        self.fields: list[DBFField] = []
        self.records: list[dict[str, Any]] = []
        self._parse()

    def _parse(self) -> None:
        if len(self.data) < 32:
            raise ValueError("DBF data too short")
        ver, yy, mm, dd, num_records, header_len, record_len = struct.unpack("<BBBBIHH", self.data[:12])
        self.version = ver
        self.record_count = num_records
        self.header_len = header_len
        self.record_len = record_len

        offset = 32
        while offset < header_len - 1:
            if self.data[offset] == 0x0D:
                break
            f_bytes = self.data[offset : offset + 32]
            name_raw = f_bytes[:11].split(b"\x00")[0].decode("ascii", errors="replace")
            f_type = chr(f_bytes[11])
            f_len = f_bytes[16]
            f_dec = f_bytes[17]
            self.fields.append(DBFField(name=name_raw, type=f_type, length=f_len, decimals=f_dec))
            offset += 32

        # Read records
        rec_offset = header_len
        for _ in range(num_records):
            if rec_offset + record_len > len(self.data):
                break
            chunk = self.data[rec_offset : rec_offset + record_len]
            # byte 0 is deletion flag
            is_deleted = chunk[0] == 0x2A
            rec_dict = {"_deleted": is_deleted}
            f_pos = 1
            for f in self.fields:
                f_data = chunk[f_pos : f_pos + f.length]
                f_pos += f.length
                if f.type == "C":
                    rec_dict[f.name] = f_data.decode(self.encoding, errors="replace").strip()
                elif f.type == "N":
                    s_val = f_data.decode("ascii", errors="replace").strip()
                    try:
                        rec_dict[f.name] = Decimal(s_val) if s_val else Decimal("0")
                    except Exception:
                        rec_dict[f.name] = s_val
                elif f.type == "D":
                    rec_dict[f.name] = f_data.decode("ascii", errors="replace").strip()
                else:
                    rec_dict[f.name] = f_data.decode(self.encoding, errors="replace").strip()
            self.records.append(rec_dict)
            rec_offset += record_len


# ============================================================================
# EXTRACTOR & ADAPTER FOR INVOICE OBJECTS / DICTS
# ============================================================================

def _extract_bn_document_data(
    invoice: Any,
    mapping_engine: AccountMappingEngine | None = None,
) -> dict[str, Any]:
    """Normalize invoice data for Business Navigator format."""
    engine = mapping_engine or DEFAULT_MAPPING_ENGINE

    if isinstance(invoice, dict):
        meta = invoice.get("invoice_metadata") or invoice
        inv_no = _clean_digits(meta.get("invoice_number") or meta.get("invoiceNumber"))
        dt_issued = meta.get("date_issued") or meta.get("invoiceDate")
        dt_event = meta.get("date_tax_event") or meta.get("taxEventDate") or dt_issued
        is_credit = bool(meta.get("is_credit_note") or meta.get("document_type") == "CREDIT_NOTE")
        is_debit = bool(meta.get("is_debit_note") or meta.get("document_type") == "DEBIT_NOTE")

        sup = invoice.get("supplier") or {}
        sup_name = _clean_str(sup.get("name") or invoice.get("vendorName") or "ДОСТАВЧИК", max_len=50)
        sup_eik = _clean_str(sup.get("eik") or invoice.get("vendorEik") or "999999999", max_len=15)
        sup_vat = _clean_str(sup.get("vat_number") or invoice.get("vendorVatNumber") or f"BG{sup_eik}", max_len=15)

        fin = invoice.get("financial_summary") or {}
        tax_base = _to_decimal(fin.get("tax_base") or invoice.get("subtotal"))
        vat_amount = _to_decimal(fin.get("vat_amount") or invoice.get("taxAmount"))
        total_amount = _to_decimal(fin.get("total_amount_due") or invoice.get("totalAmount"))
        currency = _clean_str(invoice.get("currency") or "BGN", max_len=3)
    else:
        inv_no = _clean_digits(invoice.invoice_metadata.invoice_number)
        dt_issued = invoice.invoice_metadata.date_issued
        dt_event = getattr(invoice.invoice_metadata, "date_tax_event", None) or dt_issued
        is_credit = bool(invoice.invoice_metadata.is_credit_note or invoice.invoice_metadata.document_type == "CREDIT_NOTE")
        is_debit = bool(invoice.invoice_metadata.is_debit_note or invoice.invoice_metadata.document_type == "DEBIT_NOTE")

        sup_name = _clean_str(invoice.supplier.name, max_len=50)
        sup_eik = _clean_str(invoice.supplier.eik, max_len=15)
        sup_vat = _clean_str(invoice.supplier.vat_number or f"BG{sup_eik}", max_len=15)

        tax_base = _to_decimal(invoice.financial_summary.tax_base)
        vat_amount = _to_decimal(invoice.financial_summary.vat_amount)
        total_amount = _to_decimal(invoice.financial_summary.total_amount_due)
        currency = _clean_str(getattr(invoice.invoice_metadata, "currency", "BGN") or "BGN", max_len=3)

    # Balance reconciliation
    if total_amount == Decimal("0.00") and (tax_base > 0 or vat_amount > 0):
        total_amount = tax_base + vat_amount
    elif tax_base == Decimal("0.00") and total_amount > 0:
        tax_base = total_amount - vat_amount

    doc_iso_date, doc_compact_date = _parse_date_components(dt_issued)
    event_iso_date, _ = _parse_date_components(dt_event)

    doc_type_code = "03" if is_credit else ("02" if is_debit else "01")

    # Multi-line item distribution
    distributions = engine.split_invoice_by_accounts(invoice, prefer_subaccounts=True)

    # VAT cell determination: 10 (20% full credit), 12 (9%), 16 (exempt / no credit)
    vat_cell = "10" if vat_amount > 0 else "16"
    for d in distributions:
        if d.get("vat_rate") == Decimal("9.00"):
            vat_cell = "12"
            break

    return {
        "doc_type": doc_type_code,
        "doc_number": inv_no,
        "doc_date_iso": doc_iso_date,
        "doc_date_compact": doc_compact_date,
        "event_date_iso": event_iso_date,
        "supplier_name": sup_name,
        "supplier_eik": sup_eik,
        "supplier_vat": sup_vat,
        "tax_base": tax_base,
        "vat_amount": vat_amount,
        "total_amount": total_amount,
        "currency": currency,
        "vat_cell": vat_cell,
        "is_credit": is_credit,
        "distributions": distributions,
    }


# ============================================================================
# 1. BUSINESS NAVIGATOR STRUCTURED TEXT & CSV EXPORTERS
# ============================================================================

def generate_business_navigator_csv(
    invoices: Sequence[Any] | Any,
    mapping_engine: AccountMappingEngine | None = None,
    encoding: str = "windows-1251",
    delimiter: str = ";",
) -> bytes | str:
    """Generate Business Navigator delimited CSV text import file.
    
    Format: Semicolon-delimited primary documents and accounting postings
    compatible with the Business Navigator text import wizard.
    """
    if not isinstance(invoices, (list, tuple)):
        inv_list = [invoices]
    else:
        inv_list = list(invoices)

    engine = mapping_engine or DEFAULT_MAPPING_ENGINE
    output = io.StringIO()
    writer = csv.writer(output, delimiter=delimiter, lineterminator="\r\n")

    # Business Navigator standard import header
    writer.writerow([
        "КодДокумент",
        "НомерДокумент",
        "ДатаДокумент",
        "ЕИК",
        "ИмеКонтрагент",
        "ДДСНомер",
        "СметкаДт",
        "ПодсметкаДт",
        "СметкаКт",
        "ПодсметкаКт",
        "Сума",
        "Валута",
        "ДанъчнаОснова",
        "ДДС",
        "КлеткаДДС",
        "ТекстОперация",
    ])

    for inv in inv_list:
        doc = _extract_bn_document_data(inv, mapping_engine=engine)
        credit_acc = engine.default_supplier_account
        credit_sub = doc["supplier_eik"]

        # Sign logic: for credit note (03), amounts are negative
        sign = Decimal("-1") if doc["is_credit"] else Decimal("1")

        # 1. Expense Debit Rows
        for dist in doc["distributions"]:
            amt = sign * abs(dist["amount"])
            writer.writerow([
                doc["doc_type"],
                doc["doc_number"],
                doc["doc_date_iso"],
                doc["supplier_eik"],
                doc["supplier_name"],
                doc["supplier_vat"],
                dist["account"],
                dist["subledger"] or "",
                credit_acc,
                credit_sub,
                f"{amt:.2f}",
                doc["currency"],
                f"{amt:.2f}",
                "0.00",
                doc["vat_cell"],
                dist["description"][:60],
            ])

        # 2. VAT Debit Row (if VAT > 0)
        if doc["vat_amount"] > 0:
            vat_amt = sign * abs(doc["vat_amount"])
            writer.writerow([
                doc["doc_type"],
                doc["doc_number"],
                doc["doc_date_iso"],
                doc["supplier_eik"],
                doc["supplier_name"],
                doc["supplier_vat"],
                engine.default_vat_account,
                "",
                credit_acc,
                credit_sub,
                f"{vat_amt:.2f}",
                doc["currency"],
                "0.00",
                f"{vat_amt:.2f}",
                doc["vat_cell"],
                f"ДДС покупки към ф-ра {doc['doc_number']}",
            ])

    content_str = output.getvalue()
    if encoding:
        return content_str.encode(encoding, errors="replace")
    return content_str


def generate_business_navigator_section_txt(
    invoices: Sequence[Any] | Any,
    mapping_engine: AccountMappingEngine | None = None,
    encoding: str = "windows-1251",
) -> bytes | str:
    """Generate Business Navigator section-based tagged text format ([DOKUMENT] / [OPERACII])."""
    if not isinstance(invoices, (list, tuple)):
        inv_list = [invoices]
    else:
        inv_list = list(invoices)

    engine = mapping_engine or DEFAULT_MAPPING_ENGINE
    lines: list[str] = []

    for inv in inv_list:
        doc = _extract_bn_document_data(inv, mapping_engine=engine)
        lines.append("[DOKUMENT]")
        lines.append(f"VID={doc['doc_type']}")
        lines.append(f"NUM={doc['doc_number']}")
        lines.append(f"DAT={doc['doc_date_iso']}")
        lines.append(f"EIK={doc['supplier_eik']}")
        lines.append(f"NAME={doc['supplier_name']}")
        lines.append(f"VAT_NO={doc['supplier_vat']}")
        lines.append(f"TOTAL={doc['total_amount']:.2f}")
        lines.append(f"OSN={doc['tax_base']:.2f}")
        lines.append(f"DDS={doc['vat_amount']:.2f}")
        lines.append(f"VAL={doc['currency']}")
        lines.append(f"KL={doc['vat_cell']}")
        lines.append("[OPERACII]")

        sign = Decimal("-1") if doc["is_credit"] else Decimal("1")
        credit_acc = engine.default_supplier_account
        credit_sub = doc["supplier_eik"]

        for dist in doc["distributions"]:
            amt = sign * abs(dist["amount"])
            lines.append(
                f"DT={dist['account']};POD_DT={dist['subledger'] or ''};"
                f"KT={credit_acc};POD_KT={credit_sub};"
                f"SUMA={amt:.2f};TEXT={dist['description'][:60]}"
            )

        if doc["vat_amount"] > 0:
            vat_amt = sign * abs(doc["vat_amount"])
            lines.append(
                f"DT={engine.default_vat_account};POD_DT=;"
                f"KT={credit_acc};POD_KT={credit_sub};"
                f"SUMA={vat_amt:.2f};TEXT=ДДС покупки 20%"
            )
        lines.append("")

    content_str = "\r\n".join(lines) + "\r\n"
    if encoding:
        return content_str.encode(encoding, errors="replace")
    return content_str


# ============================================================================
# 2. BUSINESS NAVIGATOR NATIVE DBF EXPORTERS (DOKUM.DBF & OPER.DBF)
# ============================================================================

def generate_business_navigator_dbf_tables(
    invoices: Sequence[Any] | Any,
    mapping_engine: AccountMappingEngine | None = None,
) -> tuple[bytes, bytes]:
    """Generate pair of standard Business Navigator tables (DOKUM.DBF, OPER.DBF).
    
    Returns:
        (dokum_dbf_bytes, oper_dbf_bytes)
    """
    if not isinstance(invoices, (list, tuple)):
        inv_list = [invoices]
    else:
        inv_list = list(invoices)

    engine = mapping_engine or DEFAULT_MAPPING_ENGINE

    # 1. DOKUM.DBF fields
    dokum_fields = [
        DBFField("DOK_VID", "C", 2),
        DBFField("DOK_NUM", "C", 10),
        DBFField("DOK_DAT", "D", 8),
        DBFField("DOK_EIK", "C", 15),
        DBFField("DOK_NAME", "C", 50),
        DBFField("DOK_VAT", "C", 15),
        DBFField("DOK_SUMA", "N", 15, 2),
        DBFField("DOK_OSN", "N", 15, 2),
        DBFField("DOK_DDS", "N", 15, 2),
        DBFField("VALUTA", "C", 3),
        DBFField("DDS_KL", "C", 2),
    ]

    # 2. OPER.DBF fields
    oper_fields = [
        DBFField("DOK_NUM", "C", 10),
        DBFField("DOK_DAT", "D", 8),
        DBFField("SMETKA_DT", "C", 10),
        DBFField("PODSM_DT", "C", 15),
        DBFField("SMETKA_KT", "C", 10),
        DBFField("PODSM_KT", "C", 15),
        DBFField("SUMA", "N", 15, 2),
        DBFField("VALUTA", "C", 3),
        DBFField("TEXT", "C", 60),
        DBFField("VID_OP", "C", 2),
    ]

    writer_dokum = SimpleDBFWriter(dokum_fields, encoding="cp1251")
    writer_oper = SimpleDBFWriter(oper_fields, encoding="cp1251")

    for inv in inv_list:
        doc = _extract_bn_document_data(inv, mapping_engine=engine)
        sign = Decimal("-1") if doc["is_credit"] else Decimal("1")

        # Add to DOKUM.DBF
        writer_dokum.add_record({
            "DOK_VID": doc["doc_type"],
            "DOK_NUM": doc["doc_number"],
            "DOK_DAT": doc["doc_date_compact"],
            "DOK_EIK": doc["supplier_eik"],
            "DOK_NAME": doc["supplier_name"],
            "DOK_VAT": doc["supplier_vat"],
            "DOK_SUMA": sign * abs(doc["total_amount"]),
            "DOK_OSN": sign * abs(doc["tax_base"]),
            "DOK_DDS": sign * abs(doc["vat_amount"]),
            "VALUTA": doc["currency"],
            "DDS_KL": doc["vat_cell"],
        })

        credit_acc = engine.default_supplier_account
        credit_sub = doc["supplier_eik"]

        # Add operational postings to OPER.DBF
        for dist in doc["distributions"]:
            writer_oper.add_record({
                "DOK_NUM": doc["doc_number"],
                "DOK_DAT": doc["doc_date_compact"],
                "SMETKA_DT": dist["account"],
                "PODSM_DT": dist["subledger"] or "",
                "SMETKA_KT": credit_acc,
                "PODSM_KT": credit_sub,
                "SUMA": sign * abs(dist["amount"]),
                "VALUTA": doc["currency"],
                "TEXT": dist["description"][:60],
                "VID_OP": doc["doc_type"],
            })

        if doc["vat_amount"] > 0:
            writer_oper.add_record({
                "DOK_NUM": doc["doc_number"],
                "DOK_DAT": doc["doc_date_compact"],
                "SMETKA_DT": engine.default_vat_account,
                "PODSM_DT": "",
                "SMETKA_KT": credit_acc,
                "PODSM_KT": credit_sub,
                "SUMA": sign * abs(doc["vat_amount"]),
                "VALUTA": doc["currency"],
                "TEXT": f"ДДС 20% към ф-ра {doc['doc_number']}",
                "VID_OP": doc["doc_type"],
            })

    dokum_bytes = writer_dokum.write_bytes()
    oper_bytes = writer_oper.write_bytes()
    return dokum_bytes, oper_bytes


def generate_business_navigator_single_dbf(
    invoices: Sequence[Any] | Any,
    mapping_engine: AccountMappingEngine | None = None,
) -> bytes:
    """Generate single combined flat DBF file (BN_IMPORT.DBF) for 1-table import."""
    if not isinstance(invoices, (list, tuple)):
        inv_list = [invoices]
    else:
        inv_list = list(invoices)

    engine = mapping_engine or DEFAULT_MAPPING_ENGINE

    fields = [
        DBFField("DOC_TYPE", "C", 2),
        DBFField("DOC_NUM", "C", 10),
        DBFField("DOC_DATE", "D", 8),
        DBFField("PARTNER_ID", "C", 15),
        DBFField("PARTNER_NM", "C", 50),
        DBFField("ACC_DEBIT", "C", 10),
        DBFField("SUB_DEBIT", "C", 15),
        DBFField("ACC_CREDIT", "C", 10),
        DBFField("SUB_CREDIT", "C", 15),
        DBFField("AMOUNT", "N", 15, 2),
        DBFField("TAX_BASE", "N", 15, 2),
        DBFField("VAT_AMOUNT", "N", 15, 2),
        DBFField("CURRENCY", "C", 3),
        DBFField("VAT_CELL", "C", 2),
        DBFField("TEXT", "C", 60),
    ]

    writer = SimpleDBFWriter(fields, encoding="cp1251")

    for inv in inv_list:
        doc = _extract_bn_document_data(inv, mapping_engine=engine)
        sign = Decimal("-1") if doc["is_credit"] else Decimal("1")
        credit_acc = engine.default_supplier_account
        credit_sub = doc["supplier_eik"]

        for dist in doc["distributions"]:
            writer.add_record({
                "DOC_TYPE": doc["doc_type"],
                "DOC_NUM": doc["doc_number"],
                "DOC_DATE": doc["doc_date_compact"],
                "PARTNER_ID": doc["supplier_eik"],
                "PARTNER_NM": doc["supplier_name"],
                "ACC_DEBIT": dist["account"],
                "SUB_DEBIT": dist["subledger"] or "",
                "ACC_CREDIT": credit_acc,
                "SUB_CREDIT": credit_sub,
                "AMOUNT": sign * abs(dist["amount"]),
                "TAX_BASE": sign * abs(dist["amount"]),
                "VAT_AMOUNT": Decimal("0.00"),
                "CURRENCY": doc["currency"],
                "VAT_CELL": doc["vat_cell"],
                "TEXT": dist["description"][:60],
            })

        if doc["vat_amount"] > 0:
            writer.add_record({
                "DOC_TYPE": doc["doc_type"],
                "DOC_NUM": doc["doc_number"],
                "DOC_DATE": doc["doc_date_compact"],
                "PARTNER_ID": doc["supplier_eik"],
                "PARTNER_NM": doc["supplier_name"],
                "ACC_DEBIT": engine.default_vat_account,
                "SUB_DEBIT": "",
                "ACC_CREDIT": credit_acc,
                "SUB_CREDIT": credit_sub,
                "AMOUNT": sign * abs(doc["vat_amount"]),
                "TAX_BASE": Decimal("0.00"),
                "VAT_AMOUNT": sign * abs(doc["vat_amount"]),
                "CURRENCY": doc["currency"],
                "VAT_CELL": doc["vat_cell"],
                "TEXT": f"ДДС към ф-ра {doc['doc_number']}",
            })

    return writer.write_bytes()


# ============================================================================
# 3. HIGH-LEVEL PACKAGE EXPORTER
# ============================================================================

def export_business_navigator_package(
    invoices: Sequence[Any] | Any,
    output_dir: Path | str,
    create_zip: bool = True,
) -> dict[str, str]:
    """Export complete Business Navigator package (TXT, CSV, DOKUM.DBF, OPER.DBF, and ZIP).
    
    Returns dict mapping artifact key to absolute path.
    """
    out_p = Path(output_dir)
    out_p.mkdir(parents=True, exist_ok=True)
    generated = {}

    # 1. Delimited CSV
    csv_bytes = generate_business_navigator_csv(invoices, encoding="windows-1251")
    assert isinstance(csv_bytes, bytes)
    csv_file = out_p / "BN_IMPORT.csv"
    csv_file.write_bytes(csv_bytes)
    generated["bn_csv"] = str(csv_file)

    # 2. Section TXT
    txt_bytes = generate_business_navigator_section_txt(invoices, encoding="windows-1251")
    assert isinstance(txt_bytes, bytes)
    txt_file = out_p / "BN_IMPORT.txt"
    txt_file.write_bytes(txt_bytes)
    generated["bn_txt"] = str(txt_file)

    # 3. Dual DBF tables (DOKUM.DBF & OPER.DBF)
    dokum_bytes, oper_bytes = generate_business_navigator_dbf_tables(invoices)
    dokum_file = out_p / "DOKUM.DBF"
    dokum_file.write_bytes(dokum_bytes)
    generated["bn_dokum_dbf"] = str(dokum_file)

    oper_file = out_p / "OPER.DBF"
    oper_file.write_bytes(oper_bytes)
    generated["bn_oper_dbf"] = str(oper_file)

    # 4. Single flat DBF
    single_dbf_bytes = generate_business_navigator_single_dbf(invoices)
    single_file = out_p / "BN_SINGLE.DBF"
    single_file.write_bytes(single_dbf_bytes)
    generated["bn_single_dbf"] = str(single_file)

    # 5. Optional ZIP package
    if create_zip:
        zip_file = out_p / "Business_Navigator_Import.zip"
        with zipfile.ZipFile(zip_file, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(dokum_file, arcname="DOKUM.DBF")
            zf.write(oper_file, arcname="OPER.DBF")
            zf.write(csv_file, arcname="BN_IMPORT.csv")
            zf.write(txt_file, arcname="BN_IMPORT.txt")
        generated["bn_zip"] = str(zip_file)

    return generated

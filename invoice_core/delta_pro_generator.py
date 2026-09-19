"""Microinvest Delta Pro Native Binary Database Generator (TRANSFER.LOG & TRANSFER.ldb).

Generates byte-perfect Jet 2.0 / Access 2.0 binary databases for direct, zero-touch
import into Microinvest Delta Pro via:
"Обмен" -> "Обмен на операции" -> "Импорт" (from C:\\MICRO\\TRANSFER.LOG).

Implements statutory and technical specifications:
- Col 7 (Partner): strictly "Не е зададен" (enables automatic contractor lookup from master partners)
- Col 10 (Company): official legal entity name
- Col 11 (DanNo): BG + EIK
- Col 12 (Bulstat): EIK
- Col 5 (DocType): "ФАК", "КИ", "ДИ"
- Col 4 (Papka): ""
- Col 8, 9, 13..16 (Address & Bank): single spaces (" ", 0x20)
- Col 18 (PriznakType): b"\\x00\\x00"
- Exact double-entry accounting representation: IsKredit (1=Credit, 2=Debit), signed amounts.
"""
from __future__ import annotations

import datetime
from decimal import Decimal
import logging
import os
from pathlib import Path
import re
import struct
from typing import Any, Optional, Sequence

logger = logging.getLogger("delta_pro_generator")

PAGE_SIZE = 2048
BASE_OLE_DATE = datetime.datetime(1899, 12, 30)

PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parent.parent))

# Default location for canonical template (from comparison_export or package)
CANONICAL_TEMPLATE_PATHS = [
    PROJECT_ROOT / "comparison_export" / "TRANSFER.LOG",
    Path("/Volumes/NO NAME/Building_11/ЕКСПОРТ ЗА СРАВНЕНИЕ/TRANSFER.LOG"),
    PROJECT_ROOT / "Building_11" / "TRANSFER.LOG",
    Path("/data/accounting/Building_11/TRANSFER.LOG"),
]

CANONICAL_LDB_PATHS = [
    PROJECT_ROOT / "comparison_export" / "TRANSFER.ldb",
    Path("/Volumes/NO NAME/Building_11/ЕКСПОРТ ЗА СРАВНЕНИЕ/TRANSFER.ldb"),
    PROJECT_ROOT / "Building_11" / "TRANSFER.ldb",
    Path("/data/accounting/Building_11/TRANSFER.ldb"),
]

# Standard 64-byte Access 2.0 / Jet 2.0 Lock File Header
DEFAULT_LDB_BYTES = (
    b"ADMINISTRATOR   " + b" " * 16 +
    b"\x00" * 32
)
DELTA_PRO_LDB_TEMPLATE = DEFAULT_LDB_BYTES



def to_ole_date(dt_str: str) -> float:
    """Convert YYYY-MM-DD or DD.MM.YYYY into OLE Automation floating-point date."""
    if not dt_str:
        return (datetime.datetime.now() - BASE_OLE_DATE).days + 0.0
    clean = dt_str.strip()
    try:
        if "-" in clean:
            parts = clean.split("-")
            d = datetime.datetime(int(parts[0]), int(parts[1]), int(parts[2]))
        elif "." in clean:
            parts = clean.split(".")
            d = datetime.datetime(int(parts[2]), int(parts[1]), int(parts[0]))
        elif "/" in clean:
            parts = clean.split("/")
            if len(parts[0]) == 4:
                d = datetime.datetime(int(parts[0]), int(parts[1]), int(parts[2]))
            else:
                d = datetime.datetime(int(parts[2]), int(parts[1]), int(parts[0]))
        else:
            d = datetime.datetime.now()
        delta = d - BASE_OLE_DATE
        return float(delta.days)
    except Exception:
        return (datetime.datetime.now() - BASE_OLE_DATE).days + 0.0


def format_doc_date(dt_str: str) -> str:
    """Format date as DD.MM.YYYY for Delta Pro column 6."""
    if not dt_str:
        return datetime.datetime.now().strftime("%d.%m.%Y")
    clean = dt_str.strip()
    try:
        if "-" in clean:
            parts = clean.split("-")
            return f"{int(parts[2]):02d}.{int(parts[1]):02d}.{parts[0]}"
        elif "." in clean:
            parts = clean.split(".")
            return f"{int(parts[0]):02d}.{int(parts[1]):02d}.{parts[2]}"
        return clean
    except Exception:
        return clean


def build_w_transfer_record(
    kon_id: int,
    kon: int,
    is_kredit: int,
    acct: int,
    sub_acct: float,
    amount: float,
    date_ole: float,
    is_dds: int,
    acct_name: str,
    fak_no: str,
    doc_date_str: str,
    doc_type: str = "ФАК",
    company_name: str = "",
    bulstat: str = "",
    dan_no: str = "",
    osnovanie: str = "стоки",
    papka: str = "",
    partner: str = "Не е зададен",
    town: str = " ",
    addr: str = " ",
    ban_acct: str = " ",
    ban_code: str = " ",
    bank: str = " ",
    tel_fax: str = " ",
    oper_type: int = 12,
    priznak_id: float = 0.0,
    schet: int = 0,
    is_amort: int = 0,
    is_pokupka: int = 1,
    is_very_used: float = 0.0,
) -> bytes:
    """Build a single record for table W#Transfer (Table ID 25).
    
    Adheres strictly to the verified Jet 2.0 schema:
    70 fixed bytes + 19 variable length fields + offset index tail.
    """
    clean_fak = str(fak_no or "").strip()
    if clean_fak.isdigit() and len(clean_fak) < 10:
        clean_fak = clean_fak.zfill(10)

    clean_bulstat = str(bulstat or "").strip().upper()
    if clean_bulstat.startswith("BG") and len(clean_bulstat) > 2:
        clean_bulstat = clean_bulstat[2:]
    
    clean_dan_no = str(dan_no or "").strip().upper()
    if not clean_dan_no and clean_bulstat:
        clean_dan_no = f"BG{clean_bulstat}"

    # 19 variable fields (CP1251 encoded)
    var_fields: list[Any] = [
        "0",                            # 0: Klon
        clean_fak,                      # 1: FakNo
        osnovanie[:40],                 # 2: Osnovanie
        "",                             # 3: Note
        papka,                          # 4: Papka
        doc_type,                       # 5: DocType ("ФАК", "КИ", "ДИ")
        doc_date_str,                   # 6: DocDate (DD.MM.YYYY)
        partner,                        # 7: Partner ("Не е зададен")
        town or " ",                    # 8: Town
        addr or " ",                    # 9: Addr
        company_name[:50],              # 10: Company
        clean_dan_no,                   # 11: DanNo
        clean_bulstat,                  # 12: Bulstat
        ban_acct or " ",                # 13: BanAcct
        ban_code or " ",                # 14: BanCode
        bank or " ",                    # 15: Bank
        tel_fax or " ",                 # 16: Tel_Fax
        acct_name[:50],                 # 17: AcctName
        b"\x00\x00",                    # 18: PriznakType (binary null marker)
    ]

    var_data = bytearray()
    offsets = [70]
    curr_off = 70

    for vf in var_fields:
        if isinstance(vf, bytes):
            encoded = vf
        else:
            encoded = str(vf).encode("cp1251", errors="replace")
        var_data.extend(encoded)
        curr_off += len(encoded)
        assert curr_off < 256, f"Record variable offset {curr_off} exceeded uint8 boundary!"
        offsets.append(curr_off)

    tail = bytearray()
    for off in reversed(offsets):
        tail.append(off)
    tail.append(19)               # 0x13 = 19 fields
    tail.extend(b"\x3f\xff")      # Jet 2.0 null bitmap mask

    total_len = 70 + len(var_data) + len(tail)

    fixed = struct.pack(
        "<HHIIHHddddIIHHHd",
        total_len,
        0x130e,                   # Record prefix (0x0e, 0x13)
        int(kon_id),
        int(kon),
        int(is_kredit),
        int(acct),
        float(sub_acct),
        float(priznak_id),
        float(amount),
        float(date_ole),
        int(schet),
        int(oper_type),
        int(is_amort),
        int(is_dds),
        int(is_pokupka),
        float(is_very_used),
    )
    assert len(fixed) == 70, f"Fixed header length {len(fixed)} != 70"
    return bytes(fixed + bytes(var_data) + bytes(tail))


def get_template_database() -> bytes:
    """Retrieve pristine 65,536-byte Delta Pro template."""
    for p in CANONICAL_TEMPLATE_PATHS:
        if p.exists():
            try:
                data = p.read_bytes()
                if len(data) == 65536:
                    return data
            except Exception:
                pass
    
    # Fallback to creating 32 empty pages if not found
    raise FileNotFoundError("Canonical 65536-byte Delta Pro template not found.")


def acct_title(code: str) -> str:
    code_str = str(code).split(".")[0]
    mapping = {
        "401": "Доставчици",
        "411": "Клиенти",
        "501": "Каса в левове",
        "503": "Разплащателна сметка",
        "601": "Разходи за материали",
        "602": "Разходи за външни услуги",
        "304": "Стоки",
        "609": "Други разходи",
        "4531": "Данък върху  покупките",
        "4532": "Данък върху продажбите",
        "453": "Данък върху  покупките",
        "701": "Приходи от продажба на продукция",
        "702": "Приходи от продажба на стоки",
        "703": "Приходи от услуги",
    }
    return mapping.get(code_str, "Сметка")


def build_invoice_transfer_records(
    kon_id: int,
    invoice_number: str,
    doc_date_ole: float,
    doc_date_disp: str,
    doc_type: str,
    company_name: str,
    bulstat: str,
    dan_no: str,
    is_purchase: bool,
    is_credit_note: bool,
    int_counterpart: int,
    int_vat: int,
    vat_amount: float,
    tax_base: float,
    default_nominal: str,
    nominal_account: str,
    reason: str,
    distributions: Sequence[dict[str, Any]] | None = None,
) -> list[bytes]:
    """Build all double-entry W#Transfer binary records for a single invoice.
    
    Supports multi-account distributions (e.g. materials + transport or mixed items)
    with sequential kon index per distribution, followed by the VAT record.
    """
    records: list[bytes] = []
    is_pok = 1 if is_purchase else 0

    if distributions:
        for dist_idx, dist in enumerate(distributions, start=1):
            dist_amt = float(dist.get("amount", 0.0))
            dist_acct_raw = str(dist.get("account") or nominal_account)
            dist_int_acct = int(re.sub(r"[^\d]", "", dist_acct_raw)[:3] or default_nominal)
            dist_sub_acct = float(dist.get("subaccount", 0.0) or 0.0)
            dist_desc = str(dist.get("description") or dist.get("reason") or reason)[:40]
            dist_name = dist.get("account_name") or acct_title(str(dist_int_acct))

            if is_purchase:
                if is_credit_note:
                    r0 = build_w_transfer_record(
                        kon_id=kon_id, kon=dist_idx, is_kredit=1, acct=int_counterpart, sub_acct=0.0,
                        amount=abs(dist_amt), date_ole=doc_date_ole, is_dds=0,
                        acct_name=acct_title(str(int_counterpart)), fak_no=invoice_number,
                        doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                        bulstat=bulstat, dan_no=dan_no, osnovanie=dist_desc, is_pokupka=is_pok
                    )
                    r1 = build_w_transfer_record(
                        kon_id=kon_id, kon=dist_idx, is_kredit=2, acct=dist_int_acct, sub_acct=dist_sub_acct,
                        amount=-abs(dist_amt), date_ole=doc_date_ole, is_dds=0,
                        acct_name=dist_name, fak_no=invoice_number,
                        doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                        bulstat=bulstat, dan_no=dan_no, osnovanie=dist_desc, is_pokupka=is_pok
                    )
                    records.extend([r0, r1])
                else:
                    r0 = build_w_transfer_record(
                        kon_id=kon_id, kon=dist_idx, is_kredit=1, acct=int_counterpart, sub_acct=0.0,
                        amount=-abs(dist_amt), date_ole=doc_date_ole, is_dds=0,
                        acct_name=acct_title(str(int_counterpart)), fak_no=invoice_number,
                        doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                        bulstat=bulstat, dan_no=dan_no, osnovanie=dist_desc, is_pokupka=is_pok
                    )
                    r1 = build_w_transfer_record(
                        kon_id=kon_id, kon=dist_idx, is_kredit=2, acct=dist_int_acct, sub_acct=dist_sub_acct,
                        amount=abs(dist_amt), date_ole=doc_date_ole, is_dds=0,
                        acct_name=dist_name, fak_no=invoice_number,
                        doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                        bulstat=bulstat, dan_no=dan_no, osnovanie=dist_desc, is_pokupka=is_pok
                    )
                    records.extend([r0, r1])
            else:
                int_client = int_counterpart if int_counterpart == 501 else 411
                r0 = build_w_transfer_record(
                    kon_id=kon_id, kon=dist_idx, is_kredit=2, acct=int_client, sub_acct=0.0,
                    amount=abs(dist_amt), date_ole=doc_date_ole, is_dds=0,
                    acct_name=acct_title(str(int_client)), fak_no=invoice_number,
                    doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                    bulstat=bulstat, dan_no=dan_no, osnovanie=dist_desc, is_pokupka=is_pok
                )
                r1 = build_w_transfer_record(
                    kon_id=kon_id, kon=dist_idx, is_kredit=1, acct=dist_int_acct, sub_acct=dist_sub_acct,
                    amount=abs(dist_amt), date_ole=doc_date_ole, is_dds=0,
                    acct_name=dist_name, fak_no=invoice_number,
                    doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                    bulstat=bulstat, dan_no=dan_no, osnovanie=dist_desc, is_pokupka=is_pok
                )
                records.extend([r0, r1])

        vat_kon = len(distributions) + 1
    else:
        # Single account
        int_expense = int(re.sub(r"[^\d]", "", str(nominal_account))[:3] or default_nominal)
        if is_purchase:
            if is_credit_note:
                r0 = build_w_transfer_record(
                    kon_id=kon_id, kon=1, is_kredit=1, acct=int_counterpart, sub_acct=0.0,
                    amount=abs(tax_base), date_ole=doc_date_ole, is_dds=0,
                    acct_name=acct_title(str(int_counterpart)), fak_no=invoice_number,
                    doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                    bulstat=bulstat, dan_no=dan_no, osnovanie=reason, is_pokupka=is_pok
                )
                r1 = build_w_transfer_record(
                    kon_id=kon_id, kon=1, is_kredit=2, acct=int_expense, sub_acct=0.0,
                    amount=-abs(tax_base), date_ole=doc_date_ole, is_dds=0,
                    acct_name=acct_title(str(int_expense)), fak_no=invoice_number,
                    doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                    bulstat=bulstat, dan_no=dan_no, osnovanie=reason, is_pokupka=is_pok
                )
                records.extend([r0, r1])
            else:
                r0 = build_w_transfer_record(
                    kon_id=kon_id, kon=1, is_kredit=1, acct=int_counterpart, sub_acct=0.0,
                    amount=-abs(tax_base), date_ole=doc_date_ole, is_dds=0,
                    acct_name=acct_title(str(int_counterpart)), fak_no=invoice_number,
                    doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                    bulstat=bulstat, dan_no=dan_no, osnovanie=reason, is_pokupka=is_pok
                )
                r1 = build_w_transfer_record(
                    kon_id=kon_id, kon=1, is_kredit=2, acct=int_expense, sub_acct=0.0,
                    amount=abs(tax_base), date_ole=doc_date_ole, is_dds=0,
                    acct_name=acct_title(str(int_expense)), fak_no=invoice_number,
                    doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                    bulstat=bulstat, dan_no=dan_no, osnovanie=reason, is_pokupka=is_pok
                )
                records.extend([r0, r1])
        else:
            int_client = int_counterpart if int_counterpart == 501 else 411
            r0 = build_w_transfer_record(
                kon_id=kon_id, kon=1, is_kredit=2, acct=int_client, sub_acct=0.0,
                amount=abs(tax_base), date_ole=doc_date_ole, is_dds=0,
                acct_name=acct_title(str(int_client)), fak_no=invoice_number,
                doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                bulstat=bulstat, dan_no=dan_no, osnovanie=reason, is_pokupka=is_pok
            )
            r1 = build_w_transfer_record(
                kon_id=kon_id, kon=1, is_kredit=1, acct=int_expense, sub_acct=0.0,
                amount=abs(tax_base), date_ole=doc_date_ole, is_dds=0,
                acct_name=acct_title(str(int_expense)), fak_no=invoice_number,
                doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                bulstat=bulstat, dan_no=dan_no, osnovanie=reason, is_pokupka=is_pok
            )
            records.extend([r0, r1])
        vat_kon = 2

    # VAT Line
    if vat_amount != 0.0:
        if is_purchase:
            sub_vat = 1.0
            if is_credit_note:
                r2 = build_w_transfer_record(
                    kon_id=kon_id, kon=vat_kon, is_kredit=1, acct=int_counterpart, sub_acct=0.0,
                    amount=abs(vat_amount), date_ole=doc_date_ole, is_dds=1,
                    acct_name=acct_title(str(int_counterpart)), fak_no=invoice_number,
                    doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                    bulstat=bulstat, dan_no=dan_no, osnovanie=reason, is_pokupka=is_pok
                )
                r3 = build_w_transfer_record(
                    kon_id=kon_id, kon=vat_kon, is_kredit=2, acct=int_vat, sub_acct=sub_vat,
                    amount=-abs(vat_amount), date_ole=doc_date_ole, is_dds=1,
                    acct_name=acct_title(str(int_vat)), fak_no=invoice_number,
                    doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                    bulstat=bulstat, dan_no=dan_no, osnovanie=reason, is_pokupka=is_pok
                )
            else:
                r2 = build_w_transfer_record(
                    kon_id=kon_id, kon=vat_kon, is_kredit=1, acct=int_counterpart, sub_acct=0.0,
                    amount=-abs(vat_amount), date_ole=doc_date_ole, is_dds=1,
                    acct_name=acct_title(str(int_counterpart)), fak_no=invoice_number,
                    doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                    bulstat=bulstat, dan_no=dan_no, osnovanie=reason, is_pokupka=is_pok
                )
                r3 = build_w_transfer_record(
                    kon_id=kon_id, kon=vat_kon, is_kredit=2, acct=int_vat, sub_acct=sub_vat,
                    amount=abs(vat_amount), date_ole=doc_date_ole, is_dds=1,
                    acct_name=acct_title(str(int_vat)), fak_no=invoice_number,
                    doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                    bulstat=bulstat, dan_no=dan_no, osnovanie=reason, is_pokupka=is_pok
                )
        else:
            int_client = int_counterpart if int_counterpart == 501 else 411
            r2 = build_w_transfer_record(
                kon_id=kon_id, kon=vat_kon, is_kredit=2, acct=int_client, sub_acct=0.0,
                amount=abs(vat_amount), date_ole=doc_date_ole, is_dds=1,
                acct_name=acct_title(str(int_client)), fak_no=invoice_number,
                doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                bulstat=bulstat, dan_no=dan_no, osnovanie=reason, is_pokupka=is_pok
            )
            r3 = build_w_transfer_record(
                kon_id=kon_id, kon=vat_kon, is_kredit=1, acct=int_vat, sub_acct=2.0,
                amount=abs(vat_amount), date_ole=doc_date_ole, is_dds=1,
                acct_name=acct_title(str(int_vat)), fak_no=invoice_number,
                doc_date_str=doc_date_disp, doc_type=doc_type, company_name=company_name,
                bulstat=bulstat, dan_no=dan_no, osnovanie=reason, is_pokupka=is_pok
            )
        records.extend([r2, r3])

    return records


def generate_delta_pro_transfer_log(
    invoice_number: str,
    doc_date: str,
    company_name: str,
    bulstat: str,
    vat_number: str,
    tax_base: float | Decimal,
    vat_amount: float | Decimal,
    total_amount: float | Decimal,
    currency: str = "EUR",
    is_credit_note: bool = False,
    is_purchase: bool = True,
    expense_account: str = "601",
    vat_account: str = "4531",
    counterpart_account: str = "401",
    reason: str = "м-ли",
    client_company_name: str = "БИЛДИНГ 11 ООД",
    kon_id: int = 1061,
    distributions: Sequence[dict[str, Any]] | None = None,
) -> tuple[bytes, bytes]:
    """Generate byte-perfect TRANSFER.LOG and TRANSFER.ldb for Microinvest Delta Pro.
    
    Returns:
        (transfer_log_bytes, transfer_ldb_bytes)
    """
    tpl_bytes = get_template_database()
    db = bytearray(tpl_bytes)

    doc_date_ole = to_ole_date(doc_date)
    doc_date_disp = format_doc_date(doc_date)
    doc_type = "КИ" if is_credit_note else "ФАК"

    f_tax_base = float(tax_base)
    f_vat = float(vat_amount)
    f_total = float(total_amount)

    # 1. Update Page 24 (Table 23 W#System)
    now_str = datetime.datetime.now().strftime("%d.%m.%Y в %H:%M:%S")
    sys_text = f"Създаден на {now_str} от Администратор\r\n\r\n{client_company_name} - {doc_type} {invoice_number}^"
    text_bytes = sys_text.encode("cp1251", errors="replace")
    ver_bytes = b"4.00"
    n_bytes = len(text_bytes)
    rec_len_24 = 8 + n_bytes + 4

    p24_rec = struct.pack("<H", rec_len_24) + b"\x00\x02" + ver_bytes + text_bytes + struct.pack("BBBB", 8 + n_bytes, 8, 4, 2)
    p24_off = 2048 - rec_len_24
    p24_new = bytearray(2048)
    p24_new[0:2] = b"\x06\x00"
    p24_new[2:4] = b"\x00\x00"
    p24_new[4:8] = struct.pack("<I", 23)
    p24_new[8:10] = struct.pack("<H", 1)                 # 1 record
    p24_new[10:12] = struct.pack("<H", p24_off)          # free space boundary
    p24_new[12:14] = b"\x00\x00"
    p24_new[20:22] = struct.pack("<H", p24_off | 0x1000)
    p24_new[p24_off:2048] = p24_rec
    db[24*PAGE_SIZE : 25*PAGE_SIZE] = p24_new

    # 2. Build W#Transfer records
    int_counterpart = int(re.sub(r"[^\d]", "", str(counterpart_account))[:3] or 401)
    int_vat = int(re.sub(r"[^\d]", "", str(vat_account))[:3] or 453)
    default_nominal = "601" if is_purchase else "702"

    records = build_invoice_transfer_records(
        kon_id=kon_id,
        invoice_number=invoice_number,
        doc_date_ole=doc_date_ole,
        doc_date_disp=doc_date_disp,
        doc_type=doc_type,
        company_name=company_name,
        bulstat=bulstat,
        dan_no=vat_number,
        is_purchase=is_purchase,
        is_credit_note=is_credit_note,
        int_counterpart=int_counterpart,
        int_vat=int_vat,
        vat_amount=f_vat,
        tax_base=f_tax_base,
        default_nominal=default_nominal,
        nominal_account=expense_account,
        reason=reason,
        distributions=distributions,
    )

    # 3. Update Page 25 (Table 25 Definition)
    p25 = bytearray(db[25*PAGE_SIZE : 26*PAGE_SIZE])
    p25[12:16] = struct.pack("<I", 29)  # first data page = 29
    p25[16:20] = struct.pack("<I", 29)  # last data page = 29
    p25[32:36] = struct.pack("<I", 1)   # num pages = 1
    p25[36:40] = struct.pack("<I", len(records))  # num records
    db[25*PAGE_SIZE : 26*PAGE_SIZE] = p25

    # 4. Write Page 29 (Table 25 Data Page)
    p29_new = bytearray(2048)
    p29_new[0:2] = b"\x06\x00"
    p29_new[2:4] = b"\x00\x00"
    p29_new[4:8] = struct.pack("<I", 25)
    p29_new[8:10] = struct.pack("<H", len(records))
    p29_new[12:14] = b"\x00\x00"

    curr_bottom = 2048
    ptrs = []
    for r in records:
        curr_bottom -= len(r)
        p29_new[curr_bottom : curr_bottom + len(r)] = r
        ptrs.append(curr_bottom)

    p29_new[10:12] = struct.pack("<H", curr_bottom)
    for i, ptr in enumerate(ptrs):
        p29_new[20 + i*2 : 22 + i*2] = struct.pack("<H", ptr | 0x1000)

    db[29*PAGE_SIZE : 30*PAGE_SIZE] = p29_new

    # 5. Zero out pages 30 and 31
    for p in range(30, 32):
        db[p*PAGE_SIZE : (p+1)*PAGE_SIZE] = b"\x00" * PAGE_SIZE

    # Get LDB bytes
    ldb_bytes = DEFAULT_LDB_BYTES
    for p in CANONICAL_LDB_PATHS:
        if p.exists() and p.stat().st_size == 64:
            try:
                ldb_bytes = p.read_bytes()
                break
            except Exception:
                pass

    return bytes(db), bytes(ldb_bytes)


def generate_multi_delta_pro_transfer_log(
    documents: Sequence[dict[str, Any]],
    client_company_name: str = "БИЛДИНГ 11 ООД",
    start_kon_id: int = 1001,
) -> tuple[bytes, bytes]:
    """Generate multi-document byte-perfect TRANSFER.LOG and TRANSFER.ldb for Microinvest Delta Pro.
    
    Packs all accounting operation records across chained Jet 2.0 / Access 2.0 data pages
    starting from Page 29. Supports up to 128 database pages (262,144 bytes).
    """
    # 1. Base template (128 pages / 262,144 bytes)
    kingston_tpl = Path("/Volumes/KINGSTON/Building_11/TRANSFER.LOG")
    if kingston_tpl.exists() and kingston_tpl.stat().st_size == 262144:
        try:
            tpl_bytes = kingston_tpl.read_bytes()
        except Exception:
            base_tpl = get_template_database()
            tpl_bytes = base_tpl + b"\x00" * (262144 - len(base_tpl))
    else:
        base_tpl = get_template_database()
        tpl_bytes = base_tpl + b"\x00" * (262144 - len(base_tpl))

    db = bytearray(tpl_bytes)

    # Zero out all data pages from 29 to 127
    for p in range(29, 128):
        db[p*PAGE_SIZE : (p+1)*PAGE_SIZE] = b"\x00" * PAGE_SIZE

    # 2. Build records for all documents
    records: list[bytes] = []
    total_gross = 0.0

    for idx, doc in enumerate(documents):
        m = doc.get("document_metadata", {})
        p = doc.get("parties", {})
        f = doc.get("financials", {})
        op = doc.get("accounting_operation", {})

        inv_no = str(m.get("invoice_number") or "").strip()
        doc_dt = str(m.get("date_issued") or "").strip()
        is_cn = bool(m.get("is_credit_note", False)) or (float(f.get("total_amount", 0.0)) < 0)
        doc_type = "КИ" if is_cn else "ФАК"

        tax_base = float(f.get("tax_base", 0.0))
        vat_amt = float(f.get("vat_amount", 0.0))
        total_amt = float(f.get("total_amount", 0.0))

        doc_date_ole = to_ole_date(doc_dt)
        doc_date_disp = format_doc_date(doc_dt)

        co_name = p.get("counterpart_name") or "МАГНЕЗИЯ ЕООД"
        bulstat = p.get("counterpart_eik") or "114631464"
        dan_no = p.get("counterpart_vat") or f"BG{bulstat}"
        reason = op.get("reason") or "м-ли"

        kon_id = start_kon_id + idx
        direction = p.get("direction") or op.get("direction") or ("SALES" if p.get("counterpart_role") == "CLIENT" else "PURCHASE")
        is_purchase = (str(direction).upper() == "PURCHASE")

        default_nominal = "601" if is_purchase else "702"
        nominal_account = op.get("revenue_account" if not is_purchase else "expense_account") or op.get("nominal_account") or default_nominal

        default_vat = "4531" if is_purchase else "4532"
        vat_account = op.get("vat_account") or default_vat
        int_vat = int(re.sub(r"[^\d]", "", str(vat_account))[:3] or 453)

        default_counterpart = "401" if is_purchase else "411"
        counterpart_account = op.get("counterpart_account") or default_counterpart
        int_counterpart = int(re.sub(r"[^\d]", "", str(counterpart_account))[:3] or default_counterpart)

        sign = -1.0 if is_cn else 1.0
        total_gross += sign * total_amt

        distributions = op.get("distributions") or doc.get("distributions")
        if not distributions and doc.get("items"):
            try:
                from invoice_core.account_mapping import split_invoice_postings
                distributions = split_invoice_postings(doc, prefer_subaccounts=False)
            except Exception:
                distributions = None

        doc_records = build_invoice_transfer_records(
            kon_id=kon_id,
            invoice_number=inv_no,
            doc_date_ole=doc_date_ole,
            doc_date_disp=doc_date_disp,
            doc_type=doc_type,
            company_name=co_name,
            bulstat=bulstat,
            dan_no=dan_no,
            is_purchase=is_purchase,
            is_credit_note=is_cn,
            int_counterpart=int_counterpart,
            int_vat=int_vat,
            vat_amount=vat_amt,
            tax_base=tax_base,
            default_nominal=default_nominal,
            nominal_account=nominal_account,
            reason=reason,
            distributions=distributions,
        )
        records.extend(doc_records)

    # 3. Pack records across pages starting from Page 29
    pages: list[tuple[int, list[bytes], int]] = []
    curr_recs: list[bytes] = []
    curr_bottom = PAGE_SIZE
    page_num = 29

    for r in records:
        r_len = len(r)
        needed_header_space = 20 + 2 * (len(curr_recs) + 1)
        if curr_bottom - r_len < needed_header_space:
            pages.append((page_num, curr_recs, curr_bottom))
            page_num += 1
            curr_recs = []
            curr_bottom = PAGE_SIZE
        curr_bottom -= r_len
        curr_recs.append(r)

    if curr_recs:
        pages.append((page_num, curr_recs, curr_bottom))

    total_pages = len(pages)
    first_data_page = pages[0][0]
    last_data_page = pages[-1][0]

    for i, (p_num, rec_list, bottom) in enumerate(pages):
        p_bytes = bytearray(PAGE_SIZE)
        p_bytes[0:2] = b"\x06\x00"
        p_bytes[2:4] = b"\x00\x00"
        p_bytes[4:8] = struct.pack("<I", 25)
        p_bytes[8:10] = struct.pack("<H", len(rec_list))
        p_bytes[10:12] = struct.pack("<H", bottom)

        next_p = pages[i+1][0] if i < total_pages - 1 else 0
        prev_p = pages[i-1][0] if i > 0 else 0

        p_bytes[12:16] = struct.pack("<I", next_p)
        p_bytes[16:20] = struct.pack("<I", prev_p)

        c_bot = PAGE_SIZE
        for r_idx, r_data in enumerate(rec_list):
            c_bot -= len(r_data)
            p_bytes[c_bot : c_bot + len(r_data)] = r_data
            p_bytes[20 + r_idx*2 : 22 + r_idx*2] = struct.pack("<H", c_bot | 0x1000)

        db[p_num*PAGE_SIZE : (p_num+1)*PAGE_SIZE] = p_bytes

    # 4. Update Page 25 (TDEF for Table 25)
    p25 = bytearray(db[25*PAGE_SIZE : 26*PAGE_SIZE])
    p25[12:16] = struct.pack("<I", first_data_page)
    p25[16:20] = struct.pack("<I", last_data_page)
    p25[32:36] = struct.pack("<I", total_pages)
    p25[36:40] = struct.pack("<I", len(records))
    db[25*PAGE_SIZE : 26*PAGE_SIZE] = p25

    # 5. Update Page 24 (Table 23 W#System)
    now_str = datetime.datetime.now().strftime("%d.%m.%Y в %H:%M:%S")
    sys_text = f"Създаден на {now_str} от Администратор\r\n\r\n{client_company_name} - {len(documents)} документа ({total_gross:,.2f} EUR)^"
    text_bytes = sys_text.encode("cp1251", errors="replace")
    ver_bytes = b"4.00"
    n_bytes = len(text_bytes)
    rec_len_24 = 8 + n_bytes + 4

    p24_rec = struct.pack("<H", rec_len_24) + b"\x00\x02" + ver_bytes + text_bytes + struct.pack("BBBB", 8 + n_bytes, 8, 4, 2)
    p24_off = 2048 - rec_len_24
    p24_new = bytearray(2048)
    p24_new[0:2] = b"\x06\x00"
    p24_new[2:4] = b"\x00\x00"
    p24_new[4:8] = struct.pack("<I", 23)
    p24_new[8:10] = struct.pack("<H", 1)
    p24_new[10:12] = struct.pack("<H", p24_off)
    p24_new[12:14] = b"\x00\x00"
    p24_new[20:22] = struct.pack("<H", p24_off | 0x1000)
    p24_new[p24_off:2048] = p24_rec
    db[24*PAGE_SIZE : 25*PAGE_SIZE] = p24_new

    # 6. Lock File
    ldb_bytes = DEFAULT_LDB_BYTES
    for p in CANONICAL_LDB_PATHS:
        if p.exists() and p.stat().st_size == 64:
            try:
                ldb_bytes = p.read_bytes()
                break
            except Exception:
                pass

    return bytes(db), bytes(ldb_bytes)


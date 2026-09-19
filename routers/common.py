"""Shared constants, helpers, and converters for FastAPI modular routers."""

import base64
from decimal import Decimal
import io
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
import pytesseract

from pydantic import BaseModel, Field

from invoice_ocr import (
    DEFAULT_OCR_CACHE_DIR,
    DEFAULT_OCR_LANG,
    SUPPORTED_EXTENSIONS,
    DocumentType,
    FinancialSummary,
    Invoice,
    InvoiceMetadata,
    LineItem,
    MoneyAmount,
    Party,
    PaymentDetails,
    process_invoice,
    serialize_invoice,
)

logger = logging.getLogger("invoice_ocr_api")
SERVICE_START_TIME = time.time()
API_VERSION = "1.0.0"
BG_TZ = ZoneInfo("Europe/Sofia")


class BatchDirRequest(BaseModel):
    input_dir: str = Field(..., description="Server path to input directory containing invoice files", json_schema_extra={"example": "/data/invoices"})
    output_dir: str = Field(default="results/", description="Server path to save JSON files", json_schema_extra={"example": "/data/results"})
    summary_file: Optional[str] = Field(default=None, description="Custom path for batch_summary.json", json_schema_extra={"example": "/data/results/batch_summary.json"})
    lang: str = Field(default=DEFAULT_OCR_LANG, description="Tesseract OCR language(s)", json_schema_extra={"example": "bul+eng"})
    debug: bool = Field(default=False, description="Save visual debug artifacts")
    debug_dir: str = Field(default="debug/", description="Directory to save debug artifacts")
    export_nap: bool = Field(default=False, description="Generate statutory НАП POKUPKI.TXT purchase ledger")
    export_entries: bool = Field(default=False, description="Generate double-entry bookkeeping journal entries")
    nap_period: Optional[str] = Field(default=None, description="VAT period YYYYMM for НАП ledger")
    expense_account: Optional[str] = Field(default=None, description="Default expense account (e.g. 304 or 602)")
    erp_format: str = Field(default="universal", description="ERP journal entries format (universal, microinvest, etc.)")
    workers: Optional[int] = Field(default=None, description="Number of worker processes for parallel batch execution (default: auto)")
    use_cache: bool = Field(default=True, description="Enable OCR token caching")
    ocr_cache_dir: Optional[str] = Field(default=str(DEFAULT_OCR_CACHE_DIR), description="OCR token cache directory")
    async_mode: bool = Field(default=False, description="Queue as asynchronous background job and return job_id")
    stream: bool = Field(default=False, description="Stream intermediate results as newline-delimited JSON (NDJSON)")

# Security: Restrict batch-dir and debug-dir paths to allowed roots
ALLOWED_BATCH_ROOTS = [
    p for p in [
        os.environ.get("ALLOWED_BATCH_ROOT"),
        "/data",
        str(Path.cwd()),
        tempfile.gettempdir(),
        "/tmp",
        "/private/tmp",
    ] if p
]


def _validate_server_path(path_str: str, label: str = "path") -> Path:
    """Validate that a server-side path is within allowed roots to prevent path traversal."""
    resolved = Path(path_str).resolve()
    for root in ALLOWED_BATCH_ROOTS:
        root_resolved = Path(root).resolve()
        try:
            resolved.relative_to(root_resolved)
            return resolved
        except ValueError:
            continue
    raise HTTPException(
        status_code=403,
        detail=f"Access denied: {label} '{path_str}' is outside allowed directories. "
               f"Allowed roots: {ALLOWED_BATCH_ROOTS}",
    )


def _validate_uploaded_extension(filename: str) -> str:
    """Validate that the uploaded file has a supported invoice extension."""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported file extension '{suffix}'. Supported formats: "
                f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            ),
        )
    return suffix


def _invoice_to_dict(invoice: Invoice, include_raw_evidence: bool = False) -> dict[str, Any]:
    """Convert an Invoice instance to a clean Python dictionary."""
    data = json.loads(serialize_invoice(invoice))
    if not include_raw_evidence and "raw_ocr_evidence" in data:
        data.pop("raw_ocr_evidence", None)
    return data


def _dict_to_invoice(data: dict[str, Any]) -> Invoice:
    """Reconstruct an Invoice instance from a JSON dictionary."""
    norm = data.get("normalized_data") or data
    meta_d = norm.get("invoice_metadata", {})
    sup_d = norm.get("supplier", {})
    rec_d = norm.get("recipient", {})
    fin_d = norm.get("financial_summary", {})
    pay_d = norm.get("payment_details", {})
    items_d = norm.get("line_items", [])

    def _to_money(val: Any) -> MoneyAmount:
        if isinstance(val, MoneyAmount):
            return val
        if isinstance(val, dict):
            amt = val.get("amount")
            curr = val.get("currency")
            return MoneyAmount(amount=Decimal(str(amt)) if amt is not None else None, currency=curr)
        if val is not None:
            return MoneyAmount(amount=Decimal(str(val)), currency=None)
        return MoneyAmount(amount=None, currency=None)

    meta = InvoiceMetadata(
        invoice_number=meta_d.get("invoice_number"),
        date_issued=meta_d.get("date_issued"),
        date_tax_event=meta_d.get("date_tax_event"),
        due_date=meta_d.get("due_date"),
        place_issued=meta_d.get("place_issued"),
        document_type=meta_d.get("document_type", DocumentType.INVOICE.value),
        is_credit_note=bool(meta_d.get("is_credit_note", False)),
        is_debit_note=bool(meta_d.get("is_debit_note", False)),
        compiled_by=meta_d.get("compiled_by"),
        received_by=meta_d.get("received_by"),
        currency=meta_d.get("currency") or (fin_d.get("total_amount_due", {}).get("currency") if isinstance(fin_d.get("total_amount_due"), dict) else None),
    )
    supplier = Party(
        name=sup_d.get("name"),
        eik=sup_d.get("eik"),
        vat_number=sup_d.get("vat_number"),
        address=sup_d.get("address"),
        mol=sup_d.get("mol"),
    )
    recipient = Party(
        name=rec_d.get("name"),
        eik=rec_d.get("eik"),
        vat_number=rec_d.get("vat_number"),
        address=rec_d.get("address"),
        mol=rec_d.get("mol"),
    )
    line_items = []
    for idx, item in enumerate(items_d, 1):
        if isinstance(item, dict):
            line_items.append(LineItem(
                index=item.get("index", idx),
                description=item.get("description"),
                unit=item.get("unit"),
                quantity=Decimal(str(item["quantity"])) if item.get("quantity") is not None else None,
                unit_price_net=_to_money(item.get("unit_price_net")),
                total_price_net=_to_money(item.get("total_price_net")),
                vat_rate_pct=Decimal(str(item["vat_rate_pct"])) if item.get("vat_rate_pct") is not None else None,
                article_code=item.get("article_code"),
            ))

    fin = FinancialSummary(
        tax_base=_to_money(fin_d.get("tax_base")),
        vat_amount=_to_money(fin_d.get("vat_amount")),
        total_amount_due=_to_money(fin_d.get("total_amount_due")),
        total_amount_words=fin_d.get("total_amount_words"),
        total_amount_bgn=_to_money(fin_d.get("total_amount_bgn")),
        total_amount_eur=_to_money(fin_d.get("total_amount_eur")),
        dual_display_total=_to_money(fin_d.get("dual_display_total")),
    )
    payment = PaymentDetails(
        method=pay_d.get("method"),
        bank_name=pay_d.get("bank_name"),
        iban=pay_d.get("iban"),
        bic=pay_d.get("bic"),
        due_date=pay_d.get("due_date"),
        bank_code=pay_d.get("bank_code"),
        is_iban_valid=pay_d.get("is_iban_valid"),
        is_bic_valid=pay_d.get("is_bic_valid"),
        bank_recognized=bool(pay_d.get("bank_recognized", False)),
    )
    return Invoice(
        invoice_metadata=meta,
        supplier=supplier,
        recipient=recipient,
        line_items=line_items,
        financial_summary=fin,
        payment_details=payment,
    )


def _decode_image_payload(image_payload: str) -> tuple[bytes, str]:
    """Decode base64 string, handling data URL prefix and determining file suffix."""
    if not image_payload:
        raise ValueError("Image data is required")
    raw = image_payload.strip()
    suffix = ".jpg"
    if raw.startswith("data:"):
        comma = raw.find(",")
        if comma != -1:
            header = raw[:comma]
            if "pdf" in header:
                suffix = ".pdf"
            elif "png" in header:
                suffix = ".png"
            elif "webp" in header:
                suffix = ".webp"
            raw = raw[comma + 1:]
    raw = re.sub(r"\s+", "", raw)
    file_bytes = base64.b64decode(raw)
    if file_bytes.startswith(b"%PDF"):
        suffix = ".pdf"
    elif file_bytes.startswith(b"\x89PNG"):
        suffix = ".png"
    elif file_bytes.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif file_bytes.startswith(b"RIFF") and b"WEBP" in file_bytes[:16]:
        suffix = ".webp"
    return file_bytes, suffix


def _process_bytes_locally(file_bytes: bytes, suffix: str = ".jpg") -> tuple[Invoice, str]:
    """Execute local multi-pass Tesseract OCR pipeline on raw bytes without external network calls."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)
    try:
        inv = process_invoice(tmp_path)
        raw_text = ""
        if hasattr(inv, "raw_ocr_evidence") and inv.raw_ocr_evidence and "full_text" in inv.raw_ocr_evidence:
            raw_text = inv.raw_ocr_evidence["full_text"]
        elif tmp_path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"):
            try:
                import cv2
                img = cv2.imread(str(tmp_path))
                if img is not None:
                    raw_text = pytesseract.image_to_string(img, lang="bul+eng")
            except Exception:
                pass
        return inv, raw_text
    finally:
        tmp_path.unlink(missing_ok=True)


def _format_smartscan_response(inv: Invoice, raw_text: str = "") -> dict[str, Any]:
    """Format Invoice into the camelCase schema required by SmartScan / MICROINVEST-OCR."""
    items = []
    for it in inv.line_items:
        qty = float(it.quantity) if it.quantity is not None else 1.0
        up = float(it.unit_price_net.amount) if (it.unit_price_net and it.unit_price_net.amount is not None) else 0.0
        tp = float(it.total_price_net.amount) if (it.total_price_net and it.total_price_net.amount is not None) else (up * qty)
        vr = float(it.vat_rate_pct) if it.vat_rate_pct is not None else 20.0
        items.append({
            "description": it.description or "Стока / Услуга",
            "quantity": qty,
            "unit": it.unit or "бр.",
            "unitPrice": up,
            "totalPrice": tp,
            "vatRate": vr,
        })

    subtotal = float(inv.financial_summary.tax_base.amount) if (inv.financial_summary.tax_base and inv.financial_summary.tax_base.amount is not None) else 0.0
    tax_amount = float(inv.financial_summary.vat_amount.amount) if (inv.financial_summary.vat_amount and inv.financial_summary.vat_amount.amount is not None) else 0.0
    total_amount = float(inv.financial_summary.total_amount_due.amount) if (inv.financial_summary.total_amount_due and inv.financial_summary.total_amount_due.amount is not None) else 0.0
    if total_amount == 0.0 and (subtotal > 0 or tax_amount > 0):
        total_amount = subtotal + tax_amount
    elif subtotal == 0.0 and total_amount > 0:
        subtotal = round(total_amount - tax_amount, 2)

    if not items:
        base_val = subtotal if subtotal > 0 else total_amount
        items.append({
            "description": "Стоки / Услуги по фактура",
            "quantity": 1.0,
            "unit": "бр.",
            "unitPrice": base_val,
            "totalPrice": base_val,
            "vatRate": 20.0,
        })

    cur = (
        (inv.financial_summary.total_amount_due.currency if inv.financial_summary.total_amount_due else None)
        or (inv.financial_summary.tax_base.currency if inv.financial_summary.tax_base else None)
        or getattr(inv.invoice_metadata, "currency", None)
        or ("EUR" if str(inv.invoice_metadata.date_issued or "") >= "2026-01-01" else "BGN")
    )

    sup_vat = inv.supplier.vat_number
    if not sup_vat and inv.supplier.eik:
        sup_vat = f"BG{inv.supplier.eik}"

    rec_vat = inv.recipient.vat_number
    if not rec_vat and inv.recipient.eik:
        rec_vat = f"BG{inv.recipient.eik}"

    doc_type_raw = str(getattr(inv.invoice_metadata, "document_type", "INVOICE")).upper()
    mapping = {
        "INVOICE": "invoice",
        "CREDIT_NOTE": "invoice",
        "DEBIT_NOTE": "invoice",
        "FISCAL_RECEIPT": "receipt",
        "GOODS_RECEIPT": "form",
        "PAYMENT_ORDER_NAP": "form",
        "PROTOCOL_CHL_117": "invoice",
        "FISCAL_MEMORY_REPORT": "receipt",
    }
    doc_type = mapping.get(doc_type_raw, "invoice")
    conf_score = float(inv.invoice_metadata.ocr_confidence_score or 0.95)

    fields_dict = {
        "invoiceNumber": inv.invoice_metadata.invoice_number,
        "invoiceDate": inv.invoice_metadata.date_issued,
        "dueDate": inv.payment_details.due_date,
        "vendorName": inv.supplier.name,
        "vendorAddress": inv.supplier.address,
        "vendorTaxId": inv.supplier.eik,
        "vendorVatId": sup_vat,
        "iban": inv.payment_details.iban,
        "customerName": inv.recipient.name,
        "customerAddress": inv.recipient.address,
        "customerTaxId": inv.recipient.eik,
        "customerVatNumber": rec_vat,
        "subtotal": subtotal,
        "taxRate": 20.0,
        "taxAmount": tax_amount,
        "totalAmount": total_amount,
        "currency": cur,
        "paymentTerms": inv.payment_details.method,
    }

    data_payload = {
        "invoiceNumber": inv.invoice_metadata.invoice_number,
        "invoiceDate": inv.invoice_metadata.date_issued,
        "dueDate": inv.payment_details.due_date,
        "vendorName": inv.supplier.name,
        "vendorAddress": inv.supplier.address,
        "vendorTaxId": inv.supplier.eik,
        "vendorVatId": sup_vat,
        "iban": inv.payment_details.iban,
        "customerName": inv.recipient.name,
        "customerAddress": inv.recipient.address,
        "customerTaxId": inv.recipient.eik,
        "customerVatNumber": rec_vat,
        "items": items,
        "lineItems": items,
        "fields": fields_dict,
        "documentType": doc_type,
        "classificationConfidence": conf_score,
        "subtotal": subtotal,
        "taxRate": 20.0,
        "taxAmount": tax_amount,
        "totalAmount": total_amount,
        "currency": cur,
        "paymentTerms": inv.payment_details.method,
        "notes": None,
        "rawText": raw_text or "",
    }

    return {
        "success": True,
        "data": data_payload,
        "documentType": doc_type,
        "classificationConfidence": conf_score,
        "confidence": conf_score,
        "fields": fields_dict,
        "lineItems": items,
        "items": items,
        "needsValidation": not inv.validation.is_valid,
    }

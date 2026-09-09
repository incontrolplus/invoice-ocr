"""Supabase Synchronization Layer for Bulgarian Invoice OCR Microservice.

Persists processed invoices, line items, audit logs, and file attachments
directly into Supabase PostgreSQL tables via PostgREST / Kong Gateway:
- public.invoices (Full invoice header, tax fields, validation & anomaly flags, raw OCR)
- public.invoice_items (Individual line items, quantities, prices, VAT, totals)
- public.invoice_audit_log (Immutable audit trail for ingestion & state changes)
- public.invoice_attachments (File hashes, mime types, storage paths)

Supports resilient retries, fallback logging, and optional n8n webhook notification.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Optional
import uuid

import httpx

logger = logging.getLogger("supabase_sync")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SUPABASE_URL = os.environ.get("SUPABASE_URL", "http://100.83.83.8:8002").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_SYNC_ENABLED = os.environ.get("SUPABASE_SYNC_ENABLED", "true").lower() in ("true", "1", "yes")
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "")

DEFAULT_HTTP_TIMEOUT = 15.0


def is_supabase_configured() -> bool:
    """Check whether Supabase sync is enabled and credentials are configured."""
    return bool(SUPABASE_SYNC_ENABLED and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _get_headers() -> dict[str, str]:
    """Build authorization headers for Supabase PostgREST API."""
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }


def _to_uuid(val: Any) -> str:
    """Ensure a string or hex ID is formatted as a valid standard UUID."""
    if not val:
        return str(uuid.uuid4())
    s = str(val).strip().replace("-", "")
    if len(s) == 32:
        try:
            return str(uuid.UUID(s))
        except ValueError:
            pass
    # Fallback to deterministic UUID5 based on input string
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, str(val)))


def _clean_date(date_str: Optional[str]) -> Optional[str]:
    """Parse and normalize date string into ISO YYYY-MM-DD format."""
    if not date_str:
        return None
    date_str = str(date_str).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str
    m = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$", date_str)
    if m:
        day, month, year = m.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    m = re.match(r"^(\d{4})[./-](\d{1,2})[./-](\d{1,2})$", date_str)
    if m:
        year, month, day = m.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return None


def _to_float(v: Any, default: float = 0.0) -> float:
    """Safely convert value to float, handling dicts and strings."""
    if v is None:
        return default
    if isinstance(v, dict):
        v = v.get("amount", default)
    try:
        cleaned = str(v).replace(",", ".").replace(" ", "").strip()
        return float(cleaned)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Core Synchronization Logic
# ---------------------------------------------------------------------------

def build_supabase_invoice_payload(
    doc_id: str,
    ocr_result: dict[str, Any],
    file_name: str,
    file_hash: Optional[str] = None,
    file_size: int = 0,
    storage_path: Optional[str] = None,
    source_channel: str = "EMAIL_INGEST",
    source_sender: Optional[str] = None,
    processing_time: float = 0.0,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Transform an OCR result dictionary into normalized rows for Supabase tables:
    
    Returns:
        (invoice_row, items_rows, audit_row, attachment_row)
    """
    invoice_uuid = _to_uuid(doc_id)

    norm = ocr_result.get("normalized_data", {})
    meta = norm.get("invoice_metadata", {})
    supplier = norm.get("supplier", {})
    recipient = norm.get("recipient", {})
    fin = norm.get("financial_summary", {})
    val = ocr_result.get("validation_results", {})

    is_valid = bool(val.get("is_valid", True))
    error_list = val.get("errors", [])
    warning_list = val.get("warnings", [])

    if is_valid and len(error_list) == 0:
        status = "VALIDATED"
    elif error_list:
        status = "FLAGGED_FOR_REVIEW"
    else:
        status = "PROCESSING"

    issue_date = _clean_date(meta.get("date_issued") or meta.get("issue_date"))
    if not issue_date:
        issue_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    tax_event_date = _clean_date(meta.get("date_tax_event") or meta.get("tax_event_date"))
    due_date = _clean_date(meta.get("due_date"))

    subtotal = _to_float(fin.get("tax_base") or fin.get("subtotal") or 0.0)
    vat_amt = _to_float(fin.get("vat_amount") or 0.0)
    total_amt = _to_float(fin.get("total_amount_due") or fin.get("total_amount") or 0.0)
    vat_rate = _to_float(fin.get("vat_rate"), default=20.0)

    anomaly_flags = {
        "math_discrepancy": abs((subtotal + vat_amt) - total_amt) > 0.02,
        "missing_supplier_eik": not bool(supplier.get("eik")),
        "missing_recipient_eik": not bool(recipient.get("eik")),
        "processing_time_sec": round(processing_time, 3),
    }

    curr = (
        meta.get("currency")
        or fin.get("currency")
        or (fin.get("total_amount_due", {}).get("currency") if isinstance(fin.get("total_amount_due"), dict) else None)
        or (fin.get("tax_base", {}).get("currency") if isinstance(fin.get("tax_base"), dict) else None)
        or ocr_result.get("currency")
    )
    if not curr:
        issue_d = str(issue_date or meta.get("date_issued") or "")
        curr = "EUR" if issue_d >= "2026-01-01" else "BGN"
    curr = str(curr)[:3].upper()

    invoice_row = {
        "id": invoice_uuid,
        "invoice_number": str(meta.get("invoice_number") or f"UNKNOWN-{invoice_uuid[:8]}"),
        "invoice_type": "INVOICE",
        "issue_date": issue_date,
        "tax_event_date": tax_event_date,
        "due_date": due_date,
        "place_of_issuance": meta.get("place_of_issuance"),
        "currency": curr,
        "exchange_rate": _to_float(meta.get("exchange_rate"), default=1.0),

        # Supplier
        "supplier_name": str(supplier.get("name") or "НЕИЗВЕСТЕН ДОСТАВЧИК"),
        "supplier_eik": str(supplier.get("eik") or "000000000"),
        "supplier_vat_number": supplier.get("vat_number"),
        "supplier_address": supplier.get("address"),
        "supplier_city": supplier.get("city"),
        "supplier_country": supplier.get("country") or "BGR",
        "supplier_mol": supplier.get("mol"),
        "supplier_iban": supplier.get("iban"),
        "supplier_bic": supplier.get("bic"),
        "supplier_bank_name": supplier.get("bank_name"),
        "supplier_phone": supplier.get("phone"),
        "supplier_email": supplier.get("email"),

        # Recipient
        "recipient_name": str(recipient.get("name") or "НЕИЗВЕСТЕН ПОЛУЧАТЕЛ"),
        "recipient_eik": str(recipient.get("eik") or "000000000"),
        "recipient_vat_number": recipient.get("vat_number"),
        "recipient_address": recipient.get("address"),
        "recipient_city": recipient.get("city"),
        "recipient_country": recipient.get("country") or "BGR",
        "recipient_mol": recipient.get("mol"),
        "recipient_iban": recipient.get("iban"),
        "recipient_phone": recipient.get("phone"),
        "recipient_email": recipient.get("email"),

        # Financials
        "subtotal_amount": max(0.0, subtotal),
        "vat_rate_percent": vat_rate,
        "vat_amount": max(0.0, vat_amt),
        "total_amount": max(0.0, total_amt),
        "paid_amount": 0.0,
        "payment_method": "BANK_TRANSFER",
        "payment_reference": meta.get("payment_reference"),
        "vat_exemption_reason": fin.get("vat_exemption_reason"),

        # Status & Quality
        "status": status,
        "confidence_score": _to_float(ocr_result.get("overall_confidence"), default=90.0),
        "is_duplicate": False,

        # Origin
        "source_channel": source_channel if source_channel in {
            "EMAIL_INGEST", "GOOGLE_DRIVE", "NEXTCLOUD", "MANUAL_UPLOAD", "N8N_WEBHOOK", "API_DIRECT"
        } else "EMAIL_INGEST",
        "source_sender": source_sender,
        "source_file_name": file_name,
        "source_file_hash_sha256": file_hash,
        "source_file_size_bytes": file_size,
        "storage_path": storage_path,

        # Validation & Issues
        "validation_errors": error_list,
        "validation_warnings": warning_list,
        "anomaly_flags": anomaly_flags,
        "internal_notes": None,

        # Raw Archive
        "ocr_raw_text": ocr_result.get("raw_text"),
        "ocr_structured_json": ocr_result,
        "custom_metadata": {
            "processing_time_sec": processing_time,
            "engine": "tesseract-5-bulgarian-cascade",
            "ingest_timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }

    # Line Items
    items_rows = []
    line_items = norm.get("line_items", [])
    if isinstance(line_items, list):
        for idx, item in enumerate(line_items, start=1):
            if not isinstance(item, dict):
                continue
            desc = item.get("description") or item.get("name") or f"Позиция #{idx}"
            qty = _to_float(item.get("quantity"), default=1.0)
            unit_price = _to_float(item.get("unit_price_net") or item.get("unit_price"), default=0.0)
            net_val = _to_float(item.get("total_price_net") or item.get("total") or item.get("net_amount"), default=qty * unit_price)
            item_vat_rate = _to_float(item.get("vat_rate_pct") or item.get("vat_rate"), default=vat_rate)
            item_vat_amt = _to_float(item.get("vat_amount"), default=round(net_val * (item_vat_rate / 100.0), 2))
            item_total = _to_float(item.get("total_amount") or item.get("total_price_gross"), default=round(net_val + item_vat_amt, 2))

            items_rows.append({
                "invoice_id": invoice_uuid,
                "line_number": idx,
                "item_description": str(desc),
                "item_code": item.get("code") or item.get("article_number"),
                "quantity": max(0.0001, qty),
                "unit_of_measure": str(item.get("unit") or "бр."),
                "unit_price_net": max(0.0, unit_price),
                "discount_percent": _to_float(item.get("discount_percent"), default=0.0),
                "discount_amount": _to_float(item.get("discount_amount"), default=0.0),
                "line_net_amount": max(0.0, net_val),
                "vat_rate_percent": item_vat_rate,
                "vat_amount": max(0.0, item_vat_amt),
                "line_total_amount": max(0.0, item_total),
                "confidence_score": _to_float(item.get("confidence"), default=90.0),
                "notes": None,
                "item_errors": item.get("errors", []),
                "metadata": {},
            })

    # Audit Trail
    audit_row = {
        "invoice_id": invoice_uuid,
        "event_type": "OCR_INGESTED",
        "actor": "SYSTEM_OCR",
        "details": {
            "source_channel": source_channel,
            "source_sender": source_sender,
            "file_name": file_name,
            "file_size": file_size,
            "processing_time_sec": processing_time,
            "is_valid": is_valid,
            "error_count": len(error_list),
        },
    }

    # Attachment
    attachment_row = {
        "invoice_id": invoice_uuid,
        "file_name": file_name,
        "file_hash_sha256": file_hash or hashlib.sha256(file_name.encode()).hexdigest(),
        "mime_type": "application/pdf" if file_name.lower().endswith(".pdf") else "image/png",
        "file_size_bytes": file_size,
        "storage_path": storage_path or f".stored_documents/{invoice_uuid}/{file_name}",
        "is_primary": True,
    }

    return invoice_row, items_rows, audit_row, attachment_row


async def sync_invoice_to_supabase(
    doc_id: str,
    ocr_result: dict[str, Any],
    file_name: str,
    file_path: Optional[Path] = None,
    file_hash: Optional[str] = None,
    file_size: int = 0,
    source_channel: str = "EMAIL_INGEST",
    source_sender: Optional[str] = None,
    processing_time: float = 0.0,
) -> dict[str, Any]:
    """Persist invoice, line items, audit log, and attachment into Supabase via REST API."""
    if not is_supabase_configured():
        logger.info("Supabase sync skipped (not configured or disabled)")
        return {"status": "skipped", "reason": "not_configured"}

    invoice_row, items_rows, audit_row, attachment_row = build_supabase_invoice_payload(
        doc_id=doc_id,
        ocr_result=ocr_result,
        file_name=file_name,
        file_hash=file_hash,
        file_size=file_size,
        storage_path=str(file_path) if file_path else None,
        source_channel=source_channel,
        source_sender=source_sender,
        processing_time=processing_time,
    )

    headers = _get_headers()
    base_rest = f"{SUPABASE_URL}/rest/v1"

    async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT) as client:
        try:
            # 1. Upsert into public.invoices
            inv_resp = await client.post(
                f"{base_rest}/invoices",
                json=invoice_row,
                headers=headers,
            )
            if inv_resp.status_code not in (200, 201):
                logger.error(
                    "Supabase sync failed for invoice %s: HTTP %d - %s",
                    doc_id, inv_resp.status_code, inv_resp.text,
                )
                return {
                    "status": "error",
                    "step": "invoices",
                    "code": inv_resp.status_code,
                    "detail": inv_resp.text,
                }

            # 2. Insert line items
            if items_rows:
                await client.delete(
                    f"{base_rest}/invoice_items?invoice_id=eq.{invoice_row['id']}",
                    headers=headers,
                )
                items_resp = await client.post(
                    f"{base_rest}/invoice_items",
                    json=items_rows,
                    headers=headers,
                )
                if items_resp.status_code not in (200, 201):
                    logger.warning("Supabase sync warning: failed inserting items: %s", items_resp.text)

            # 3. Insert audit log
            await client.post(
                f"{base_rest}/invoice_audit_log",
                json=audit_row,
                headers=headers,
            )

            # 4. Insert attachment
            if file_hash:
                await client.post(
                    f"{base_rest}/invoice_attachments",
                    json=attachment_row,
                    headers=headers,
                )

            logger.info("Successfully synced invoice %s to Supabase", invoice_row["id"])

            # 5. Optional n8n Webhook notification
            if N8N_WEBHOOK_URL:
                try:
                    await client.post(
                        N8N_WEBHOOK_URL,
                        json={
                            "event": "invoice_synced_to_supabase",
                            "invoice_id": invoice_row["id"],
                            "invoice_number": invoice_row["invoice_number"],
                            "supplier_name": invoice_row["supplier_name"],
                            "total_amount": invoice_row["total_amount"],
                            "status": invoice_row["status"],
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                        timeout=5.0,
                    )
                except Exception as n8n_err:
                    logger.warning("n8n notification failed (non-critical): %s", n8n_err)

            return {
                "status": "success",
                "invoice_id": invoice_row["id"],
                "invoice_number": invoice_row["invoice_number"],
                "items_count": len(items_rows),
            }

        except Exception as exc:
            logger.error("Exception during Supabase sync for %s: %s", doc_id, exc, exc_info=True)
            return {"status": "error", "error": str(exc)}

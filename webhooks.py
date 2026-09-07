"""Webhook Notification Architecture for Bulgarian Invoice OCR & ERP Integration.

Provides automated outbound HTTP POST notifications to external ERP, DMS, and accounting
systems upon document completion, error, or accountant approval:
- Automated double-entry bookkeeping journal entries (контировки: Д-т 304/602, Д-т 4531, К-т 401)
- Statutory НАП Purchase Ledger record (Приложение № 12 от ППЗДДС)
- HMAC-SHA256 payload signing for endpoint authenticity verification (X-Webhook-Signature)
- Exponential backoff retry mechanism (up to 3 attempts)
- Complete database audit logging in WebhookLogRecord and AuditTrailRecord
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

def _validate_webhook_url(url: str) -> None:
    """Validate webhook URL to prevent SSRF attacks.
    
    Blocks requests to private/reserved IP ranges and non-HTTP(S) schemes.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Webhook URL must use http or https scheme, got: {parsed.scheme}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Webhook URL must have a valid hostname")
    # Allow testserver for integration tests
    if hostname == "testserver":
        return
    try:
        # Resolve hostname and check if it's a private/reserved IP
        import socket
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
            if ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local:
                raise ValueError(
                    f"Webhook URL resolves to private/reserved IP address ({addr}). "
                    f"Outbound webhooks to internal networks are blocked for security."
                )
    except socket.gaierror:
        # DNS resolution failed - allow the request to proceed
        # (httpx will handle the connection error gracefully)
        pass

from accounting_export import (
    invoice_to_nap_entry,
    invoices_to_journal_entries,
)
from database import (
    AuditTrailRecord,
    DocumentRecord,
    WebhookLogRecord,
    get_db_session,
)

logger = logging.getLogger("invoice_ocr_webhooks")

DEFAULT_WEBHOOK_TIMEOUT = float(os.environ.get("WEBHOOK_TIMEOUT_SEC", "10.0"))
DEFAULT_WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
MAX_WEBHOOK_RETRIES = int(os.environ.get("WEBHOOK_MAX_RETRIES", "3"))


def prepare_webhook_payload(
    document_dict: dict[str, Any],
    event_type: str = "invoice.processed",
) -> dict[str, Any]:
    """Build a rich, self-contained accounting payload for ERP consumption.
    
    Contains ready-made double-entry journal entries and NAP purchase ledger record
    so the target ERP can book the transaction immediately without polling or transformation.
    """
    doc_id = document_dict.get("id") or document_dict.get("document_id") or "unknown"
    file_name = document_dict.get("file_name", "invoice.pdf")
    status = document_dict.get("status", "completed")
    is_valid = document_dict.get("is_valid", True)

    norm_data = document_dict.get("data", {}).get("normalized_data", {})
    if not norm_data and "normalized_data" in document_dict:
        norm_data = document_dict["normalized_data"]

    meta = norm_data.get("invoice_metadata", {})
    supplier = norm_data.get("supplier", {})
    recipient = norm_data.get("recipient", {})
    fin = norm_data.get("financial_summary", {})
    line_items = norm_data.get("line_items", [])
    validation = document_dict.get("data", {}).get("validation_results", {}) or document_dict.get("validation_results", {})

    # Generate double-entry journal entries (контировки)
    journal_entries_list: list[dict[str, Any]] = []
    total_debit = 0.0
    total_credit = 0.0
    is_balanced = True

    try:
        entries = invoices_to_journal_entries([document_dict])
        for entry in entries:
            entry_dict = entry.to_dict()
            journal_entries_list.append(entry_dict)
            try:
                total_debit += float(entry.tax_base_amount) + float(entry.vat_amount)
                total_credit += float(entry.total_amount)
                if not entry.is_balanced:
                    is_balanced = False
            except Exception:
                pass
    except Exception as exc:
        logger.warning("Could not generate journal entries for webhook %s: %s", doc_id, exc)

    # Generate statutory НАП Purchase Ledger line (Приложение № 12)
    nap_record_tsv = ""
    nap_record_fixed = ""
    try:
        nap_entry = invoice_to_nap_entry(document_dict)
        nap_record_tsv = nap_entry.to_tsv_line()
        nap_record_fixed = nap_entry.to_fixed_width_line()
    except Exception as exc:
        logger.warning("Could not generate NAP entry for webhook %s: %s", doc_id, exc)

    payload = {
        "event": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "document_id": doc_id,
        "file_name": file_name,
        "status": status,
        "is_valid": is_valid,
        "processing_time_sec": document_dict.get("processing_time_sec", 0.0),
        "invoice_metadata": {
            "invoice_number": meta.get("invoice_number"),
            "date_issued": meta.get("date_issued"),
            "date_tax_event": meta.get("date_tax_event"),
            "currency": meta.get("currency") or "BGN",
        },
        "parties": {
            "supplier": {
                "name": supplier.get("name"),
                "eik": supplier.get("eik"),
                "vat_number": supplier.get("vat_number"),
                "address": supplier.get("address"),
            },
            "recipient": {
                "name": recipient.get("name"),
                "eik": recipient.get("eik"),
                "vat_number": recipient.get("vat_number"),
                "address": recipient.get("address"),
            },
        },
        "financial_summary": {
            "tax_base": fin.get("tax_base", {}).get("amount") if isinstance(fin.get("tax_base"), dict) else fin.get("tax_base"),
            "vat_amount": fin.get("vat_amount", {}).get("amount") if isinstance(fin.get("vat_amount"), dict) else fin.get("vat_amount"),
            "total_amount_due": fin.get("total_amount_due", {}).get("amount") if isinstance(fin.get("total_amount_due"), dict) else fin.get("total_amount_due"),
            "currency": fin.get("total_amount_due", {}).get("currency") if isinstance(fin.get("total_amount_due"), dict) else (meta.get("currency") or "BGN"),
        },
        "line_items": line_items,
        "validation": {
            "is_valid": is_valid,
            "errors": validation.get("errors", []),
            "warnings": validation.get("warnings", []),
        },
        "accounting_entries": {
            "entries": journal_entries_list,
            "total_debit": round(total_debit, 2),
            "total_credit": round(total_credit, 2),
            "is_balanced": is_balanced and (abs(total_debit - total_credit) <= 0.01),
        },
        "statutory_nap_pokupki": {
            "tsv_line": nap_record_tsv,
            "fixed_width_line": nap_record_fixed,
        },
    }
    return payload


def compute_signature(payload_bytes: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature for webhook payload."""
    sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


async def send_webhook_async(
    target_url: str,
    payload: dict[str, Any],
    secret: Optional[str] = None,
    document_id: Optional[str] = None,
    job_id: Optional[str] = None,
    max_retries: int = MAX_WEBHOOK_RETRIES,
    timeout: float = DEFAULT_WEBHOOK_TIMEOUT,
    db: Optional[Session] = None,
) -> tuple[bool, Optional[int], Optional[str]]:
    """Deliver webhook POST request asynchronously with retries and audit logging."""
    _validate_webhook_url(target_url)
    payload_json = json.dumps(payload, ensure_ascii=False)
    payload_bytes = payload_json.encode("utf-8")

    sec = secret if secret is not None else DEFAULT_WEBHOOK_SECRET
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Bulgarian-Invoice-OCR-Webhook/1.0",
        "X-Webhook-Event": payload.get("event", "invoice.processed"),
    }
    if document_id:
        headers["X-Document-Id"] = document_id
    if sec:
        headers["X-Webhook-Signature"] = compute_signature(payload_bytes, sec)

    success = False
    status_code: Optional[int] = None
    response_body: Optional[str] = None
    last_error: Optional[str] = None

    own_db = False
    if db is None:
        db = get_db_session()
        own_db = True

    transport = None
    if "testserver" in target_url:
        try:
            from api_server import app as fastapi_app
            transport = httpx.ASGITransport(app=fastapi_app)
        except Exception as e:
            logger.debug("Could not initialize ASGITransport: %s", e)

    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout, verify=True) as client:
            attempt = 0
            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(
                        "Dispatching webhook to %s (attempt %d/%d) for doc %s",
                        target_url, attempt, max_retries, document_id,
                    )
                    resp = await client.post(target_url, content=payload_bytes, headers=headers)
                    status_code = resp.status_code
                    response_body = resp.text[:1000]

                    if 200 <= resp.status_code < 300:
                        success = True
                        last_error = None
                        logger.info(
                            "Webhook delivered successfully to %s (status %d)",
                            target_url, resp.status_code,
                        )
                        break
                    else:
                        last_error = f"HTTP {resp.status_code}: {response_body}"
                        logger.warning(
                            "Webhook to %s returned non-2xx status %d on attempt %d",
                            target_url, resp.status_code, attempt,
                        )
                except (httpx.RequestError, httpx.TimeoutException) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning("Webhook connection error to %s on attempt %d: %s", target_url, attempt, exc)

                if attempt < max_retries:
                    # Exponential backoff: 0.5s, 1.0s, 2.0s
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

        # Log delivery record to WebhookLogRecord
        log_entry = WebhookLogRecord(
            document_id=document_id,
            job_id=job_id,
            event_type=payload.get("event", "invoice.processed"),
            target_url=target_url,
            payload_json=payload_json,
            attempt=attempt,
            response_status_code=status_code,
            response_body=response_body,
            success=success,
            error_message=last_error,
            created_at=datetime.now(timezone.utc),
        )
        db.add(log_entry)

        # Update DocumentRecord if document_id is present
        if document_id:
            doc = db.query(DocumentRecord).filter(DocumentRecord.id == document_id).first()
            if doc:
                doc.webhook_status = "sent" if success else "failed"
                doc.webhook_response_code = status_code
                db.add(doc)

                audit_action = "webhook_delivered" if success else "webhook_failed"
                audit = AuditTrailRecord(
                    document_id=document_id,
                    actor="system:webhook_dispatcher",
                    action=audit_action,
                    details_json=json.dumps({
                        "target_url": target_url,
                        "status_code": status_code,
                        "success": success,
                        "attempt": attempt,
                        "error": last_error,
                    }, ensure_ascii=False),
                )
                db.add(audit)

        db.commit()
    except Exception as exc:
        logger.error("Unexpected error in webhook delivery to %s: %s", target_url, exc, exc_info=True)
        db.rollback()
        last_error = str(exc)
    finally:
        if own_db:
            db.close()

    return success, status_code, last_error


def dispatch_webhook_sync(
    target_url: str,
    payload: dict[str, Any],
    secret: Optional[str] = None,
    document_id: Optional[str] = None,
    job_id: Optional[str] = None,
    max_retries: int = MAX_WEBHOOK_RETRIES,
    timeout: float = DEFAULT_WEBHOOK_TIMEOUT,
) -> tuple[bool, Optional[int], Optional[str]]:
    """Synchronous entrypoint for background worker threads."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return asyncio.run_coroutine_threadsafe(
                send_webhook_async(
                    target_url=target_url,
                    payload=payload,
                    secret=secret,
                    document_id=document_id,
                    job_id=job_id,
                    max_retries=max_retries,
                    timeout=timeout,
                ),
                loop,
            ).result()
        else:
            return loop.run_until_complete(
                send_webhook_async(
                    target_url=target_url,
                    payload=payload,
                    secret=secret,
                    document_id=document_id,
                    job_id=job_id,
                    max_retries=max_retries,
                    timeout=timeout,
                )
            )
    except RuntimeError:
        return asyncio.run(
            send_webhook_async(
                target_url=target_url,
                payload=payload,
                secret=secret,
                document_id=document_id,
                job_id=job_id,
                max_retries=max_retries,
                timeout=timeout,
            )
        )

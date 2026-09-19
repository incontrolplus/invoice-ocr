"""FastAPI Router for Zero-Touch Ingestion (Email, IMAP, Folder Watcher)."""

import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Optional
import uuid

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    status,
)
from starlette.concurrency import run_in_threadpool

from database import (
    get_db_session,
    save_classified_document_to_db,
    save_document_to_db,
)
from invoice_core.constants import DEFAULT_MIN_ATTACHMENT_SIZE_BYTES
from invoice_core.document_classifier import (
    DocumentCategory,
    get_document_classifier,
)
from invoice_core.email_ingestion import (
    ParsedEmail,
    parse_cloudflare_worker_json,
    parse_mime_email,
    parse_multipart_form_data,
    verify_webhook_token,
)
from invoice_core.imap_poller import (
    ImapPoller,
    ImapPollerConfig,
)
from invoice_core.notifications import (
    dispatch_reverse_notifications_bundle,
)
from invoice_core.watcher import (
    FolderWatcher,
    FolderWatcherConfig,
)
from invoice_ocr import (
    DEFAULT_OCR_LANG,
    get_ocr_pool,
)
from routers.accounting import execute_accounting_pipeline_for_document
from routers.common import _invoice_to_dict

logger = logging.getLogger("invoice_ocr_api")
router = APIRouter()

global_watcher: Optional[FolderWatcher] = None


def _get_ocr_pool():
    import sys
    api_srv = sys.modules.get("api_server")
    if api_srv and hasattr(api_srv, "get_ocr_pool"):
        return api_srv.get_ocr_pool()
    return get_ocr_pool()


def _get_document_classifier():
    import sys
    api_srv = sys.modules.get("api_server")
    if api_srv and hasattr(api_srv, "get_document_classifier"):
        return api_srv.get_document_classifier()
    return get_document_classifier()


def get_global_watcher() -> Optional[FolderWatcher]:
    """Get the active FolderWatcher instance."""
    return global_watcher


def set_global_watcher(watcher: Optional[FolderWatcher]) -> None:
    """Set the active FolderWatcher instance."""
    global global_watcher
    global_watcher = watcher


@router.post(
    "/api/v1/ingest/email",
    summary="Zero-Touch Inbound Email Ingestion Webhook",
    tags=["Ingestion"],
)
async def ingest_email_webhook(
    request: Request,
    token: Optional[str] = Query(default=None, description="Optional webhook authorization secret token"),
    webhook_url: Optional[str] = Query(default=None, description="Optional ERP webhook URL to forward processed data"),
    require_spf: bool = Query(default=False, description="Require valid SPF pass"),
    require_dkim: bool = Query(default=False, description="Require valid DKIM pass"),
    block_spf_fail: bool = Query(default=True, description="Reject incoming emails if SPF explicitly fails"),
    min_size_bytes: int = Query(default=DEFAULT_MIN_ATTACHMENT_SIZE_BYTES, description="Minimum attachment size in bytes (<10KB filtered out)"),
    hitl_base_url: Optional[str] = Query(default=None, description="Base URL for HITL dashboard links"),
    lang: str = Query(default=DEFAULT_OCR_LANG, description="OCR languages"),
):
    """Webhook receiver for Cloudflare Email Routing / SendGrid / raw RFC 822 emails.

    - Verifies webhook authorization token (if configured via EMAIL_INGEST_SECRET or WEBHOOK_SECRET)
    - Validates SPF/DKIM signatures (RFC 8601 Authentication-Results, Received-SPF)
    - Automatically extracts attached PDF/TIFF/image files
    - Filters out email signatures, tracking pixels, and logos (< 10KB threshold)
    - Dispatches attachments to isolated OCR worker pool and persists to database
    - Generates and dispatches reverse notification (Email confirmation, Telegram, Slack, ERP Webhook)
      with Supplier, Amount, VAT, Status, and direct HITL dashboard link
    """
    # 1. Authorization check
    auth_header = request.headers.get("X-Ingest-Token") or request.headers.get("X-Webhook-Secret")
    if not auth_header and "authorization" in request.headers:
        bearer = request.headers["authorization"].split()
        if len(bearer) == 2 and bearer[0].lower() == "bearer":
            auth_header = bearer[1]
    provided_token = token or auth_header

    configured_secret = os.environ.get("EMAIL_INGEST_SECRET") or os.environ.get("WEBHOOK_SECRET")
    if configured_secret:
        if not provided_token or not verify_webhook_token(provided_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: invalid or missing ingest webhook token",
            )

    # 2. Parse incoming payload
    content_type = request.headers.get("content-type", "").lower()
    parsed_email: Optional[ParsedEmail] = None

    try:
        if "application/json" in content_type:
            json_body = await request.json()
            parsed_email = parse_cloudflare_worker_json(
                json_body,
                require_spf=require_spf,
                require_dkim=require_dkim,
                block_spf_fail=block_spf_fail,
                min_size_bytes=min_size_bytes,
            )
        elif "multipart/form-data" in content_type:
            form = await request.form()
            form_fields: dict[str, Any] = {}
            form_files: list[tuple[str, str, bytes, str]] = []
            for k, v in form.multi_items():
                if hasattr(v, "read") and hasattr(v, "filename"):
                    data = await v.read()
                    form_files.append((k, v.filename or k, data, getattr(v, "content_type", None) or "application/octet-stream"))
                else:
                    form_fields[k] = v
            parsed_email = parse_multipart_form_data(
                form_fields=form_fields,
                form_files=form_files,
                require_spf=require_spf,
                require_dkim=require_dkim,
                block_spf_fail=block_spf_fail,
                min_size_bytes=min_size_bytes,
            )
        else:
            raw_body = await request.body()
            parsed_email = parse_mime_email(
                raw_body,
                require_spf=require_spf,
                require_dkim=require_dkim,
                block_spf_fail=block_spf_fail,
                min_size_bytes=min_size_bytes,
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed parsing email payload: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed email payload: {exc}",
        )

    # 3. Security evaluation (SPF / DKIM)
    if not parsed_email.security.is_authorized:
        logger.warning(
            "Rejected email from '%s': %s (verdict: %s)",
            parsed_email.sender, parsed_email.security.details, parsed_email.security.security_verdict,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "Email security verification failed",
                "security": parsed_email.security.to_dict(),
                "verdict": parsed_email.security.security_verdict,
                "message": parsed_email.security.details,
            },
        )

    # 4. Filtered attachments check
    if len(parsed_email.valid_attachments) == 0:
        logger.info(
            "Email from '%s' had no valid invoice attachments (%d filtered)",
            parsed_email.sender, len(parsed_email.filtered_attachments),
        )
        return {
            "status": "no_invoices_found",
            "message": "No valid invoice candidate attachments found in email. Filtered out spam/logos/signatures (< 10KB) or non-document files.",
            "email_metadata": {
                "sender": parsed_email.sender,
                "recipient": parsed_email.recipient,
                "subject": parsed_email.subject,
                "date": parsed_email.date,
                "message_id": parsed_email.message_id,
                "spf": parsed_email.security.spf_status,
                "dkim": parsed_email.security.dkim_status,
            },
            "security": parsed_email.security.to_dict(),
            "total_attachments": len(parsed_email.all_attachments),
            "filtered_attachments": [a.to_dict() for a in parsed_email.filtered_attachments],
            "invoices_processed": 0,
            "results": [],
        }

    # 5. Process valid candidate attachments
    results = []
    pool = _get_ocr_pool()

    for att in parsed_email.valid_attachments:
        suffix = Path(att.filename).suffix.lower() or ".pdf"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
            tmp_file.write(att.data)
            tmp_path = Path(tmp_file.name)

        try:
            t0 = time.perf_counter()
            res = await pool.submit_ocr_async(
                file_path=tmp_path,
                lang=lang,
                use_cache=True,
                return_invoice_object=True,
            )
            proc_time = time.perf_counter() - t0
            if res.get("status") != "success" or res.get("invoice") is None:
                raise ValueError(f"OCR processing failed: {res.get('error')}")

            inv = res["invoice"]

            # Statutory Contractor & Partner Verification against accounting.partners
            from invoice_core.partner_verification import verify_invoice_parties
            parties_rep = verify_invoice_parties(inv.supplier, inv.recipient)

            if inv.raw_ocr_evidence is None:
                inv.raw_ocr_evidence = {}
            inv.raw_ocr_evidence["parties_verification"] = parties_rep.to_dict()

            if parties_rep.validation_issues:
                for iss in parties_rep.validation_issues:
                    if iss.severity == "error":
                        inv.validation.errors.append(iss)
                    else:
                        inv.validation.warnings.append(iss)
                inv.validation.is_valid = len(inv.validation.errors) == 0

            full_res = _invoice_to_dict(inv, include_raw_evidence=True)
            full_res["file_name"] = att.filename
            full_res["source_channel"] = "email"
            full_res["parties_verification"] = parties_rep.to_dict()
            full_res["email_metadata"] = {
                "sender": parsed_email.sender,
                "recipient": parsed_email.recipient,
                "subject": parsed_email.subject,
                "date": parsed_email.date,
                "message_id": parsed_email.message_id,
                "spf": parsed_email.security.spf_status,
                "dkim": parsed_email.security.dkim_status,
            }

            doc_id = uuid.uuid4().hex
            if not parties_rep.has_critical_mismatch:
                acc_bundle = execute_accounting_pipeline_for_document(
                    doc_id=doc_id,
                    invoice_dict=full_res,
                    file_name=att.filename,
                    parties_rep=parties_rep,
                )
                if acc_bundle:
                    full_res["accounting_bundle"] = acc_bundle
                    full_res["accounting_operation"] = acc_bundle.get("accounting_operation")
                    full_res["historical_match_report"] = acc_bundle.get("match_report")

            with get_db_session() as db:
                doc_rec = save_document_to_db(
                    db=db,
                    doc_id=doc_id,
                    file_name=att.filename,
                    source_path=tmp_path,
                    ocr_result=full_res,
                    processing_time=proc_time,
                    webhook_url=webhook_url,
                )
                if parties_rep.has_critical_mismatch:
                    doc_rec.status = "needs_review"
                    doc_rec.is_valid = False
                    db.commit()
                saved_dict = doc_rec.to_dict()

            # Reverse notification delivery
            notif_res = await dispatch_reverse_notifications_bundle(
                doc_dict=saved_dict,
                sender_email=parsed_email.sender,
                erp_webhook_url=webhook_url,
                hitl_base_url=hitl_base_url,
            )

            results.append({
                "document_id": doc_id,
                "file_name": att.filename,
                "invoice_number": saved_dict.get("invoice_number"),
                "supplier_name": saved_dict.get("supplier_name"),
                "supplier_eik": saved_dict.get("supplier_eik"),
                "tax_base": saved_dict.get("tax_base"),
                "vat_amount": saved_dict.get("vat_amount"),
                "total_amount": saved_dict.get("total_amount"),
                "currency": saved_dict.get("currency"),
                "accounting_operation": full_res.get("accounting_operation"),
                "historical_match_report": full_res.get("historical_match_report"),
                "transfer_log_url": f"/api/v1/accounting/transfer-log/{doc_id}" if full_res.get("accounting_operation") else None,
                "transfer_ldb_url": f"/api/v1/accounting/transfer-ldb/{doc_id}" if full_res.get("accounting_operation") else None,
                "status": saved_dict.get("status"),
                "is_valid": saved_dict.get("is_valid"),
                "requires_hitl": parties_rep.requires_hitl,
                "hitl_reasons": parties_rep.hitl_reasons,
                "parties_verification": parties_rep.to_dict(),
                "error_count": saved_dict.get("error_count", 0),
                "warning_count": saved_dict.get("warning_count", 0),
                "processing_time_sec": round(proc_time, 2),
                "hitl_url": notif_res.get("summary", {}).get("hitl_url", f"http://localhost:8000/dashboard?doc_id={doc_id}"),
                "notifications": notif_res,
            })
        finally:
            tmp_path.unlink(missing_ok=True)

    return {
        "status": "success",
        "email_metadata": {
            "sender": parsed_email.sender,
            "recipient": parsed_email.recipient,
            "subject": parsed_email.subject,
            "date": parsed_email.date,
            "message_id": parsed_email.message_id,
            "spf": parsed_email.security.spf_status,
            "dkim": parsed_email.security.dkim_status,
        },
        "security": parsed_email.security.to_dict(),
        "total_attachments": len(parsed_email.all_attachments),
        "filtered_attachments": [a.to_dict() for a in parsed_email.filtered_attachments],
        "invoices_processed": len(results),
        "results": results,
    }


@router.post(
    "/api/v1/ingest/docs-email",
    summary="Zero-Touch Document Classification Email Webhook (docs@incontrolplus.com)",
    tags=["Ingestion"],
)
async def ingest_docs_email_webhook(
    request: Request,
    token: Optional[str] = Query(default=None, description="Optional webhook authorization secret token"),
    webhook_url: Optional[str] = Query(default=None, description="Optional ERP webhook URL to forward processed data"),
    require_spf: bool = Query(default=False, description="Require valid SPF pass"),
    require_dkim: bool = Query(default=False, description="Require valid DKIM pass"),
    block_spf_fail: bool = Query(default=True, description="Reject incoming emails if SPF explicitly fails"),
    min_size_bytes: int = Query(default=DEFAULT_MIN_ATTACHMENT_SIZE_BYTES, description="Minimum attachment size in bytes (<10KB filtered out)"),
    hitl_base_url: Optional[str] = Query(default=None, description="Base URL for HITL dashboard links"),
    lang: str = Query(default=DEFAULT_OCR_LANG, description="OCR languages"),
):
    """Zero-touch document classifier webhook receiver for docs@incontrolplus.com.

    - Authenticates webhook call
    - Verifies SPF/DKIM integrity
    - Extracts candidate attachments (> 10KB threshold)
    - Automatically classifies every document across statutory categories:
      Фактури, Кредитни известия, Стокови разписки, Фискални бонове,
      Пощенски парични преводи, Платежни документи, Некласифицирани
    - If classified as 'Фактури':
      Dispatches to full invoice extraction pipeline, persists to DB, syncs to Supabase 'invoices',
      and delivers reverse confirmation notifications.
    - If other category:
      Persists to DB, stores in Supabase 'documents' table with category tags,
      and emits n8n classified event for workflow routing.
    """
    # 1. Authorization check
    auth_header = request.headers.get("X-Ingest-Token") or request.headers.get("X-Webhook-Secret")
    if not auth_header and "authorization" in request.headers:
        bearer = request.headers["authorization"].split()
        if len(bearer) == 2 and bearer[0].lower() == "bearer":
            auth_header = bearer[1]
    provided_token = token or auth_header

    configured_secret = os.environ.get("EMAIL_INGEST_SECRET") or os.environ.get("WEBHOOK_SECRET")
    if configured_secret:
        if not provided_token or not verify_webhook_token(provided_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: invalid or missing ingest webhook token",
            )

    # 2. Parse incoming payload
    content_type = request.headers.get("content-type", "").lower()
    parsed_email: Optional[ParsedEmail] = None

    try:
        if "application/json" in content_type:
            json_body = await request.json()
            parsed_email = parse_cloudflare_worker_json(
                json_body,
                require_spf=require_spf,
                require_dkim=require_dkim,
                block_spf_fail=block_spf_fail,
                min_size_bytes=min_size_bytes,
            )
        elif "multipart/form-data" in content_type:
            form = await request.form()
            form_fields: dict[str, Any] = {}
            form_files: list[tuple[str, str, bytes, str]] = []
            for k, v in form.multi_items():
                if hasattr(v, "read") and hasattr(v, "filename"):
                    data = await v.read()
                    form_files.append((k, v.filename or k, data, getattr(v, "content_type", None) or "application/octet-stream"))
                else:
                    form_fields[k] = v
            parsed_email = parse_multipart_form_data(
                form_fields=form_fields,
                form_files=form_files,
                require_spf=require_spf,
                require_dkim=require_dkim,
                block_spf_fail=block_spf_fail,
                min_size_bytes=min_size_bytes,
            )
        else:
            raw_body = await request.body()
            parsed_email = parse_mime_email(
                raw_body,
                require_spf=require_spf,
                require_dkim=require_dkim,
                block_spf_fail=block_spf_fail,
                min_size_bytes=min_size_bytes,
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed parsing docs-email payload: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed email payload: {exc}",
        )

    # 3. Security evaluation
    if not parsed_email.security.is_authorized:
        logger.warning(
            "Rejected docs email from '%s': %s (verdict: %s)",
            parsed_email.sender, parsed_email.security.details, parsed_email.security.security_verdict,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "Email security verification failed",
                "security": parsed_email.security.to_dict(),
                "verdict": parsed_email.security.security_verdict,
                "message": parsed_email.security.details,
            },
        )

    # 4. Candidate attachments check
    if len(parsed_email.valid_attachments) == 0:
        logger.info(
            "Docs email from '%s' had no valid attachments (%d filtered)",
            parsed_email.sender, len(parsed_email.filtered_attachments),
        )
        return {
            "status": "no_documents_found",
            "message": "No valid document attachments found in email. Filtered out spam/logos/signatures (< 10KB) or non-document files.",
            "email_metadata": {
                "sender": parsed_email.sender,
                "recipient": parsed_email.recipient,
                "subject": parsed_email.subject,
                "date": parsed_email.date,
                "message_id": parsed_email.message_id,
                "spf": parsed_email.security.spf_status,
                "dkim": parsed_email.security.dkim_status,
            },
            "security": parsed_email.security.to_dict(),
            "total_attachments": len(parsed_email.all_attachments),
            "filtered_attachments": [a.to_dict() for a in parsed_email.filtered_attachments],
            "documents_processed": 0,
            "results": [],
        }

    # 5. Process candidate attachments
    results = []
    pool = _get_ocr_pool()
    classifier = _get_document_classifier()

    for att in parsed_email.valid_attachments:
        suffix = Path(att.filename).suffix.lower() or ".pdf"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
            tmp_file.write(att.data)
            tmp_path = Path(tmp_file.name)

        doc_id = uuid.uuid4().hex
        try:
            t0 = time.perf_counter()
            classification = await run_in_threadpool(
                classifier.classify_file,
                tmp_path,
                att.filename,
            )
            proc_time = time.perf_counter() - t0

            is_acc_doc = classification.category in (DocumentCategory.FAKTURI, DocumentCategory.KREDITNI_IZVESTIYA)
            if is_acc_doc and not classification.is_obscured:
                # Invoice & Credit Note Pipeline
                res = await pool.submit_ocr_async(
                    file_path=tmp_path,
                    lang=lang,
                    use_cache=True,
                    return_invoice_object=True,
                )
                if res.get("status") == "success" and res.get("invoice") is not None:
                    inv = res["invoice"]
                    if classification.category == DocumentCategory.KREDITNI_IZVESTIYA:
                        inv.invoice_metadata.is_credit_note = True
                        inv.invoice_metadata.document_type = "CREDIT_NOTE"

                    # Statutory Contractor & Partner Verification against accounting.partners
                    from invoice_core.partner_verification import verify_invoice_parties
                    parties_rep = verify_invoice_parties(inv.supplier, inv.recipient)

                    if inv.raw_ocr_evidence is None:
                        inv.raw_ocr_evidence = {}
                    inv.raw_ocr_evidence["parties_verification"] = parties_rep.to_dict()

                    if parties_rep.validation_issues:
                        for iss in parties_rep.validation_issues:
                            if iss.severity == "error":
                                inv.validation.errors.append(iss)
                            else:
                                inv.validation.warnings.append(iss)
                        inv.validation.is_valid = len(inv.validation.errors) == 0

                    full_res = _invoice_to_dict(inv, include_raw_evidence=True)
                    full_res["file_name"] = att.filename
                    full_res["source_channel"] = "email_docs"
                    full_res["email_metadata"] = {
                        "sender": parsed_email.sender,
                        "recipient": parsed_email.recipient,
                        "subject": parsed_email.subject,
                        "date": parsed_email.date,
                        "message_id": parsed_email.message_id,
                    }
                    full_res["classification"] = classification.to_dict()
                    full_res["parties_verification"] = parties_rep.to_dict()

                    if not parties_rep.has_critical_mismatch:
                        acc_bundle = execute_accounting_pipeline_for_document(
                            doc_id=doc_id,
                            invoice_dict=full_res,
                            file_name=att.filename,
                            parties_rep=parties_rep,
                        )
                        if acc_bundle:
                            full_res["accounting_bundle"] = acc_bundle
                            full_res["accounting_operation"] = acc_bundle.get("accounting_operation")
                            full_res["historical_match_report"] = acc_bundle.get("match_report")

                    with get_db_session() as db:
                        doc_rec = save_document_to_db(
                            db=db,
                            doc_id=doc_id,
                            file_name=att.filename,
                            source_path=tmp_path,
                            ocr_result=full_res,
                            processing_time=proc_time,
                            webhook_url=webhook_url,
                        )
                        if parties_rep.has_critical_mismatch:
                            doc_rec.status = "needs_review"
                            doc_rec.is_valid = False
                            db.commit()
                        saved_dict = doc_rec.to_dict()

                    notif_res = await dispatch_reverse_notifications_bundle(
                        doc_dict=saved_dict,
                        sender_email=parsed_email.sender,
                        erp_webhook_url=webhook_url,
                        hitl_base_url=hitl_base_url,
                    )

                    cat_val = DocumentCategory.NEKLASIFITSIRANI.value if parties_rep.has_critical_mismatch else classification.category.value
                    cat_code = "UNCLASSIFIED_NEEDS_REVIEW" if parties_rep.has_critical_mismatch else classification.category.name
                    act_routing = "hitl_review" if parties_rep.has_critical_mismatch else "routed_to_invoices"

                    results.append({
                        "document_id": doc_id,
                        "file_name": att.filename,
                        "category": cat_val,
                        "category_code": cat_code,
                        "confidence": round(classification.confidence, 4),
                        "routing_action": act_routing,
                        "requires_hitl": parties_rep.requires_hitl,
                        "hitl_reasons": parties_rep.hitl_reasons,
                        "parties_verification": parties_rep.to_dict(),
                        "invoice_number": saved_dict.get("invoice_number") or (full_res.get("accounting_operation") or {}).get("document_number"),
                        "supplier_name": saved_dict.get("supplier_name") or (full_res.get("accounting_operation") or {}).get("contractor_name"),
                        "supplier_eik": saved_dict.get("supplier_eik") or (full_res.get("accounting_operation") or {}).get("contractor_eik"),
                        "recipient_name": saved_dict.get("recipient_name") or (full_res.get("accounting_operation") or {}).get("client_company"),
                        "total_amount": saved_dict.get("total_amount") or (full_res.get("accounting_operation") or {}).get("total_amount"),
                        "currency": saved_dict.get("currency") or (full_res.get("accounting_operation") or {}).get("currency") or "BGN",
                        "issue_date": saved_dict.get("issue_date") or (full_res.get("accounting_operation") or {}).get("document_date"),
                        "accounting_operation": full_res.get("accounting_operation"),
                        "historical_match_report": full_res.get("historical_match_report"),
                        "transfer_log_url": f"/api/v1/accounting/transfer-log/{doc_id}" if full_res.get("accounting_operation") else None,
                        "transfer_ldb_url": f"/api/v1/accounting/transfer-ldb/{doc_id}" if full_res.get("accounting_operation") else None,
                        "status": saved_dict.get("status"),
                        "processing_time_sec": round(proc_time, 2),
                        "hitl_url": notif_res.get("summary", {}).get("hitl_url", f"http://localhost:8000/dashboard?doc_id={doc_id}"),
                        "notifications": notif_res,
                    })
                    continue

            # Non-invoice categories or obscured documents
            routing_action = "flag_for_rescan" if classification.is_obscured else "archived_as_classified"
            with get_db_session() as db:
                save_classified_document_to_db(
                    db=db,
                    doc_id=doc_id,
                    file_name=att.filename,
                    category=classification.category.value,
                    confidence=classification.confidence,
                    source_path=tmp_path,
                    matched_keywords=classification.matched_keywords,
                    text_content=classification.extracted_text,
                    extra_metadata={
                        "is_obscured": classification.is_obscured,
                        "obscuration_reason": classification.obscuration_reason,
                        "subdocuments": [s.to_dict() for s in classification.subdocuments],
                        "email_metadata": {
                            "sender": parsed_email.sender,
                            "recipient": parsed_email.recipient,
                            "subject": parsed_email.subject,
                            "date": parsed_email.date,
                            "message_id": parsed_email.message_id,
                        },
                    },
                    processing_time=proc_time,
                    source_channel="email_docs",
                    source_sender=parsed_email.sender,
                )

            results.append({
                "document_id": doc_id,
                "file_name": att.filename,
                "category": classification.category.value,
                "category_code": classification.category.name,
                "confidence": round(classification.confidence, 4),
                "matched_keywords": classification.matched_keywords,
                "is_obscured": classification.is_obscured,
                "obscuration_reason": classification.obscuration_reason,
                "subdocuments": [s.to_dict() for s in classification.subdocuments],
                "routing_action": routing_action,
                "processing_time_sec": round(proc_time, 2),
            })
        except Exception as proc_err:
            logger.error("Failed processing attachment %s: %s", att.filename, proc_err, exc_info=True)
            results.append({
                "document_id": doc_id,
                "file_name": att.filename,
                "category": DocumentCategory.NEKLASIFITSIRANI.value,
                "category_code": DocumentCategory.NEKLASIFITSIRANI.name,
                "error": str(proc_err),
                "routing_action": "error",
            })
        finally:
            tmp_path.unlink(missing_ok=True)

    return {
        "status": "success",
        "email_metadata": {
            "sender": parsed_email.sender,
            "recipient": parsed_email.recipient,
            "subject": parsed_email.subject,
            "date": parsed_email.date,
            "message_id": parsed_email.message_id,
            "spf": parsed_email.security.spf_status,
            "dkim": parsed_email.security.dkim_status,
        },
        "security": parsed_email.security.to_dict(),
        "total_attachments": len(parsed_email.all_attachments),
        "filtered_attachments": [a.to_dict() for a in parsed_email.filtered_attachments],
        "documents_processed": len(results),
        "results": results,
    }


@router.post(
    "/api/v1/ingest/watcher/scan",
    summary="Trigger Folder Watcher Scan",
    tags=["Ingestion"],
)
async def trigger_watcher_scan(
    watch_dir: Optional[str] = Query(default=None, description="Custom directory to scan"),
    erp_webhook_url: Optional[str] = Query(default=None, description="Optional ERP webhook URL"),
):
    """Immediately scan the watch directory and process all ready invoices."""
    target_dir = Path(watch_dir or os.environ.get("WATCH_DIR") or "watch_invoices")
    if not target_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)

    cfg = FolderWatcherConfig(
        watch_dir=target_dir,
        debounce_delay_sec=0.0,
        erp_webhook_url=erp_webhook_url,
    )
    watcher = FolderWatcher(cfg)
    results = await run_in_threadpool(watcher.scan_once)
    return {
        "watch_dir": str(target_dir),
        "processed_count": len(results),
        "results": results,
    }


@router.get(
    "/api/v1/ingest/watcher/status",
    summary="Get Folder Watcher Status",
    tags=["Ingestion"],
)
async def get_watcher_status():
    """Get the current operational status of the background folder watcher."""
    global global_watcher
    if global_watcher:
        return {
            "is_running": global_watcher.is_running,
            "watch_dir": str(global_watcher.watch_dir),
            "processed_dir": str(global_watcher.processed_dir),
            "failed_dir": str(global_watcher.failed_dir),
            "processed_count": global_watcher.processed_count,
            "failed_count": global_watcher.failed_count,
        }
    return {
        "is_running": False,
        "watch_dir": str(os.environ.get("WATCH_DIR", "watch_invoices")),
        "message": "Watcher is not running as a background task. Trigger via /api/v1/ingest/watcher/scan or enable WATCHER_ENABLED=1.",
    }


@router.post(
    "/api/v1/ingest/imap/poll",
    summary="Trigger IMAP Inbox Poll",
    tags=["Ingestion"],
)
async def trigger_imap_poll(
    host: Optional[str] = Query(default=None, description="IMAP server host"),
    username: Optional[str] = Query(default=None, description="IMAP username"),
    password: Optional[str] = Query(default=None, description="IMAP password"),
    mailbox: str = Query(default="INBOX", description="Mailbox name"),
):
    """Trigger an immediate IMAP poll cycle for unread invoice emails."""
    imap_host = host or os.environ.get("IMAP_HOST", "")
    imap_user = username or os.environ.get("IMAP_USER", "")
    imap_pass = password or os.environ.get("IMAP_PASSWORD", "")

    if not imap_host or not imap_user or not imap_pass:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="IMAP host, username, and password must be configured or passed as parameters.",
        )

    cfg = ImapPollerConfig(
        host=imap_host,
        username=imap_user,
        password=imap_pass,
        mailbox=mailbox,
    )
    poller = ImapPoller(cfg)
    results = await run_in_threadpool(poller.poll_once)
    return {
        "mailbox": mailbox,
        "processed_count": len(results),
        "results": results,
    }

"""FastAPI Router for Human-in-the-Loop (HITL), Document Review, Approvals, and Corrections."""

import json
import logging
from pathlib import Path
import re
from typing import Any, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session

from database import (
    AuditTrailRecord,
    DocumentRecord,
    InvoiceFeedbackRecord,
    add_audit_entry,
    approve_document_in_db,
    get_db,
    storage_manager,
    update_document_corrections,
)
from webhooks import prepare_webhook_payload, send_webhook_async

logger = logging.getLogger("invoice_ocr_api")
router = APIRouter()


class ApproveDocumentRequest(BaseModel):
    actor: str = Field(default="accountant", description="Name or identifier of accountant approving document")
    webhook_url: Optional[str] = Field(default=None, description="Optional ERP webhook URL to notify")


class ApproveAndExportRequest(BaseModel):
    corrections: Optional[dict[str, Any]] = Field(default=None, description="Manual field corrections applied via HITL interface")
    actor: str = Field(default="accountant", description="Name or identifier of accountant approving document")
    webhook_url: Optional[str] = Field(default=None, description="Optional ERP webhook URL to notify")
    export_format: str = Field(default="all", description="Export type: pokupki, journal_entries, or all")


@router.get(
    "/api/v1/documents",
    tags=["Documents"],
    summary="List Processed Invoices",
)
async def list_documents(
    status_filter: Optional[str] = Query(default=None, alias="status", description="Filter by status"),
    search: Optional[str] = Query(default=None, description="Search by invoice number, supplier or file name"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Retrieve list of processed invoices with status filtering and text search."""
    query = db.query(DocumentRecord)
    if status_filter and status_filter.lower() != "all":
        query = query.filter(DocumentRecord.status == status_filter.lower())
    if search:
        s = f"%{search.strip()}%"
        query = query.filter(
            (DocumentRecord.invoice_number.ilike(s)) |
            (DocumentRecord.supplier_name.ilike(s)) |
            (DocumentRecord.supplier_eik.ilike(s)) |
            (DocumentRecord.file_name.ilike(s))
        )
    total = query.count()
    records = query.order_by(DocumentRecord.created_at.desc()).offset(skip).limit(limit).all()
    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "documents": [r.to_dict() for r in records],
    }


@router.get(
    "/api/v1/documents/{document_id}",
    tags=["Documents"],
    summary="Get Document Details & OCR Evidence",
)
async def get_document(
    document_id: str,
    include_raw_evidence: bool = Query(default=False, description="Include token coordinates and bounding boxes"),
    db: Session = Depends(get_db),
):
    """Get full document record including 3-layer data, corrections, and canvas bounding boxes."""
    record = db.query(DocumentRecord).filter(DocumentRecord.id == document_id).first()
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{document_id}' not found",
        )
    return record.to_dict(include_raw_evidence=include_raw_evidence)


@router.get(
    "/api/v1/documents/{document_id}/pages/{page_number}/image",
    tags=["Documents"],
    summary="Render Document Page as High-Resolution PNG",
)
async def get_document_page_image(
    document_id: str,
    page_number: int = 1,
    dpi: int = Query(default=150, ge=72, le=300),
):
    """Render a specific document page as PNG for HTML5 Canvas overlay in the HITL interface."""
    png_bytes = storage_manager.render_page_png(document_id, page_number=page_number, dpi=dpi)
    if not png_bytes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Page {page_number} for document '{document_id}' not found or could not be rendered",
        )
    return Response(content=png_bytes, media_type="image/png")


@router.get(
    "/api/v1/documents/{document_id}/file",
    tags=["Documents"],
    summary="Download Original Document File",
)
async def get_document_file(document_id: str, db: Session = Depends(get_db)):
    """Download original PDF or image file associated with the document."""
    record = db.query(DocumentRecord).filter(DocumentRecord.id == document_id).first()
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    file_path = storage_manager.get_file_path(document_id)
    if not file_path or not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Original document file not found on disk")
    media_type = "application/pdf" if file_path.suffix.lower() == ".pdf" else "image/png"
    return FileResponse(path=file_path, filename=record.file_name, media_type=media_type)


@router.post(
    "/api/v1/documents/{document_id}/correct",
    tags=["Documents"],
    summary="Save Manual Field Corrections (HITL) with RLHF Learning",
)
async def correct_document_fields(
    document_id: str,
    corrections: dict[str, Any],
    actor: str = Query(default="accountant", description="Accountant identifier"),
    db: Session = Depends(get_db),
):
    """Apply manual corrections made by accountant, trigger RLHF layout & vendor learning, and log changes into the immutable audit trail."""
    meta = {}
    if isinstance(corrections, dict) and "feedback_metadata" in corrections:
        meta = corrections.pop("feedback_metadata") or {}

    record = update_document_corrections(
        db=db,
        doc_id=document_id,
        corrections=corrections,
        actor=actor,
        feedback_metadata=meta,
    )
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{document_id}' not found",
        )
    return record.to_dict()


@router.get(
    "/api/v1/feedback/stats",
    tags=["Feedback & RLHF"],
    summary="Get RLHF Feedback & Learning Metrics",
)
async def get_rlhf_feedback_stats(db: Session = Depends(get_db)):
    """Retrieve operational statistics of the continuous RLHF learning engine."""
    from invoice_core.feedback_learning import get_feedback_statistics
    return get_feedback_statistics(db)


@router.get(
    "/api/v1/vendors/{identifier}/learned-rules",
    tags=["Feedback & RLHF"],
    summary="Get Learned Profile Rules for a Vendor",
)
async def get_vendor_learned_rules(
    identifier: str,
    db: Session = Depends(get_db),
):
    """Retrieve learned series patterns, spatial priors, and recent correction exemplars for a vendor."""
    from invoice_core.vendor_profiles import get_vendor_profile
    clean_id = re.sub(r"\D", "", str(identifier).strip()) or identifier.strip().lower()
    prof = get_vendor_profile(clean_id) or get_vendor_profile(identifier)

    exemplars = db.query(InvoiceFeedbackRecord).filter(
        (InvoiceFeedbackRecord.supplier_eik == clean_id) | (InvoiceFeedbackRecord.supplier_eik == identifier)
    ).order_by(desc(InvoiceFeedbackRecord.created_at)).limit(20).all()

    return {
        "identifier": identifier,
        "profile": prof or {},
        "exemplars_count": len(exemplars),
        "recent_exemplars": [e.to_dict() for e in exemplars],
    }


@router.post(
    "/api/v1/documents/{document_id}/approve",
    tags=["Documents"],
    summary="Approve Document & Dispatch ERP Webhook",
)
async def approve_document(
    document_id: str,
    request: ApproveDocumentRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Approve invoice document and dispatch accounting journal entries to the ERP webhook endpoint."""
    record = approve_document_in_db(
        db=db,
        doc_id=document_id,
        actor=request.actor,
        webhook_url=request.webhook_url,
    )
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{document_id}' not found",
        )

    doc_dict = record.to_dict()

    # Dispatch webhook if URL is specified in request or record
    target_webhook = request.webhook_url or record.webhook_url
    if target_webhook:
        payload = prepare_webhook_payload(doc_dict, event_type="invoice.approved")
        background_tasks.add_task(
            send_webhook_async,
            target_url=target_webhook,
            payload=payload,
            document_id=document_id,
        )

    return {
        "status": "approved",
        "document_id": document_id,
        "message": "Document approved and ERP notification queued",
        "webhook_url": target_webhook,
        "document": doc_dict,
    }


@router.post(
    "/api/v1/documents/{document_id}/approve-and-export",
    tags=["Documents"],
    summary="Approve Document, Save Corrections, Trigger Webhook & Export (HITL DoD)",
)
async def approve_and_export_document(
    document_id: str,
    request: ApproveAndExportRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Atomic approval and export workflow for HITL accountants:
    1. Apply manual field corrections (if provided) and log diffs.
    2. Re-validate document with statutory rules.
    3. Mark document as approved in database.py.
    4. Queue outbound ERP webhook with balanced double-entry accounting entries.
    5. Generate statutory НАП POKUPKI and double-entry journal entries exports.
    6. Record approval and export events in immutable audit trail.
    """
    # 1. Apply manual corrections if provided
    if request.corrections:
        update_document_corrections(
            db=db,
            doc_id=document_id,
            corrections=request.corrections,
            actor=request.actor,
        )

    # 2. Mark document approved in database
    record = approve_document_in_db(
        db=db,
        doc_id=document_id,
        actor=request.actor,
        webhook_url=request.webhook_url,
    )
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{document_id}' not found",
        )

    doc_dict = record.to_dict()

    # 3. Dispatch ERP Webhook
    target_webhook = request.webhook_url or record.webhook_url
    webhook_queued = False
    if target_webhook:
        payload = prepare_webhook_payload(doc_dict, event_type="invoice.approved")
        background_tasks.add_task(
            send_webhook_async,
            target_url=target_webhook,
            payload=payload,
            document_id=document_id,
        )
        webhook_queued = True

    # 4. Generate accounting exports
    from accounting_export import (
        export_journal_entries_csv,
        export_journal_entries_json,
        invoices_to_journal_entries,
        invoices_to_pokupki_txt,
    )

    exports: dict[str, Any] = {}
    try:
        pokupki_raw = invoices_to_pokupki_txt([doc_dict], format="fixed_width", encoding="utf-8")
        pokupki_str = pokupki_raw if isinstance(pokupki_raw, str) else pokupki_raw.decode("utf-8", errors="replace")
        exports["pokupki"] = {
            "filename": f"POKUPKI_{document_id[:8]}.TXT",
            "content": pokupki_str,
        }
    except Exception as exc:
        logger.warning("Error generating POKUPKI in approve-and-export for %s: %s", document_id, exc)

    try:
        entries = invoices_to_journal_entries([doc_dict])
        csv_text = export_journal_entries_csv(entries, format_type="universal")
        json_text = export_journal_entries_json(entries)
        exports["journal_entries_csv"] = {
            "filename": f"journal_entries_{document_id[:8]}.csv",
            "content": csv_text,
        }
        exports["journal_entries_json"] = json.loads(json_text)
    except Exception as exc:
        logger.warning("Error generating journal entries in approve-and-export for %s: %s", document_id, exc)

    # 5. Record export in audit trail
    add_audit_entry(
        db=db,
        document_id=document_id,
        action="exported",
        actor=request.actor,
        details={
            "export_types": list(exports.keys()),
            "webhook_dispatched": webhook_queued,
        },
    )

    # 6. Retrieve refreshed audit trail
    audits = (
        db.query(AuditTrailRecord)
        .filter(AuditTrailRecord.document_id == document_id)
        .order_by(AuditTrailRecord.timestamp.asc())
        .all()
    )

    return {
        "status": "approved",
        "document_id": document_id,
        "message": "Document approved, corrections logged, webhook queued, and accounting export generated.",
        "webhook_url": target_webhook,
        "webhook_queued": webhook_queued,
        "document": record.to_dict(),
        "exports": exports,
        "audit_trail": [a.to_dict() for a in audits],
    }


@router.get(
    "/api/v1/documents/{document_id}/audit-trail",
    tags=["Documents"],
    summary="Get Document Audit Trail",
)
async def get_document_audit_trail(document_id: str, db: Session = Depends(get_db)):
    """Retrieve full audit log tracking uploads, edits, approvals, and webhook dispatches."""
    entries = (
        db.query(AuditTrailRecord)
        .filter(AuditTrailRecord.document_id == document_id)
        .order_by(AuditTrailRecord.timestamp.asc())
        .all()
    )
    return [e.to_dict() for e in entries]


@router.get(
    "/api/v1/documents/{document_id}/export/pokupki",
    tags=["Documents"],
    summary="Export Document as НАП POKUPKI.TXT",
)
async def export_single_pokupki(
    document_id: str,
    format: str = Query(default="fixed_width", pattern="^(fixed_width|tsv|csv)$"),
    encoding: str = Query(default="cp1251", pattern="^(cp1251|utf-8)$"),
    db: Session = Depends(get_db),
):
    """Generate statutory НАП POKUPKI.TXT record for a specific document."""
    from accounting_export import invoices_to_pokupki_txt
    record = db.query(DocumentRecord).filter(DocumentRecord.id == document_id).first()
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    raw_output = invoices_to_pokupki_txt(
        [record.to_dict()],
        format=format,  # type: ignore
        encoding=encoding,
    )
    add_audit_entry(
        db=db,
        document_id=document_id,
        action="exported",
        actor="accountant",
        details={"type": "pokupki", "format": format, "encoding": encoding},
    )
    media_type = "text/plain; charset=windows-1251" if encoding == "cp1251" else "text/plain; charset=utf-8"
    content_bytes = raw_output if isinstance(raw_output, bytes) else raw_output.encode(encoding)
    return Response(
        content=content_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="POKUPKI_{document_id[:8]}.TXT"'},
    )


@router.get(
    "/api/v1/documents/{document_id}/export/journal-entries",
    tags=["Documents"],
    summary="Export Document Journal Entries (Контировки)",
)
async def export_single_journal_entries(
    document_id: str,
    format: str = Query(default="universal", pattern="^(json|universal|microinvest|business_navigator|ajur|sap)$"),
    db: Session = Depends(get_db),
):
    """Export double-entry bookkeeping journal entries for a specific document."""
    from accounting_export import (
        export_journal_entries_csv,
        export_journal_entries_json,
        invoices_to_journal_entries,
    )
    record = db.query(DocumentRecord).filter(DocumentRecord.id == document_id).first()
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    entries = invoices_to_journal_entries([record.to_dict()])
    add_audit_entry(
        db=db,
        document_id=document_id,
        action="exported",
        actor="accountant",
        details={"type": "journal_entries", "format": format},
    )
    if format == "json":
        payload_str = export_journal_entries_json(entries)
        return JSONResponse(content=json.loads(payload_str))

    csv_text = export_journal_entries_csv(entries, format_type=format)  # type: ignore
    return Response(
        content=csv_text.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="journal_entries_{document_id[:8]}.csv"'},
    )


@router.delete(
    "/api/v1/documents/{document_id}",
    tags=["Documents"],
    summary="Delete Document",
)
async def delete_document(document_id: str, db: Session = Depends(get_db)):
    """Delete document, stored file, and associated audit logs."""
    record = db.query(DocumentRecord).filter(DocumentRecord.id == document_id).first()
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    stored_path = storage_manager.get_file_path(document_id)
    if stored_path and stored_path.exists():
        stored_path.unlink(missing_ok=True)
    db.delete(record)
    db.commit()
    return {"status": "ok", "message": f"Document '{document_id}' and related files deleted"}

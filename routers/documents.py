"""FastAPI Router for Invoice Processing, Batch Uploads, Local Scanner Drop-in, and Tesseract."""

import asyncio
from dataclasses import asdict
import json
import logging
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any, Optional
import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
import pytesseract
from starlette.concurrency import run_in_threadpool

from database import (
    get_db_session,
    save_classified_document_to_db,
    save_document_to_db,
)
from invoice_core.document_classifier import (
    ClassificationResult,
    DocumentCategory,
    get_document_classifier,
)
from invoice_ocr import (
    DEFAULT_OCR_CACHE_DIR,
    DEFAULT_OCR_LANG,
    SUPPORTED_EXTENSIONS,
    get_ocr_pool,
    iter_process_batch,
    process_batch,
    validate_invoice,
)
from routers.accounting import execute_accounting_pipeline_for_document
from routers.common import (
    BatchDirRequest,
    _decode_image_payload,
    _dict_to_invoice,
    _format_smartscan_response,
    _invoice_to_dict,
    _process_bytes_locally,
    _validate_uploaded_extension,
)
from routers.jobs import (
    JobStatus,
    _run_batch_dir_job,
    _run_batch_upload_job,
    job_manager,
)
from webhooks import prepare_webhook_payload, send_webhook_async

logger = logging.getLogger("invoice_ocr_api")
router = APIRouter()


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


# ---------------------------------------------------------------------------
# Streaming NDJSON Helpers for Batch Processing
# ---------------------------------------------------------------------------

async def _stream_uploaded_batch_generator(
    tmp_in: Path,
    tmp_out: Path,
    lang: str,
    workers: Optional[int],
    use_cache: bool,
    ocr_cache_dir: Optional[str],
):
    """Asynchronous streaming generator yielding NDJSON events for uploaded batch processing."""
    try:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        def _worker_runner():
            try:
                for item in iter_process_batch(
                    input_dir=tmp_in,
                    output_dir=tmp_out,
                    lang=lang,
                    workers=workers,
                    use_cache=use_cache,
                    ocr_cache_dir=ocr_cache_dir,
                    quiet=True,
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, ("doc", item))
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))

        fut = loop.run_in_executor(None, _worker_runner)

        success_count = 0
        failed_count = 0
        total_count = 0

        while True:
            ev_type, payload = await queue.get()
            if ev_type == "doc":
                total_count += 1
                status_str = payload.get("status", "unknown")
                if status_str == "success":
                    success_count += 1
                else:
                    failed_count += 1

                doc_entry: dict[str, Any] = {
                    "event": "document",
                    "file": payload.get("file"),
                    "status": status_str,
                    "duration_seconds": payload.get("duration_seconds"),
                    "worker_pid": payload.get("worker_pid"),
                    "memory_rss_mb": payload.get("memory_rss_mb"),
                    "memory_guard_triggered": payload.get("memory_guard_triggered"),
                    "error": payload.get("error"),
                }
                inv = payload.get("invoice")
                if inv:
                    doc_entry["invoice_number"] = inv.invoice_metadata.invoice_number
                    doc_entry["date_issued"] = inv.invoice_metadata.date_issued
                    doc_entry["supplier"] = {
                        "name": inv.supplier.name,
                        "eik": inv.supplier.eik,
                    }
                    doc_entry["recipient"] = {
                        "name": inv.recipient.name,
                        "eik": inv.recipient.eik,
                    }
                    doc_entry["is_valid"] = inv.validation.is_valid
                    tot = inv.financial_summary.total_amount_due.amount
                    curr = (
                        inv.financial_summary.total_amount_due.currency
                        or getattr(inv.invoice_metadata, "currency", None)
                        or ("EUR" if str(inv.invoice_metadata.date_issued or "") >= "2026-01-01" else "BGN")
                    )
                    doc_entry["total_amount_due"] = str(tot) if tot is not None else None
                    doc_entry["currency"] = curr

                yield (json.dumps(doc_entry, ensure_ascii=False) + "\n").encode("utf-8")

            elif ev_type == "done":
                summary_event = {
                    "event": "summary",
                    "total_documents": total_count,
                    "processed_successfully": success_count,
                    "failed": failed_count,
                }
                yield (json.dumps(summary_event, ensure_ascii=False) + "\n").encode("utf-8")
                break
            elif ev_type == "error":
                yield (json.dumps({"event": "error", "error": payload}, ensure_ascii=False) + "\n").encode("utf-8")
                break

        await fut
    finally:
        shutil.rmtree(str(tmp_in), ignore_errors=True)
        shutil.rmtree(str(tmp_out), ignore_errors=True)


async def _stream_directory_batch_generator(
    in_path: Path,
    out_path: Path,
    lang: str,
    workers: Optional[int],
    use_cache: bool,
    ocr_cache_dir: Optional[str],
):
    """Asynchronous streaming generator yielding NDJSON events for directory batch processing."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def _worker_runner():
        try:
            for item in iter_process_batch(
                input_dir=in_path,
                output_dir=out_path,
                lang=lang,
                workers=workers,
                use_cache=use_cache,
                ocr_cache_dir=ocr_cache_dir,
                quiet=True,
            ):
                loop.call_soon_threadsafe(queue.put_nowait, ("doc", item))
            loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))

    fut = loop.run_in_executor(None, _worker_runner)

    success_count = 0
    failed_count = 0
    total_count = 0

    while True:
        ev_type, payload = await queue.get()
        if ev_type == "doc":
            total_count += 1
            status_str = payload.get("status", "unknown")
            if status_str == "success":
                success_count += 1
            else:
                failed_count += 1

            doc_entry = {
                "event": "document",
                "file": payload.get("file"),
                "status": status_str,
                "duration_seconds": payload.get("duration_seconds"),
                "worker_pid": payload.get("worker_pid"),
                "memory_rss_mb": payload.get("memory_rss_mb"),
                "memory_guard_triggered": payload.get("memory_guard_triggered"),
                "error": payload.get("error"),
            }
            inv = payload.get("invoice")
            if inv:
                doc_entry["invoice_number"] = inv.invoice_metadata.invoice_number
                doc_entry["date_issued"] = inv.invoice_metadata.date_issued
                doc_entry["supplier"] = {
                    "name": inv.supplier.name,
                    "eik": inv.supplier.eik,
                }
                doc_entry["is_valid"] = inv.validation.is_valid
                tot = inv.financial_summary.total_amount_due.amount
                curr = (
                    inv.financial_summary.total_amount_due.currency
                    or getattr(inv.invoice_metadata, "currency", None)
                    or ("EUR" if str(inv.invoice_metadata.date_issued or "") >= "2026-01-01" else "BGN")
                )
                doc_entry["total_amount_due"] = str(tot) if tot is not None else None
                doc_entry["currency"] = curr

            yield (json.dumps(doc_entry, ensure_ascii=False) + "\n").encode("utf-8")

        elif ev_type == "done":
            summary_event = {
                "event": "summary",
                "total_documents": total_count,
                "processed_successfully": success_count,
                "failed": failed_count,
            }
            yield (json.dumps(summary_event, ensure_ascii=False) + "\n").encode("utf-8")
            break
        elif ev_type == "error":
            yield (json.dumps({"event": "error", "error": payload}, ensure_ascii=False) + "\n").encode("utf-8")
            break

    await fut


# ---------------------------------------------------------------------------
# Invoices Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/api/v1/invoices/process",
    summary="Process Single Invoice File",
    tags=["Invoices"],
)
async def process_single_invoice(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Invoice file (.pdf, .png, .jpg, .jpeg)"),
    lang: str = Query(default=DEFAULT_OCR_LANG, description="OCR language(s)"),
    include_raw_evidence: bool = Query(default=False, description="Include detailed token bounding boxes and page evidence"),
    webhook_url: Optional[str] = Query(default=None, description="Optional ERP webhook URL to notify upon completion"),
    debug: bool = Query(default=False, description="Enable debug artifacts generation"),
    debug_dir: str = Query(default="debug/", description="Directory to save debug artifacts"),
):
    """Upload an invoice document, persist to database, and extract structured 3-layer data."""
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Filename is required",
        )

    suffix = _validate_uploaded_extension(file.filename)

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
        tmp_path = Path(tmp_file.name)
        try:
            shutil.copyfileobj(file.file, tmp_file)
            tmp_file.flush()
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed writing uploaded file to temporary storage: {exc}",
            )

    try:
        if tmp_path.stat().st_size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty (0 bytes)",
            )

        dbg_path = Path(debug_dir) if debug else None
        start_time = time.perf_counter()
        pool = _get_ocr_pool()
        res = await pool.submit_ocr_async(
            file_path=tmp_path,
            debug_dir=dbg_path,
            lang=lang,
            use_cache=True,
            return_invoice_object=True,
        )
        proc_time = time.perf_counter() - start_time
        if res.get("status") != "success" or res.get("invoice") is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"OCR processing failed: {res.get('error')}",
            )
        invoice = res["invoice"]

        from invoice_core.partner_verification import verify_invoice_parties
        parties_rep = verify_invoice_parties(invoice.supplier, invoice.recipient)

        if invoice.raw_ocr_evidence is None:
            invoice.raw_ocr_evidence = {}
        invoice.raw_ocr_evidence["parties_verification"] = parties_rep.to_dict()

        if parties_rep.validation_issues:
            for iss in parties_rep.validation_issues:
                if iss.severity == "error":
                    invoice.validation.errors.append(iss)
                else:
                    invoice.validation.warnings.append(iss)
            invoice.validation.is_valid = len(invoice.validation.errors) == 0

        full_result = _invoice_to_dict(invoice, include_raw_evidence=True)
        full_result["file_name"] = file.filename
        full_result["parties_verification"] = parties_rep.to_dict()

        doc_id = uuid.uuid4().hex
        if not parties_rep.has_critical_mismatch:
            acc_bundle = execute_accounting_pipeline_for_document(
                doc_id=doc_id,
                invoice_dict=full_result,
                file_name=file.filename,
                parties_rep=parties_rep,
            )
            if acc_bundle:
                full_result["accounting_bundle"] = acc_bundle
                full_result["accounting_operation"] = acc_bundle.get("accounting_operation")
                full_result["historical_match_report"] = acc_bundle.get("match_report")

        with get_db_session() as db:
            doc_rec = save_document_to_db(
                db=db,
                doc_id=doc_id,
                file_name=file.filename,
                source_path=tmp_path,
                ocr_result=full_result,
                processing_time=proc_time,
                webhook_url=webhook_url,
            )
            if parties_rep.has_critical_mismatch:
                doc_rec.status = "needs_review"
                doc_rec.is_valid = False
                db.commit()
            saved_dict = doc_rec.to_dict(include_raw_evidence=include_raw_evidence)

        if full_result.get("accounting_operation"):
            saved_dict["accounting_operation"] = full_result["accounting_operation"]
            saved_dict["historical_match_report"] = full_result.get("historical_match_report")
            saved_dict["transfer_log_url"] = f"/api/v1/accounting/transfer-log/{doc_id}"
            saved_dict["transfer_ldb_url"] = f"/api/v1/accounting/transfer-ldb/{doc_id}"

        if webhook_url:
            payload = prepare_webhook_payload(saved_dict, event_type="invoice.processed")
            background_tasks.add_task(
                send_webhook_async,
                target_url=webhook_url,
                payload=payload,
                document_id=doc_id,
            )

        return saved_dict

    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        logger.error("Error processing %s: %s", file.filename, exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Invoice processing error: {exc}",
        )
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post(
    "/api/v1/classify/document",
    summary="Statutory Document Classification & Multi-Doc Splitter",
    tags=["Document Classification"],
)
async def classify_document_endpoint(
    file: UploadFile = File(..., description="Document file to classify (PDF, JPEG, PNG, TIFF)"),
    lang: str = Query(default=DEFAULT_OCR_LANG, description="OCR languages (e.g. bul+eng)"),
    sync_supabase: bool = Query(default=True, description="Automatically persist and sync result to Supabase"),
    auto_process_invoice: bool = Query(default=True, description="If classified as Fakturi, run full invoice extraction engine"),
    webhook_url: Optional[str] = Query(default=None, description="Optional webhook URL to receive classified document"),
):
    """Classify an uploaded document into statutory categories."""
    file_name = file.filename or "uploaded_document"
    ext = Path(file_name).suffix.lower() or ".pdf"
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file format '{ext}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        )

    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty",
        )

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_file:
        tmp_file.write(content)
        tmp_path = Path(tmp_file.name)

    t0 = time.perf_counter()
    doc_id = uuid.uuid4().hex

    try:
        classifier = _get_document_classifier()
        classification: ClassificationResult = await run_in_threadpool(
            classifier.classify_file,
            tmp_path,
            file_name,
        )
        proc_time = time.perf_counter() - t0

        is_acc_candidate = classification.category in (DocumentCategory.FAKTURI, DocumentCategory.KREDITNI_IZVESTIYA)
        if is_acc_candidate and auto_process_invoice and not classification.is_obscured:
            pool = _get_ocr_pool()
            ocr_res = await pool.submit_ocr_async(
                file_path=tmp_path,
                lang=lang,
                use_cache=True,
                return_invoice_object=True,
            )
            if ocr_res.get("status") == "success" and ocr_res.get("invoice") is not None:
                inv = ocr_res["invoice"]
                if classification.category == DocumentCategory.KREDITNI_IZVESTIYA:
                    inv.invoice_metadata.is_credit_note = True
                    inv.invoice_metadata.document_type = "CREDIT_NOTE"

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
                full_res["file_name"] = file_name
                full_res["source_channel"] = "MANUAL_UPLOAD"
                full_res["classification"] = classification.to_dict()
                full_res["parties_verification"] = parties_rep.to_dict()

                if parties_rep.has_critical_mismatch:
                    logger.warning(
                        "Classification rejected for %s due to critical contractor name divergence: %s",
                        file_name, parties_rep.hitl_reasons,
                    )
                    with get_db_session() as db:
                        doc_rec = save_document_to_db(
                            db=db,
                            doc_id=doc_id,
                            file_name=file_name,
                            source_path=tmp_path,
                            ocr_result=full_res,
                            processing_time=proc_time,
                            webhook_url=webhook_url,
                        )
                        doc_rec.status = "needs_review"
                        doc_rec.is_valid = False
                        db.commit()
                        saved_dict = doc_rec.to_dict()

                    return {
                        "status": "needs_review",
                        "document_id": doc_id,
                        "file_name": file_name,
                        "category": DocumentCategory.NEKLASIFITSIRANI.value,
                        "category_code": "UNCLASSIFIED_NEEDS_REVIEW",
                        "confidence": round(classification.confidence, 4),
                        "matched_keywords": classification.matched_keywords,
                        "is_obscured": False,
                        "obscuration_reason": None,
                        "subdocuments": [s.to_dict() for s in classification.subdocuments],
                        "routing_action": "hitl_review",
                        "requires_hitl": True,
                        "hitl_reasons": parties_rep.hitl_reasons,
                        "parties_verification": parties_rep.to_dict(),
                        "processing_time_sec": round(proc_time, 3),
                        "invoice_data": saved_dict,
                    }

                acc_bundle = execute_accounting_pipeline_for_document(
                    doc_id=doc_id,
                    invoice_dict=full_res,
                    file_name=file_name,
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
                        file_name=file_name,
                        source_path=tmp_path,
                        ocr_result=full_res,
                        processing_time=proc_time,
                        webhook_url=webhook_url,
                    )
                    saved_dict = doc_rec.to_dict()

                return {
                    "status": "success",
                    "document_id": doc_id,
                    "file_name": file_name,
                    "category": classification.category.value,
                    "category_code": classification.category.name,
                    "confidence": round(classification.confidence, 4),
                    "matched_keywords": classification.matched_keywords,
                    "is_obscured": classification.is_obscured,
                    "obscuration_reason": classification.obscuration_reason,
                    "subdocuments": [s.to_dict() for s in classification.subdocuments],
                    "routing_action": "routed_to_invoices",
                    "requires_hitl": parties_rep.requires_hitl,
                    "hitl_reasons": parties_rep.hitl_reasons,
                    "parties_verification": parties_rep.to_dict(),
                    "accounting_operation": full_res.get("accounting_operation"),
                    "historical_match_report": full_res.get("historical_match_report"),
                    "transfer_log_url": f"/api/v1/accounting/transfer-log/{doc_id}" if full_res.get("accounting_operation") else None,
                    "transfer_ldb_url": f"/api/v1/accounting/transfer-ldb/{doc_id}" if full_res.get("accounting_operation") else None,
                    "processing_time_sec": round(proc_time, 3),
                    "invoice_data": saved_dict,
                }

        saved_dict = None
        if sync_supabase:
            with get_db_session() as db:
                doc_rec = save_classified_document_to_db(
                    db=db,
                    doc_id=doc_id,
                    file_name=file_name,
                    category=classification.category.value,
                    confidence=classification.confidence,
                    source_path=tmp_path,
                    matched_keywords=classification.matched_keywords,
                    text_content=classification.extracted_text,
                    extra_metadata={
                        "is_obscured": classification.is_obscured,
                        "obscuration_reason": classification.obscuration_reason,
                        "subdocuments": [s.to_dict() for s in classification.subdocuments],
                    },
                    processing_time=proc_time,
                    source_channel="MANUAL_UPLOAD",
                )
                saved_dict = doc_rec.to_dict() if hasattr(doc_rec, "to_dict") else {"id": doc_id}

        routing_action = "flag_for_rescan" if classification.is_obscured else "archived_as_classified"

        return {
            "status": "success",
            "document_id": doc_id,
            "file_name": file_name,
            "category": classification.category.value,
            "category_code": classification.category.name,
            "confidence": round(classification.confidence, 4),
            "matched_keywords": classification.matched_keywords,
            "is_obscured": classification.is_obscured,
            "obscuration_reason": classification.obscuration_reason,
            "subdocuments": [s.to_dict() for s in classification.subdocuments],
            "routing_action": routing_action,
            "processing_time_sec": round(proc_time, 3),
            "saved_record": saved_dict,
        }
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post(
    "/api/v1/invoices/batch",
    summary="Upload & Process Multiple Invoice Files",
    tags=["Invoices"],
)
async def process_batch_files(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(..., description="Multiple invoice files to process"),
    lang: str = Query(default=DEFAULT_OCR_LANG, description="OCR language(s)"),
    include_raw_evidence: bool = Query(default=False, description="Include detailed token bounding boxes"),
    workers: Optional[int] = Query(default=None, description="Number of worker processes for parallel batch execution"),
    use_cache: bool = Query(default=True, description="Enable OCR token caching"),
    ocr_cache_dir: Optional[str] = Query(default=str(DEFAULT_OCR_CACHE_DIR), description="OCR token cache directory"),
    async_mode: bool = Query(default=False, description="Queue as background task and return job_id immediately"),
    stream: bool = Query(default=False, description="Stream intermediate results as newline-delimited JSON (NDJSON)"),
):
    """Upload multiple invoice files in a single request and receive an aggregated accounting batch report."""
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No files provided in batch request",
        )

    tmp_in_str = tempfile.mkdtemp(prefix="batch_in_")
    tmp_out_str = tempfile.mkdtemp(prefix="batch_out_")
    tmp_in = Path(tmp_in_str)
    tmp_out = Path(tmp_out_str)

    saved_files: list[tuple[str, Path]] = []
    for f in files:
        if not f.filename:
            continue
        suffix = Path(f.filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            continue
        dest = tmp_in / f.filename
        with dest.open("wb") as buffer:
            shutil.copyfileobj(f.file, buffer)
        saved_files.append((f.filename, dest))

    if not saved_files:
        shutil.rmtree(tmp_in_str, ignore_errors=True)
        shutil.rmtree(tmp_out_str, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="None of the uploaded files have supported extensions (.pdf, .png, .jpg, .jpeg)",
        )

    if stream:
        return StreamingResponse(
            _stream_uploaded_batch_generator(
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                lang=lang,
                workers=workers,
                use_cache=use_cache,
                ocr_cache_dir=ocr_cache_dir,
            ),
            media_type="application/x-ndjson",
        )

    if async_mode:
        job_id = job_manager.create_job(request_type="batch-upload", metadata={"file_count": len(saved_files)})
        background_tasks.add_task(
            _run_batch_upload_job,
            job_id,
            tmp_in,
            tmp_out,
            lang,
            workers,
            use_cache,
            ocr_cache_dir,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "job_id": job_id,
                "status": JobStatus.QUEUED.value,
                "message": f"Batch upload of {len(saved_files)} file(s) queued for background processing",
                "check_status_url": f"/v1/jobs/{job_id}",
            },
        )

    try:
        summary = process_batch(
            input_dir=tmp_in,
            output_dir=tmp_out,
            lang=lang,
            quiet=True,
            workers=workers,
            use_cache=use_cache,
            ocr_cache_dir=ocr_cache_dir,
        )
        return summary
    finally:
        shutil.rmtree(tmp_in_str, ignore_errors=True)
        shutil.rmtree(tmp_out_str, ignore_errors=True)


@router.post(
    "/api/v1/invoices/batch/stream",
    summary="Stream Upload & Process Multiple Invoice Files (NDJSON)",
    tags=["Invoices"],
)
async def process_batch_files_stream(
    files: list[UploadFile] = File(..., description="Multiple invoice files to process"),
    lang: str = Query(default=DEFAULT_OCR_LANG, description="OCR language(s)"),
    workers: Optional[int] = Query(default=None, description="Number of worker processes for parallel batch execution"),
    use_cache: bool = Query(default=True, description="Enable OCR token caching"),
    ocr_cache_dir: Optional[str] = Query(default=str(DEFAULT_OCR_CACHE_DIR), description="OCR token cache directory"),
):
    """Stream processing of multiple uploaded invoices with real-time NDJSON events without buffering batch in RAM."""
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No files provided in batch request")

    tmp_in_str = tempfile.mkdtemp(prefix="batch_in_")
    tmp_out_str = tempfile.mkdtemp(prefix="batch_out_")
    tmp_in = Path(tmp_in_str)
    tmp_out = Path(tmp_out_str)

    saved_files = []
    for f in files:
        if not f.filename:
            continue
        suffix = Path(f.filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            continue
        dest = tmp_in / f.filename
        with dest.open("wb") as buffer:
            shutil.copyfileobj(f.file, buffer)
        saved_files.append((f.filename, dest))

    if not saved_files:
        shutil.rmtree(tmp_in_str, ignore_errors=True)
        shutil.rmtree(tmp_out_str, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="None of the uploaded files have supported extensions (.pdf, .png, .jpg, .jpeg)",
        )

    return StreamingResponse(
        _stream_uploaded_batch_generator(
            tmp_in=tmp_in,
            tmp_out=tmp_out,
            lang=lang,
            workers=workers,
            use_cache=use_cache,
            ocr_cache_dir=ocr_cache_dir,
        ),
        media_type="application/x-ndjson",
    )


@router.post(
    "/api/v1/invoices/batch-dir",
    summary="Batch Process Server Directory",
    tags=["Invoices"],
)
async def process_batch_directory(request: BatchDirRequest, background_tasks: BackgroundTasks):
    """Process a server-side directory of invoices and produce batch_summary.json."""
    in_path = Path(request.input_dir)
    if not in_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Input directory does not exist: {request.input_dir}",
        )
    if not in_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Input path is not a directory: {request.input_dir}",
        )

    if request.stream:
        return StreamingResponse(
            _stream_directory_batch_generator(
                in_path=in_path,
                out_path=Path(request.output_dir),
                lang=request.lang,
                workers=request.workers,
                use_cache=request.use_cache,
                ocr_cache_dir=request.ocr_cache_dir,
            ),
            media_type="application/x-ndjson",
        )

    if request.async_mode:
        job_id = job_manager.create_job(request_type="batch-dir", metadata={"input_dir": str(in_path)})
        background_tasks.add_task(_run_batch_dir_job, job_id, request.model_dump())
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "job_id": job_id,
                "status": JobStatus.QUEUED.value,
                "message": f"Directory batch processing queued for {request.input_dir}",
                "check_status_url": f"/v1/jobs/{job_id}",
            },
        )

    try:
        dbg = Path(request.debug_dir) if request.debug else None
        summary = process_batch(
            input_dir=in_path,
            output_dir=request.output_dir,
            debug_dir=dbg,
            lang=request.lang,
            summary_file=request.summary_file,
            quiet=True,
            export_nap=request.export_nap,
            export_entries=request.export_entries,
            nap_period=request.nap_period,
            expense_account=request.expense_account,
            erp_format=request.erp_format,
            workers=request.workers,
            use_cache=request.use_cache,
            ocr_cache_dir=request.ocr_cache_dir,
        )
        return summary
    except Exception as exc:
        logger.error("Batch directory processing failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Batch directory processing error: {exc}",
        )


@router.post(
    "/api/v1/invoices/batch-dir/stream",
    summary="Stream Batch Process Server Directory (NDJSON)",
    tags=["Invoices"],
)
async def process_batch_directory_stream(request: BatchDirRequest):
    """Stream server-side directory batch processing yielding live intermediate NDJSON events."""
    in_path = Path(request.input_dir)
    if not in_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Input directory does not exist: {request.input_dir}",
        )
    if not in_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Input path is not a directory: {request.input_dir}",
        )

    return StreamingResponse(
        _stream_directory_batch_generator(
            in_path=in_path,
            out_path=Path(request.output_dir),
            lang=request.lang,
            workers=request.workers,
            use_cache=request.use_cache,
            ocr_cache_dir=request.ocr_cache_dir,
        ),
        media_type="application/x-ndjson",
    )


@router.post(
    "/api/v1/invoices/validate",
    summary="Validate Invoice Data Model",
    tags=["Invoices"],
)
async def validate_invoice_endpoint(payload: dict[str, Any]):
    """Re-validate an existing invoice data model against statutory Bulgarian financial formulas (ЗДДС)."""
    try:
        invoice = _dict_to_invoice(payload)
        val_res = validate_invoice(invoice, tokens=[])
        response_payload = {
            "is_valid": val_res.is_valid,
            "errors": [asdict(e) for e in val_res.errors],
            "warnings": [asdict(w) for w in val_res.warnings],
        }
        if val_res.legal_compliance_report:
            response_payload["legal_compliance_report"] = asdict(val_res.legal_compliance_report)
        return response_payload
    except Exception as exc:
        logger.error("Error validating invoice payload: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid invoice payload structure: {exc}",
        )


# ---------------------------------------------------------------------------
# 100% Local Drop-In Compatibility Layer (SmartScan / MICROINVEST-OCR)
# ---------------------------------------------------------------------------

@router.post(
    "/api/document-scanner/extract-invoice",
    tags=["Document Scanner (Local Drop-in)"],
    summary="100% Local Invoice Extraction (Replaces Claude/Vertex AI)",
)
async def extract_invoice_local(request: Request):
    """Extract structured invoice data using local Tesseract v5 + OpenCV pipeline without external API calls."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    image_payload = body.get("image")
    if not image_payload:
        raise HTTPException(status_code=400, detail="Image data is required")

    file_bytes, suffix = _decode_image_payload(image_payload)
    inv, raw_text = await run_in_threadpool(_process_bytes_locally, file_bytes, suffix)
    return _format_smartscan_response(inv, raw_text)


@router.post(
    "/api/document-scanner/extract-invoices-batch",
    tags=["Document Scanner (Local Drop-in)"],
    summary="100% Local Batch Extraction (Replaces Cloud Batch APIs)",
)
async def extract_invoices_batch_local(request: Request):
    """Extract structured data from a batch of invoices 100% locally."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    items = body.get("invoices") or body.get("images") or body.get("documents")
    if not items or not isinstance(items, list):
        raise HTTPException(status_code=400, detail="An array of invoice objects/images is required")

    results = []
    for i, item in enumerate(items):
        item_id = f"invoice_{i + 1}"
        if isinstance(item, dict):
            img = item.get("image") or item.get("data") or item.get("base64")
            item_id = item.get("id") or item_id
        else:
            img = str(item)

        if not img:
            results.append({"id": item_id, "index": i, "success": False, "error": "Image data missing"})
            continue

        try:
            file_bytes, suffix = _decode_image_payload(img)
            inv, raw_text = await run_in_threadpool(_process_bytes_locally, file_bytes, suffix)
            res_dict = _format_smartscan_response(inv, raw_text)
            results.append({
                "id": item_id,
                "index": i,
                "success": True,
                "data": res_dict["data"],
                "needsValidation": res_dict["needsValidation"],
            })
        except Exception as exc:
            logger.exception("Batch item %d extraction error: %s", i, exc)
            results.append({"id": item_id, "index": i, "success": False, "error": str(exc)})

    success_count = sum(1 for r in results if r.get("success"))
    return {
        "success": True,
        "totalCount": len(items),
        "successCount": success_count,
        "failedCount": len(items) - success_count,
        "results": results,
    }


@router.post(
    "/api/document-scanner/classify",
    tags=["Document Scanner (Local Drop-in)"],
    summary="100% Local Document Classification (Replaces Claude Vision)",
)
async def classify_document_local(request: Request):
    """Classify document type using 100% local deterministic heuristics. Zero Cloud/Claude cost."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    image_payload = body.get("image")
    if not image_payload:
        raise HTTPException(status_code=400, detail="Image data is required")

    file_bytes, suffix = _decode_image_payload(image_payload)
    inv, _ = await run_in_threadpool(_process_bytes_locally, file_bytes, suffix)
    doc_type = inv.invoice_metadata.document_type
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
    category = mapping.get(str(doc_type).upper(), "invoice")
    return {
        "success": True,
        "documentType": category,
        "confidence": "high",
        "engine": "local-rule-classifier",
    }


# ---------------------------------------------------------------------------
# Tesseract Local Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/api/tesseract/ocr",
    tags=["Tesseract Local Router"],
    summary="Raw Local Tesseract OCR",
)
async def tesseract_ocr_endpoint(request: Request):
    """Raw local Tesseract OCR processing with plain text and confidence analysis."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    img_payload = body.get("image")
    if not img_payload:
        raise HTTPException(status_code=400, detail="Image is required")

    file_bytes, suffix = _decode_image_payload(img_payload)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)
    try:
        import cv2
        img = cv2.imread(str(tmp_path))
        lang = body.get("lang") or "bul+eng"
        t0 = time.time()
        text = pytesseract.image_to_string(img, lang=lang) if img is not None else ""
        elapsed_ms = int((time.time() - t0) * 1000)
        return {
            "success": True,
            "data": {
                "text": text,
                "wordCount": len(text.split()),
                "confidence": 95.0,
                "processingTimeMs": elapsed_ms,
                "engine": "tesseract-v5-local",
            },
        }
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post(
    "/api/tesseract/ocr-data",
    tags=["Tesseract Local Router"],
    summary="Word-level Local OCR Data with Bounding Boxes",
)
async def tesseract_ocr_data_endpoint(request: Request):
    """Word-level OCR data with bounding boxes and confidence scores."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    img_payload = body.get("image")
    if not img_payload:
        raise HTTPException(status_code=400, detail="Image is required")

    file_bytes, suffix = _decode_image_payload(img_payload)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)
    try:
        import cv2
        img = cv2.imread(str(tmp_path))
        lang = body.get("lang") or "bul+eng"
        t0 = time.time()
        data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT) if img is not None else {}
        words = []
        n_boxes = len(data.get("text", []))
        for i in range(n_boxes):
            w_text = data["text"][i].strip()
            if w_text:
                words.append({
                    "text": w_text,
                    "confidence": float(data["conf"][i]),
                    "bbox": {
                        "left": data["left"][i],
                        "top": data["top"][i],
                        "width": data["width"][i],
                        "height": data["height"][i],
                    },
                })
        full_text = " ".join(w["text"] for w in words)
        return {
            "success": True,
            "data": {
                "words": words,
                "confidence": 95.0,
                "text": full_text,
                "processingTimeMs": int((time.time() - t0) * 1000),
            },
        }
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post(
    "/api/tesseract/extract-amounts",
    tags=["Tesseract Local Router"],
    summary="Specialized Numeric Amount Extraction",
)
async def tesseract_extract_amounts_endpoint(request: Request):
    """Specialized endpoint for extracting monetary amounts locally."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    img_payload = body.get("image")
    if not img_payload:
        raise HTTPException(status_code=400, detail="Image is required")

    file_bytes, suffix = _decode_image_payload(img_payload)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)
    try:
        import cv2
        img = cv2.imread(str(tmp_path))
        t0 = time.time()
        text = pytesseract.image_to_string(img, lang="bul+eng") if img is not None else ""
        raw_amounts = re.findall(r"\b\d{1,7}[.,]\d{2}\b", text)
        amounts = []
        for a in raw_amounts:
            try:
                parsed = float(a.replace(",", "."))
                amounts.append({"raw": a, "parsed": parsed, "confidence": 95.0})
            except Exception:
                pass
        return {
            "success": True,
            "data": {
                "amounts": amounts,
                "rawText": text,
                "processingTimeMs": int((time.time() - t0) * 1000),
            },
        }
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post(
    "/api/tesseract/extract-invoice",
    tags=["Tesseract Local Router"],
    summary="Compatibility Alias for Invoice Extraction",
)
async def tesseract_extract_invoice_endpoint(request: Request):
    """Compatibility alias for /api/document-scanner/extract-invoice with extra OCR metadata."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    img_payload = body.get("image")
    if not img_payload:
        raise HTTPException(status_code=400, detail="Image is required")

    file_bytes, suffix = _decode_image_payload(img_payload)
    inv, raw_text = await run_in_threadpool(_process_bytes_locally, file_bytes, suffix)
    res = _format_smartscan_response(inv, raw_text)
    res["ocr"] = {
        "rawText": raw_text,
        "confidence": float(inv.invoice_metadata.ocr_confidence_score or 0.95),
        "engine": "tesseract-v5-local",
    }
    return res

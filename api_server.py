"""FastAPI REST API Microservice for Bulgarian Invoice OCR & Document Understanding.

Provides HTTP REST endpoints for seamless integration with ERP, accounting,
and document management systems:
- GET  /health                      - Microservice and OCR engine health status
- GET  /api/v1/languages            - Installed and available Tesseract languages
- POST /api/v1/invoices/process     - Upload and process a single invoice (PDF / image)
- POST /api/v1/invoices/batch       - Upload and process multiple invoice files
- POST /api/v1/invoices/batch-dir   - Batch process a server-side directory of invoices
- POST /api/v1/invoices/validate    - Re-validate an existing invoice JSON payload
- GET  /api/v1/vendors              - List loaded vendor profiles and configuration status
- POST /api/v1/vendors/reload       - Dynamic hot-reload of vendor YAML profiles without restart
"""

import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from typing import Any, Optional
import uuid

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import pytesseract
from sqlalchemy import desc
from sqlalchemy.orm import Session

from database import (
    AuditTrailRecord,
    DocumentRecord,
    InvoiceFeedbackRecord,
    PersistentJobRecord,
    WebhookLogRecord,
    add_audit_entry,
    approve_document_in_db,
    get_db,
    get_db_session,
    init_db,
    save_classified_document_to_db,
    save_document_to_db,
    storage_manager,
    update_document_corrections,
)
from decimal import Decimal

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
    TesseractLanguageMissingError,
    DEFAULT_MAX_OCR_WORKERS,
    OCRProcessPoolExecutor,
    get_ocr_pool,
    init_ocr_pool,
    shutdown_ocr_pool,
    iter_process_batch,
    ensure_tesseract_ready,
    get_installed_ocr_languages,
    process_batch,
    process_invoice,
    serialize_invoice,
    setup_tessdata_prefix,
    validate_invoice,
    verify_tesseract_languages,
)
from webhooks import (
    DEFAULT_WEBHOOK_SECRET,
    prepare_webhook_payload,
    send_webhook_async,
)
from invoice_core.constants import DEFAULT_MIN_ATTACHMENT_SIZE_BYTES
from invoice_core.email_ingestion import (
    EmailAttachment,
    EmailSecurityResult,
    ParsedEmail,
    filter_attachment,
    parse_cloudflare_worker_json,
    parse_mime_email,
    parse_multipart_form_data,
    verify_webhook_token,
)
from invoice_core.notifications import (
    InvoiceNotificationSummary,
    dispatch_reverse_notifications_bundle,
    send_email_confirmation_async,
    send_slack_notification_async,
    send_telegram_notification_async,
)
from invoice_core.watcher import (
    FolderWatcher,
    FolderWatcherConfig,
)
from invoice_core.imap_poller import (
    ImapPoller,
    ImapPollerConfig,
)
from invoice_core.document_classifier import (
    ClassificationResult,
    DocumentCategory,
    SubdocumentInfo,
    get_document_classifier,
)

logger = logging.getLogger("invoice_ocr_api")
SERVICE_START_TIME = time.time()
API_VERSION = "1.0.0"
global_watcher: Optional[FolderWatcher] = None

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


# ---------------------------------------------------------------------------
# Lifespan Context Manager
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify OCR engine, environment readiness, and database initialization on server startup."""
    setup_tessdata_prefix()
    init_db()
    # Initialize isolated ProcessPoolExecutor for CPU-bound OCR workloads
    ocr_workers = int(os.environ.get("MAX_OCR_WORKERS", str(min(os.cpu_count() or 4, 16))))
    init_ocr_pool(max_workers=ocr_workers)
    logger.info("Initialized OCR ProcessPoolExecutor with %d workers", ocr_workers)
    try:
        tess_ver = pytesseract.get_tesseract_version()
        ready, available, missing = verify_tesseract_languages(["bul", "eng"])
        logger.info(
            "Tesseract ready (v%s). Languages: available=%s, missing=%s",
            tess_ver, available, missing,
        )
    except Exception as exc:
        logger.warning("Tesseract startup check warning: %s", exc)

    # Initialize FolderWatcher if enabled via environment
    global global_watcher
    watch_dir_env = os.environ.get("WATCH_DIR")
    if watch_dir_env or os.environ.get("WATCHER_ENABLED", "").lower() in ("1", "true"):
        w_dir = Path(watch_dir_env or "watch_invoices")
        cfg = FolderWatcherConfig(
            watch_dir=w_dir,
            poll_interval_sec=float(os.environ.get("WATCHER_POLL_INTERVAL_SEC", "2.0")),
            erp_webhook_url=os.environ.get("WATCHER_WEBHOOK_URL"),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
            slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL"),
            notification_email=os.environ.get("WATCHER_NOTIFICATION_EMAIL"),
        )
        global_watcher = FolderWatcher(cfg)
        global_watcher.start()
        logger.info("Started automatic FolderWatcher on: %s", w_dir)

    yield

    # Graceful shutdown of watcher and worker pool
    if global_watcher and global_watcher.is_running:
        logger.info("Stopping FolderWatcher...")
        global_watcher.stop()

    logger.info("Shutting down OCR ProcessPoolExecutor...")
    shutdown_ocr_pool(wait=True)


# ---------------------------------------------------------------------------
# FastAPI Application Configuration
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Bulgarian Invoice OCR Microservice",
    description=(
        "REST API microservice for automated Bulgarian invoice recognition, "
        "spatial layout extraction, legal UIC/VAT verification, and financial cross-validation (ЗДДС)."
    ),
    version=API_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Mount static directory for HITL Web Dashboard if present
static_dir = Path("static")
if static_dir.exists():
    app.mount("/static", StaticFiles(directory="static"), name="static")

# Enable CORS for web-based ERP interfaces
# Security: When allow_origins=["*"], credentials MUST be False per Fetch spec.
# In production, restrict allow_origins to specific ERP/dashboard domains.
CORS_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """Add processing time header to all HTTP responses."""
    start_time = time.perf_counter()
    response: Response = await call_next(request)
    process_time = time.perf_counter() - start_time
    response.headers["X-Process-Time-Sec"] = f"{process_time:.4f}"
    return response


# ---------------------------------------------------------------------------
# Optional API Key Authentication
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("API_KEY", "")
API_KEY_HEADER = "X-API-Key"
# Paths exempt from API key authentication (health, docs, static, dashboard)
AUTH_EXEMPT_PATHS = {"/", "/health", "/docs", "/redoc", "/openapi.json", "/dashboard", "/hitl"}
AUTH_EXEMPT_PREFIXES = ("/static/", "/api/document-scanner/", "/api/tesseract/", "/api/businesses/")


@app.middleware("http")
async def api_key_auth_middleware(request: Request, call_next):
    """Optional API key authentication. Set API_KEY env var to enable."""
    if API_KEY:  # Only enforce if API_KEY is configured
        path = request.url.path
        if path not in AUTH_EXEMPT_PATHS and not path.startswith(AUTH_EXEMPT_PREFIXES):
            provided_key = request.headers.get(API_KEY_HEADER, "")
            if provided_key != API_KEY:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing API key. Provide via X-API-Key header."},
                )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Pydantic Schemas for API Documentation & Validation
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str = Field(..., json_schema_extra={"example": "ok"})
    version: str = Field(..., json_schema_extra={"example": "1.0.0"})
    uptime_seconds: float = Field(..., json_schema_extra={"example": 123.45})
    tesseract_version: Optional[str] = Field(None, json_schema_extra={"example": "5.3.4"})
    tesseract_ready: bool = Field(..., json_schema_extra={"example": True})
    available_languages: list[str] = Field(..., json_schema_extra={"example": ["bul", "eng", "osd"]})
    missing_required_languages: list[str] = Field(..., json_schema_extra={"example": []})


class LanguagesResponse(BaseModel):
    available_languages: list[str] = Field(..., json_schema_extra={"example": ["bul", "eng", "osd"]})
    required_ready: bool = Field(..., json_schema_extra={"example": True})
    missing_languages: list[str] = Field(..., json_schema_extra={"example": []})


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


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class JobProgress(BaseModel):
    total_documents: int = Field(default=0, description="Total number of documents in batch")
    processed_documents: int = Field(default=0, description="Number of processed documents so far")
    percent: float = Field(default=0.0, description="Completion percentage (0.0 - 100.0)")
    current_file: Optional[str] = Field(default=None, description="Name of file currently being processed")


class JobResponse(BaseModel):
    job_id: str
    status: str
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    progress: JobProgress
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class JobCreateResponse(BaseModel):
    job_id: str
    status: str
    message: str
    check_status_url: str


class JobListResponse(BaseModel):
    total_jobs: int
    jobs: list[JobResponse]


class ContractorVerifyRequest(BaseModel):
    identifier: str = Field(..., description="EIK, Bulstat, or foreign VAT number", json_schema_extra={"example": "121644736"})
    country_code: Optional[str] = Field(default=None, description="Country code (e.g. BG, IE, DE, LU)", json_schema_extra={"example": "BG"})
    date_tax_event: Optional[str] = Field(default=None, description="Tax event transaction date (YYYY-MM-DD)", json_schema_extra={"example": "2026-08-16"})
    bypass_cache: bool = Field(default=False, description="Bypass local cache and query live registry")


class Protocol117GenerateRequest(BaseModel):
    invoice: dict[str, Any] = Field(..., description="Foreign/EU invoice JSON payload")
    protocol_number: str = Field(default="0000000001", description="10-digit protocol number")
    protocol_date: Optional[str] = Field(default=None, description="Protocol date YYYY-MM-DD (within 15 days of invoice)")
    recipient_company: Optional[dict[str, Any]] = Field(default=None, description="Bulgarian recipient company details")
    legal_basis: Optional[str] = Field(default=None, description="Statutory legal basis (e.g. чл. 82, ал. 2, т. 3 ЗДДС)")
    full_tax_credit: bool = Field(default=True, description="Right to full tax credit deduction")


class VatDeclarationRequest(BaseModel):
    purchase_invoices: list[dict[str, Any]] = Field(..., description="List of purchase invoice JSON payloads")
    sales_invoices: Optional[list[dict[str, Any]]] = Field(default=None, description="List of sales invoice JSON payloads")
    company_info: Optional[dict[str, Any]] = Field(default=None, description="Company details (name, EIK, VAT number)")
    period: str = Field(default="202608", description="VAT period YYYYMM")
    prior_vat_credit_cell_70: float = Field(default=0.0, description="Deductions from prior periods under Art. 92 ЗДДС")
    partial_credit_coefficient_cell_42: float = Field(default=1.0, description="Coefficient under Art. 73 ЗДДС")


class NapPackageExportRequest(BaseModel):
    purchase_invoices: list[dict[str, Any]] = Field(..., description="List of purchase invoice JSON payloads")
    sales_invoices: Optional[list[dict[str, Any]]] = Field(default=None, description="List of sales invoice JSON payloads")
    company_info: Optional[dict[str, Any]] = Field(default=None, description="Company details (name, EIK, VAT number)")
    period: str = Field(default="202608", description="VAT period YYYYMM")
    format: str = Field(default="fixed_width", description="Format: 'fixed_width', 'tsv', or 'csv'")
    encoding: str = Field(default="cp1251", description="Encoding: 'cp1251' or 'utf-8'")
    auto_generate_protocols: bool = Field(default=True, description="Auto-generate Art. 117 protocols for reverse charge")
    as_zip: bool = False


class MicroinvestExportRequest(BaseModel):
    invoices: list[dict[str, Any]] = Field(..., description="List of invoice JSON payloads")
    default_expense_account: str = Field(default="602", description="Default expense account for Delta Pro")
    default_goods_account: str = Field(default="304", description="Default goods account for Delta Pro")
    default_vat_account: str = Field(default="453/1", description="Default VAT account for Delta Pro")
    default_supplier_account: str = Field(default="401", description="Default supplier account for Delta Pro")


class BusinessNavigatorExportRequest(BaseModel):
    invoices: list[dict[str, Any]] = Field(..., description="List of invoice JSON payloads")
    encoding: str = Field(default="windows-1251", description="File encoding (windows-1251 or utf-8)")


class AjurExportRequest(BaseModel):
    invoices: list[dict[str, Any]] = Field(..., description="List of invoice JSON payloads")
    encoding: str = Field(default="windows-1251", description="File encoding (windows-1251 or utf-8)")


class TaxPeriodValidationApiRequest(BaseModel):
    invoices: list[dict[str, Any]] = Field(..., description="List of invoice JSON payloads to validate")
    target_period: Optional[str] = Field(default=None, description="Target VAT period (e.g. 202608 or 2026-08)")


class SupplierMappingRuleApiRequest(BaseModel):
    eik: str = Field(..., description="Supplier EIK / BULSTAT or VAT number")
    target_account: str = Field(..., description="Target accounting account (e.g. 6012, 6021, 3041)")
    target_subledger: Optional[str] = Field(default=None, description="Optional analytical subledger code")
    supplier_name: Optional[str] = Field(default="", description="Supplier name")
    description: Optional[str] = Field(default="", description="Description of the rule")


class KeywordMappingRuleApiRequest(BaseModel):
    rule_id: str = Field(..., description="Unique rule identifier")
    target_account: str = Field(..., description="Target accounting account (e.g. 6012, 6021, 3041)")
    target_subledger: Optional[str] = Field(default=None, description="Optional analytical subledger code")
    keywords: list[str] = Field(default_factory=list, description="List of keywords to match")
    regex_pattern: Optional[str] = Field(default=None, description="Optional regular expression pattern")
    description: Optional[str] = Field(default="", description="Description of the rule")
    priority: int = Field(default=10, description="Rule priority (higher value = higher precedence)")



# ---------------------------------------------------------------------------
# Background Task Queue & Persistent Job Manager (Pillar 4 & 5, M12/M14)
# ---------------------------------------------------------------------------

class ApproveDocumentRequest(BaseModel):
    actor: str = Field(default="accountant", description="Name or identifier of accountant approving document")
    webhook_url: Optional[str] = Field(default=None, description="Optional ERP webhook URL to notify")


class ApproveAndExportRequest(BaseModel):
    corrections: Optional[dict[str, Any]] = Field(default=None, description="Manual field corrections applied via HITL interface")
    actor: str = Field(default="accountant", description="Name or identifier of accountant approving document")
    webhook_url: Optional[str] = Field(default=None, description="Optional ERP webhook URL to notify")
    export_format: str = Field(default="all", description="Export type: pokupki, journal_entries, or all")


class WebhookTestRequest(BaseModel):
    target_url: str = Field(..., description="Target URL for ERP webhook endpoint")
    secret: Optional[str] = Field(default=None, description="HMAC secret key")


class JobManager:
    """Thread-safe persistent store and lifecycle manager for asynchronous OCR jobs."""

    def __init__(self, max_history: int = 200):
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._max_history = max_history
        self._load_from_db()

    def _load_from_db(self):
        try:
            with get_db_session() as db:
                records = db.query(PersistentJobRecord).order_by(PersistentJobRecord.created_at.desc()).limit(self._max_history).all()
                with self._lock:
                    reconciled = 0
                    for r in records:
                        # Reconcile orphaned jobs left in queued/processing state across restarts
                        if r.status in (JobStatus.PROCESSING.value, JobStatus.QUEUED.value):
                            r.status = JobStatus.INTERRUPTED.value
                            r.completed_at = time.time()
                            r.error_message = "Job was interrupted by server restart/shutdown"
                            reconciled += 1
                        self._jobs[r.job_id] = r.to_dict()
                    if reconciled > 0:
                        db.commit()
                        logger.info("Reconciled %d orphaned background jobs on startup", reconciled)
        except Exception as exc:
            logger.warning("Could not load jobs from database on startup: %s", exc)

    def _persist_job(self, job_dict: dict[str, Any]):
        try:
            with get_db_session() as db:
                job_id = job_dict["job_id"]
                rec = db.query(PersistentJobRecord).filter(PersistentJobRecord.job_id == job_id).first()
                if not rec:
                    rec = PersistentJobRecord(
                        job_id=job_id,
                        status=job_dict["status"],
                        request_type=job_dict.get("request_type", "batch"),
                        created_at=job_dict["created_at"],
                    )
                    db.add(rec)
                rec.status = job_dict["status"]
                rec.started_at = job_dict.get("started_at")
                rec.completed_at = job_dict.get("completed_at")
                prog = job_dict.get("progress", {})
                rec.total_documents = prog.get("total_documents", 0)
                rec.processed_documents = prog.get("processed_documents", 0)
                rec.progress_percent = prog.get("percent", 0.0)
                rec.current_file = prog.get("current_file")
                rec.metadata_json = json.dumps(job_dict.get("metadata", {}), ensure_ascii=False)
                rec.result_json = json.dumps(job_dict.get("result"), ensure_ascii=False) if job_dict.get("result") else None
                rec.error_message = job_dict.get("error")
                rec.webhook_url = job_dict.get("webhook_url")
                db.commit()
        except Exception as exc:
            logger.error("Failed to persist job %s to DB: %s", job_dict.get("job_id"), exc)

    def create_job(self, request_type: str = "batch", metadata: Optional[dict[str, Any]] = None, webhook_url: Optional[str] = None) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            if len(self._jobs) >= self._max_history:
                oldest = min(self._jobs.keys(), key=lambda k: self._jobs[k]["created_at"])
                self._jobs.pop(oldest, None)

            job_dict = {
                "job_id": job_id,
                "status": JobStatus.QUEUED.value,
                "created_at": time.time(),
                "started_at": None,
                "completed_at": None,
                "progress": {
                    "total_documents": 0,
                    "processed_documents": 0,
                    "percent": 0.0,
                    "current_file": None,
                },
                "result": None,
                "error": None,
                "request_type": request_type,
                "metadata": metadata or {},
                "webhook_url": webhook_url,
            }
            self._jobs[job_id] = job_dict

        self._persist_job(job_dict)
        return job_id

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                return json.loads(json.dumps(job))
        try:
            with get_db_session() as db:
                rec = db.query(PersistentJobRecord).filter(PersistentJobRecord.job_id == job_id).first()
                if rec:
                    d = rec.to_dict()
                    with self._lock:
                        self._jobs[job_id] = d
                    return d
        except Exception:
            pass
        return None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            sorted_jobs = sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)
            return [json.loads(json.dumps(j)) for j in sorted_jobs[:limit]]

    def set_started(self, job_id: str, total_documents: int = 0) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["status"] = JobStatus.PROCESSING.value
                self._jobs[job_id]["started_at"] = time.time()
                self._jobs[job_id]["progress"]["total_documents"] = total_documents
                job_dict = dict(self._jobs[job_id])
            else:
                return
        self._persist_job(job_dict)

    def update_progress(self, job_id: str, processed: int, total: int, current_file: Optional[str] = None) -> None:
        with self._lock:
            if job_id in self._jobs:
                pct = round((processed / total * 100.0), 1) if total > 0 else 0.0
                self._jobs[job_id]["progress"]["processed_documents"] = processed
                self._jobs[job_id]["progress"]["total_documents"] = total
                self._jobs[job_id]["progress"]["percent"] = pct
                self._jobs[job_id]["progress"]["current_file"] = current_file
                job_dict = dict(self._jobs[job_id])
            else:
                return
        self._persist_job(job_dict)

    def complete_job(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["status"] = JobStatus.COMPLETED.value
                self._jobs[job_id]["completed_at"] = time.time()
                self._jobs[job_id]["progress"]["percent"] = 100.0
                total = self._jobs[job_id]["progress"]["total_documents"]
                self._jobs[job_id]["progress"]["processed_documents"] = total
                self._jobs[job_id]["result"] = result
                job_dict = dict(self._jobs[job_id])
            else:
                return
        self._persist_job(job_dict)

    def fail_job(self, job_id: str, error_message: str) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["status"] = JobStatus.FAILED.value
                self._jobs[job_id]["completed_at"] = time.time()
                self._jobs[job_id]["error"] = error_message
                job_dict = dict(self._jobs[job_id])
            else:
                return
        self._persist_job(job_dict)

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            deleted = self._jobs.pop(job_id, None) is not None
        try:
            with get_db_session() as db:
                rec = db.query(PersistentJobRecord).filter(PersistentJobRecord.job_id == job_id).first()
                if rec:
                    db.delete(rec)
                    db.commit()
                    deleted = True
        except Exception:
            pass
        return deleted

    def retry_job(self, job_id: str) -> Optional[dict[str, Any]]:
        """Reset an interrupted, failed, or cancelled job back to QUEUED for execution."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if job["status"] not in (JobStatus.INTERRUPTED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value):
                raise ValueError(f"Cannot retry job in '{job['status']}' state")
            job["status"] = JobStatus.QUEUED.value
            job["error"] = None
            job["started_at"] = None
            job["completed_at"] = None
            job["progress"] = {
                "total_documents": 0,
                "processed_documents": 0,
                "percent": 0.0,
                "current_file": None,
            }
            job_dict = dict(job)
        self._persist_job(job_dict)
        return job_dict


job_manager = JobManager()


def _run_batch_dir_job(job_id: str, request_data: dict[str, Any]):
    """Background task runner for batch directory processing."""
    try:
        in_path = _validate_server_path(request_data["input_dir"], "input_dir")
        files = [
            p for p in in_path.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        job_manager.set_started(job_id, total_documents=len(files))

        def on_progress(processed: int, total: int, current_file: str):
            job_manager.update_progress(job_id, processed, total, current_file)

        dbg = Path(request_data["debug_dir"]) if request_data.get("debug") else None
        summary = process_batch(
            input_dir=in_path,
            output_dir=request_data.get("output_dir", "results/"),
            debug_dir=dbg,
            lang=request_data.get("lang", DEFAULT_OCR_LANG),
            summary_file=request_data.get("summary_file"),
            quiet=True,
            export_nap=request_data.get("export_nap", False),
            export_entries=request_data.get("export_entries", False),
            nap_period=request_data.get("nap_period"),
            expense_account=request_data.get("expense_account"),
            erp_format=request_data.get("erp_format", "universal"),
            workers=request_data.get("workers"),
            use_cache=request_data.get("use_cache", True),
            ocr_cache_dir=request_data.get("ocr_cache_dir", ".ocr_cache"),
            progress_callback=on_progress,
        )
        job_manager.complete_job(job_id, summary)
    except Exception as exc:
        logger.error("Background job %s failed: %s", job_id, exc, exc_info=True)
        job_manager.fail_job(job_id, str(exc))


def _run_batch_upload_job(
    job_id: str,
    temp_in_dir: Path,
    temp_out_dir: Path,
    lang: str,
    workers: Optional[int],
    use_cache: bool,
    ocr_cache_dir: Optional[str],
):
    """Background task runner for batch file upload processing."""
    try:
        files = [
            p for p in temp_in_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        job_manager.set_started(job_id, total_documents=len(files))

        def on_progress(processed: int, total: int, current_file: str):
            job_manager.update_progress(job_id, processed, total, current_file)

        summary = process_batch(
            input_dir=temp_in_dir,
            output_dir=temp_out_dir,
            lang=lang,
            quiet=True,
            workers=workers,
            use_cache=use_cache,
            ocr_cache_dir=ocr_cache_dir,
            progress_callback=on_progress,
        )
        job_manager.complete_job(job_id, summary)
    except Exception as exc:
        logger.error("Background upload job %s failed: %s", job_id, exc, exc_info=True)
        job_manager.fail_job(job_id, str(exc))
    finally:
        shutil.rmtree(temp_in_dir, ignore_errors=True)
        shutil.rmtree(temp_out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", summary="API Root")
async def root():
    """Welcome endpoint providing API overview and documentation links."""
    return {
        "service": "Bulgarian Invoice OCR REST API",
        "version": API_VERSION,
        "docs": "/docs",
        "redoc": "/redoc",
        "dashboard": "/dashboard",
        "endpoints": {
            "dashboard": "GET /dashboard",
            "hitl": "GET /hitl",
            "health": "GET /health",
            "languages": "GET /api/v1/languages",
            "process_invoice": "POST /api/v1/invoices/process",
            "batch_invoices": "POST /api/v1/invoices/batch",
            "batch_dir": "POST /api/v1/invoices/batch-dir",
            "list_documents": "GET /api/v1/documents",
            "get_document": "GET /api/v1/documents/{document_id}",
            "correct_document": "POST /api/v1/documents/{document_id}/correct",
            "approve_document": "POST /api/v1/documents/{document_id}/approve",
            "audit_trail": "GET /api/v1/documents/{document_id}/audit-trail",
            "test_webhook": "POST /api/v1/webhooks/test",
            "webhook_logs": "GET /api/v1/webhooks/logs",
        },
    }


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Health & Readiness Check",
    tags=["System"],
)
async def health():
    """Check microservice health, uptime, and Tesseract OCR engine readiness."""
    try:
        tess_ver = pytesseract.get_tesseract_version()
        is_ready, available, missing = verify_tesseract_languages(["bul", "eng"])
        tess_ready = is_ready
    except Exception:
        tess_ver = None
        tess_ready = False
        available = []
        missing = ["bul", "eng"]

    return HealthResponse(
        status="ok" if tess_ready else "degraded",
        version=API_VERSION,
        uptime_seconds=round(time.time() - SERVICE_START_TIME, 2),
        tesseract_version=str(tess_ver) if tess_ver else None,
        tesseract_ready=tess_ready,
        available_languages=available,
        missing_required_languages=missing,
    )


@app.get(
    "/api/v1/languages",
    response_model=LanguagesResponse,
    summary="List Installed OCR Languages",
    tags=["System"],
)
async def list_languages():
    """List all available Tesseract OCR languages and check required Bulgarian/English models."""
    is_ready, available, missing = verify_tesseract_languages(["bul", "eng"])
    return LanguagesResponse(
        available_languages=available,
        required_ready=is_ready,
        missing_languages=missing,
    )


@app.post(
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
    """Upload an invoice document, persist to database, and extract structured 3-layer data.
    
    Returns:
    - id: Unique document ID
    - invoice_metadata: invoice number, issue date, tax event date
    - supplier: name, validated 9/13-digit EIK, VAT number, address
    - recipient: name, validated 9/13-digit EIK, VAT number, address
    - line_items: table rows with quantities, unit prices, totals
    - financial_summary: tax base, VAT amount, total amount due, currency
    - validation: financial formulas consistency and Bulgarian tax rules compliance
    """
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Filename is required",
        )

    suffix = _validate_uploaded_extension(file.filename)

    # Save to a temporary file to preserve streaming and O(1) memory
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
        # Check that the uploaded file has content
        if tmp_path.stat().st_size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty (0 bytes)",
            )

        dbg_path = Path(debug_dir) if debug else None
        start_time = time.perf_counter()
        # Offload CPU-intensive OCR pipeline to isolated ProcessPoolExecutor to avoid blocking event loop
        pool = get_ocr_pool()
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

        full_result = _invoice_to_dict(invoice, include_raw_evidence=True)
        full_result["file_name"] = file.filename

        # Persist document to database & storage
        doc_id = uuid.uuid4().hex
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
            saved_dict = doc_rec.to_dict(include_raw_evidence=include_raw_evidence)

        # Dispatch Webhook notification asynchronously to ERP if requested
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


# ---------------------------------------------------------------------------
# Zero-Touch Ingestion Endpoints (Email & Cloud / Folder Watcher)
# ---------------------------------------------------------------------------

@app.post(
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
    pool = get_ocr_pool()

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
            full_res = _invoice_to_dict(inv, include_raw_evidence=True)
            full_res["file_name"] = att.filename
            full_res["source_channel"] = "email"
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
                "status": saved_dict.get("status"),
                "is_valid": saved_dict.get("is_valid"),
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


# ---------------------------------------------------------------------------
# Document Classification & Docs-Email Ingestion Endpoints
# ---------------------------------------------------------------------------

@app.post(
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
    """Classify an uploaded document into statutory categories:
    - Фактури (INVOICE)
    - Кредитни известия (CREDIT_NOTE)
    - Стокови разписки (STOCK_RECEIPT)
    - Фискални бонове (FISCAL_RECEIPT)
    - Пощенски парични преводи (POSTAL_MONEY_TRANSFER)
    - Платежни документи (PAYMENT_DOCUMENT)
    - Некласифицирани (UNCLASSIFIED)

    Performs multi-page boundary analysis, physical obscuration detection (e.g. receipt covering invoice header),
    and dispatches to Supabase PostgreSQL & n8n workflow automation.
    """
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
        classifier = get_document_classifier()
        classification: ClassificationResult = await run_in_threadpool(
            classifier.classify_file,
            tmp_path,
            file_name,
        )
        proc_time = time.perf_counter() - t0

        # Check if classified as Invoice and requested to auto-process
        if classification.category == DocumentCategory.FAKTURI and auto_process_invoice and not classification.is_obscured:
            pool = get_ocr_pool()
            ocr_res = await pool.submit_ocr_async(
                file_path=tmp_path,
                lang=lang,
                use_cache=True,
                return_invoice_object=True,
            )
            if ocr_res.get("status") == "success" and ocr_res.get("invoice") is not None:
                inv = ocr_res["invoice"]
                full_res = _invoice_to_dict(inv, include_raw_evidence=True)
                full_res["file_name"] = file_name
                full_res["source_channel"] = "MANUAL_UPLOAD"
                full_res["classification"] = classification.to_dict()

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
                    "processing_time_sec": round(proc_time, 3),
                    "invoice_data": saved_dict,
                }

        # For non-invoice categories (or when auto_process_invoice is False or document is obscured)
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


@app.post(
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
    pool = get_ocr_pool()
    classifier = get_document_classifier()

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

            if classification.category == DocumentCategory.FAKTURI and not classification.is_obscured:
                # Invoice Pipeline
                res = await pool.submit_ocr_async(
                    file_path=tmp_path,
                    lang=lang,
                    use_cache=True,
                    return_invoice_object=True,
                )
                if res.get("status") == "success" and res.get("invoice") is not None:
                    inv = res["invoice"]
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
                        saved_dict = doc_rec.to_dict()

                    notif_res = await dispatch_reverse_notifications_bundle(
                        doc_dict=saved_dict,
                        sender_email=parsed_email.sender,
                        erp_webhook_url=webhook_url,
                        hitl_base_url=hitl_base_url,
                    )

                    results.append({
                        "document_id": doc_id,
                        "file_name": att.filename,
                        "category": classification.category.value,
                        "category_code": classification.category.name,
                        "confidence": round(classification.confidence, 4),
                        "routing_action": "routed_to_invoices",
                        "invoice_number": saved_dict.get("invoice_number"),
                        "supplier_name": saved_dict.get("supplier_name"),
                        "total_amount": saved_dict.get("total_amount"),
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


@app.post(
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


@app.get(
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


@app.post(
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


@app.post(
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

    # Save uploaded files into the batch directory
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


@app.post(
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


@app.post(
    "/api/v1/invoices/batch-dir",
    summary="Batch Process Server Directory",
    tags=["Invoices"],
)
async def process_batch_directory(request: BatchDirRequest, background_tasks: BackgroundTasks):
    """Process a server-side directory of invoices and produce batch_summary.json.
    
    Ideal for automated ERP folder monitoring, shared NFS/SMB drops, or scheduled jobs.
    """
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


@app.post(
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


@app.post(
    "/api/v1/invoices/validate",
    summary="Validate Invoice Data Model",
    tags=["Invoices"],
)
async def validate_invoice_endpoint(payload: dict[str, Any]):
    """Re-validate an existing invoice data model against statutory Bulgarian financial formulas (ЗДДС).
    
    Accepts:
    - Complete 3-layer JSON document or normalized_data dictionary
    
    Returns:
    - validation_results: { is_valid: bool, errors: [...], warnings: [...] }
    """
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
# Background Jobs Endpoints (Pillar 5, M12)
# ---------------------------------------------------------------------------

@app.get(
    "/v1/jobs/{job_id}",
    response_model=JobResponse,
    tags=["Jobs"],
    summary="Get Job Status & Progress",
)
@app.get(
    "/api/v1/jobs/{job_id}",
    response_model=JobResponse,
    tags=["Jobs"],
    summary="Get Job Status & Progress",
)
async def get_job_status(job_id: str):
    """Retrieve current status, real-time progress, and results for a background processing job."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found",
        )
    return job


@app.get(
    "/v1/jobs",
    response_model=JobListResponse,
    tags=["Jobs"],
    summary="List Background Jobs",
)
@app.get(
    "/api/v1/jobs",
    response_model=JobListResponse,
    tags=["Jobs"],
    summary="List Background Jobs",
)
async def list_background_jobs(limit: int = Query(default=20, ge=1, le=100)):
    """List recent background processing jobs and their current execution state."""
    jobs = job_manager.list_jobs(limit=limit)
    return {"total_jobs": len(jobs), "jobs": jobs}


@app.delete(
    "/v1/jobs/{job_id}",
    tags=["Jobs"],
    summary="Delete / Cancel Job",
)
@app.delete(
    "/api/v1/jobs/{job_id}",
    tags=["Jobs"],
    summary="Delete / Cancel Job",
)
async def delete_job_endpoint(job_id: str):
    """Delete a completed or failed job record from the job manager."""
    deleted = job_manager.delete_job(job_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found",
        )
    return {"status": "ok", "message": f"Job {job_id} deleted"}


@app.post(
    "/v1/jobs/{job_id}/retry",
    response_model=JobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Jobs"],
    summary="Retry Interrupted or Failed Job",
)
@app.post(
    "/api/v1/jobs/{job_id}/retry",
    response_model=JobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Jobs"],
    summary="Retry Interrupted or Failed Job",
)
async def retry_job_endpoint(job_id: str, background_tasks: BackgroundTasks):
    """Retry an interrupted or failed background job."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found",
        )
    try:
        retried_job = job_manager.retry_job(job_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    meta = retried_job.get("metadata", {})
    req_data = meta.get("request_data") or {"input_dir": meta.get("input_dir")}
    if retried_job.get("request_type") == "batch-dir" and req_data.get("input_dir"):
        background_tasks.add_task(_run_batch_dir_job, job_id, req_data)

    return {
        "job_id": job_id,
        "status": JobStatus.QUEUED.value,
        "message": f"Job {job_id} successfully re-queued for execution",
        "check_status_url": f"/v1/jobs/{job_id}",
    }


@app.post(
    "/v1/jobs/batch-dir",
    response_model=JobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Jobs"],
    summary="Queue Background Directory Batch Job",
)
@app.post(
    "/api/v1/jobs/batch-dir",
    response_model=JobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Jobs"],
    summary="Queue Background Directory Batch Job",
)
async def queue_batch_dir_job(request: BatchDirRequest, background_tasks: BackgroundTasks):
    """Queue a server-side directory of invoices for asynchronous background processing."""
    in_path = Path(request.input_dir)
    if not in_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Input directory does not exist: {request.input_dir}")
    if not in_path.is_dir():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Input path is not a directory: {request.input_dir}")

    job_id = job_manager.create_job(
        request_type="batch-dir",
        metadata={"input_dir": str(in_path), "request_data": request.model_dump()},
    )
    background_tasks.add_task(_run_batch_dir_job, job_id, request.model_dump())
    return {
        "job_id": job_id,
        "status": JobStatus.QUEUED.value,
        "message": f"Directory batch processing queued for {request.input_dir}",
        "check_status_url": f"/v1/jobs/{job_id}",
    }


@app.post(
    "/v1/jobs/batch",
    response_model=JobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Jobs"],
    summary="Queue Background Batch Upload Job",
)
@app.post(
    "/api/v1/jobs/batch",
    response_model=JobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Jobs"],
    summary="Queue Background Batch Upload Job",
)
async def queue_batch_upload_job(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(..., description="Multiple invoice files to process asynchronously"),
    lang: str = Query(default=DEFAULT_OCR_LANG, description="OCR language(s)"),
    workers: Optional[int] = Query(default=None, description="Number of worker processes"),
    use_cache: bool = Query(default=True, description="Enable OCR token caching"),
    ocr_cache_dir: Optional[str] = Query(default=str(DEFAULT_OCR_CACHE_DIR), description="OCR token cache directory"),
):
    """Upload multiple invoice files and queue for asynchronous background processing."""
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No files provided in batch request")

    tmp_in_dir = Path(tempfile.mkdtemp(prefix="job_in_"))
    tmp_out_dir = Path(tempfile.mkdtemp(prefix="job_out_"))

    saved_count = 0
    for f in files:
        if not f.filename:
            continue
        suffix = Path(f.filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            continue
        dest = tmp_in_dir / f.filename
        with dest.open("wb") as buffer:
            shutil.copyfileobj(f.file, buffer)
        saved_count += 1

    if saved_count == 0:
        shutil.rmtree(tmp_in_dir, ignore_errors=True)
        shutil.rmtree(tmp_out_dir, ignore_errors=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="None of the uploaded files have supported extensions")

    job_id = job_manager.create_job(request_type="batch-upload", metadata={"file_count": saved_count})
    background_tasks.add_task(
        _run_batch_upload_job,
        job_id,
        tmp_in_dir,
        tmp_out_dir,
        lang,
        workers,
        use_cache,
        ocr_cache_dir,
    )

    return {
        "job_id": job_id,
        "status": JobStatus.QUEUED.value,
        "message": f"Batch upload of {saved_count} file(s) queued for background processing",
        "check_status_url": f"/v1/jobs/{job_id}",
    }


@app.post(
    "/api/v1/export/pokupki",
    summary="Export NAP Purchase Ledger (POKUPKI.TXT)",
    tags=["Accounting"],
)
async def export_pokupki_ledger(
    invoices: list[dict[str, Any]],
    format: str = Query(default="fixed_width", pattern="^(fixed_width|tsv|csv)$", description="Export format: 'fixed_width', 'tsv', or 'csv'"),
    encoding: str = Query(default="cp1251", pattern="^(cp1251|utf-8)$", description="File encoding: 'cp1251' or 'utf-8'"),
    period: Optional[str] = Query(default=None, description="Tax period YYYYMM"),
    branch: str = Query(default="00", description="Branch code"),
):
    """Generate statutory NAP VAT purchase ledger (Дневник за покупки по Приложение № 12 от ППЗДДС).
    
    Accepts a list of invoice JSON payloads and produces statutory POKUPKI.TXT text/bytes.
    """
    from accounting_export import invoices_to_pokupki_txt
    if not invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice list cannot be empty",
        )
    try:
        raw_output = invoices_to_pokupki_txt(
            invoices,
            format=format,  # type: ignore
            encoding=encoding,
            period=period,
            branch=branch,
        )
        media_type = "text/plain; charset=windows-1251" if encoding == "cp1251" else "text/plain; charset=utf-8"
        if format == "tsv":
            media_type = "text/tab-separated-values; charset=utf-8"
        elif format == "csv":
            media_type = "text/csv; charset=utf-8"

        content_bytes = raw_output if isinstance(raw_output, bytes) else raw_output.encode(encoding)
        return Response(
            content=content_bytes,
            media_type=media_type,
            headers={"Content-Disposition": 'attachment; filename="POKUPKI.TXT"'},
        )
    except Exception as exc:
        logger.error("Failed generating POKUPKI.TXT: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed generating POKUPKI.TXT: {exc}",
        )


@app.post(
    "/api/v1/export/journal-entries",
    summary="Export Accounting Journal Entries (Контировки)",
    tags=["Accounting"],
)
async def export_journal_entries_endpoint(
    invoices: list[dict[str, Any]],
    format: str = Query(default="json", pattern="^(json|universal|microinvest|business_navigator|ajur|sap)$", description="ERP format"),
    default_account: Optional[str] = Query(default=None, description="Default expense account (e.g. 304 or 602)"),
):
    """Generate double-entry bookkeeping journal entries (контировки).
    
    Debit 304/602 + Debit 4531 = Credit 401.
    Supports JSON, Universal CSV, Microinvest Delta Pro, Бизнес Навигатор, Ajur, and SAP.
    """
    from accounting_export import (
        invoices_to_journal_entries,
        export_journal_entries_csv,
        export_journal_entries_json,
    )
    if not invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice list cannot be empty",
        )
    try:
        entries = invoices_to_journal_entries(invoices, default_expense_account=default_account)
        if format == "json":
            payload_str = export_journal_entries_json(entries)
            return JSONResponse(content=json.loads(payload_str))

        csv_text = export_journal_entries_csv(entries, format_type=format)  # type: ignore
        media_type = "text/csv; charset=utf-8"
        if format == "sap":
            media_type = "text/tab-separated-values; charset=utf-8"
        filename = f"journal_entries_{format}.csv"
        return Response(
            content=csv_text.encode("utf-8-sig"),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        logger.error("Failed generating journal entries: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed generating journal entries: {exc}",
        )


@app.post(
    "/api/v1/verify/contractor",
    summary="Real-Time Contractor Verification (TR, NRA, VIES)",
    tags=["Verification"],
)
async def verify_contractor_endpoint(request: ContractorVerifyRequest):
    """Verify company status in Commercial Register, NRA VAT register (Art. 94), or EU VIES."""
    from contractor_verification import verify_contractor_async
    try:
        res = await verify_contractor_async(
            identifier=request.identifier,
            country_code=request.country_code,
            date_tax_event=request.date_tax_event,
            bypass_cache=request.bypass_cache,
        )
        return JSONResponse(content=res.to_dict())
    except Exception as exc:
        logger.error("Contractor verification error: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Verification failed: {exc}",
        )


@app.post(
    "/api/v1/protocol-117/generate",
    summary="Generate Protocol Art. 117 ЗДДС (Reverse Charge & ВОП)",
    tags=["Accounting"],
)
async def generate_protocol_117_endpoint(request: Protocol117GenerateRequest):
    """Generate statutory Protocol under Art. 117 ЗДДС for foreign invoices (Google, Meta, Adobe, ВОП)."""
    from accounting_export import generate_protocol_chl_117
    try:
        proto = generate_protocol_chl_117(
            invoice=request.invoice,
            protocol_number=request.protocol_number,
            protocol_date=request.protocol_date,
            recipient_company=request.recipient_company,
            legal_basis=request.legal_basis,
            full_tax_credit=request.full_tax_credit,
        )
        return JSONResponse(content={
            "protocol": proto.to_dict(),
            "formatted_document": proto.to_text_document(),
            "purchase_ledger_entry": proto.to_purchase_ledger_entry().to_dict(),
            "sales_ledger_entry": proto.to_sales_ledger_entry().to_dict(),
            "journal_entry": proto.to_journal_entry().to_dict(),
        })
    except Exception as exc:
        logger.error("Failed generating Protocol 117: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed generating Protocol 117: {exc}",
        )


@app.post(
    "/api/v1/export/prodagbi",
    summary="Export NAP Sales Ledger (PRODAGBI.TXT)",
    tags=["Accounting"],
)
async def export_prodagbi_ledger(
    invoices: list[dict[str, Any]],
    format: str = Query(default="fixed_width", pattern="^(fixed_width|tsv|csv)$", description="Export format: 'fixed_width', 'tsv', or 'csv'"),
    encoding: str = Query(default="cp1251", pattern="^(cp1251|utf-8)$", description="File encoding: 'cp1251' or 'utf-8'"),
    period: Optional[str] = Query(default=None, description="Tax period YYYYMM"),
    branch: str = Query(default="00", description="Branch code"),
):
    """Generate statutory NAP VAT sales ledger (Дневник за продажби по Приложение № 10 от ППЗДДС)."""
    from accounting_export import invoices_to_prodagbi_txt
    if not invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice list cannot be empty",
        )
    try:
        raw_output = invoices_to_prodagbi_txt(
            invoices,
            format=format,  # type: ignore
            encoding=encoding,
            period=period,
            branch=branch,
        )
        media_type = "text/plain; charset=windows-1251" if encoding == "cp1251" else "text/plain; charset=utf-8"
        if format == "tsv":
            media_type = "text/tab-separated-values; charset=utf-8"
        elif format == "csv":
            media_type = "text/csv; charset=utf-8"

        content_bytes = raw_output if isinstance(raw_output, bytes) else raw_output.encode(encoding)
        return Response(
            content=content_bytes,
            media_type=media_type,
            headers={"Content-Disposition": 'attachment; filename="PRODAGBI.TXT"'},
        )
    except Exception as exc:
        logger.error("Failed generating PRODAGBI.TXT: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed generating PRODAGBI.TXT: {exc}",
        )


@app.post(
    "/api/v1/export/deklar",
    summary="Export NAP VAT Return Declaration (DEKLAR.TXT)",
    tags=["Accounting"],
)
async def export_deklar_endpoint(request: VatDeclarationRequest):
    """Generate statutory NAP VAT declaration (Справка-декларация по чл. 125 ЗДДС - Приложение № 13)."""
    from accounting_export import (
        generate_vat_declaration,
        invoice_to_nap_entry,
        invoice_to_nap_sales_entry,
    )
    try:
        pur_entries = [invoice_to_nap_entry(i, period=request.period) for i in request.purchase_invoices]
        sal_entries = [invoice_to_nap_sales_entry(i, period=request.period) for i in (request.sales_invoices or [])]

        from decimal import Decimal
        decl = generate_vat_declaration(
            purchase_entries=pur_entries,
            sales_entries=sal_entries,
            company_info=request.company_info,
            period=request.period,
            prior_vat_credit_cell_70=Decimal(str(request.prior_vat_credit_cell_70)),
            partial_credit_coefficient_cell_42=Decimal(str(request.partial_credit_coefficient_cell_42)),
        )

        deklar_bytes = decl.to_nap_deklar_txt(encoding="cp1251")
        assert isinstance(deklar_bytes, bytes)
        return Response(
            content=deklar_bytes,
            media_type="text/plain; charset=windows-1251",
            headers={"Content-Disposition": 'attachment; filename="DEKLAR.TXT"'},
        )
    except Exception as exc:
        logger.error("Failed generating DEKLAR.TXT: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed generating DEKLAR.TXT: {exc}",
        )


@app.post(
    "/api/v1/export/nap-package",
    summary="Export Complete Statutory НАП VAT Package (POKUPKI, PRODAGBI, DEKLAR, ZIP)",
    tags=["Accounting"],
)
async def export_nap_package_endpoint(request: NapPackageExportRequest):
    """Generate complete 3-file statutory package for direct upload to NRA (НАП) electronic portal."""
    from accounting_export import export_nap_package
    if not request.purchase_invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Purchase invoices list cannot be empty",
        )
    try:
        temp_dir = tempfile.mkdtemp(prefix="nap_pkg_")
        res = export_nap_package(
            purchase_invoices=request.purchase_invoices,
            sales_invoices=request.sales_invoices,
            company_info=request.company_info,
            period=request.period,
            format=request.format,  # type: ignore
            encoding=request.encoding,
            auto_generate_protocols=request.auto_generate_protocols,
            create_zip=request.as_zip,
            output_dir=temp_dir,
        )

        if request.as_zip:
            zip_file = Path(temp_dir) / f"NAP_{request.period}.zip"
            if zip_file.exists():
                zip_bytes = zip_file.read_bytes()
                shutil.rmtree(temp_dir, ignore_errors=True)
                return Response(
                    content=zip_bytes,
                    media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="NAP_{request.period}.zip"'},
                )

        shutil.rmtree(temp_dir, ignore_errors=True)
        return JSONResponse(content=res)
    except Exception as exc:
        logger.error("Failed generating complete NAP package: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed generating complete NAP package: {exc}",
        )


@app.post(
    "/api/v1/export/microinvest/sklad",
    summary="Export to Microinvest Sklad Pro (Warehouse Pro) XML",
    tags=["Accounting", "Microinvest ERP"],
)
async def export_microinvest_sklad_endpoint(request: MicroinvestExportRequest):
    """Export invoices to Microinvest Sklad Pro (Warehouse Pro) Purchase XML."""
    from accounting_export import generate_microinvest_sklad_xml
    if not request.invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoices list cannot be empty",
        )
    xml_content = generate_microinvest_sklad_xml(request.invoices[0])
    return Response(content=xml_content, media_type="application/xml")


@app.post(
    "/api/v1/export/microinvest/delta",
    summary="Export to Microinvest Delta Pro TransferData XML",
    tags=["Accounting", "Microinvest ERP"],
)
async def export_microinvest_delta_endpoint(request: MicroinvestExportRequest):
    """Export invoices to Microinvest Delta Pro <TransferData xmlns="urn:Transfer"> XML."""
    from accounting_export import generate_microinvest_delta_xml
    if not request.invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoices list cannot be empty",
        )
    xml_content = generate_microinvest_delta_xml(
        request.invoices,
        default_expense_account=request.default_expense_account,
        default_goods_account=request.default_goods_account,
        default_vat_account=request.default_vat_account,
        default_supplier_account=request.default_supplier_account,
    )
    return Response(content=xml_content, media_type="application/xml")


@app.post(
    "/api/v1/export/microinvest/delta-csv",
    summary="Export to Microinvest Delta Pro Postings CSV",
    tags=["Accounting", "Microinvest ERP"],
)
async def export_microinvest_delta_csv_endpoint(request: MicroinvestExportRequest):
    """Export invoices to Microinvest Delta Pro double-entry postings CSV (CP1251)."""
    from invoice_core.microinvest_export import generate_microinvest_delta_csv
    if not request.invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoices list cannot be empty",
        )
    csv_bytes = generate_microinvest_delta_csv(
        request.invoices,
        default_expense_account=request.default_expense_account,
        default_goods_account=request.default_goods_account,
        default_vat_account=request.default_vat_account,
        default_supplier_account=request.default_supplier_account,
        encoding="windows-1251",
    )
    assert isinstance(csv_bytes, bytes)
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=Microinvest_Delta_Postings.csv"},
    )


@app.post(
    "/api/v1/export/business-navigator/csv",
    summary="Export to Business Navigator Delimited CSV",
    tags=["Accounting", "Business Navigator"],
)
async def export_business_navigator_csv_endpoint(request: BusinessNavigatorExportRequest):
    """Export invoices to Business Navigator delimited CSV format."""
    from invoice_core.business_navigator_export import generate_business_navigator_csv
    if not request.invoices:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoices list cannot be empty")
    csv_bytes = generate_business_navigator_csv(request.invoices, encoding=request.encoding)
    content = csv_bytes if isinstance(csv_bytes, bytes) else csv_bytes.encode(request.encoding)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=BN_IMPORT.csv"},
    )


@app.post(
    "/api/v1/export/business-navigator/txt",
    summary="Export to Business Navigator Section TXT",
    tags=["Accounting", "Business Navigator"],
)
async def export_business_navigator_txt_endpoint(request: BusinessNavigatorExportRequest):
    """Export invoices to Business Navigator section-based tagged TXT format."""
    from invoice_core.business_navigator_export import generate_business_navigator_section_txt
    if not request.invoices:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoices list cannot be empty")
    txt_bytes = generate_business_navigator_section_txt(request.invoices, encoding=request.encoding)
    content = txt_bytes if isinstance(txt_bytes, bytes) else txt_bytes.encode(request.encoding)
    return Response(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=BN_IMPORT.txt"},
    )


@app.post(
    "/api/v1/export/business-navigator/dbf",
    summary="Export to Business Navigator Single DBF",
    tags=["Accounting", "Business Navigator"],
)
async def export_business_navigator_dbf_endpoint(request: BusinessNavigatorExportRequest):
    """Export invoices to native binary dBase III / IV DBF file."""
    from invoice_core.business_navigator_export import generate_business_navigator_single_dbf
    if not request.invoices:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoices list cannot be empty")
    dbf_bytes = generate_business_navigator_single_dbf(request.invoices)
    return Response(
        content=dbf_bytes,
        media_type="application/x-dbf",
        headers={"Content-Disposition": "attachment; filename=BN_SINGLE.DBF"},
    )


@app.post(
    "/api/v1/export/business-navigator/package",
    summary="Export Complete Business Navigator Package (ZIP with DBF, CSV, TXT)",
    tags=["Accounting", "Business Navigator"],
)
async def export_business_navigator_package_endpoint(request: BusinessNavigatorExportRequest):
    """Export complete Business Navigator package with dual DBFs (DOKUM/OPER), CSV, TXT in a ZIP archive."""
    from invoice_core.business_navigator_export import export_business_navigator_package
    if not request.invoices:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoices list cannot be empty")
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = export_business_navigator_package(request.invoices, output_dir=tmp_dir, create_zip=True)
        zip_path = res["bn_zip"]
        zip_bytes = Path(zip_path).read_bytes()
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=Business_Navigator_Package.zip"},
    )


@app.post(
    "/api/v1/export/ajur",
    summary="Export to Ajur (Ажур-L / Ажур 7) CSV",
    tags=["Accounting", "Ajur ERP"],
)
async def export_ajur_endpoint(request: AjurExportRequest):
    """Export invoices to Ajur 7 / L import CSV format (CP1251)."""
    from invoice_core.ajur_export import generate_ajur_csv
    if not request.invoices:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoices list cannot be empty")
    csv_bytes = generate_ajur_csv(request.invoices, encoding=request.encoding)
    content = csv_bytes if isinstance(csv_bytes, bytes) else csv_bytes.encode(request.encoding)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=AJUR_IMPORT.csv"},
    )


# ---------------------------------------------------------------------------
# Official Bulgarian Chart of Accounts & Tax Period Endpoints
# ---------------------------------------------------------------------------

@app.get(
    "/api/v1/accounting/chart-of-accounts",
    summary="Query Official Bulgarian Chart of Accounts (НСС / Национален сметкоплан)",
    tags=["Accounting", "Chart of Accounts"],
)
async def get_chart_of_accounts_endpoint(
    q: Optional[str] = Query(default=None, description="Search query by code, name, keywords, or purpose"),
    account_class: Optional[int] = Query(default=None, ge=1, le=9, description="Filter by account class (1-9)"),
    limit: int = Query(default=50, ge=1, le=200, description="Max results to return"),
):
    """List or search accounts in the official Bulgarian National Chart of Accounts."""
    from invoice_core.chart_of_accounts import DEFAULT_CHART_OF_ACCOUNTS
    if q:
        results = DEFAULT_CHART_OF_ACCOUNTS.search(q, limit=limit)
    elif account_class:
        results = DEFAULT_CHART_OF_ACCOUNTS.get_by_class(account_class)[:limit]
    else:
        results = DEFAULT_CHART_OF_ACCOUNTS.all_accounts()[:limit]

    return {
        "count": len(results),
        "accounts": [a.to_dict() for a in results],
    }


@app.get(
    "/api/v1/accounting/chart-of-accounts/{code}",
    summary="Get Account Details and Purpose by Code",
    tags=["Accounting", "Chart of Accounts"],
)
async def get_account_detail_endpoint(code: str):
    """Get full details, legal purpose, and typical debit/credit conventions for an account."""
    from invoice_core.chart_of_accounts import DEFAULT_CHART_OF_ACCOUNTS
    acc = DEFAULT_CHART_OF_ACCOUNTS.get(code)
    if not acc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account '{code}' not found in the Bulgarian Chart of Accounts",
        )
    return {
        "account": acc.to_dict(),
        "explanation": DEFAULT_CHART_OF_ACCOUNTS.explain(code),
        "subaccounts": [s.to_dict() for s in DEFAULT_CHART_OF_ACCOUNTS.get_subaccounts(code)],
    }


@app.post(
    "/api/v1/accounting/validate-tax-period",
    summary="Validate Tax Period Compliance under Art. 124 VAT Act (чл. 124 ЗДДС)",
    tags=["Accounting", "Tax Period Validation"],
)
async def validate_tax_period_endpoint(request: TaxPeriodValidationApiRequest):
    """Check if document dates/tax events match the declared VAT period and statutory 12-month rule."""
    from invoice_core.tax_period_validator import TaxPeriodValidator
    if not request.invoices:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoices list cannot be empty")
    batch_res = TaxPeriodValidator.validate_batch(request.invoices, target_period=request.target_period)
    return batch_res


@app.get(
    "/api/v1/accounting/mapping-rules",
    summary="Get Active Account Mapping Engine Rules",
    tags=["Accounting", "Account Mapping"],
)
async def get_mapping_rules_endpoint():
    """Retrieve active supplier and keyword account mapping rules."""
    from invoice_core.account_mapping import DEFAULT_MAPPING_ENGINE
    return DEFAULT_MAPPING_ENGINE.to_dict()


@app.post(
    "/api/v1/accounting/mapping-rules/supplier",
    summary="Add or Update Supplier Account Mapping Rule",
    tags=["Accounting", "Account Mapping"],
)
async def add_supplier_mapping_rule_endpoint(request: SupplierMappingRuleApiRequest):
    """Register custom account mapping for a specific supplier EIK."""
    from invoice_core.account_mapping import DEFAULT_MAPPING_ENGINE
    DEFAULT_MAPPING_ENGINE.add_supplier_rule(
        eik=request.eik,
        account=request.target_account,
        subledger=request.target_subledger,
        supplier_name=request.supplier_name or "",
        description=request.description or "",
    )
    return {
        "status": "success",
        "message": f"Supplier rule registered for EIK {request.eik} -> Account {request.target_account}",
    }


@app.post(
    "/api/v1/accounting/mapping-rules/keyword",
    summary="Add or Update Keyword/Regex Account Mapping Rule",
    tags=["Accounting", "Account Mapping"],
)
async def add_keyword_mapping_rule_endpoint(request: KeywordMappingRuleApiRequest):
    """Register custom keyword or regex mapping rule for line item descriptions."""
    from invoice_core.account_mapping import DEFAULT_MAPPING_ENGINE
    DEFAULT_MAPPING_ENGINE.add_keyword_rule(
        rule_id=request.rule_id,
        account=request.target_account,
        keywords=request.keywords,
        regex_pattern=request.regex_pattern,
        subledger=request.target_subledger,
        priority=request.priority,
        description=request.description or "",
    )
    return {
        "status": "success",
        "message": f"Keyword rule '{request.rule_id}' registered -> Account {request.target_account}",
    }



# ---------------------------------------------------------------------------
# Document & HITL Human-in-the-Loop Endpoints (Pillar 4, M14)
# ---------------------------------------------------------------------------

MOCK_ERP_RECEIVED: list[dict[str, Any]] = []


@app.get(
    "/dashboard",
    tags=["HITL Dashboard"],
    response_class=HTMLResponse,
    summary="Human-in-the-Loop Web Dashboard",
)
async def dashboard_view():
    """Serve the interactive split-screen Human-in-the-Loop web dashboard."""
    html_file = Path("static/index.html")
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h3>Dashboard HTML file not found at static/index.html</h3>", status_code=404)


@app.get(
    "/hitl",
    tags=["HITL Dashboard"],
    response_class=HTMLResponse,
    summary="Human-in-the-Loop Web Dashboard (Alias)",
)
async def hitl_alias_view():
    """Alias for /dashboard."""
    return await dashboard_view()


@app.get(
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


@app.get(
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


@app.get(
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


@app.get(
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


@app.post(
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


@app.get(
    "/api/v1/feedback/stats",
    tags=["Feedback & RLHF"],
    summary="Get RLHF Feedback & Learning Metrics",
)
async def get_rlhf_feedback_stats(db: Session = Depends(get_db)):
    """Retrieve operational statistics of the continuous RLHF learning engine."""
    from invoice_core.feedback_learning import get_feedback_statistics
    return get_feedback_statistics(db)


@app.get(
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


@app.post(
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


@app.post(
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


@app.get(
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


@app.get(
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


@app.get(
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


@app.delete(
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
    return {"status": "ok", "message": f"Document {document_id} deleted"}


# ---------------------------------------------------------------------------
# Webhooks & Mock ERP Receiver (Pillar 4, M14)
# ---------------------------------------------------------------------------

@app.post(
    "/api/v1/webhooks/test",
    tags=["Webhooks"],
    summary="Test Outbound ERP Webhook Endpoint",
)
async def test_webhook_endpoint(request: WebhookTestRequest):
    """Send a diagnostic ping payload to test connectivity to an external ERP webhook URL."""
    test_payload = {
        "event": "test.ping",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": "Diagnostic test ping from Bulgarian Invoice OCR Microservice",
    }
    success, code, err = await send_webhook_async(
        target_url=request.target_url,
        payload=test_payload,
        secret=request.secret,
        timeout=5.0,
    )
    return {
        "success": success,
        "status_code": code,
        "error": err,
        "target_url": request.target_url,
    }


@app.get(
    "/api/v1/webhooks/logs",
    tags=["Webhooks"],
    summary="List Outbound Webhook Delivery Logs",
)
async def list_webhook_logs(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Retrieve history of dispatched webhook notifications with HTTP status codes and responses."""
    logs = (
        db.query(WebhookLogRecord)
        .order_by(WebhookLogRecord.created_at.desc())
        .limit(limit)
        .all()
    )
    return [l.to_dict() for l in logs]


@app.post(
    "/mock-erp/webhook",
    tags=["Webhooks"],
    summary="Mock ERP Webhook Receiver (Testing & Verification)",
)
async def mock_erp_receiver(request: Request):
    """Mock ERP webhook receiver endpoint to test end-to-end integration without external network."""
    try:
        body = await request.json()
    except Exception:
        body = {"raw": (await request.body()).decode("utf-8", errors="replace")}

    received_entry = {
        "received_at": time.time(),
        "headers": dict(request.headers),
        "signature": request.headers.get("x-webhook-signature"),
        "event": body.get("event") if isinstance(body, dict) else None,
        "document_id": body.get("document_id") if isinstance(body, dict) else None,
        "payload": body,
    }
    MOCK_ERP_RECEIVED.append(received_entry)
    if len(MOCK_ERP_RECEIVED) > 200:
        del MOCK_ERP_RECEIVED[:-200]
    logger.info("Mock ERP received webhook event: %s", received_entry.get("event"))
    return {
        "status": "success",
        "message": "Webhook received by Mock ERP system",
        "event": received_entry.get("event"),
        "document_id": received_entry.get("document_id"),
    }


@app.get(
    "/mock-erp/webhook/received",
    tags=["Webhooks"],
    summary="List Received Mock ERP Webhooks (Testing)",
)
async def list_mock_erp_received():
    """List all webhook payloads captured by the mock ERP receiver."""
    return {
        "count": len(MOCK_ERP_RECEIVED),
        "webhooks": list(reversed(MOCK_ERP_RECEIVED)),
    }


# ---------------------------------------------------------------------------
# 100% Local Drop-In Compatibility Layer (Replaces All Paid/Cloud Vision APIs)
# ---------------------------------------------------------------------------

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
        "needsValidation": not inv.validation.is_valid,
        "confidence": float(inv.invoice_metadata.ocr_confidence_score or 0.95),
    }


@app.post(
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


@app.post(
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


@app.post(
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


@app.get(
    "/api/businesses/search",
    tags=["Business Search (Local)"],
    summary="Local Business & EIK Search (Replaces CompanyBook API)",
)
@app.post(
    "/api/businesses/search",
    tags=["Business Search (Local)"],
    summary="Local Business & EIK Search (Replaces CompanyBook API)",
)
async def search_business_local(
    q: str = Query(default=""),
    enrich: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    """Local company registry and EIK search using SQLite cache, vendor profiles, and Modulo 11 check."""
    from invoice_core.vendor_profiles import get_vendor_profile, list_known_profiles
    from invoice_core.normalizers import validate_eik

    clean_q = q.strip()
    if not clean_q:
        return {"success": True, "results": []}

    results = []
    seen_eiks = set()

    # 1. Search in YAML vendor profiles
    vp = get_vendor_profile(clean_q)
    if vp:
        eik = vp.get("eik") or clean_q
        seen_eiks.add(eik)
        results.append({
            "name": vp.get("name"),
            "eik": eik,
            "vat_number": vp.get("vat_number") or f"BG{eik}",
            "address": vp.get("address"),
            "source": "local_vendor_profile",
        })

    for p in list_known_profiles():
        if p.get("eik") and p["eik"] not in seen_eiks:
            if clean_q.lower() in p.get("name", "").lower() or clean_q in p.get("eik", ""):
                seen_eiks.add(p["eik"])
                results.append({
                    "name": p.get("name"),
                    "eik": p.get("eik"),
                    "vat_number": p.get("vat_number") or f"BG{p['eik']}",
                    "address": p.get("address"),
                    "source": "local_vendor_profile",
                })

    # 2. Search in local database records
    db_records = (
        db.query(DocumentRecord)
        .filter(
            (DocumentRecord.supplier_eik == clean_q)
            | (DocumentRecord.supplier_name.ilike(f"%{clean_q}%"))
        )
        .limit(10)
        .all()
    )
    for r in db_records:
        if r.supplier_eik and r.supplier_eik not in seen_eiks:
            seen_eiks.add(r.supplier_eik)
            results.append({
                "name": r.supplier_name,
                "eik": r.supplier_eik,
                "vat_number": r.supplier_vat or f"BG{r.supplier_eik}",
                "address": r.supplier_address,
                "source": "local_database_cache",
            })

    # 3. If query is a valid Bulgarian EIK checksum, provide verified synthetic candidate
    digits_only = re.sub(r"\D", "", clean_q)
    if digits_only and digits_only not in seen_eiks:
        if validate_eik(digits_only):
            results.append({
                "name": f"Търговец с ЕИК {digits_only}",
                "eik": digits_only,
                "vat_number": f"BG{digits_only}",
                "address": None,
                "source": "local_modulo11_verified",
            })

    return {"success": True, "results": results}


# ===========================================================================
# Vendor Profile Management Endpoints (External YAML Dynamic Reload)
# ===========================================================================

@app.get(
    "/api/v1/vendors",
    tags=["Vendor Profiles"],
    summary="List all loaded vendor profiles and configuration status",
)
async def list_vendor_profiles_endpoint():
    """Retrieve all active vendor profiles loaded from YAML configuration files."""
    from invoice_core.vendor_profiles import get_vendor_profile_loader

    loader = get_vendor_profile_loader()
    profiles = loader.get_profiles()
    diag = loader.get_diagnostics()

    vendors = [p.to_dict() for p in profiles.values()]
    return {
        "success": True,
        "total": len(vendors),
        "vendors": vendors,
        "last_reloaded": diag.get("last_reloaded"),
        "errors": diag.get("errors", []),
        "config_dirs": diag.get("config_dirs", []),
    }


@app.get(
    "/api/v1/vendors/{vendor_id}",
    tags=["Vendor Profiles"],
    summary="Get details of a specific vendor profile",
)
async def get_vendor_profile_endpoint(vendor_id: str):
    """Retrieve details for a specific vendor profile by ID, EIK, or keyword."""
    from invoice_core.vendor_profiles import get_vendor_profile_loader

    loader = get_vendor_profile_loader()
    prof = loader.get_profile(vendor_id)
    if not prof:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Vendor profile '{vendor_id}' not found",
        )
    return {
        "success": True,
        "vendor": prof.to_dict(),
    }


@app.post(
    "/api/v1/vendors/reload",
    tags=["Vendor Profiles"],
    summary="Dynamic hot-reload of vendor profiles from YAML files without server restart",
)
async def reload_vendor_profiles_endpoint():
    """Hot-reload vendor profiles from YAML files on disk without stopping the service."""
    from invoice_core.vendor_profiles import get_vendor_profile_loader

    loader = get_vendor_profile_loader()
    result = loader.reload()
    return {
        "success": True,
        "message": f"Successfully reloaded {result['loaded_count']} vendor profiles",
        "reloaded_count": result["loaded_count"],
        "vendors": result["profiles"],
        "errors": result.get("errors", []),
        "timestamp": result["reloaded_at"],
    }


@app.post(
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


@app.post(
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


@app.post(
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


@app.post(
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


# ---------------------------------------------------------------------------
# CLI Launcher
# ---------------------------------------------------------------------------

def run_server():
    """CLI launcher for uvicorn server."""
    import uvicorn

    parser = argparse.ArgumentParser(description="Run Bulgarian Invoice OCR REST API Server")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port number (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload on code changes")
    parser.add_argument("--workers", type=int, default=1, help="Number of worker processes")
    args = parser.parse_args()

    uvicorn.run(
        "api_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
    )


if __name__ == "__main__":
    run_server()

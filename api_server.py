"""FastAPI REST API Microservice for Bulgarian Invoice OCR & Document Understanding.

Provides HTTP REST endpoints for seamless integration with ERP, accounting,
and document management systems:
- GET  /health                      - Microservice and OCR engine health status
- GET  /api/v1/languages            - Installed and available Tesseract languages
- POST /api/v1/invoices/process     - Upload and process a single invoice (PDF / image)
- POST /api/v1/invoices/batch       - Upload and process multiple invoice files
- POST /api/v1/invoices/batch-dir   - Batch process a server-side directory of invoices
- POST /api/v1/invoices/validate    - Re-validate an existing invoice JSON payload
"""

import argparse
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
from pathlib import Path
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import pytesseract
from sqlalchemy.orm import Session

from database import (
    AuditTrailRecord,
    DocumentRecord,
    PersistentJobRecord,
    WebhookLogRecord,
    add_audit_entry,
    approve_document_in_db,
    get_db,
    get_db_session,
    init_db,
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

logger = logging.getLogger("invoice_ocr_api")
SERVICE_START_TIME = time.time()
API_VERSION = "1.0.0"

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
    try:
        tess_ver = pytesseract.get_tesseract_version()
        ready, available, missing = verify_tesseract_languages(["bul", "eng"])
        logger.info(
            "Tesseract ready (v%s). Languages: available=%s, missing=%s",
            tess_ver, available, missing,
        )
    except Exception as exc:
        logger.warning("Tesseract startup check warning: %s", exc)
    yield


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
AUTH_EXEMPT_PREFIXES = ("/static/",)


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


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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


# ---------------------------------------------------------------------------
# Background Task Queue & Persistent Job Manager (Pillar 4 & 5, M12/M14)
# ---------------------------------------------------------------------------

class ApproveDocumentRequest(BaseModel):
    actor: str = Field(default="accountant", description="Name or identifier of accountant approving document")
    webhook_url: Optional[str] = Field(default=None, description="Optional ERP webhook URL to notify")


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
                    for r in records:
                        self._jobs[r.job_id] = r.to_dict()
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
        # Run CPU-intensive OCR pipeline in a threadpool to avoid blocking the event loop
        invoice = await run_in_threadpool(
            process_invoice,
            tmp_path,
            debug_dir=dbg_path,
            lang=lang,
        )
        proc_time = time.perf_counter() - start_time

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
        return {
            "is_valid": val_res.is_valid,
            "errors": [asdict(e) for e in val_res.errors],
            "warnings": [asdict(w) for w in val_res.warnings],
        }
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

    job_id = job_manager.create_job(request_type="batch-dir", metadata={"input_dir": str(in_path)})
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
    summary="Save Manual Field Corrections (HITL)",
)
async def correct_document_fields(
    document_id: str,
    corrections: dict[str, Any],
    actor: str = Query(default="accountant", description="Accountant identifier"),
    db: Session = Depends(get_db),
):
    """Apply manual corrections made by accountant and log changes into the immutable audit trail."""
    record = update_document_corrections(
        db=db,
        doc_id=document_id,
        corrections=corrections,
        actor=actor,
    )
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{document_id}' not found",
        )
    return record.to_dict()


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

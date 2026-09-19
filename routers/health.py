"""FastAPI Router for Health, Readiness, Metrics, and HITL Web Dashboard."""

import os
from pathlib import Path
import resource
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import pytesseract

from invoice_ocr import verify_tesseract_languages
from routers.common import API_VERSION, SERVICE_START_TIME

router = APIRouter()


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


class TesseractHealth(BaseModel):
    ready: bool
    version: Optional[str] = None
    available_languages: list[str] = []
    missing_required_languages: list[str] = []
    required_ready: bool = False


class DatabaseHealth(BaseModel):
    connected: bool
    status: str
    tables: dict[str, bool]
    audit_table_ready: bool
    document_count: int = 0
    audit_trail_count: int = 0


class WorkerPoolHealth(BaseModel):
    ready: bool
    status: str
    max_workers: int = 0
    is_initialized: bool = False


class VendorProfilesHealth(BaseModel):
    available: bool
    path: str
    accessible: bool
    vendor_count: int = 0
    vendors: list[str] = []


class MicroinvestHealth(BaseModel):
    module_ready: bool
    status: str
    delta_pro_available: bool
    sklad_pro_available: bool
    formats: list[str] = []


class EcosystemHealthResponse(BaseModel):
    status: str = Field(..., json_schema_extra={"example": "healthy"})
    timestamp: str = Field(..., json_schema_extra={"example": "2026-09-19T10:30:00Z"})
    version: str = Field(..., json_schema_extra={"example": "1.0.0"})
    uptime_seconds: float = Field(..., json_schema_extra={"example": 123.45})
    tesseract: TesseractHealth
    database: DatabaseHealth
    worker_pool: WorkerPoolHealth
    vendor_profiles: VendorProfilesHealth
    microinvest_export: MicroinvestHealth


@router.get(
    "/dashboard",
    tags=["HITL Dashboard"],
    response_class=HTMLResponse,
    summary="Human-in-the-Loop Web Dashboard",
)
@router.head("/dashboard", include_in_schema=False)
async def dashboard_view():
    """Serve the interactive split-screen Human-in-the-Loop web dashboard."""
    html_file = Path("static/index.html")
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h3>Dashboard HTML file not found at static/index.html</h3>", status_code=404)


@router.get(
    "/hitl",
    tags=["HITL Dashboard"],
    response_class=HTMLResponse,
    summary="Human-in-the-Loop Web Dashboard (Alias)",
)
@router.head("/hitl", include_in_schema=False)
async def hitl_alias_view():
    """Alias for /dashboard."""
    return await dashboard_view()


@router.get("/", summary="Root Web Dashboard / API Overview")
@router.head("/", include_in_schema=False)
async def root(request: Request):
    """Serve Dashboard for browser requests, or API overview JSON for API clients."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        return await dashboard_view()
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
            "ecosystem_health": "GET /api/v1/system/ecosystem-health",
            "metrics": "GET /metrics",
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


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health & Readiness Check",
    tags=["System"],
)
@router.head("/health", include_in_schema=False)
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


@router.get(
    "/metrics",
    summary="System & Execution Metrics",
    tags=["System"],
)
async def metrics():
    """Expose service execution, memory, and OCR engine metrics."""
    uptime = round(time.time() - SERVICE_START_TIME, 2)
    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # On macOS, ru_maxrss is in bytes; on Linux, in kilobytes
    max_rss_mb = round(max_rss / (1024 * 1024), 2) if os.uname().sysname == "Darwin" else round(max_rss / 1024, 2)

    try:
        tess_ver = str(pytesseract.get_tesseract_version())
        is_ready, available, _ = verify_tesseract_languages(["bul", "eng"])
    except Exception:
        tess_ver = None
        is_ready = False
        available = []

    return {
        "status": "ok",
        "version": API_VERSION,
        "uptime_seconds": uptime,
        "memory_max_rss_mb": max_rss_mb,
        "tesseract": {
            "version": tess_ver,
            "ready": is_ready,
            "languages_count": len(available),
        },
    }


@router.get(
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


@router.get(
    "/api/v1/system/ecosystem-health",
    response_model=EcosystemHealthResponse,
    summary="Microinvest Ecosystem Diagnostic Status",
    tags=["System"],
)
@router.head("/api/v1/system/ecosystem-health", include_in_schema=False)
async def ecosystem_health():
    """Verify end-to-end ecosystem health: Tesseract OCR, DB & Audit tables, Worker Pool, Vendors config, and Microinvest Delta Pro export."""
    from datetime import datetime, timezone
    from sqlalchemy import inspect
    from database import get_db_session, DocumentRecord, AuditTrailRecord
    from invoice_ocr import get_ocr_pool

    # 1. Tesseract OCR readiness (version and bul/eng languages)
    try:
        tess_ver = str(pytesseract.get_tesseract_version())
        is_ready, available, missing = verify_tesseract_languages(["bul", "eng"])
    except Exception:
        tess_ver = None
        is_ready = False
        available = []
        missing = ["bul", "eng"]

    tess_health = TesseractHealth(
        ready=is_ready,
        version=tess_ver,
        available_languages=available,
        missing_required_languages=missing,
        required_ready=is_ready,
    )

    # 2. Database and audit tables state
    db_connected = False
    tables_status = {"documents": False, "audit_trail": False, "jobs": False}
    doc_count = 0
    audit_count = 0
    try:
        with get_db_session() as db:
            inspector = inspect(db.bind)
            existing_tables = inspector.get_table_names() if inspector else []
            tables_status["documents"] = "documents" in existing_tables
            tables_status["audit_trail"] = "audit_trail" in existing_tables
            tables_status["jobs"] = "jobs" in existing_tables
            db_connected = True
            doc_count = db.query(DocumentRecord).count() if tables_status["documents"] else 0
            audit_count = db.query(AuditTrailRecord).count() if tables_status["audit_trail"] else 0
    except Exception:
        pass

    db_health = DatabaseHealth(
        connected=db_connected,
        status="ok" if (db_connected and tables_status["audit_trail"]) else "degraded",
        tables=tables_status,
        audit_table_ready=tables_status["audit_trail"],
        document_count=doc_count,
        audit_trail_count=audit_count,
    )

    # 3. Worker Pool working state
    pool = get_ocr_pool()
    pool_health = WorkerPoolHealth(
        ready=pool is not None,
        status="active" if pool is not None else "uninitialized",
        max_workers=pool.max_workers if pool else 0,
        is_initialized=pool is not None,
    )

    # 4. Presence and accessibility of config/vendors/
    vendors_dir = Path("config/vendors")
    vendors_accessible = vendors_dir.exists() and vendors_dir.is_dir()
    vendor_files = []
    if vendors_accessible:
        try:
            vendor_files = [f.stem for f in vendors_dir.glob("*.yaml")] + [f.stem for f in vendors_dir.glob("*.yml")]
        except Exception:
            pass

    vendor_health = VendorProfilesHealth(
        available=vendors_accessible and len(vendor_files) > 0,
        path=str(vendors_dir),
        accessible=vendors_accessible,
        vendor_count=len(vendor_files),
        vendors=sorted(vendor_files),
    )

    # 5. Connectivity status with Microinvest Delta Pro export module
    try:
        from invoice_core.microinvest_export import (
            generate_microinvest_delta_xml,
            generate_microinvest_delta_csv,
            generate_microinvest_sklad_xml,
        )
        delta_pro_ready = callable(generate_microinvest_delta_xml) and callable(generate_microinvest_delta_csv)
        sklad_ready = callable(generate_microinvest_sklad_xml)
        microinvest_health = MicroinvestHealth(
            module_ready=delta_pro_ready and sklad_ready,
            status="ready" if (delta_pro_ready and sklad_ready) else "error",
            delta_pro_available=delta_pro_ready,
            sklad_pro_available=sklad_ready,
            formats=["delta_xml", "sklad_xml", "delta_csv", "kontirovki"],
        )
    except Exception as exc:
        microinvest_health = MicroinvestHealth(
            module_ready=False,
            status=f"error: {exc}",
            delta_pro_available=False,
            sklad_pro_available=False,
            formats=[],
        )

    all_ok = (
        is_ready
        and db_connected
        and tables_status["audit_trail"]
        and vendors_accessible
        and microinvest_health.module_ready
    )

    return EcosystemHealthResponse(
        status="healthy" if all_ok else "degraded",
        timestamp=datetime.now(timezone.utc).isoformat(),
        version=API_VERSION,
        uptime_seconds=round(time.time() - SERVICE_START_TIME, 2),
        tesseract=tess_health,
        database=db_health,
        worker_pool=pool_health,
        vendor_profiles=vendor_health,
        microinvest_export=microinvest_health,
    )


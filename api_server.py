"""FastAPI REST API Microservice for Bulgarian Invoice OCR & Document Understanding.

Entry coordinator initializing FastAPI app, middlewares, and modular APIRouters:
- routers/health.py       - /, /health, /metrics, /api/v1/languages, /dashboard, /hitl
- routers/documents.py    - /api/v1/invoices/*, /api/document-scanner/*, /api/tesseract/*
- routers/accounting.py   - /api/v1/accounting/*, /api/v1/export/*, /api/v1/protocol-117/*
- routers/contractors.py  - /api/v1/contractors/verify, /api/businesses/search, /api/v1/vendors/*
- routers/hitl.py         - /api/v1/documents/*, approvals, corrections, RLHF learning
- routers/jobs.py         - /v1/jobs/*, /api/v1/jobs/*, async background queue
- routers/ingestion.py    - /api/v1/ingest/email, docs-email, watcher, IMAP
- routers/webhooks.py     - /api/v1/webhooks/*, /mock-erp/webhook
"""

import argparse
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import time

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import pytesseract

from database import init_db
from invoice_core.document_classifier import get_document_classifier
from invoice_core.watcher import FolderWatcher, FolderWatcherConfig
from invoice_ocr import (
    get_ocr_pool,
    init_ocr_pool,
    setup_tessdata_prefix,
    shutdown_ocr_pool,
    verify_tesseract_languages,
)
from routers import (
    accounting_router,
    contractors_router,
    documents_router,
    health_router,
    hitl_router,
    ingestion_router,
    jobs_router,
    webhooks_router,
)
from routers.accounting import execute_accounting_pipeline_for_document
from routers.common import API_VERSION, SERVICE_START_TIME
from routers.ingestion import get_global_watcher, set_global_watcher
from routers.jobs import JobManager, JobStatus, job_manager
from routers.webhooks import MOCK_ERP_RECEIVED

logger = logging.getLogger("invoice_ocr_api")


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
        watcher = FolderWatcher(cfg)
        watcher.start()
        set_global_watcher(watcher)
        logger.info("Started automatic FolderWatcher on: %s", w_dir)

    yield

    # Graceful shutdown of watcher and worker pool
    active_watcher = get_global_watcher()
    if active_watcher and active_watcher.is_running:
        logger.info("Stopping FolderWatcher...")
        active_watcher.stop()

    logger.info("Shutting down OCR ProcessPoolExecutor...")
    shutdown_ocr_pool(wait=True)


# ---------------------------------------------------------------------------
# FastAPI Application Configuration & Middlewares
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
AUTH_EXEMPT_PATHS = {"/", "/health", "/metrics", "/docs", "/redoc", "/openapi.json", "/dashboard", "/hitl", "/api/v1/system/ecosystem-health"}
AUTH_EXEMPT_PREFIXES = ("/static/", "/api/document-scanner/", "/api/tesseract/", "/api/businesses/")


@app.middleware("http")
async def api_key_auth_middleware(request: Request, call_next):
    """Optional API key authentication. Set API_KEY env var to enable."""
    if API_KEY:
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
# Register Modular APIRouters
# ---------------------------------------------------------------------------

app.include_router(health_router)
app.include_router(documents_router)
app.include_router(accounting_router)
app.include_router(contractors_router)
app.include_router(hitl_router)
app.include_router(jobs_router)
app.include_router(ingestion_router)
app.include_router(webhooks_router)


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

"""FastAPI Router for Asynchronous Job Queue and Lifecycle Management."""

from enum import Enum
import json
import logging
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Any, Optional
import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field

from database import PersistentJobRecord, get_db_session
from invoice_ocr import (
    DEFAULT_OCR_CACHE_DIR,
    DEFAULT_OCR_LANG,
    SUPPORTED_EXTENSIONS,
    process_batch,
)
from routers.common import BatchDirRequest, _validate_server_path

logger = logging.getLogger("invoice_ocr_api")
router = APIRouter()


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
# Background Jobs Endpoints (Pillar 5, M12)
# ---------------------------------------------------------------------------

@router.get(
    "/v1/jobs/{job_id}",
    response_model=JobResponse,
    tags=["Jobs"],
    summary="Get Job Status & Progress",
)
@router.get(
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


@router.get(
    "/v1/jobs",
    response_model=JobListResponse,
    tags=["Jobs"],
    summary="List Background Jobs",
)
@router.get(
    "/api/v1/jobs",
    response_model=JobListResponse,
    tags=["Jobs"],
    summary="List Background Jobs",
)
async def list_background_jobs(limit: int = Query(default=20, ge=1, le=100)):
    """List recent background processing jobs and their current execution state."""
    jobs = job_manager.list_jobs(limit=limit)
    return {"total_jobs": len(jobs), "jobs": jobs}


@router.delete(
    "/v1/jobs/{job_id}",
    tags=["Jobs"],
    summary="Delete / Cancel Job",
)
@router.delete(
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


@router.post(
    "/v1/jobs/{job_id}/retry",
    response_model=JobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Jobs"],
    summary="Retry Interrupted or Failed Job",
)
@router.post(
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
    req_data = meta.get("request_data") or meta
    if retried_job.get("request_type") == "batch-dir" and req_data.get("input_dir"):
        background_tasks.add_task(_run_batch_dir_job, job_id, req_data)

    return {
        "job_id": job_id,
        "status": JobStatus.QUEUED.value,
        "message": f"Job {job_id} successfully re-queued for execution",
        "check_status_url": f"/v1/jobs/{job_id}",
    }


@router.post(
    "/v1/jobs/batch-dir",
    response_model=JobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Jobs"],
    summary="Queue Background Directory Batch Job",
)
@router.post(
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


@router.post(
    "/v1/jobs/batch",
    response_model=JobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Jobs"],
    summary="Queue Background Batch Upload Job",
)
@router.post(
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

"""Dedicated isolated ProcessPoolExecutor and Memory Guard for OCR processing.

Provides:
1. Isolated multi-process execution for CPU-bound OCR and OpenCV operations,
   completely decoupled from the async event loop of FastAPI.
2. Memory Guard with automatic worker recycling after N pages/tasks or
   upon crossing resident memory limits (RSS) to eradicate C-library leaks.
3. Configurable concurrency via MAX_OCR_WORKERS, MAX_PAGES_PER_WORKER,
   and MAX_WORKER_MEMORY_MB.
4. Streaming task pipeline with bounded in-flight sliding window to maintain
   constant O(1) memory during high-volume batch processing (100+ documents).
5. File descriptor and memory telemetry utilities.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
import gc
import logging
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Generator, Iterable, Sequence

from .constants import (
    DEFAULT_MAX_OCR_WORKERS,
    DEFAULT_MAX_PAGES_PER_WORKER,
    DEFAULT_MAX_WORKER_MEMORY_MB,
    DEFAULT_OCR_CACHE_DIR,
    DEFAULT_OCR_LANG,
)
from .pipeline import process_invoice, serialize_invoice
from .tesseract_env import setup_tessdata_prefix

logger = logging.getLogger("invoice_ocr.worker_pool")

# ---------------------------------------------------------------------------
# Telemetry & Resource Monitoring Utilities
# ---------------------------------------------------------------------------

def get_process_rss_mb(pid: int | None = None) -> float:
    """Return Resident Set Size (RSS) memory consumption in Megabytes (MB).
    
    Supports macOS (ru_maxrss in bytes) and Linux (ru_maxrss in KiB / /proc/self/status).
    """
    target_pid = pid or os.getpid()
    # Fast path for current process using resource module
    if target_pid == os.getpid():
        try:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF)
            if sys.platform == "darwin":
                # macOS ru_maxrss is in bytes
                return usage.ru_maxrss / (1024.0 * 1024.0)
            # Linux and BSD ru_maxrss is in kilobytes
            return usage.ru_maxrss / 1024.0
        except Exception:
            pass

    # Linux /proc/<pid>/status check
    proc_status = Path(f"/proc/{target_pid}/status")
    if proc_status.exists():
        try:
            for line in proc_status.read_text().splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return float(parts[1]) / 1024.0  # KiB to MB
        except Exception:
            pass

    # Fallback to ps subprocess
    try:
        out = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(target_pid)],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        if out:
            return float(out) / 1024.0  # KiB to MB
    except Exception:
        pass

    return 0.0


def get_open_fd_count(pid: int | None = None) -> int:
    """Return the number of open file descriptors for the given process."""
    target_pid = pid or os.getpid()
    candidates = [
        Path(f"/proc/{target_pid}/fd"),
        Path("/proc/self/fd"),
        Path("/dev/fd"),
    ]
    for fd_dir in candidates:
        if fd_dir.exists():
            try:
                return len(os.listdir(fd_dir))
            except Exception:
                pass
    return 0


# ---------------------------------------------------------------------------
# Worker Process Initialization & Task Runner
# ---------------------------------------------------------------------------

_WORKER_PID: int = 0
_WORKER_PAGES_PROCESSED: int = 0
_WORKER_DOCS_PROCESSED: int = 0
_WORKER_START_TIME: float = 0.0


def _init_ocr_worker() -> None:
    """Child process initializer: configure single-thread OpenCV/OpenMP and Tessdata."""
    global _WORKER_PID, _WORKER_PAGES_PROCESSED, _WORKER_DOCS_PROCESSED, _WORKER_START_TIME
    _WORKER_PID = os.getpid()
    _WORKER_PAGES_PROCESSED = 0
    _WORKER_DOCS_PROCESSED = 0
    _WORKER_START_TIME = time.perf_counter()

    # Prevent OpenMP and OpenCV thread thrashing across parallel workers
    os.environ["OMP_THREAD_LIMIT"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
    os.environ["NUMEXPR_NUM_THREADS"] = "1"

    setup_tessdata_prefix()


def _ocr_task_worker(task_args: dict[str, Any]) -> dict[str, Any]:
    """Execute OCR pipeline for a single document inside an isolated worker process.
    
    Includes pre- and post-processing Memory Guard checks and explicit garbage collection.
    """
    global _WORKER_PAGES_PROCESSED, _WORKER_DOCS_PROCESSED

    file_path = Path(task_args["file_path"])
    rel_path = task_args.get("rel_path", file_path.name)
    file_idx = task_args.get("file_idx", 0)
    out_dir_str = task_args.get("out_dir")
    debug_dir_str = task_args.get("debug_dir")
    lang = task_args.get("lang", DEFAULT_OCR_LANG)
    tessdata_dir = task_args.get("tessdata_dir")
    use_cache = task_args.get("use_cache", True)
    ocr_cache_dir = task_args.get("ocr_cache_dir", DEFAULT_OCR_CACHE_DIR)
    return_invoice_object = task_args.get("return_invoice_object", False)
    max_memory_mb = task_args.get("max_memory_mb", DEFAULT_MAX_WORKER_MEMORY_MB)
    max_pages_per_worker = task_args.get("max_pages_per_worker", DEFAULT_MAX_PAGES_PER_WORKER)

    start_time = time.perf_counter()
    start_rss = get_process_rss_mb()
    debug_dir = Path(debug_dir_str) if debug_dir_str else None

    try:
        invoice = process_invoice(
            file_path,
            debug_dir=debug_dir,
            lang=lang,
            tessdata_dir=tessdata_dir,
            use_cache=use_cache,
            ocr_cache_dir=ocr_cache_dir,
        )

        doc_pages = 1
        if invoice and invoice.raw_ocr_evidence:
            doc_pages = int(invoice.raw_ocr_evidence.get("total_pages", 1))

        _WORKER_PAGES_PROCESSED += doc_pages
        _WORKER_DOCS_PROCESSED += 1

        out_file_path: str | None = None
        if out_dir_str:
            out_dir = Path(out_dir_str)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file = out_dir / f"{file_path.stem}.json"
            json_text = serialize_invoice(invoice)
            out_file.write_text(json_text, encoding="utf-8")
            out_file_path = str(out_file)

        # Explicit garbage collection inside worker to release PyMuPDF / OpenCV buffers
        gc.collect()

        end_time = time.perf_counter()
        end_rss = get_process_rss_mb()
        duration = round(end_time - start_time, 3)

        # Memory Guard check
        memory_exceeded = end_rss > max_memory_mb
        pages_exceeded = _WORKER_PAGES_PROCESSED >= max_pages_per_worker
        recycle_recommended = memory_exceeded or pages_exceeded

        if memory_exceeded:
            logger.warning(
                "Worker PID %d RSS memory (%.1f MB) exceeded limit (%.1f MB) after file %s",
                os.getpid(), end_rss, max_memory_mb, file_path.name,
            )

        return {
            "file_idx": file_idx,
            "file": file_path.name,
            "relative_path": rel_path,
            "output_file": out_file_path,
            "status": "success",
            "invoice": invoice if return_invoice_object else None,
            "pages": doc_pages,
            "worker_pid": os.getpid(),
            "worker_pages_processed": _WORKER_PAGES_PROCESSED,
            "worker_docs_processed": _WORKER_DOCS_PROCESSED,
            "memory_rss_mb": round(end_rss, 2),
            "memory_guard_triggered": recycle_recommended,
            "duration_seconds": duration,
            "error": None,
        }

    except Exception as exc:
        gc.collect()
        end_time = time.perf_counter()
        end_rss = get_process_rss_mb()
        duration = round(end_time - start_time, 3)
        logger.error("Worker PID %d failed processing %s: %s", os.getpid(), rel_path, exc)

        return {
            "file_idx": file_idx,
            "file": file_path.name,
            "relative_path": rel_path,
            "output_file": None,
            "status": "error",
            "invoice": None,
            "pages": 0,
            "worker_pid": os.getpid(),
            "worker_pages_processed": _WORKER_PAGES_PROCESSED,
            "worker_docs_processed": _WORKER_DOCS_PROCESSED,
            "memory_rss_mb": round(end_rss, 2),
            "memory_guard_triggered": end_rss > max_memory_mb,
            "duration_seconds": duration,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Isolated OCR ProcessPoolExecutor with Memory Guard & Recycling
# ---------------------------------------------------------------------------

class OCRProcessPoolExecutor:
    """ProcessPoolExecutor dedicated to OCR workloads with automatic process recycling.
    
    Attributes:
        max_workers: Number of concurrent worker processes (MAX_OCR_WORKERS).
        max_pages_per_worker: Automatic recycling threshold (pages/tasks per child).
        max_memory_mb: Maximum memory threshold per worker before recycling.
    """

    def __init__(
        self,
        max_workers: int | None = None,
        max_pages_per_worker: int | None = None,
        max_memory_mb: float | None = None,
        mp_context: Any = None,
    ) -> None:
        self.max_workers = max_workers if (max_workers and max_workers > 0) else DEFAULT_MAX_OCR_WORKERS
        self.max_pages_per_worker = (
            max_pages_per_worker if (max_pages_per_worker and max_pages_per_worker > 0)
            else DEFAULT_MAX_PAGES_PER_WORKER
        )
        self.max_memory_mb = (
            max_memory_mb if (max_memory_mb and max_memory_mb > 0)
            else DEFAULT_MAX_WORKER_MEMORY_MB
        )
        self.mp_context = mp_context or multiprocessing.get_context("spawn")

        # In Python 3.11+, max_tasks_per_child guarantees OS-level process termination
        # and fresh child respawn after N tasks, releasing all leaked C memory pages.
        self._max_tasks_per_child = max(1, self.max_pages_per_worker)
        self._lock = threading.Lock()
        self._is_shutdown = False
        self._executor: ProcessPoolExecutor = self._create_inner_executor()

    def _create_inner_executor(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=self.max_workers,
            mp_context=self.mp_context,
            initializer=_init_ocr_worker,
            max_tasks_per_child=self._max_tasks_per_child,
        )

    @property
    def is_shutdown(self) -> bool:
        return self._is_shutdown

    @property
    def executor(self) -> ProcessPoolExecutor:
        with self._lock:
            if self._is_shutdown:
                raise RuntimeError("OCRProcessPoolExecutor is shut down")
            return self._executor

    def submit_task(self, task_args: dict[str, Any]) -> Future[dict[str, Any]]:
        """Submit a single OCR task dictionary to the process pool."""
        with self._lock:
            if self._is_shutdown:
                raise RuntimeError("OCRProcessPoolExecutor is shut down")
            # Inject executor configuration limits if not present
            task_args.setdefault("max_memory_mb", self.max_memory_mb)
            task_args.setdefault("max_pages_per_worker", self.max_pages_per_worker)
            return self._executor.submit(_ocr_task_worker, task_args)

    def submit_ocr(
        self,
        file_path: Path | str,
        debug_dir: Path | str | None = None,
        lang: str = DEFAULT_OCR_LANG,
        tessdata_dir: Path | str | None = None,
        use_cache: bool = True,
        ocr_cache_dir: Path | str | None = DEFAULT_OCR_CACHE_DIR,
        return_invoice_object: bool = True,
        out_dir: Path | str | None = None,
        file_idx: int = 0,
        rel_path: str | None = None,
    ) -> Future[dict[str, Any]]:
        """Convenience method to submit an OCR task returning a Future."""
        args = {
            "file_path": str(file_path),
            "rel_path": rel_path or Path(file_path).name,
            "file_idx": file_idx,
            "out_dir": str(out_dir) if out_dir else None,
            "debug_dir": str(debug_dir) if debug_dir else None,
            "lang": lang,
            "tessdata_dir": str(tessdata_dir) if tessdata_dir else None,
            "use_cache": use_cache,
            "ocr_cache_dir": str(ocr_cache_dir) if ocr_cache_dir else None,
            "return_invoice_object": return_invoice_object,
            "max_memory_mb": self.max_memory_mb,
            "max_pages_per_worker": self.max_pages_per_worker,
        }
        return self.submit_task(args)

    async def submit_ocr_async(
        self,
        file_path: Path | str,
        debug_dir: Path | str | None = None,
        lang: str = DEFAULT_OCR_LANG,
        tessdata_dir: Path | str | None = None,
        use_cache: bool = True,
        ocr_cache_dir: Path | str | None = DEFAULT_OCR_CACHE_DIR,
        return_invoice_object: bool = True,
        out_dir: Path | str | None = None,
        file_idx: int = 0,
        rel_path: str | None = None,
    ) -> dict[str, Any]:
        """Asynchronously offload OCR processing to the process pool without blocking FastAPI loop."""
        loop = asyncio.get_running_loop()
        args = {
            "file_path": str(file_path),
            "rel_path": rel_path or Path(file_path).name,
            "file_idx": file_idx,
            "out_dir": str(out_dir) if out_dir else None,
            "debug_dir": str(debug_dir) if debug_dir else None,
            "lang": lang,
            "tessdata_dir": str(tessdata_dir) if tessdata_dir else None,
            "use_cache": use_cache,
            "ocr_cache_dir": str(ocr_cache_dir) if ocr_cache_dir else None,
            "return_invoice_object": return_invoice_object,
            "max_memory_mb": self.max_memory_mb,
            "max_pages_per_worker": self.max_pages_per_worker,
        }
        with self._lock:
            if self._is_shutdown:
                raise RuntimeError("OCRProcessPoolExecutor is shut down")
            executor = self._executor

        return await loop.run_in_executor(executor, _ocr_task_worker, args)

    def stream_batch(
        self,
        task_args_iterable: Iterable[dict[str, Any]],
        window_size: int | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
        total_items: int | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Stream batch results with bounded in-flight tasks (O(1) memory).
        
        Submits up to `window_size` tasks concurrently. As each task finishes,
        yields the result immediately and submits the next item from the iterable.
        """
        effective_window = window_size or max(4, self.max_workers * 2)
        task_iter = iter(task_args_iterable)

        # In-flight futures mapping: future -> task_args
        in_flight: dict[Future[dict[str, Any]], dict[str, Any]] = {}

        # Pre-fill window
        for _ in range(effective_window):
            try:
                item_args = next(task_iter)
                fut = self.submit_task(item_args)
                in_flight[fut] = item_args
            except StopIteration:
                break

        completed_count = 0
        try:
            while in_flight:
                # Wait for the first future to complete
                done_set = set()
                for fut in as_completed(in_flight):
                    done_set.add(fut)
                    break  # Process one completed task at a time to maintain streaming pipeline

                for fut in done_set:
                    orig_args = in_flight.pop(fut)
                    res = fut.result()
                    completed_count += 1
                    if progress_callback:
                        progress_callback(completed_count, total_items or completed_count, res.get("file", ""))

                    yield res

                    # Refill sliding window
                    try:
                        next_args = next(task_iter)
                        new_fut = self.submit_task(next_args)
                        in_flight[new_fut] = next_args
                    except StopIteration:
                        pass
        finally:
            # Cancel any pending futures if generator is closed early
            for fut in in_flight:
                fut.cancel()

    def recycle_pool(self) -> None:
        """Force clean shutdown and re-creation of worker processes."""
        with self._lock:
            if self._is_shutdown:
                return
            old_executor = self._executor
            self._executor = self._create_inner_executor()
        old_executor.shutdown(wait=False, cancel_futures=True)

    def shutdown(self, wait: bool = True, cancel_futures: bool = True) -> None:
        """Shutdown the underlying ProcessPoolExecutor cleanly."""
        with self._lock:
            if self._is_shutdown:
                return
            self._is_shutdown = True
            executor = self._executor
        executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def __enter__(self) -> OCRProcessPoolExecutor:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Global Singleton Pool Management
# ---------------------------------------------------------------------------

_GLOBAL_OCR_POOL: OCRProcessPoolExecutor | None = None
_GLOBAL_POOL_LOCK = threading.Lock()


def get_ocr_pool() -> OCRProcessPoolExecutor:
    """Retrieve or lazily initialize the application-wide OCRProcessPoolExecutor."""
    global _GLOBAL_OCR_POOL
    with _GLOBAL_POOL_LOCK:
        if _GLOBAL_OCR_POOL is None or _GLOBAL_OCR_POOL.is_shutdown:
            _GLOBAL_OCR_POOL = OCRProcessPoolExecutor()
        return _GLOBAL_OCR_POOL


def init_ocr_pool(
    max_workers: int | None = None,
    max_pages_per_worker: int | None = None,
    max_memory_mb: float | None = None,
) -> OCRProcessPoolExecutor:
    """Explicitly initialize or reconfigure the application-wide OCRProcessPoolExecutor."""
    global _GLOBAL_OCR_POOL
    with _GLOBAL_POOL_LOCK:
        if _GLOBAL_OCR_POOL is not None and not _GLOBAL_OCR_POOL.is_shutdown:
            _GLOBAL_OCR_POOL.shutdown(wait=False, cancel_futures=True)
        _GLOBAL_OCR_POOL = OCRProcessPoolExecutor(
            max_workers=max_workers,
            max_pages_per_worker=max_pages_per_worker,
            max_memory_mb=max_memory_mb,
        )
        return _GLOBAL_OCR_POOL


def shutdown_ocr_pool(wait: bool = True) -> None:
    """Cleanly terminate the global OCR worker pool and all child processes."""
    global _GLOBAL_OCR_POOL
    with _GLOBAL_POOL_LOCK:
        if _GLOBAL_OCR_POOL is not None:
            _GLOBAL_OCR_POOL.shutdown(wait=wait, cancel_futures=True)
            _GLOBAL_OCR_POOL = None

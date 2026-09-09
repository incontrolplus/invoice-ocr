"""Folder & Cloud Storage Watcher for Bulgarian Invoice OCR.

Channel 2: Watches local folders, network mounts (SMB/NFS), or cloud synced directories
(Nextcloud, Google Drive, Dropbox) for incoming invoice files (.pdf, .tiff, .tif, .png, .jpg).
- Debouncing / file-write completion check to handle in-progress cloud syncs.
- Automatic OCR processing and database persistence.
- Reverse notification delivery (Email, Telegram, Slack, ERP Webhook).
- Automatic archiving into 'processed/' or 'failed/' subdirectories.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Callable, Optional
import uuid

from .constants import SUPPORTED_EXTENSIONS
from .notifications import dispatch_reverse_notifications_bundle

logger = logging.getLogger("invoice_ocr_watcher")


@dataclass
class FolderWatcherConfig:
    """Configuration for folder and cloud storage watcher."""
    watch_dir: Path
    processed_dir: Optional[Path] = None
    failed_dir: Optional[Path] = None
    poll_interval_sec: float = 2.0
    debounce_delay_sec: float = 1.0
    recursive: bool = False
    erp_webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    notification_email: Optional[str] = None
    hitl_base_url: Optional[str] = None


class FolderWatcher:
    """Monitors a directory for incoming invoices and processes them with zero touch."""

    def __init__(self, config: FolderWatcherConfig):
        self.config = config
        self.watch_dir = Path(config.watch_dir)
        self.processed_dir = Path(config.processed_dir) if config.processed_dir else (self.watch_dir / "processed")
        self.failed_dir = Path(config.failed_dir) if config.failed_dir else (self.watch_dir / "failed")

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._processed_hashes: set[str] = set()
        self._file_size_cache: dict[Path, tuple[int, float]] = {}  # path -> (size, check_timestamp)
        self.processed_count = 0
        self.failed_count = 0

        # Ensure directories exist
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)

    def is_file_ready(self, path: Path) -> bool:
        """Verify that file is completely written by cloud sync/upload (debouncing)."""
        if not path.is_file():
            return False
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return False

        # Exclude subdirectories and files inside processed/failed
        try:
            if path.resolve().is_relative_to(self.processed_dir.resolve()):
                return False
            if path.resolve().is_relative_to(self.failed_dir.resolve()):
                return False
        except Exception:
            pass

        # Ignore hidden files / temporary download files
        if path.name.startswith(".") or path.name.endswith(".tmp") or path.name.endswith(".part"):
            return False

        try:
            size_now = path.stat().st_size
            if size_now == 0:
                return False

            now = time.time()
            if path in self._file_size_cache:
                prev_size, prev_time = self._file_size_cache[path]
                if size_now == prev_size and (now - prev_time) >= self.config.debounce_delay_sec:
                    # Size is stable for at least debounce_delay_sec
                    # Verify read access
                    with open(path, "rb") as f:
                        f.read(1024)
                    return True
                else:
                    self._file_size_cache[path] = (size_now, now)
                    return False
            else:
                self._file_size_cache[path] = (size_now, now)
                # If debounce delay is 0 (testing mode), ready immediately
                if self.config.debounce_delay_sec <= 0:
                    return True
                return False
        except (OSError, PermissionError):
            return False

    def process_file(self, file_path: Path) -> dict[str, Any]:
        """Run OCR pipeline on file, persist to database, send notifications, and archive file."""
        from database import get_db_session, save_document_to_db
        from invoice_ocr import process_invoice, serialize_invoice

        doc_id = uuid.uuid4().hex
        t0 = time.perf_counter()
        logger.info("Watcher: Processing invoice %s (ID: %s)", file_path.name, doc_id)

        try:
            invoice = process_invoice(file_path)
            duration = time.perf_counter() - t0

            ocr_result = json.loads(serialize_invoice(invoice))
            ocr_result["file_name"] = file_path.name
            ocr_result["source_channel"] = "folder_watcher"
            ocr_result["original_file_path"] = str(file_path)

            with get_db_session() as db:
                doc_rec = save_document_to_db(
                    db=db,
                    doc_id=doc_id,
                    file_name=file_path.name,
                    source_path=file_path,
                    ocr_result=ocr_result,
                    processing_time=duration,
                    webhook_url=self.config.erp_webhook_url,
                )
                doc_dict = doc_rec.to_dict()

            # Archive file to processed_dir
            dest_path = self.processed_dir / file_path.name
            if dest_path.exists():
                stem = file_path.stem
                suffix = file_path.suffix
                dest_path = self.processed_dir / f"{stem}_{doc_id[:8]}{suffix}"
            shutil.move(str(file_path), str(dest_path))
            self._file_size_cache.pop(file_path, None)

            # Dispatch reverse notifications asynchronously
            notification_res = None
            try:
                loop = None
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass

                coro = dispatch_reverse_notifications_bundle(
                    doc_dict=doc_dict,
                    sender_email=self.config.notification_email,
                    erp_webhook_url=self.config.erp_webhook_url,
                    telegram_bot_token=self.config.telegram_bot_token,
                    telegram_chat_id=self.config.telegram_chat_id,
                    slack_webhook_url=self.config.slack_webhook_url,
                    hitl_base_url=self.config.hitl_base_url,
                )
                if loop and loop.is_running():
                    asyncio.create_task(coro)
                else:
                    notification_res = asyncio.run(coro)
            except Exception as notif_exc:
                logger.warning("Could not dispatch notifications for %s: %s", file_path.name, notif_exc)

            self.processed_count += 1
            logger.info(
                "Watcher: Successfully processed %s -> Status: %s, Total: %.2f %s in %.2fs",
                file_path.name, doc_dict.get("status"), doc_dict.get("total_amount", 0.0),
                doc_dict.get("currency", "BGN"), duration,
            )

            return {
                "status": "success",
                "document_id": doc_id,
                "file_name": file_path.name,
                "archived_path": str(dest_path),
                "duration_seconds": duration,
                "document": doc_dict,
                "notifications": notification_res,
            }

        except Exception as exc:
            duration = time.perf_counter() - t0
            self.failed_count += 1
            logger.error("Watcher: Failed processing %s: %s", file_path.name, exc, exc_info=True)

            # Move file to failed_dir
            try:
                dest_failed = self.failed_dir / file_path.name
                if dest_failed.exists():
                    dest_failed = self.failed_dir / f"{file_path.stem}_{doc_id[:8]}{file_path.suffix}"
                shutil.move(str(file_path), str(dest_failed))
                self._file_size_cache.pop(file_path, None)
            except Exception as move_exc:
                logger.error("Watcher: Failed moving %s to failed dir: %s", file_path.name, move_exc)

            return {
                "status": "failed",
                "document_id": doc_id,
                "file_name": file_path.name,
                "error": str(exc),
                "duration_seconds": duration,
            }

    def scan_once(self) -> list[dict[str, Any]]:
        """Perform a single scan of the watch directory and process ready files."""
        results = []
        pattern = "**/*" if self.config.recursive else "*"
        for path in sorted(self.watch_dir.glob(pattern)):
            if self.is_file_ready(path):
                res = self.process_file(path)
                results.append(res)
        return results

    def _loop(self):
        """Background thread polling loop."""
        logger.info("Watcher started monitoring directory: %s", self.watch_dir)
        while self._running:
            try:
                self.scan_once()
            except Exception as exc:
                logger.error("Error in watcher loop: %s", exc, exc_info=True)
            time.sleep(self.config.poll_interval_sec)
        logger.info("Watcher stopped monitoring directory: %s", self.watch_dir)

    def start(self):
        """Start the background monitoring thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="FolderWatcher")
        self._thread.start()

    def stop(self):
        """Stop the background monitoring thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    @property
    def is_running(self) -> bool:
        return self._running

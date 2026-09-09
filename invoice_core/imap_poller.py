r"""IMAP Email Poller for Zero-Touch Inbound Invoice Capture.

Channel 1 Option: Polls corporate email mailboxes (e.g. invoices@openbalancer.com) via IMAP/SSL.
- Automatically searches UNSEEN emails.
- Extracts attached invoices (.pdf, .tiff, .tif, .png, .jpg), filtering out signatures (<10KB).
- Submits attachments to OCR engine and persists to database.
- Dispatches reverse confirmation email and Slack/Telegram alerts.
- Marks processed emails as \Seen or moves them to an archive folder.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import email
import imaplib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Optional
import uuid

from .email_ingestion import ParsedEmail, parse_mime_email
from .notifications import dispatch_reverse_notifications_bundle

logger = logging.getLogger("invoice_ocr_imap")


@dataclass
class ImapPollerConfig:
    """Configuration for IMAP inbox polling."""
    host: str
    port: int = 993
    username: str = ""
    password: str = ""
    mailbox: str = "INBOX"
    use_ssl: bool = True
    poll_interval_sec: float = 30.0
    mark_as_read: bool = True
    erp_webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    hitl_base_url: Optional[str] = None


class ImapPoller:
    """Connects to IMAP inbox and ingests invoices automatically."""

    def __init__(self, config: ImapPollerConfig):
        self.config = config
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.processed_count = 0
        self.failed_count = 0

    def connect(self) -> imaplib.IMAP4:
        """Establish authenticated connection to IMAP server."""
        if self.config.use_ssl:
            client = imaplib.IMAP4_SSL(self.config.host, self.config.port)
        else:
            client = imaplib.IMAP4(self.config.host, self.config.port)

        client.login(self.config.username, self.config.password)
        client.select(self.config.mailbox)
        return client

    def process_raw_email(self, raw_bytes: bytes) -> list[dict[str, Any]]:
        """Process a raw email message: extract attachments, run OCR, persist to DB, send notifications."""
        from database import get_db_session, save_document_to_db
        from invoice_ocr import process_invoice, serialize_invoice

        parsed: ParsedEmail = parse_mime_email(raw_bytes)
        logger.info(
            "IMAP: Received email from '%s' with subject '%s' (%d valid / %d filtered attachments)",
            parsed.sender, parsed.subject, len(parsed.valid_attachments), len(parsed.filtered_attachments),
        )

        results = []

        for att in parsed.valid_attachments:
            t0 = time.perf_counter()
            suffix = Path(att.filename).suffix.lower() or ".pdf"
            doc_id = uuid.uuid4().hex

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(att.data)
                tmp_path = Path(tmp.name)

            try:
                invoice = process_invoice(tmp_path)
                dur = time.perf_counter() - t0

                ocr_result = json.loads(serialize_invoice(invoice))
                ocr_result["file_name"] = att.filename
                ocr_result["source_channel"] = "imap_poller"
                ocr_result["email_metadata"] = {
                    "sender": parsed.sender,
                    "recipient": parsed.recipient,
                    "subject": parsed.subject,
                    "message_id": parsed.message_id,
                    "date": parsed.date,
                    "spf": parsed.security.spf_status,
                    "dkim": parsed.security.dkim_status,
                }

                with get_db_session() as db:
                    doc_rec = save_document_to_db(
                        db=db,
                        doc_id=doc_id,
                        file_name=att.filename,
                        source_path=tmp_path,
                        ocr_result=ocr_result,
                        processing_time=dur,
                        webhook_url=self.config.erp_webhook_url,
                    )
                    doc_dict = doc_rec.to_dict()

                # Dispatch notifications
                loop = None
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass

                coro = dispatch_reverse_notifications_bundle(
                    doc_dict=doc_dict,
                    sender_email=parsed.sender,
                    erp_webhook_url=self.config.erp_webhook_url,
                    telegram_bot_token=self.config.telegram_bot_token,
                    telegram_chat_id=self.config.telegram_chat_id,
                    slack_webhook_url=self.config.slack_webhook_url,
                    hitl_base_url=self.config.hitl_base_url,
                )
                if loop and loop.is_running():
                    asyncio.create_task(coro)
                else:
                    asyncio.run(coro)

                self.processed_count += 1
                results.append({
                    "status": "success",
                    "document_id": doc_id,
                    "file_name": att.filename,
                    "invoice_number": doc_dict.get("invoice_number"),
                    "supplier_name": doc_dict.get("supplier_name"),
                    "total_amount": doc_dict.get("total_amount"),
                    "currency": doc_dict.get("currency"),
                })
            except Exception as exc:
                self.failed_count += 1
                logger.error("IMAP: Failed processing attachment %s: %s", att.filename, exc, exc_info=True)
                results.append({
                    "status": "failed",
                    "file_name": att.filename,
                    "error": str(exc),
                })
            finally:
                tmp_path.unlink(missing_ok=True)

        return results

    def poll_once(self) -> list[dict[str, Any]]:
        """Connect to IMAP server, fetch all unread emails, and process them."""
        results = []
        client = None
        try:
            client = self.connect()
            status, data = client.search(None, "UNSEEN")
            if status != "OK" or not data or not data[0]:
                logger.debug("IMAP: No unread messages found.")
                return results

            msg_ids = data[0].split()
            logger.info("IMAP: Found %d unread message(s) to process", len(msg_ids))

            for msg_id in msg_ids:
                status, msg_data = client.fetch(msg_id, "(RFC822)")
                if status != "OK" or not msg_data:
                    continue

                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        raw_email_bytes = response_part[1]
                        res = self.process_raw_email(raw_email_bytes)
                        results.extend(res)

                if self.config.mark_as_read:
                    client.store(msg_id, "+FLAGS", "\\Seen")

        except Exception as exc:
            logger.error("IMAP poller error: %s", exc, exc_info=True)
        finally:
            if client:
                try:
                    client.close()
                    client.logout()
                except Exception:
                    pass

        return results

    def _loop(self):
        """Background thread polling loop."""
        logger.info("IMAP poller started for host %s", self.config.host)
        while self._running:
            try:
                self.poll_once()
            except Exception as exc:
                logger.error("Error in IMAP poller loop: %s", exc, exc_info=True)
            time.sleep(self.config.poll_interval_sec)
        logger.info("IMAP poller stopped.")

    def start(self):
        """Start background polling thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ImapPoller")
        self._thread.start()

    def stop(self):
        """Stop background polling thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    @property
    def is_running(self) -> bool:
        return self._running

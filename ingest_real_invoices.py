#!/usr/bin/env python3
"""ingest_real_invoices.py — Ingest real Bulgarian invoices from _ФАКТУРИ into database.

Cleans out all synthetic/mock test invoices from the database and persists
real Bulgarian invoices with full multi-pass OCR tokens and PDF page storage.
"""

import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from database import (
    AuditTrailRecord,
    DocumentRecord,
    get_db_session,
    save_document_to_db,
    storage_manager,
)
from invoice_ocr import process_invoice, serialize_invoice

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ingest_real_invoices")

REAL_INVOICES_BASE = Path("/Volumes/NO NAME/_ФАКТУРИ")

# Candidate real invoice files across multiple Bulgarian vendors
CANDIDATE_PATHS = [
    # Капина 71 ООД
    REAL_INVOICES_BASE / "02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf",
    REAL_INVOICES_BASE / "02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf",
    REAL_INVOICES_BASE / "02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf",
    # Валборген ООД
    REAL_INVOICES_BASE / "04_ВАЛБОРГЕН_ООД/валборген.pdf",
    # Метро България
    REAL_INVOICES_BASE / "01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf",
    REAL_INVOICES_BASE / "01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро-2.pdf",
    # Анда 2012
    REAL_INVOICES_BASE / "08_AНДА_2012_АНКО_ПЕТРОВ_ЕООД/Анда 2026/анда-01.pdf",
    # Оскари ООД
    REAL_INVOICES_BASE / "07_О_СКАРИ_ООД/Оскари 2026/оскари-01.pdf",
    # РМ Каскада (Scanned real invoices)
    REAL_INVOICES_BASE / "00_РМ_КАСКАДА_2026_ЕООД/07_09_2026/30.pdf",
    REAL_INVOICES_BASE / "00_РМ_КАСКАДА_2026_ЕООД/07_09_2026/32.pdf",
    REAL_INVOICES_BASE / "00_РМ_КАСКАДА_2026_ЕООД/07_09_2026/35.pdf",
    REAL_INVOICES_BASE / "00_РМ_КАСКАДА_2026_ЕООД/07_09_2026/37.pdf",
    REAL_INVOICES_BASE / "00_РМ_КАСКАДА_2026_ЕООД/07_09_2026/38.pdf",
    REAL_INVOICES_BASE / "00_РМ_КАСКАДА_2026_ЕООД/07_09_2026/14.pdf",
    REAL_INVOICES_BASE / "00_РМ_КАСКАДА_2026_ЕООД/07_09_2026/48.pdf",
    REAL_INVOICES_BASE / "00_РМ_КАСКАДА_2026_ЕООД/07_09_2026/51.pdf",
    # Грестокомерс
    REAL_INVOICES_BASE / "10_ГРЕСТОКОМЕРС_ЕООД/Грестокомерс 2026/грестокомерс-01.pdf",
    # Експрес Секюрити СОД
    REAL_INVOICES_BASE / "11_ЕКСПРЕС_СЕКЮРИТИ_СОД_ЕООД/Експрес 2026/експрес.pdf",
    # Интермес ООД
    REAL_INVOICES_BASE / "12_ИНТЕРМЕС_ООД/Интермес 2026/интермес-01.pdf",
]


def clean_synthetic_records():
    """Remove artificial/synthetic demo records from DB so UI contains ONLY real invoices."""
    synthetic_file_patterns = [
        "demo_invoice.png",
        "test_invoice.png",
        "test_raw.png",
        "webhook_test_inv.png",
        "degraded_invoice.png",
        "hitl_flow.png",
        "invoice_with_errors.png",
    ]
    with get_db_session() as db:
        query = db.query(DocumentRecord).filter(
            (DocumentRecord.file_name.in_(synthetic_file_patterns))
            | (DocumentRecord.id.like("demo_%"))
            | (DocumentRecord.id.like("hitl_test_%"))
        )
        fake_docs = query.all()
        logger.info("Found %d synthetic/test records to purge from database", len(fake_docs))
        for doc in fake_docs:
            db.query(AuditTrailRecord).filter(AuditTrailRecord.document_id == doc.id).delete()
            # Clean storage file if exists
            stored_f = storage_manager.get_file_path(doc.id)
            if stored_f and stored_f.exists():
                try:
                    stored_f.unlink(missing_ok=True)
                except Exception:
                    pass
            db.delete(doc)
        db.commit()
        logger.info("Purged all synthetic records successfully.")


def ingest_real_invoices():
    """Run OCR pipeline on real Bulgarian invoices and persist to DB."""
    if not REAL_INVOICES_BASE.exists():
        logger.error("Real invoices directory '%s' not found!", REAL_INVOICES_BASE)
        return

    clean_synthetic_records()

    available_files = [p for p in CANDIDATE_PATHS if p.exists() and p.is_file()]
    logger.info("Processing %d real invoices from %s...", len(available_files), REAL_INVOICES_BASE)

    processed_count = 0
    with get_db_session() as db:
        for fpath in available_files:
            logger.info("--- Processing real invoice: %s ---", fpath.name)
            t0 = time.perf_counter()
            try:
                invoice = process_invoice(fpath)
                dur = time.perf_counter() - t0

                ocr_result = json.loads(serialize_invoice(invoice))
                ocr_result["file_name"] = fpath.name

                # Check if this real invoice is already in DB
                existing = db.query(DocumentRecord).filter(DocumentRecord.file_name == fpath.name).first()
                doc_id = existing.id if existing else uuid.uuid4().hex

                doc_rec = save_document_to_db(
                    db=db,
                    doc_id=doc_id,
                    file_name=fpath.name,
                    source_path=fpath,
                    ocr_result=ocr_result,
                    processing_time=dur,
                )
                processed_count += 1
                logger.info(
                    "Persisted '%s' (ID: %s) -> Status: %s, Valid: %s, InvNum: %s, Supplier: %s in %.2fs",
                    fpath.name,
                    doc_rec.id,
                    doc_rec.status,
                    doc_rec.is_valid,
                    doc_rec.invoice_number,
                    doc_rec.supplier_name,
                    dur,
                )
            except Exception as exc:
                logger.error("Failed to process real invoice %s: %s", fpath, exc, exc_info=True)

    logger.info("Completed ingestion: %d real invoices successfully saved to database.", processed_count)


if __name__ == "__main__":
    ingest_real_invoices()

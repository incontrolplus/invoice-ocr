"""Persistent Database & Storage Layer for Bulgarian Invoice OCR Microservice.

Provides persistent storage via SQLAlchemy (PostgreSQL / SQLite):
- DocumentRecord: Document storage, full 3-layer JSON, performance timing, validation status
- AuditTrailRecord: Full audit trail tracking uploads, edits, approvals, and exports
- PersistentJobRecord: Persistent background job store across server restarts
- WebhookLogRecord: Comprehensive webhook delivery logs with status codes and payloads
- DocumentStorageManager: Safe file storage and on-demand high-fidelity page rasterization
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
from typing import Any, Generator, Optional
import uuid

from PIL import Image
import pymupdf as fitz
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    desc,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    relationship,
    sessionmaker,
)
from sqlalchemy import event as sa_event

logger = logging.getLogger("invoice_ocr_db")

# ---------------------------------------------------------------------------
# Database Configuration & Engine Setup
# ---------------------------------------------------------------------------

DEFAULT_DB_FILE = Path("invoice_ocr.db")
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DEFAULT_DB_FILE.resolve()}")
STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", ".stored_documents"))

def get_engine(db_url: Optional[str] = None):
    url = db_url or DATABASE_URL
    is_sqlite = url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    return create_engine(url, connect_args=connect_args, echo=False, future=True)

engine = get_engine()

# Enable WAL mode for SQLite to support concurrent reads and writes
@sa_event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    if DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# SQLAlchemy Models
# ---------------------------------------------------------------------------

class DocumentRecord(Base):
    """Persistent record of an ingested invoice document and its complete lifecycle."""
    __tablename__ = "documents"

    id = Column(String(64), primary_key=True, index=True)
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(1024), nullable=True)
    file_hash = Column(String(64), nullable=True, index=True)
    mime_type = Column(String(64), nullable=True)
    file_size_bytes = Column(Integer, default=0)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    processing_time_sec = Column(Float, default=0.0)
    status = Column(String(32), default="completed", index=True)  # completed, needs_review, approved, exported, failed
    is_valid = Column(Boolean, default=True, index=True)
    error_count = Column(Integer, default=0)
    warning_count = Column(Integer, default=0)

    # Core metadata for indexing and quick filtering
    invoice_number = Column(String(64), nullable=True, index=True)
    date_issued = Column(String(32), nullable=True)
    date_tax_event = Column(String(32), nullable=True)
    currency = Column(String(8), default="BGN")

    supplier_name = Column(String(255), nullable=True)
    supplier_eik = Column(String(32), nullable=True, index=True)
    supplier_vat = Column(String(32), nullable=True)
    supplier_address = Column(String(512), nullable=True)

    recipient_name = Column(String(255), nullable=True)
    recipient_eik = Column(String(32), nullable=True, index=True)
    recipient_vat = Column(String(32), nullable=True)
    recipient_address = Column(String(512), nullable=True)

    tax_base = Column(Float, default=0.0)
    vat_amount = Column(Float, default=0.0)
    total_amount = Column(Float, default=0.0)

    # Full payloads
    ocr_result_json = Column(Text, nullable=True)       # Original 3-layer OCR result (including token bboxes)
    corrected_data_json = Column(Text, nullable=True)   # Human corrections applied via HITL
    
    # Webhook integration
    webhook_url = Column(String(1024), nullable=True)
    webhook_status = Column(String(32), default="none")  # none, pending, sent, failed
    webhook_response_code = Column(Integer, nullable=True)

    # Audit Trail Relationship
    audit_trail = relationship(
        "AuditTrailRecord",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="AuditTrailRecord.timestamp.asc()",
    )

    def to_dict(self, include_raw_evidence: bool = False) -> dict[str, Any]:
        """Convert record to API-friendly dictionary with 100% backwards-compatibility."""
        corr = json.loads(self.corrected_data_json) if self.corrected_data_json else None
        ocr = json.loads(self.ocr_result_json) if self.ocr_result_json else {}

        active_data = corr if corr else ocr
        active_norm = active_data.get("normalized_data", {})
        active_val = active_data.get("validation_results", {})

        meta = active_norm.get("invoice_metadata", {})
        supplier = active_norm.get("supplier", {})
        recipient = active_norm.get("recipient", {})
        fin = active_norm.get("financial_summary", {})
        line_items = active_norm.get("line_items", [])

        result = {
            "id": self.id,
            "file_name": self.file_name,
            "file_size_bytes": self.file_size_bytes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "processing_time_sec": self.processing_time_sec,
            "status": self.status,
            "is_valid": self.is_valid,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "invoice_number": self.invoice_number or meta.get("invoice_number"),
            "date_issued": self.date_issued or meta.get("date_issued"),
            "date_tax_event": self.date_tax_event or meta.get("date_tax_event"),
            "currency": self.currency or meta.get("currency") or "BGN",
            "tax_base": self.tax_base,
            "vat_amount": self.vat_amount,
            "total_amount": self.total_amount,
            "invoice_metadata": {
                "invoice_number": self.invoice_number or meta.get("invoice_number"),
                "date_issued": self.date_issued or meta.get("date_issued"),
                "date_tax_event": self.date_tax_event or meta.get("date_tax_event"),
                "currency": self.currency or meta.get("currency") or "BGN",
            },
            "supplier": {
                "name": self.supplier_name or supplier.get("name"),
                "eik": self.supplier_eik or supplier.get("eik"),
                "vat_number": self.supplier_vat or supplier.get("vat_number"),
                "address": self.supplier_address or supplier.get("address"),
            },
            "recipient": {
                "name": self.recipient_name or recipient.get("name"),
                "eik": self.recipient_eik or recipient.get("eik"),
                "vat_number": self.recipient_vat or recipient.get("vat_number"),
                "address": self.recipient_address or recipient.get("address"),
            },
            "financial_summary": {
                "tax_base": {"amount": f"{self.tax_base:.2f}", "currency": self.currency},
                "vat_amount": {"amount": f"{self.vat_amount:.2f}", "currency": self.currency},
                "total_amount_due": {"amount": f"{self.total_amount:.2f}", "currency": self.currency},
            },
            "line_items": line_items,
            "validation": active_val,
            "validation_results": active_val,
            "normalized_data": active_norm,
            "is_corrected": corr is not None,
            "webhook_status": self.webhook_status,
            "webhook_response_code": self.webhook_response_code,
            "data": active_data,
        }

        if include_raw_evidence and "raw_ocr_evidence" in ocr:
            result["raw_ocr_evidence"] = ocr["raw_ocr_evidence"]

        return result


class AuditTrailRecord(Base):
    """Immutable audit trail of all manual edits, uploads, approvals, and exports."""
    __tablename__ = "audit_trail"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(String(64), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    actor = Column(String(128), default="system", nullable=False)
    action = Column(String(64), nullable=False)  # uploaded, processed, field_corrected, approved, exported, webhook_sent
    field_name = Column(String(128), nullable=True)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    details_json = Column(Text, nullable=True)

    document = relationship("DocumentRecord", back_populates="audit_trail")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "actor": self.actor,
            "action": self.action,
            "field_name": self.field_name,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "details": json.loads(self.details_json) if self.details_json else None,
        }


class PersistentJobRecord(Base):
    """Persistent record of asynchronous background jobs across server reboots."""
    __tablename__ = "jobs"

    job_id = Column(String(64), primary_key=True, index=True)
    status = Column(String(32), nullable=False, index=True)
    request_type = Column(String(64), default="batch")
    created_at = Column(Float, nullable=False)
    started_at = Column(Float, nullable=True)
    completed_at = Column(Float, nullable=True)
    progress_percent = Column(Float, default=0.0)
    processed_documents = Column(Integer, default=0)
    total_documents = Column(Integer, default=0)
    current_file = Column(String(255), nullable=True)
    metadata_json = Column(Text, nullable=True)
    result_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    webhook_url = Column(String(1024), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "request_type": self.request_type,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "progress": {
                "total_documents": self.total_documents,
                "processed_documents": self.processed_documents,
                "percent": self.progress_percent,
                "current_file": self.current_file,
            },
            "metadata": json.loads(self.metadata_json) if self.metadata_json else {},
            "result": json.loads(self.result_json) if self.result_json else None,
            "error": self.error_message,
        }


class WebhookLogRecord(Base):
    """Audit log of all outbound webhook notifications sent to ERP."""
    __tablename__ = "webhook_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(String(64), nullable=True, index=True)
    job_id = Column(String(64), nullable=True, index=True)
    event_type = Column(String(64), nullable=False)
    target_url = Column(String(1024), nullable=False)
    payload_json = Column(Text, nullable=False)
    attempt = Column(Integer, default=1)
    response_status_code = Column(Integer, nullable=True)
    response_body = Column(Text, nullable=True)
    success = Column(Boolean, default=False)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "job_id": self.job_id,
            "event_type": self.event_type,
            "target_url": self.target_url,
            "attempt": self.attempt,
            "response_status_code": self.response_status_code,
            "success": self.success,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ---------------------------------------------------------------------------
# Database Initialization & Session Dependency
# ---------------------------------------------------------------------------

def init_db(custom_engine=None) -> None:
    """Initialize all database tables."""
    eng = custom_engine or engine
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=eng)
    logger.info("Database schema initialized successfully.")


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_session() -> Session:
    """Get standalone database session for background tasks."""
    return SessionLocal()


# ---------------------------------------------------------------------------
# Document Storage Manager
# ---------------------------------------------------------------------------

class DocumentStorageManager:
    """Manages secure file persistence and page rasterization for HITL web viewing."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or STORAGE_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sanitize_doc_id(doc_id: str) -> str:
        """Sanitize document ID to prevent path traversal attacks."""
        # Strip directory components and dangerous characters
        safe_id = Path(doc_id).name.replace("..", "").replace("/", "").replace("\\", "")
        if not safe_id:
            raise ValueError(f"Invalid document ID after sanitization: {doc_id!r}")
        return safe_id

    def save_file(self, doc_id: str, original_filename: str, source_path: Path) -> tuple[Path, str, int]:
        """Copy uploaded invoice file to persistent storage directory."""
        safe_id = self._sanitize_doc_id(doc_id)
        suffix = source_path.suffix.lower()
        safe_name = f"{safe_id}{suffix}"
        target_path = self.base_dir / safe_name
        # Verify target is within storage directory
        if not target_path.resolve().is_relative_to(self.base_dir.resolve()):
            raise ValueError(f"Path traversal detected: {target_path}")
        shutil.copy2(source_path, target_path)

        # Calculate SHA-256 and size
        hasher = hashlib.sha256()
        with target_path.open("rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        file_hash = hasher.hexdigest()
        file_size = target_path.stat().st_size

        return target_path, file_hash, file_size

    def get_file_path(self, doc_id: str) -> Optional[Path]:
        """Find stored file for document ID."""
        safe_id = self._sanitize_doc_id(doc_id)
        for path in self.base_dir.glob(f"{safe_id}.*"):
            if path.is_file():
                return path
        return None

    def render_page_png(self, doc_id: str, page_number: int = 1, dpi: int = 150) -> Optional[bytes]:
        """Render a specific document page as high-resolution PNG for web canvas."""
        file_path = self.get_file_path(doc_id)
        if not file_path or not file_path.exists():
            return None

        suffix = file_path.suffix.lower()
        if suffix == ".pdf":
            try:
                doc = fitz.open(str(file_path))
                if page_number < 1 or page_number > len(doc):
                    doc.close()
                    return None
                page = doc[page_number - 1]
                zoom = dpi / 72.0
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                png_bytes = pix.tobytes("png")
                doc.close()
                return png_bytes
            except Exception as exc:
                logger.error("Error rendering PDF page %d for %s: %s", page_number, doc_id, exc)
                return None
        elif suffix in {".png", ".jpg", ".jpeg"}:
            try:
                img = Image.open(file_path)
                out = io.BytesIO()
                img.save(out, format="PNG")
                return out.getvalue()
            except Exception as exc:
                logger.error("Error loading image for %s: %s", doc_id, exc)
                return None

        return None


storage_manager = DocumentStorageManager()


# ---------------------------------------------------------------------------
# Database Helper & CRUD Functions
# ---------------------------------------------------------------------------

def save_document_to_db(
    db: Session,
    doc_id: str,
    file_name: str,
    source_path: Optional[Path],
    ocr_result: dict[str, Any],
    processing_time: float,
    webhook_url: Optional[str] = None,
) -> DocumentRecord:
    """Persist processed invoice into database with full audit entry."""
    file_path_str = None
    file_hash = None
    file_size = 0

    existing = db.query(DocumentRecord).filter(DocumentRecord.id == doc_id).first()
    if existing:
        # Update existing record in-place instead of deleting to preserve audit trail
        # The fields will be updated below after computing new values
        pass

    if source_path and source_path.exists():
        stored_path, file_hash, file_size = storage_manager.save_file(doc_id, file_name, source_path)
        file_path_str = str(stored_path)

    # Extract metadata fields for indexing
    meta = ocr_result.get("normalized_data", {}).get("invoice_metadata", {})
    supplier = ocr_result.get("normalized_data", {}).get("supplier", {})
    recipient = ocr_result.get("normalized_data", {}).get("recipient", {})
    fin = ocr_result.get("normalized_data", {}).get("financial_summary", {})
    val = ocr_result.get("validation_results", {})

    is_valid = bool(val.get("is_valid", True))
    error_count = len(val.get("errors", []))
    warning_count = len(val.get("warnings", []))

    status = "completed" if (is_valid and error_count == 0) else "needs_review"

    # Extract monetary floats
    def _to_float(v: Any) -> float:
        if v is None:
            return 0.0
        if isinstance(v, dict):
            v = v.get("amount", 0.0)
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    record = DocumentRecord(
        id=doc_id,
        file_name=file_name,
        file_path=file_path_str,
        file_hash=file_hash,
        file_size_bytes=file_size,
        processing_time_sec=round(processing_time, 3),
        status=status,
        is_valid=is_valid,
        error_count=error_count,
        warning_count=warning_count,
        invoice_number=meta.get("invoice_number"),
        date_issued=meta.get("date_issued"),
        date_tax_event=meta.get("date_tax_event"),
        currency=meta.get("currency") or "BGN",
        supplier_name=supplier.get("name"),
        supplier_eik=supplier.get("eik"),
        supplier_vat=supplier.get("vat_number"),
        supplier_address=supplier.get("address"),
        recipient_name=recipient.get("name"),
        recipient_eik=recipient.get("eik"),
        recipient_vat=recipient.get("vat_number"),
        recipient_address=recipient.get("address"),
        tax_base=_to_float(fin.get("tax_base")),
        vat_amount=_to_float(fin.get("vat_amount")),
        total_amount=_to_float(fin.get("total_amount_due")),
        ocr_result_json=json.dumps(ocr_result, ensure_ascii=False),
        webhook_url=webhook_url,
        webhook_status="pending" if webhook_url else "none",
    )

    if existing:
        # Update existing record fields in-place to preserve audit trail history
        for attr in [
            "file_name", "file_path", "file_hash", "file_size_bytes",
            "processing_time_sec", "status", "is_valid", "error_count", "warning_count",
            "invoice_number", "date_issued", "date_tax_event", "currency",
            "supplier_name", "supplier_eik", "supplier_vat", "supplier_address",
            "recipient_name", "recipient_eik", "recipient_vat", "recipient_address",
            "tax_base", "vat_amount", "total_amount",
            "ocr_result_json", "webhook_url", "webhook_status",
        ]:
            setattr(existing, attr, getattr(record, attr))
        existing.updated_at = datetime.now(timezone.utc)
        existing.corrected_data_json = None  # Reset corrections on re-process
        db.add(existing)
        record = existing  # Use existing record for audit
    else:
        db.add(record)

    # Add audit trail entry
    audit_action = "re_processed" if existing else "uploaded_and_processed"
    audit = AuditTrailRecord(
        document_id=doc_id,
        actor="system:ocr_pipeline",
        action=audit_action,
        details_json=json.dumps({
            "file_name": file_name,
            "processing_time_sec": record.processing_time_sec,
            "is_valid": is_valid,
            "error_count": error_count,
            "warning_count": warning_count,
        }, ensure_ascii=False),
    )
    db.add(audit)
    db.commit()
    db.refresh(record)
    return record


def add_audit_entry(
    db: Session,
    document_id: str,
    action: str,
    actor: str = "accountant",
    field_name: Optional[str] = None,
    old_value: Optional[Any] = None,
    new_value: Optional[Any] = None,
    details: Optional[dict[str, Any]] = None,
) -> AuditTrailRecord:
    """Log an audit trail event for a document."""
    entry = AuditTrailRecord(
        document_id=document_id,
        actor=actor,
        action=action,
        field_name=field_name,
        old_value=str(old_value) if old_value is not None else None,
        new_value=str(new_value) if new_value is not None else None,
        details_json=json.dumps(details, ensure_ascii=False) if details else None,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def update_document_corrections(
    db: Session,
    doc_id: str,
    corrections: dict[str, Any],
    actor: str = "accountant",
) -> Optional[DocumentRecord]:
    """Apply manual field edits to an invoice document and log audit diff."""
    record = db.query(DocumentRecord).filter(DocumentRecord.id == doc_id).first()
    if not record:
        return None

    # Load existing active data to compute diff
    old_data = json.loads(record.corrected_data_json) if record.corrected_data_json else json.loads(record.ocr_result_json or "{}")
    old_norm = old_data.get("normalized_data", {})

    diffs: list[dict[str, Any]] = []

    # Update metadata fields if present
    if "invoice_number" in corrections and corrections["invoice_number"] != record.invoice_number:
        diffs.append({"field": "invoice_number", "old": record.invoice_number, "new": corrections["invoice_number"]})
        record.invoice_number = corrections["invoice_number"]

    if "date_issued" in corrections and corrections["date_issued"] != record.date_issued:
        diffs.append({"field": "date_issued", "old": record.date_issued, "new": corrections["date_issued"]})
        record.date_issued = corrections["date_issued"]

    if "date_tax_event" in corrections and corrections["date_tax_event"] != record.date_tax_event:
        diffs.append({"field": "date_tax_event", "old": record.date_tax_event, "new": corrections["date_tax_event"]})
        record.date_tax_event = corrections["date_tax_event"]

    if "currency" in corrections and corrections["currency"] != record.currency:
        diffs.append({"field": "currency", "old": record.currency, "new": corrections["currency"]})
        record.currency = corrections["currency"]

    # Supplier
    if "supplier_name" in corrections and corrections["supplier_name"] != record.supplier_name:
        diffs.append({"field": "supplier_name", "old": record.supplier_name, "new": corrections["supplier_name"]})
        record.supplier_name = corrections["supplier_name"]

    if "supplier_eik" in corrections and corrections["supplier_eik"] != record.supplier_eik:
        diffs.append({"field": "supplier_eik", "old": record.supplier_eik, "new": corrections["supplier_eik"]})
        record.supplier_eik = corrections["supplier_eik"]

    if "supplier_vat" in corrections and corrections["supplier_vat"] != record.supplier_vat:
        diffs.append({"field": "supplier_vat", "old": record.supplier_vat, "new": corrections["supplier_vat"]})
        record.supplier_vat = corrections["supplier_vat"]

    # Recipient
    if "recipient_name" in corrections and corrections["recipient_name"] != record.recipient_name:
        diffs.append({"field": "recipient_name", "old": record.recipient_name, "new": corrections["recipient_name"]})
        record.recipient_name = corrections["recipient_name"]

    if "recipient_eik" in corrections and corrections["recipient_eik"] != record.recipient_eik:
        diffs.append({"field": "recipient_eik", "old": record.recipient_eik, "new": corrections["recipient_eik"]})
        record.recipient_eik = corrections["recipient_eik"]

    if "recipient_vat" in corrections and corrections["recipient_vat"] != record.recipient_vat:
        diffs.append({"field": "recipient_vat", "old": record.recipient_vat, "new": corrections["recipient_vat"]})
        record.recipient_vat = corrections["recipient_vat"]

    if "recipient_address" in corrections and corrections["recipient_address"] != record.recipient_address:
        diffs.append({"field": "recipient_address", "old": record.recipient_address, "new": corrections["recipient_address"]})
        record.recipient_address = corrections["recipient_address"]

    # Financials
    def _val(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    if "tax_base" in corrections and abs(_val(corrections["tax_base"]) - record.tax_base) > 0.001:
        diffs.append({"field": "tax_base", "old": record.tax_base, "new": _val(corrections["tax_base"])})
        record.tax_base = _val(corrections["tax_base"])

    if "vat_amount" in corrections and abs(_val(corrections["vat_amount"]) - record.vat_amount) > 0.001:
        diffs.append({"field": "vat_amount", "old": record.vat_amount, "new": _val(corrections["vat_amount"])})
        record.vat_amount = _val(corrections["vat_amount"])

    if "total_amount" in corrections and abs(_val(corrections["total_amount"]) - record.total_amount) > 0.001:
        diffs.append({"field": "total_amount", "old": record.total_amount, "new": _val(corrections["total_amount"])})
        record.total_amount = _val(corrections["total_amount"])

    # Update or merge full corrected data payload
    merged_data = json.loads(record.corrected_data_json) if record.corrected_data_json else json.loads(record.ocr_result_json or "{}")
    if "normalized_data" not in merged_data:
        merged_data["normalized_data"] = {}

    norm = merged_data["normalized_data"]
    if "invoice_metadata" not in norm:
        norm["invoice_metadata"] = {}
    if "supplier" not in norm:
        norm["supplier"] = {}
    if "recipient" not in norm:
        norm["recipient"] = {}
    if "financial_summary" not in norm:
        norm["financial_summary"] = {}

    if record.invoice_number:
        norm["invoice_metadata"]["invoice_number"] = record.invoice_number
    if record.date_issued:
        norm["invoice_metadata"]["date_issued"] = record.date_issued
    if record.date_tax_event:
        norm["invoice_metadata"]["date_tax_event"] = record.date_tax_event
    if record.currency:
        norm["invoice_metadata"]["currency"] = record.currency

    if record.supplier_name:
        norm["supplier"]["name"] = record.supplier_name
    if record.supplier_eik:
        norm["supplier"]["eik"] = record.supplier_eik
    if record.supplier_vat:
        norm["supplier"]["vat_number"] = record.supplier_vat

    if record.recipient_name:
        norm["recipient"]["name"] = record.recipient_name
    if record.recipient_eik:
        norm["recipient"]["eik"] = record.recipient_eik
    if record.recipient_vat:
        norm["recipient"]["vat_number"] = record.recipient_vat
    if record.recipient_address:
        norm["recipient"]["address"] = record.recipient_address

    norm["financial_summary"]["tax_base"] = {"amount": str(record.tax_base), "currency": record.currency}
    norm["financial_summary"]["vat_amount"] = {"amount": str(record.vat_amount), "currency": record.currency}
    norm["financial_summary"]["total_amount_due"] = {"amount": str(record.total_amount), "currency": record.currency}

    if "line_items" in corrections:
        diffs.append({"field": "line_items", "old": "...", "new": f"{len(corrections['line_items'])} items"})
        norm["line_items"] = corrections["line_items"]

    record.corrected_data_json = json.dumps(merged_data, ensure_ascii=False)
    record.updated_at = datetime.now(timezone.utc)

    # Save audit logs for all diffs
    for diff in diffs:
        add_audit_entry(
            db=db,
            document_id=doc_id,
            action="field_corrected",
            actor=actor,
            field_name=diff["field"],
            old_value=diff["old"],
            new_value=diff["new"],
        )

    db.commit()
    db.refresh(record)
    return record


def approve_document_in_db(
    db: Session,
    doc_id: str,
    actor: str = "accountant",
    webhook_url: Optional[str] = None,
) -> Optional[DocumentRecord]:
    """Mark document as approved by accountant, ready for ERP export and webhook dispatch."""
    record = db.query(DocumentRecord).filter(DocumentRecord.id == doc_id).first()
    if not record:
        return None

    record.status = "approved"
    record.updated_at = datetime.now(timezone.utc)
    if webhook_url:
        record.webhook_url = webhook_url

    add_audit_entry(
        db=db,
        document_id=doc_id,
        action="approved_by_accountant",
        actor=actor,
        details={"status": "approved", "webhook_url": record.webhook_url},
    )

    db.commit()
    db.refresh(record)
    return record

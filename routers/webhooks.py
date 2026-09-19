"""FastAPI Router for Outbound Webhooks and Mock ERP Receiver."""

from datetime import datetime
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import WebhookLogRecord, get_db
from routers.common import BG_TZ
from webhooks import send_webhook_async

logger = logging.getLogger("invoice_ocr_api")
router = APIRouter()

MOCK_ERP_RECEIVED: list[dict[str, Any]] = []


class WebhookTestRequest(BaseModel):
    target_url: str = Field(..., description="Target URL for ERP webhook endpoint")
    secret: Optional[str] = Field(default=None, description="HMAC secret key")


@router.post(
    "/api/v1/webhooks/test",
    tags=["Webhooks"],
    summary="Test Outbound ERP Webhook Endpoint",
)
async def test_webhook_endpoint(request: WebhookTestRequest):
    """Send a diagnostic ping payload to test connectivity to an external ERP webhook URL."""
    test_payload = {
        "event": "test.ping",
        "timestamp": datetime.now(BG_TZ).isoformat(),
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


@router.get(
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


@router.post(
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


@router.get(
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

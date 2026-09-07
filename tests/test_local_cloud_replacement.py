"""Automated Tests for 100% Local Drop-In Compatibility & Cloud Replacement.

Verifies that all former paid/cloud OCR and AI services (Claude Vision, Vertex AI,
external company search) are seamlessly replaced by local on-premise endpoints.
"""

from __future__ import annotations

import base64
import cv2
from fastapi.testclient import TestClient
import numpy as np
import pytest

from api_server import app
from contractor_verification import ContractorVerifier, CompanyStatus, VatRegistrationStatus
from invoice_core.local_llm import (
    is_ollama_available,
    get_available_local_models,
    query_local_llm,
    correct_ocr_text_with_local_ai,
)
from tests.e2e.test_helpers import create_synthetic_test_image


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def sample_invoice_b64():
    """Generate a synthetic invoice image and return as base64 string."""
    img = create_synthetic_test_image(
        "ОРИГИНАЛ ФАКТУРА № 0000123456\n"
        "Дата: 16.08.2026\n"
        "Доставчик: МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД\n"
        "ЕИК: 121644736  ИН по ЗДДС: BG121644736\n"
        "Получател: ТЕСТ КЛИЕНТ ЕООД ЕИК: 208380135\n"
        "1. Кафе Лаваца 1кг 2 бр. х 25.00 = 50.00\n"
        "Данъчна основа: 50.00 лв.\n"
        "ДДС 20%: 10.00 лв.\n"
        "Сума за плащане: 60.00 лв."
    )
    _, buf = cv2.imencode(".png", img)
    return base64.b64encode(buf.tobytes()).decode("utf-8")


# ============================================================================
# 1. Document Scanner Drop-In Endpoints (Replacing Claude Vision)
# ============================================================================

def test_classify_document_endpoint(client, sample_invoice_b64):
    """Verify 100% local document classification without calling Anthropic Claude."""
    payload = {
        "image": sample_invoice_b64,
        "imageType": "image/png",
    }
    resp = client.post("/api/document-scanner/classify", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["documentType"] == "invoice"
    assert data["confidence"] == "high"
    assert data["engine"] == "local-rule-classifier"


def test_extract_invoice_endpoint(client, sample_invoice_b64):
    """Verify SmartScan-compatible invoice extraction returns complete camelCase schema."""
    payload = {
        "image": sample_invoice_b64,
        "imageType": "image/png",
    }
    resp = client.post("/api/document-scanner/extract-invoice", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "data" in data

    inv_data = data["data"]
    # Check all fields expected by SmartScan / Astro frontend
    assert "invoiceNumber" in inv_data
    assert "invoiceDate" in inv_data
    assert "vendorName" in inv_data
    assert "vendorTaxId" in inv_data
    assert "vendorVatId" in inv_data
    assert "customerName" in inv_data
    assert "customerTaxId" in inv_data
    assert "items" in inv_data
    assert isinstance(inv_data["items"], list)
    assert "subtotal" in inv_data
    assert "taxAmount" in inv_data
    assert "totalAmount" in inv_data
    assert "currency" in inv_data
    assert "rawText" in inv_data
    assert "needsValidation" in data


def test_extract_invoice_batch_endpoint(client, sample_invoice_b64):
    """Verify local batch extraction without external cloud batch services."""
    payload = {
        "invoices": [
            {"id": "inv_1", "image": sample_invoice_b64},
            {"id": "inv_2", "image": sample_invoice_b64},
        ]
    }
    resp = client.post("/api/document-scanner/extract-invoices-batch", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["totalCount"] == 2
    assert data["successCount"] == 2
    assert data["failedCount"] == 0
    assert len(data["results"]) == 2
    assert data["results"][0]["id"] == "inv_1"
    assert data["results"][1]["id"] == "inv_2"


# ============================================================================
# 2. Business Search Endpoint (Replacing CompanyBook API)
# ============================================================================

def test_business_search_by_eik_profile(client):
    """Find known vendor from local YAML profile (Metro Cash & Carry)."""
    resp = client.get("/api/businesses/search?q=121644736")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert len(data["results"]) > 0
    match = next((r for r in data["results"] if r["eik"] == "121644736"), None)
    assert match is not None
    assert "МЕТРО" in match["name"].upper()
    assert match["vat_number"] == "BG121644736"


def test_business_search_by_name(client):
    """Find known vendor by partial name substring."""
    resp = client.get("/api/businesses/search?q=топливо")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert any("ТОПЛИВО" in r["name"].upper() for r in data["results"])


def test_business_search_modulo11_synthetic(client):
    """Verify valid Bulgarian EIK checksum generates verified candidate locally."""
    # 208380135 has valid Bulgarian Modulo 11 check digit
    resp = client.get("/api/businesses/search?q=208380135")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert any(r["eik"] == "208380135" for r in data["results"])


# ============================================================================
# 3. Tesseract Local Router Endpoints
# ============================================================================

def test_tesseract_raw_ocr(client, sample_invoice_b64):
    """Verify raw Tesseract OCR endpoint."""
    resp = client.post("/api/tesseract/ocr", json={"image": sample_invoice_b64, "lang": "bul+eng"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "text" in data["data"]
    assert data["data"]["engine"] == "tesseract-v5-local"


def test_tesseract_ocr_data(client, sample_invoice_b64):
    """Verify word-level OCR tokens with bounding boxes."""
    resp = client.post("/api/tesseract/ocr-data", json={"image": sample_invoice_b64})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    words = data["data"]["words"]
    assert len(words) > 0
    assert "bbox" in words[0]
    assert "confidence" in words[0]


def test_tesseract_extract_amounts(client, sample_invoice_b64):
    """Verify extracting amounts using numeric OCR pass."""
    resp = client.post("/api/tesseract/extract-amounts", json={"image": sample_invoice_b64})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    amounts = data["data"]["amounts"]
    assert any(a["parsed"] in (50.0, 10.0, 60.0) for a in amounts)


def test_tesseract_extract_invoice_alias(client, sample_invoice_b64):
    """Verify /api/tesseract/extract-invoice compatibility alias."""
    resp = client.post("/api/tesseract/extract-invoice", json={"image": sample_invoice_b64})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "ocr" in data
    assert data["ocr"]["engine"] == "tesseract-v5-local"


# ============================================================================
# 4. Contractor Offline-First Verification
# ============================================================================

@pytest.mark.anyio
async def test_contractor_offline_verification():
    """Verify ContractorVerifier operates 100% offline using vendor profiles."""
    verifier = ContractorVerifier(offline_mode=True)
    res = await verifier.verify_async("121644736", country_code="BG", date_tax_event="2026-08-16")
    assert res.identifier == "121644736"
    assert res.legal_status == CompanyStatus.ACTIVE
    assert res.vat_status == VatRegistrationStatus.REGISTERED
    assert res.is_valid_for_tax_credit is True
    assert res.source in ("VENDOR_PROFILE", "OFFLINE_FALLBACK", "MOCK_REGISTRY")


# ============================================================================
# 5. Local LLM Graceful Fallback
# ============================================================================

def test_local_llm_graceful_fallback_when_daemon_absent():
    """Verify query_local_llm falls back without crashing when Ollama is not running."""
    # Use unassigned local port to simulate stopped Ollama daemon
    fake_host = "http://127.0.0.1:59999"
    assert is_ollama_available(fake_host, timeout=0.2) is False
    assert get_available_local_models(fake_host, timeout=0.2) == []

    # Test correction fallback
    raw_text = "ФАКТУРА № 123 СУМА 100.00"
    corrected, is_ai = correct_ocr_text_with_local_ai(raw_text, host=fake_host)
    assert corrected == raw_text
    assert is_ai is False

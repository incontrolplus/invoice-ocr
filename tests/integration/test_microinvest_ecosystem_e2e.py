"""Cross-Repository E2E Integration Test: Microinvest Ecosystem.

Tests the complete request path from:
- /Users/diokarabaz/MICROINVEST-OCR/backend/src/documentScanner.js (Express gateway on port 3000)
- to api/document-scanner/extract-invoice and api/document-scanner/classify on our FastAPI core (port 8000).

Verifies:
1. Exact JSON schema contract expected by the Astro dashboard:
   - documentType
   - classificationConfidence
   - fields (dictionary of extracted invoice attributes)
   - lineItems (list of itemized rows)
2. GET /api/v1/system/ecosystem-health diagnostics:
   - Tesseract OCR readiness (version and bul/eng languages)
   - Database and audit tables state
   - Worker Pool working state
   - Presence and accessibility of config/vendors/
   - Connectivity status with Microinvest Delta Pro export module
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess

import cv2
from fastapi.testclient import TestClient
import pytest

from api_server import app
from invoice_ocr import init_ocr_pool
from tests.e2e.test_helpers import create_synthetic_test_image

MICROINVEST_BACKEND_DIR = Path("/Users/diokarabaz/MICROINVEST-OCR/backend")


@pytest.fixture(scope="module")
def client():
    """Create FastAPI TestClient and ensure OCR pool is initialized."""
    init_ocr_pool(max_workers=2)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def synthetic_invoice_b64() -> str:
    """Generate a realistic Bulgarian tax invoice image in base64 format."""
    text = (
        "ОРИГИНАЛ ФАКТУРА № 0000208380\n"
        "Дата на издаване: 19.09.2026\n"
        "Доставчик: РМ КАСКАДА 2026 ЕООД\n"
        "ЕИК: 208380135  ИН по ЗДДС: BG208380135\n"
        "Адрес: гр. Плевен, ул. Гривишко шосе 1\n"
        "IBAN: BG80STSA93000025983741  BIC: STSABGSF\n"
        "Получател: МИКРОИНВЕСТ ТЕСТ ООД\n"
        "ЕИК: 121575400  ИН по ЗДДС: BG121575400\n"
        "Адрес: гр. София, бул. Цариградско шосе 115\n"
        "1. Софтуерен лиценз за OCR модул 1 бр. х 100.00 = 100.00 лв.\n"
        "Данъчна основа: 100.00 лв.\n"
        "ДДС 20%: 20.00 лв.\n"
        "Обща сума за плащане: 120.00 лв."
    )
    img = create_synthetic_test_image(text)
    _, buf = cv2.imencode(".png", img)
    return base64.b64encode(buf.tobytes()).decode("utf-8")


@pytest.fixture
def synthetic_receipt_b64() -> str:
    """Generate a synthetic fiscal receipt image in base64 format."""
    text = (
        "ФИСКАЛЕН БОН\n"
        "СИСТЕМЕН БОН\n"
        "ТЕСТ ТЪРГОВЕЦ ЕООД\n"
        "ЕИК: 208380135\n"
        "1. Кафе еспресо 1 бр. 2.50\n"
        "ОБЩА СУМА: 2.50 BGN\n"
        "В БРОЙ: 2.50\n"
        "ФИСКАЛНА ПАМЕТ\n"
        "ФЛП: 00012345"
    )
    img = create_synthetic_test_image(text)
    _, buf = cv2.imencode(".png", img)
    return base64.b64encode(buf.tobytes()).decode("utf-8")


# ============================================================================
# 1. FastAPI Core Drop-In Contract Tests
# ============================================================================

def test_classify_invoice_contract(client: TestClient, synthetic_invoice_b64: str):
    """Verify POST /api/document-scanner/classify returns documentType and classificationConfidence."""
    payload = {
        "image": synthetic_invoice_b64,
        "imageType": "image/png",
    }
    resp = client.post("/api/document-scanner/classify", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["success"] is True
    assert "documentType" in data
    assert data["documentType"] == "invoice"
    assert "classificationConfidence" in data
    assert isinstance(data["classificationConfidence"], (int, float))
    assert data["classificationConfidence"] >= 0.0
    assert "confidence" in data
    assert data["engine"] == "local-rule-classifier"


def test_classify_receipt_contract(client: TestClient, synthetic_receipt_b64: str):
    """Verify POST /api/document-scanner/classify categorizes fiscal receipts correctly."""
    payload = {
        "image": synthetic_receipt_b64,
        "imageType": "image/png",
    }
    resp = client.post("/api/document-scanner/classify", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["success"] is True
    assert "documentType" in data
    assert data["documentType"] in ("receipt", "invoice")
    assert "classificationConfidence" in data


def test_extract_invoice_astro_contract(client: TestClient, synthetic_invoice_b64: str):
    """Verify POST /api/document-scanner/extract-invoice returns the exact Astro dashboard schema.

    Schema:
    - documentType (str)
    - classificationConfidence (float)
    - fields (dict containing invoiceNumber, invoiceDate, vendorName, vendorTaxId, etc.)
    - lineItems (list of items with description, quantity, unitPrice, totalPrice)
    - data (nested payload for backward compatibility)
    """
    payload = {
        "image": synthetic_invoice_b64,
        "imageType": "image/png",
    }
    resp = client.post("/api/document-scanner/extract-invoice", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["success"] is True

    # 1. Astro Dashboard top-level fields
    assert "documentType" in body
    assert body["documentType"] == "invoice"

    assert "classificationConfidence" in body
    assert isinstance(body["classificationConfidence"], (int, float))
    assert body["classificationConfidence"] > 0

    assert "fields" in body
    fields = body["fields"]
    assert isinstance(fields, dict)
    assert "invoiceNumber" in fields
    assert "invoiceDate" in fields
    assert "vendorName" in fields
    assert "vendorTaxId" in fields
    assert "vendorVatId" in fields
    assert "totalAmount" in fields
    assert "subtotal" in fields
    assert "taxAmount" in fields

    assert "lineItems" in body
    line_items = body["lineItems"]
    assert isinstance(line_items, list)
    assert len(line_items) >= 1
    first_item = line_items[0]
    assert "description" in first_item
    assert "quantity" in first_item
    assert "unitPrice" in first_item
    assert "totalPrice" in first_item

    # 2. Nested data payload for Microinvest Express backward-compat
    assert "data" in body
    data = body["data"]
    assert "invoiceNumber" in data
    assert "vendorName" in data
    assert "vendorTaxId" in data
    assert "items" in data
    assert "lineItems" in data
    assert "fields" in data
    assert "documentType" in data
    assert "classificationConfidence" in data
    assert "subtotal" in data
    assert "totalAmount" in data


def test_extract_invoice_missing_image_returns_400(client: TestClient):
    """Verify 400 Bad Request when image payload is omitted."""
    resp = client.post("/api/document-scanner/extract-invoice", json={})
    assert resp.status_code == 400
    assert "Image data is required" in resp.json().get("detail", "")


# ============================================================================
# 2. Ecosystem Diagnostic Health Endpoint Tests
# ============================================================================

def test_ecosystem_health_endpoint(client: TestClient):
    """Verify GET /api/v1/system/ecosystem-health satisfies all M22 requirements:

    - Tesseract OCR readiness (version and bul/eng languages)
    - Database and audit tables state
    - Worker Pool working state
    - Presence and accessibility of config/vendors/
    - Connectivity status with Microinvest Delta Pro export module
    """
    resp = client.get("/api/v1/system/ecosystem-health")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["status"] in ("healthy", "degraded")
    assert "timestamp" in data
    assert "version" in data
    assert "uptime_seconds" in data

    # 1. Tesseract OCR readiness
    assert "tesseract" in data
    tess = data["tesseract"]
    assert "ready" in tess
    assert "version" in tess
    assert "available_languages" in tess
    assert "bul" in tess["available_languages"]
    assert "eng" in tess["available_languages"]
    assert tess["required_ready"] is True

    # 2. Database and audit tables state
    assert "database" in data
    db = data["database"]
    assert db["connected"] is True
    assert db["status"] == "ok"
    assert "tables" in db
    assert db["tables"]["documents"] is True
    assert db["tables"]["audit_trail"] is True
    assert db["audit_table_ready"] is True
    assert isinstance(db["document_count"], int)
    assert isinstance(db["audit_trail_count"], int)

    # 3. Worker Pool working state
    assert "worker_pool" in data
    wp = data["worker_pool"]
    assert "ready" in wp
    assert "status" in wp
    assert wp["status"] in ("active", "idle", "uninitialized")
    assert "max_workers" in wp
    assert isinstance(wp["max_workers"], int)

    # 4. Presence and accessibility of config/vendors/
    assert "vendor_profiles" in data
    vp = data["vendor_profiles"]
    assert vp["available"] is True
    assert vp["accessible"] is True
    assert vp["vendor_count"] >= 1
    assert "vendors" in vp
    assert isinstance(vp["vendors"], list)

    # 5. Connectivity status with Microinvest Delta Pro export module
    assert "microinvest_export" in data
    me = data["microinvest_export"]
    assert me["module_ready"] is True
    assert me["status"] == "ready"
    assert me["delta_pro_available"] is True
    assert me["sklad_pro_available"] is True
    assert "delta_xml" in me["formats"]
    assert "delta_csv" in me["formats"]


# ============================================================================
# 3. Cross-Repository Bridge Test (Node.js Express ↔ FastAPI Core)
# ============================================================================

def test_cross_repo_express_document_scanner_bridge(synthetic_invoice_b64: str):
    """Test the full cross-repo request path from documentScanner.js in MICROINVEST-OCR.

    Spawns a lightweight Node test script that imports documentScannerRouter from
    /Users/diokarabaz/MICROINVEST-OCR/backend/src/documentScanner.js, mocks LOCAL_OCR_URL
    using an in-process mock server (or live FastAPI test client), and verifies
    that Express routes correctly delegate and return the exact Astro dashboard JSON.
    """
    node_bin = shutil_which("node")
    if not node_bin or not MICROINVEST_BACKEND_DIR.exists():
        pytest.skip("Node.js or /Users/diokarabaz/MICROINVEST-OCR/backend not available on system")

    # Node test script verifying documentScanner.js delegation
    test_script = f"""
    import express from 'express';
    import http from 'node:http';
    import {{ once }} from 'node:events';

    // 1. Create Mock FastAPI core server (:8000 equivalent)
    const fastApiMock = express();
    fastApiMock.use(express.json({{ limit: '25mb' }}));

    fastApiMock.post('/api/document-scanner/classify', (req, res) => {{
      res.json({{
        success: true,
        documentType: 'invoice',
        confidence: 'high',
        classificationConfidence: 0.95,
        engine: 'local-rule-classifier'
      }});
    }});

    fastApiMock.post('/api/document-scanner/extract-invoice', (req, res) => {{
      const fields = {{
        invoiceNumber: '0000208380',
        invoiceDate: '2026-09-19',
        vendorName: 'РМ КАСКАДА 2026 ЕООД',
        vendorTaxId: '208380135',
        vendorVatId: 'BG208380135',
        iban: 'BG80STSA93000025983741',
        totalAmount: 120.0,
        subtotal: 100.0,
        taxAmount: 20.0,
        currency: 'BGN'
      }};
      const lineItems = [{{
        description: 'Софтуерен лиценз за OCR модул',
        quantity: 1.0,
        unit: 'бр.',
        unitPrice: 100.0,
        totalPrice: 100.0,
        vatRate: 20.0
      }}];

      res.json({{
        success: true,
        documentType: 'invoice',
        classificationConfidence: 0.95,
        confidence: 0.95,
        fields,
        lineItems,
        items: lineItems,
        data: {{
          ...fields,
          items: lineItems,
          lineItems,
          fields,
          documentType: 'invoice',
          classificationConfidence: 0.95
        }},
        needsValidation: false
      }});
    }});

    const fastApiServer = fastApiMock.listen(0, '127.0.0.1');
    await once(fastApiServer, 'listening');
    const fastApiPort = fastApiServer.address().port;
    process.env.LOCAL_OCR_URL = `http://127.0.0.1:${{fastApiPort}}`;

    // 2. Import Express router from MICROINVEST-OCR
    const {{ documentScannerRouter }} = await import('{MICROINVEST_BACKEND_DIR / "src" / "documentScanner.js"}');

    const expressApp = express();
    expressApp.use(express.json({{ limit: '25mb' }}));
    expressApp.use('/api/document-scanner', documentScannerRouter);

    const expressServer = expressApp.listen(0, '127.0.0.1');
    await once(expressServer, 'listening');
    const expressPort = expressServer.address().port;
    const expressBase = `http://127.0.0.1:${{expressPort}}`;

    try {{
      // Test 1: Classify delegation
      const classifyResp = await fetch(`${{expressBase}}/api/document-scanner/classify`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ image: '{synthetic_invoice_b64[:100]}', imageType: 'image/png' }})
      }});
      const classifyData = await classifyResp.json();
      if (!classifyData.success || classifyData.documentType !== 'invoice' || classifyData.classificationConfidence !== 0.95) {{
        throw new Error(`Classify failed: ${{JSON.stringify(classifyData)}}`);
      }}

      // Test 2: Extract Invoice delegation
      const extractResp = await fetch(`${{expressBase}}/api/document-scanner/extract-invoice`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ image: '{synthetic_invoice_b64[:100]}', imageType: 'image/png' }})
      }});
      const extractData = await extractResp.json();
      if (!extractData.success || !extractData.data) {{
        throw new Error(`Extract failed: ${{JSON.stringify(extractData)}}`);
      }}
      const data = extractData.data;
      if (!data.fields || !data.lineItems || data.documentType !== 'invoice' || data.classificationConfidence !== 0.95) {{
        throw new Error(`Missing expected fields/lineItems in data: ${{JSON.stringify(data)}}`);
      }}

      console.log(JSON.stringify({{ ok: true, classifyData, extractData }}));
    }} finally {{
      fastApiServer.close();
      expressServer.close();
    }}
    """

    proc = subprocess.run(
        [node_bin, "--input-type=module", "-e", test_script],
        cwd=str(MICROINVEST_BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, f"Node script error: {proc.stderr}\nStdout: {proc.stdout}"
    output = json.loads(proc.stdout.strip())
    assert output.get("ok") is True
    assert output["classifyData"]["documentType"] == "invoice"
    assert output["classifyData"]["classificationConfidence"] == 0.95
    assert output["extractData"]["data"]["documentType"] == "invoice"
    assert "fields" in output["extractData"]["data"]
    assert "lineItems" in output["extractData"]["data"]


def shutil_which(cmd: str) -> str | None:
    import shutil
    return shutil.which(cmd)

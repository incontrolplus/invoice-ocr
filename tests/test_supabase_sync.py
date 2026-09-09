"""Unit and Integration Tests for Supabase Synchronization Layer."""

from __future__ import annotations

import json
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch

from supabase_sync import (
    build_supabase_invoice_payload,
    is_supabase_configured,
    sync_invoice_to_supabase,
    _to_uuid,
    _clean_date,
    _to_float,
)

SAMPLE_OCR_RESULT = {
    "raw_text": "ФАКТУРА № 1000000001\nДоставчик: ТЕСТ ИНВЕСТ ЕООД\nЕИК: 123456789\nПолучател: КЛИЕНТ ЕООД\nЕИК: 987654321\nДанъчна основа: 500.00\nДДС 20%: 100.00\nОбщо: 600.00",
    "overall_confidence": 98.5,
    "normalized_data": {
        "invoice_metadata": {
            "invoice_number": "1000000001",
            "date_issued": "08.09.2026",
            "date_tax_event": "08.09.2026",
            "due_date": "20.09.2026",
            "place_of_issuance": "София",
            "currency": "BGN",
            "exchange_rate": 1.0,
        },
        "supplier": {
            "name": "ТЕСТ ИНВЕСТ ЕООД",
            "eik": "123456789",
            "vat_number": "BG123456789",
            "address": "гр. София, бул. България 1",
            "city": "София",
            "country": "BGR",
            "mol": "Иван Иванов",
            "iban": "BG80BNBG91651000123456",
            "bic": "BNBGBGSF",
            "bank_name": "БНБ",
        },
        "recipient": {
            "name": "КЛИЕНТ ЕООД",
            "eik": "987654321",
            "vat_number": "BG987654321",
            "address": "гр. Пловдив, ул. Главна 10",
            "city": "Пловдив",
            "country": "BGR",
            "mol": "Петър Петров",
            "iban": "BG12RZBB91551000654321",
        },
        "financial_summary": {
            "tax_base": 500.00,
            "vat_rate": 20.0,
            "vat_amount": 100.00,
            "total_amount_due": 600.00,
        },
        "line_items": [
            {
                "description": "Консултантски ИТ услуги",
                "code": "IT-CONS-01",
                "quantity": 10.0,
                "unit": "ч.",
                "unit_price": 50.0,
                "net_amount": 500.0,
                "vat_rate": 20.0,
                "vat_amount": 100.0,
                "total_amount": 600.0,
                "confidence": 99.0,
            }
        ],
    },
    "validation_results": {
        "is_valid": True,
        "errors": [],
        "warnings": [],
    },
}


def test_date_cleaning():
    assert _clean_date("08.09.2026") == "2026-09-08"
    assert _clean_date("2026-09-08") == "2026-09-08"
    assert _clean_date("8/9/2026") == "2026-09-08"
    assert _clean_date(None) is None
    assert _clean_date("invalid") is None


def test_float_conversion():
    assert _to_float("1 234,56") == 1234.56
    assert _to_float({"amount": "50.5"}) == 50.5
    assert _to_float(None, 0.0) == 0.0
    assert _to_float("bad", 10.0) == 10.0


def test_uuid_conversion():
    u1 = _to_uuid("5c442903b03e49bb98907c4384e23f90")
    assert u1 == "5c442903-b03e-49bb-9890-7c4384e23f90"
    u2 = _to_uuid("custom-arbitrary-id")
    assert len(u2) == 36


def test_build_supabase_invoice_payload():
    inv_row, items, audit, att = build_supabase_invoice_payload(
        doc_id="doc-test-1234",
        ocr_result=SAMPLE_OCR_RESULT,
        file_name="factura_1001.pdf",
        file_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        file_size=102400,
        source_channel="EMAIL_INGEST",
        source_sender="accounting@client.bg",
        processing_time=1.23,
    )

    # Invoices header
    assert inv_row["invoice_number"] == "1000000001"
    assert inv_row["issue_date"] == "2026-09-08"
    assert inv_row["supplier_name"] == "ТЕСТ ИНВЕСТ ЕООД"
    assert inv_row["supplier_eik"] == "123456789"
    assert inv_row["recipient_name"] == "КЛИЕНТ ЕООД"
    assert inv_row["recipient_eik"] == "987654321"
    assert inv_row["subtotal_amount"] == 500.00
    assert inv_row["vat_amount"] == 100.00
    assert inv_row["total_amount"] == 600.00
    assert inv_row["status"] == "VALIDATED"
    assert inv_row["source_channel"] == "EMAIL_INGEST"
    assert inv_row["source_sender"] == "accounting@client.bg"
    assert inv_row["anomaly_flags"]["math_discrepancy"] is False

    # Items
    assert len(items) == 1
    assert items[0]["item_description"] == "Консултантски ИТ услуги"
    assert items[0]["quantity"] == 10.0
    assert items[0]["unit_price_net"] == 50.0
    assert items[0]["line_net_amount"] == 500.0
    assert items[0]["vat_amount"] == 100.0
    assert items[0]["line_total_amount"] == 600.0

    # Audit & Attachment
    assert audit["event_type"] == "OCR_INGESTED"
    assert att["file_name"] == "factura_1001.pdf"
    assert att["mime_type"] == "application/pdf"


@pytest.mark.anyio
async def test_sync_invoice_mocked():
    with patch("supabase_sync.is_supabase_configured", return_value=True):
        with patch("httpx.AsyncClient.post") as mock_post, patch("httpx.AsyncClient.delete") as mock_del:
            mock_resp = AsyncMock()
            mock_resp.status_code = 201
            mock_resp.text = '{"id": "test"}'
            mock_post.return_value = mock_resp

            res = await sync_invoice_to_supabase(
                doc_id="doc-test-1234",
                ocr_result=SAMPLE_OCR_RESULT,
                file_name="factura_1001.pdf",
                file_hash="abc123hash",
                file_size=50000,
                source_channel="EMAIL_INGEST",
                processing_time=1.5,
            )

            assert res["status"] == "success"
            assert res["invoice_number"] == "1000000001"
            assert res["items_count"] == 1
            assert mock_post.call_count >= 3

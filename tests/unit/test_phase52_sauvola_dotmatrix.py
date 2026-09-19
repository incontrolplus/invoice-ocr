"""Tests for Phase 5.2: Sauvola Preprocessing & Dot-Matrix / Thermal Scans.

Validates reading degraded thermal/matrix scans and isolating fuel receipts:
- 30.pdf: Detelina dot-matrix invoice
- 32.pdf: Detelina dot-matrix invoice
- 35.pdf: Detelina 4-page dot-matrix invoice
- 37.pdf: Detelina dot-matrix invoice
- 38.pdf: Toplivo Gas fiscal fuel receipt
"""

from decimal import Decimal
from pathlib import Path
import pytest
from invoice_ocr import process_invoice

BASE_DIR = Path("/Volumes/NO NAME/_ФАКТУРИ/00_РМ_КАСКАДА_2026_ЕООД")


@pytest.mark.skipif(not BASE_DIR.is_dir(), reason="External volume not mounted")
def test_phase52_file38_fuel_receipt():
    """Verify 38.pdf: Toplivo Gas fiscal receipt isolated line items and date."""
    p = BASE_DIR / "38.pdf"
    inv = process_invoice(p, use_cache=True)
    assert inv.validation.is_valid is True, f"Validation errors: {[e.message for e in inv.validation.errors]}"
    assert len(inv.validation.errors) == 0
    assert inv.invoice_metadata.invoice_number == "0703054696"
    assert inv.invoice_metadata.date_issued == "2026-08-28"
    assert inv.supplier.eik == "130864186"
    assert "ТОПЛИВО" in inv.supplier.name
    assert inv.recipient.eik == "208380135"
    assert inv.financial_summary.total_amount_due.amount == Decimal("38.80")
    assert inv.financial_summary.vat_amount.amount == Decimal("6.47")
    assert inv.financial_summary.tax_base.amount == Decimal("32.33")
    assert len(inv.line_items) == 1
    assert "Пропан" in inv.line_items[0].description
    assert inv.line_items[0].quantity == Decimal("20.000")


@pytest.mark.skipif(not BASE_DIR.is_dir(), reason="External volume not mounted")
def test_phase52_file35_multipage_dotmatrix():
    """Verify 35.pdf: Detelina 4-page dot-matrix invoice."""
    p = BASE_DIR / "35.pdf"
    inv = process_invoice(p, use_cache=True)
    assert inv.validation.is_valid is True, f"Validation errors: {[e.message for e in inv.validation.errors]}"
    assert len(inv.validation.errors) == 0
    assert inv.supplier.eik == "114609507"
    assert inv.recipient.eik == "208380135"
    assert inv.invoice_metadata.date_issued == "2026-08-22"
    assert inv.financial_summary.total_amount_due.amount == Decimal("667.19")
    assert inv.financial_summary.vat_amount.amount == Decimal("111.20")
    assert inv.financial_summary.tax_base.amount == Decimal("555.99")
    assert len(inv.line_items) > 0


@pytest.mark.skipif(not BASE_DIR.is_dir(), reason="External volume not mounted")
def test_phase52_file30_dotmatrix():
    """Verify 30.pdf: Detelina dot-matrix invoice."""
    p = BASE_DIR / "30.pdf"
    inv = process_invoice(p, use_cache=True)
    assert inv.validation.is_valid is True, f"Validation errors: {[e.message for e in inv.validation.errors]}"
    assert len(inv.validation.errors) == 0
    assert inv.supplier.eik == "114609507"
    assert inv.recipient.eik == "208380135"
    assert inv.financial_summary.total_amount_due.amount == Decimal("212.14")
    assert inv.financial_summary.tax_base.amount == Decimal("176.78")


@pytest.mark.skipif(not BASE_DIR.is_dir(), reason="External volume not mounted")
def test_phase52_file32_dotmatrix():
    """Verify 32.pdf: Detelina dot-matrix invoice."""
    p = BASE_DIR / "32.pdf"
    inv = process_invoice(p, use_cache=True)
    assert inv.validation.is_valid is True, f"Validation errors: {[e.message for e in inv.validation.errors]}"
    assert len(inv.validation.errors) == 0
    assert inv.supplier.eik == "114609507"
    assert inv.recipient.eik == "208380135"
    assert inv.invoice_metadata.date_issued == "2026-08-24"


@pytest.mark.skipif(not BASE_DIR.is_dir(), reason="External volume not mounted")
def test_phase52_file37_dotmatrix():
    """Verify 37.pdf: Detelina dot-matrix invoice."""
    p = BASE_DIR / "37.pdf"
    inv = process_invoice(p, use_cache=True)
    assert inv.validation.is_valid is True, f"Validation errors: {[e.message for e in inv.validation.errors]}"
    assert len(inv.validation.errors) == 0
    assert inv.supplier.eik == "114609507"
    assert inv.recipient.eik == "208380135"
    assert inv.invoice_metadata.date_issued == "2026-08-28"
    assert inv.financial_summary.total_amount_due.amount == Decimal("173.64")
    assert inv.financial_summary.vat_amount.amount == Decimal("28.94")
    assert inv.financial_summary.tax_base.amount == Decimal("144.70")

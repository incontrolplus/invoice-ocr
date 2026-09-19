#!/usr/bin/env python3
"""Unit tests for invoice_ocr.py pure functions."""
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from decimal import Decimal
import pytest
from invoice_ocr import (
    parse_money,
    normalize_eik,
    normalize_vat_number,
    normalize_iban,
    normalize_bic,
    parse_date,
    clean_ocr_artifacts,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("30,00", Decimal("30.00")),
        ("30.00", Decimal("30.00")),
        ("1 234,56", Decimal("1234.56")),
        ("1.234,56", Decimal("1234.56")),
        ("1,234.56", Decimal("1234.56")),
        ("1234.56", Decimal("1234.56")),
        ("1234,56", Decimal("1234.56")),
        ("573.00", Decimal("573.00")),
        ("114.60", Decimal("114.60")),
        ("687.60", Decimal("687.60")),
        ("0,50", Decimal("0.50")),
        ("1234", Decimal("1234")),
        ("", None),
        ("abc", None),
        ("-30,00", Decimal("-30.00")),
        ("120.00 лв.", Decimal("120.00")),
        ("€573.00", Decimal("573.00")),
    ],
)
def test_parse_money(raw, expected):
    """Test money parsing across Bulgarian/European/Anglo formats."""
    result = parse_money(raw)
    assert result == expected, f"parse_money({raw!r}) = {result}, expected {expected}"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("123456789", "123456789"),
        ("1234567890", "1234567890"),
        ("1234567890123", "1234567890123"),
        ("12345678", None),  # too short
        ("12345", None),  # too short
        ("BG123456789", "123456789"),  # strip non-digits
        ("", None),
        (None, None),
    ],
)
def test_normalize_eik(raw, expected):
    """Test EIK normalisation."""
    result = normalize_eik(raw)
    assert result == expected, f"normalize_eik({raw!r}) = {result}, expected {expected}"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BG123456789", "BG123456789"),
        ("bg123456789", "BG123456789"),
        ("BG 123456789", "BG123456789"),
        ("B G 123456789", "BG123456789"),
        ("123456789", "BG123456789"),
        ("BG1234567890123", "BG1234567890123"),
        ("BG12345", None),  # too short
        ("", None),
        (None, None),
    ],
)
def test_normalize_vat_number(raw, expected):
    """Test VAT number normalisation."""
    result = normalize_vat_number(raw)
    assert result == expected, f"normalize_vat_number({raw!r}) = {result}, expected {expected}"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("28.08.2026", "2026-08-28"),
        ("28/08/2026", "2026-08-28"),
        ("2026-08-28", "2026-08-28"),
        ("1.1.2026", "2026-01-01"),
        ("31.12.2025", "2025-12-31"),
        ("invalid", None),
        ("", None),
        (None, None),
        ("32.13.2026", None),  # invalid day/month
    ],
)
def test_parse_date(raw, expected):
    """Test date parsing."""
    result = parse_date(raw)
    assert result == expected, f"parse_date({raw!r}) = {result}, expected {expected}"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("[123456789](tel:123456789)", "123456789"),
        ("[click here](http://example.com)", "click here"),
        ("normal text", "normal text"),
        ("1.234,56", "1.234,56"),  # preserve financial punctuation
        ("  multiple   spaces  ", "multiple spaces"),
        ("[some](tel:123) text [link](http://x)", "some text link"),
    ],
)
def test_clean_ocr_artifacts(raw, expected):
    """Test OCR artifact cleaning."""
    result = clean_ocr_artifacts(raw)
    assert result == expected, f"clean_ocr_artifacts({raw!r}) = {result!r}, expected {expected!r}"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BG80BNBG96611020345678", "BG80BNBG96611020345678"),
        ("BG80 BNBG 9661 1020 3456 78", "BG80BNBG96611020345678"),
        ("bg80bnbg96611020345678", "BG80BNBG96611020345678"),
        ("DE80BNBG96611020345678", None),  # not BG
        ("BG12345", None),  # too short
        ("", None),
    ],
)
def test_normalize_iban(raw, expected):
    """Test IBAN normalisation."""
    result = normalize_iban(raw)
    assert result == expected, f"normalize_iban({raw!r}) = {result}, expected {expected}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

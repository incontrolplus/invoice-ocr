#!/usr/bin/env python3
"""Unit tests for invoice_ocr.py pure functions."""
import sys
sys.path.insert(0, ".")

from decimal import Decimal
from invoice_ocr import (
    parse_money,
    normalize_eik,
    normalize_vat_number,
    normalize_iban,
    normalize_bic,
    parse_date,
    clean_ocr_artifacts,
)

def test_parse_money():
    """Test money parsing across Bulgarian/European/Anglo formats."""
    cases = [
        # (input, expected_output)
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
    ]
    passed = 0
    failed = 0
    for raw, expected in cases:
        result = parse_money(raw)
        if result == expected:
            passed += 1
            print(f"  ✅ parse_money({raw!r}) = {result}")
        else:
            failed += 1
            print(f"  ❌ parse_money({raw!r}) = {result}, expected {expected}")
    return passed, failed


def test_normalize_eik():
    """Test EIK normalisation."""
    cases = [
        ("123456789", "123456789"),
        ("1234567890", "1234567890"),
        ("1234567890123", "1234567890123"),
        ("12345678", None),  # too short
        ("12345", None),  # too short
        ("BG123456789", "123456789"),  # strip non-digits
        ("", None),
        (None, None),
    ]
    passed = 0
    failed = 0
    for raw, expected in cases:
        result = normalize_eik(raw)
        if result == expected:
            passed += 1
            print(f"  ✅ normalize_eik({raw!r}) = {result}")
        else:
            failed += 1
            print(f"  ❌ normalize_eik({raw!r}) = {result}, expected {expected}")
    return passed, failed


def test_normalize_vat_number():
    """Test VAT number normalisation."""
    cases = [
        ("BG123456789", "BG123456789"),
        ("bg123456789", "BG123456789"),
        ("BG 123456789", "BG123456789"),
        ("B G 123456789", "BG123456789"),
        ("123456789", "BG123456789"),
        ("BG1234567890123", "BG1234567890123"),
        ("BG12345", None),  # too short
        ("", None),
        (None, None),
    ]
    passed = 0
    failed = 0
    for raw, expected in cases:
        result = normalize_vat_number(raw)
        if result == expected:
            passed += 1
            print(f"  ✅ normalize_vat_number({raw!r}) = {result}")
        else:
            failed += 1
            print(f"  ❌ normalize_vat_number({raw!r}) = {result}, expected {expected}")
    return passed, failed


def test_parse_date():
    """Test date parsing."""
    cases = [
        ("28.08.2026", "2026-08-28"),
        ("28/08/2026", "2026-08-28"),
        ("2026-08-28", "2026-08-28"),
        ("1.1.2026", "2026-01-01"),
        ("31.12.2025", "2025-12-31"),
        ("invalid", None),
        ("", None),
        (None, None),
        ("32.13.2026", None),  # invalid day/month
    ]
    passed = 0
    failed = 0
    for raw, expected in cases:
        result = parse_date(raw)
        if result == expected:
            passed += 1
            print(f"  ✅ parse_date({raw!r}) = {result}")
        else:
            failed += 1
            print(f"  ❌ parse_date({raw!r}) = {result}, expected {expected}")
    return passed, failed


def test_clean_ocr_artifacts():
    """Test OCR artifact cleaning."""
    cases = [
        ("[123456789](tel:123456789)", "123456789"),
        ("[click here](http://example.com)", "click here"),
        ("normal text", "normal text"),
        ("1.234,56", "1.234,56"),  # preserve financial punctuation
        ("  multiple   spaces  ", "multiple spaces"),
        ("[some](tel:123) text [link](http://x)", "some text link"),
    ]
    passed = 0
    failed = 0
    for raw, expected in cases:
        result = clean_ocr_artifacts(raw)
        if result == expected:
            passed += 1
            print(f"  ✅ clean_ocr_artifacts({raw!r}) = {result!r}")
        else:
            failed += 1
            print(f"  ❌ clean_ocr_artifacts({raw!r}) = {result!r}, expected {expected!r}")
    return passed, failed


def test_normalize_iban():
    """Test IBAN normalisation."""
    cases = [
        ("BG80BNBG96611020345678", "BG80BNBG96611020345678"),
        ("BG80 BNBG 9661 1020 3456 78", "BG80BNBG96611020345678"),
        ("bg80bnbg96611020345678", "BG80BNBG96611020345678"),
        ("DE80BNBG96611020345678", None),  # not BG
        ("BG12345", None),  # too short
        ("", None),
    ]
    passed = 0
    failed = 0
    for raw, expected in cases:
        result = normalize_iban(raw)
        if result == expected:
            passed += 1
            print(f"  ✅ normalize_iban({raw!r}) = {result}")
        else:
            failed += 1
            print(f"  ❌ normalize_iban({raw!r}) = {result}, expected {expected}")
    return passed, failed


if __name__ == "__main__":
    total_passed = 0
    total_failed = 0
    
    tests = [
        ("parse_money", test_parse_money),
        ("normalize_eik", test_normalize_eik),
        ("normalize_vat_number", test_normalize_vat_number),
        ("parse_date", test_parse_date),
        ("clean_ocr_artifacts", test_clean_ocr_artifacts),
        ("normalize_iban", test_normalize_iban),
    ]
    
    for name, fn in tests:
        print(f"\n{'='*60}")
        print(f"Testing {name}")
        print(f"{'='*60}")
        p, f = fn()
        total_passed += p
        total_failed += f
    
    print(f"\n{'='*60}")
    print(f"TOTAL: {total_passed} passed, {total_failed} failed")
    print(f"{'='*60}")
    
    sys.exit(1 if total_failed > 0 else 0)

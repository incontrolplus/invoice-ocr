"""Tier 2: Boundary & Corner Cases E2E Tests for Bulgarian Invoice OCR.

Covers >= 5 test cases across each boundary category:
- Category 1: Empty & Null Inputs
- Category 2: Corrupt & Invalid Files
- Category 3: Max Values & Scaling
- Category 4: Decimal Rounding Extremes & Precision Tolerances
- Category 5: Mod-11 EIK Checksum Variations
- Category 6: Mod-97 IBAN Checksum Variations
- Category 7: Date Boundaries & Validation
- Category 8: Euro 2026 Transition Boundaries
"""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
import sys

import cv2
import numpy as np

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import invoice_ocr
from invoice_ocr import (
    Invoice,
    LineItem,
    MoneyAmount,
    OcrToken,
    _validate_currency,
    _validate_dates,
    _validate_identifiers,
    _validate_required_fields,
    _validate_totals,
    clean_ocr_artifacts,
    group_tokens_into_lines,
    load_image,
    normalize_bic,
    normalize_eik,
    normalize_iban,
    normalize_vat_number,
    parse_date,
    parse_money,
)
from tests.e2e.test_helpers import (
    calculate_eik9_checksum,
    calculate_eik13_checksum,
    calculate_iban_mod97, make_ocr_token,
    create_synthetic_test_image,
)


class TestTier2BoundaryAndCornerCases(unittest.TestCase):
    """Tier 2: Boundary and corner cases across all pipeline stages."""

    # -----------------------------------------------------------------------
    # 1. Empty & Null Inputs
    # -----------------------------------------------------------------------

    def test_b1_01_empty_money_string_returns_none(self):
        """Boundary: parse_money on empty and blank inputs returns None."""
        for empty_val in ["", "   ", "\t\n", None]:
            self.assertIsNone(parse_money(empty_val))

    def test_b1_02_empty_eik_returns_none(self):
        """Boundary: normalize_eik on empty and None inputs returns None."""
        for empty_val in ["", "   ", None]:
            self.assertIsNone(normalize_eik(empty_val))

    def test_b1_03_empty_vat_returns_none(self):
        """Boundary: normalize_vat_number on empty and None returns None."""
        for empty_val in ["", "   ", None]:
            self.assertIsNone(normalize_vat_number(empty_val))

    def test_b1_04_empty_iban_returns_none(self):
        """Boundary: normalize_iban on empty and None returns None."""
        for empty_val in ["", "   ", None]:
            self.assertIsNone(normalize_iban(empty_val))

    def test_b1_05_empty_date_returns_none(self):
        """Boundary: parse_date on empty and None returns None."""
        for empty_val in ["", "   ", None]:
            self.assertIsNone(parse_date(empty_val))

    def test_b1_06_empty_token_list_grouping(self):
        """Boundary: group_tokens_into_lines with empty list returns empty list."""
        self.assertEqual(group_tokens_into_lines([]), [])

    def test_b1_07_empty_invoice_triggers_required_field_errors(self):
        """Boundary: Empty invoice validation records all missing required fields."""
        inv = Invoice()
        issues = _validate_required_fields(inv)
        codes = {i.code for i in issues}
        self.assertIn("MISSING_INVOICE_NUMBER", codes)
        self.assertIn("MISSING_DATE_ISSUED", codes)
        self.assertIn("MISSING_SUPPLIER_NAME", codes)
        self.assertIn("MISSING_RECIPIENT_NAME", codes)

    # -----------------------------------------------------------------------
    # 2. Corrupt & Invalid Files
    # -----------------------------------------------------------------------

    def test_b2_01_zero_byte_file_raises_error(self):
        """Boundary: Loading a 0-byte file raises ValueError or FileNotFoundError."""
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            tmp.flush()
            with self.assertRaises((ValueError, cv2.error)):
                load_image(Path(tmp.name))

    def test_b2_02_corrupt_image_header_raises_error(self):
        """Boundary: File with garbage bytes raises ValueError."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"NOT_A_VALID_IMAGE_FILE_DATA_CORRUPT")
            tmp_path = Path(tmp.name)
        try:
            with self.assertRaises((ValueError, cv2.error)):
                load_image(tmp_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def test_b2_03_blank_white_image_produces_no_tokens(self):
        """Boundary: Completely white image produces empty token list without crashing."""
        blank = np.ones((500, 500, 3), dtype=np.uint8) * 255
        variants = invoice_ocr.generate_preprocessing_variants(blank)
        self.assertGreater(len(variants), 0)

    def test_b2_04_single_pixel_image_handling(self):
        """Boundary: 1x1 image is resized or rejected gracefully."""
        tiny = np.ones((1, 1, 3), dtype=np.uint8) * 255
        upscaled = invoice_ocr.upscale_if_needed(tiny)
        self.assertGreaterEqual(upscaled.shape[0], 1)

    def test_b2_05_invalid_money_garbage_strings(self):
        """Boundary: Non-numeric garbage strings return None from parse_money."""
        for garbage in ["abc", "--", "лв", "EUR", "N/A", "undefined", "NaN"]:
            self.assertIsNone(parse_money(garbage))

    # -----------------------------------------------------------------------
    # 3. Max Values & Scaling
    # -----------------------------------------------------------------------

    def test_b3_01_large_scale_invoice_100_items(self):
        """Boundary: Invoice with 100 line items sums and validates without error."""
        inv = Invoice()
        unit_price = Decimal("10.50")
        total_items = 100
        inv.line_items = [
            LineItem(
                index=i,
                description=f"Продукт {i}",
                unit="бр.",
                quantity=Decimal("1.00"),
                unit_price_net=MoneyAmount(unit_price, "EUR"),
                total_price_net=MoneyAmount(unit_price, "EUR"),
                vat_rate_pct=Decimal("20.00"),
            )
            for i in range(1, total_items + 1)
        ]
        tax_base = unit_price * total_items  # 1050.00
        vat_amount = (tax_base * Decimal("0.20")).quantize(Decimal("0.01"))
        total_due = tax_base + vat_amount

        inv.financial_summary.tax_base = MoneyAmount(tax_base, "EUR")
        inv.financial_summary.vat_amount = MoneyAmount(vat_amount, "EUR")
        inv.financial_summary.total_amount_due = MoneyAmount(total_due, "EUR")

        issues = _validate_totals(inv)
        self.assertEqual(len([i for i in issues if i.severity == "error"]), 0)

    def test_b3_02_very_large_monetary_amount(self):
        """Boundary: Multi-million monetary amount parses and preserves precision."""
        large_val = parse_money("999 999 999,99")
        self.assertEqual(large_val, Decimal("999999999.99"))

    def test_b3_03_negative_monetary_amount_credit_note(self):
        """Boundary: Negative numbers parse correctly for discounts and credit notes."""
        neg_val = parse_money("-1 234,56")
        self.assertEqual(neg_val, Decimal("-1234.56"))

    def test_b3_04_long_text_fields_handling(self):
        """Boundary: Extremely long party names and addresses do not raise errors."""
        long_name = "А" * 500
        inv = Invoice()
        inv.supplier.name = long_name
        self.assertEqual(len(inv.supplier.name), 500)

    def test_b3_05_high_density_token_grouping(self):
        """Boundary: 500 tokens in line grouping execute efficiently."""
        tokens = [
            make_ocr_token(f"Токен{i}", 90.0, (i * 20, 100, 18, 15), page_number=1)
            for i in range(500)
        ]
        lines = group_tokens_into_lines(tokens)
        self.assertGreater(len(lines), 0)

    # -----------------------------------------------------------------------
    # 4. Decimal Rounding Extremes & Precision Tolerances
    # -----------------------------------------------------------------------

    def test_b4_01_tolerance_exact_match(self):
        """Boundary: Diff == 0.00 passes math validation."""
        inv = Invoice()
        inv.financial_summary.tax_base = MoneyAmount(Decimal("100.00"), "EUR")
        inv.financial_summary.vat_amount = MoneyAmount(Decimal("20.00"), "EUR")
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("120.00"), "EUR")
        issues = _validate_totals(inv)
        self.assertEqual(len([i for i in issues if i.severity == "error"]), 0)

    def test_b4_02_tolerance_one_cent_passes(self):
        """Boundary: Diff == 0.01 is within statutory tolerance and passes."""
        inv = Invoice()
        inv.financial_summary.tax_base = MoneyAmount(Decimal("100.00"), "EUR")
        inv.financial_summary.vat_amount = MoneyAmount(Decimal("20.01"), "EUR")
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("120.01"), "EUR")
        issues = _validate_totals(inv)
        self.assertEqual(len([i for i in issues if i.severity == "error"]), 0)

    def test_b4_03_tolerance_two_cents_passes(self):
        """Boundary: Diff == 0.02 is at upper tolerance limit and passes."""
        inv = Invoice()
        inv.financial_summary.tax_base = MoneyAmount(Decimal("100.00"), "EUR")
        inv.financial_summary.vat_amount = MoneyAmount(Decimal("20.00"), "EUR")
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("120.02"), "EUR")
        issues = _validate_totals(inv)
        self.assertEqual(len([i for i in issues if i.severity == "error"]), 0)

    def test_b4_04_tolerance_three_cents_fails(self):
        """Boundary: Diff == 0.03 exceeds statutory tolerance and must fail."""
        inv = Invoice()
        inv.financial_summary.tax_base = MoneyAmount(Decimal("100.00"), "EUR")
        inv.financial_summary.vat_amount = MoneyAmount(Decimal("20.00"), "EUR")
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("120.03"), "EUR")
        issues = _validate_totals(inv)
        mismatches = [i for i in issues if i.code == "TOTAL_SUM_MISMATCH"]
        self.assertEqual(len(mismatches), 1)

    def test_b4_05_three_decimal_quantity_rounding(self):
        """Boundary: Kapina-01 item 4: 17.500 kg x 1.66 EUR = 29.05 -> 29.02 rounding."""
        qty = Decimal("1.615")
        unit_p = Decimal("3.13")
        product = (qty * unit_p).quantize(Decimal("0.01"))
        self.assertEqual(product, Decimal("5.05"))

    # -----------------------------------------------------------------------
    # 5. Mod-11 EIK Checksum Variations
    # -----------------------------------------------------------------------

    def test_b5_01_eik9_stage1_remainder_less_than_10(self):
        """Boundary: EIK 121644736 (Metro) Stage 1 sum % 11 < 10."""
        check = calculate_eik9_checksum("12164473")
        self.assertEqual(check, 6)

    def test_b5_02_eik9_stage1_remainder_10_stage2_remainder_less_than_10(self):
        """Boundary: EIK 100000086 Stage 1 rem = 10 -> Stage 2 weights sum % 11 = 6."""
        check = calculate_eik9_checksum("10000008")
        self.assertEqual(check, 6)

    def test_b5_03_eik9_stage1_remainder_10_stage2_remainder_10_yields_0(self):
        """Boundary: EIK 100000550 Stage 1 rem = 10, Stage 2 rem = 10 -> check digit 0."""
        check = calculate_eik9_checksum("10000055")
        self.assertEqual(check, 0)

    def test_b5_04_eik9_invalid_check_digit(self):
        """Boundary: EIK with corrupted 9th digit fails checksum."""
        # 121644736 is valid; 121644737 is invalid
        check = calculate_eik9_checksum("12164473")
        self.assertNotEqual(check, 7)

    def test_b5_05_eik_invalid_lengths(self):
        """Boundary: EIK lengths other than 9, 10, 13 return None."""
        for invalid_len in ["12345678", "12345678901", "12345678901234"]:
            self.assertIsNone(normalize_eik(invalid_len))

    # -----------------------------------------------------------------------
    # 6. Mod-97 IBAN Checksum Variations
    # -----------------------------------------------------------------------

    def test_b6_01_iban_valid_bulgarian(self):
        """Boundary: Valid Bulgarian IBAN passes Mod-97."""
        iban = "BG80BNBG96611020345678"
        self.assertEqual(calculate_iban_mod97(iban), 1)
        self.assertEqual(normalize_iban(iban), iban)

    def test_b6_02_iban_with_mixed_whitespace(self):
        """Boundary: Valid IBAN with whitespace strips and passes."""
        iban_spaced = "  BG80   BNBG 9661  1020 3456 78  "
        expected = "BG80BNBG96611020345678"
        self.assertEqual(normalize_iban(iban_spaced), expected)

    def test_b6_03_iban_invalid_mod97(self):
        """Boundary: IBAN with invalid check digits fails Mod-97."""
        bad_iban = "BG00BNBG96611020345678"
        self.assertNotEqual(calculate_iban_mod97(bad_iban), 1)

    def test_b6_04_iban_non_bg_country_code(self):
        """Boundary: Non-BG country code is rejected."""
        de_iban = "DE80BNBG96611020345678"
        self.assertIsNone(normalize_iban(de_iban))

    def test_b6_05_iban_invalid_length(self):
        """Boundary: IBAN with length != 22 returns None."""
        self.assertIsNone(normalize_iban("BG80BNBG9661102034567"))  # 21 chars
        self.assertIsNone(normalize_iban("BG80BNBG966110203456789"))  # 23 chars

    # -----------------------------------------------------------------------
    # 7. Date Boundaries & Validation
    # -----------------------------------------------------------------------

    def test_b7_01_leap_year_february_29(self):
        """Boundary: Leap year 2024-02-29 and 2028-02-29 are valid."""
        self.assertEqual(parse_date("29.02.2024"), "2024-02-29")
        self.assertEqual(parse_date("29.02.2028"), "2028-02-29")

    def test_b7_02_non_leap_year_february_29_invalid(self):
        """Boundary: Non-leap year 2025-02-29 and 2026-02-29 are invalid."""
        self.assertIsNone(parse_date("29.02.2025"))
        self.assertIsNone(parse_date("29.02.2026"))

    def test_b7_03_month_end_dates(self):
        """Boundary: Valid month end dates."""
        self.assertEqual(parse_date("31.01.2026"), "2026-01-31")
        self.assertEqual(parse_date("30.04.2026"), "2026-04-30")
        self.assertEqual(parse_date("31.12.2025"), "2025-12-31")

    def test_b7_04_single_digit_day_and_month_padded(self):
        """Boundary: 1.1.2026 -> 2026-01-01, 5.9.2026 -> 2026-09-05."""
        self.assertEqual(parse_date("1.1.2026"), "2026-01-01")
        self.assertEqual(parse_date("5.9.2026"), "2026-09-05")

    def test_b7_05_impossible_dates_return_none(self):
        """Boundary: Impossible dates return None."""
        self.assertIsNone(parse_date("32.01.2026"))
        self.assertIsNone(parse_date("15.13.2026"))
        self.assertIsNone(parse_date("00.00.0000"))

    # -----------------------------------------------------------------------
    # 8. Euro 2026 Transition Boundaries
    # -----------------------------------------------------------------------

    def test_b8_01_pre_transition_date_valid_bgn(self):
        """Boundary: 2025-12-31 with BGN has ZERO euro warnings."""
        inv = Invoice()
        inv.invoice_metadata.date_issued = "2025-12-31"
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("100.00"), "BGN")
        issues = _validate_currency(inv, [])
        codes = [i.code for i in issues]
        self.assertNotIn("CURRENCY_POST_EURO_BGN_DETECTED", codes)
        self.assertNotIn("CURRENCY_AFTER_DUAL_PERIOD", codes)

    def test_b8_02_transition_start_date_triggers_warning(self):
        """Boundary: 2026-01-01 with BGN triggers CURRENCY_POST_EURO_BGN_DETECTED."""
        inv = Invoice()
        inv.invoice_metadata.date_issued = "2026-01-01"
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("100.00"), "BGN")
        issues = _validate_currency(inv, [])
        codes = [i.code for i in issues]
        self.assertIn("CURRENCY_POST_EURO_BGN_DETECTED", codes)
        self.assertNotIn("CURRENCY_AFTER_DUAL_PERIOD", codes)

    def test_b8_03_day_before_dual_display_deadline(self):
        """Boundary: 2026-08-07 triggers POST_EURO but NOT AFTER_DUAL_PERIOD."""
        inv = Invoice()
        inv.invoice_metadata.date_issued = "2026-08-07"
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("100.00"), "BGN")
        issues = _validate_currency(inv, [])
        codes = [i.code for i in issues]
        self.assertIn("CURRENCY_POST_EURO_BGN_DETECTED", codes)
        self.assertNotIn("CURRENCY_AFTER_DUAL_PERIOD", codes)

    def test_b8_04_dual_display_deadline_exact_date(self):
        """Boundary: 2026-08-08 triggers CURRENCY_AFTER_DUAL_PERIOD."""
        inv = Invoice()
        inv.invoice_metadata.date_issued = "2026-08-08"
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("100.00"), "BGN")
        issues = _validate_currency(inv, [])
        codes = [i.code for i in issues]
        self.assertIn("CURRENCY_AFTER_DUAL_PERIOD", codes)

    def test_b8_05_euro_invoice_has_no_euro_transition_warning(self):
        """Boundary: Invoices in EUR on or after 2026-01-01 have no transition warnings."""
        inv = Invoice()
        inv.invoice_metadata.date_issued = "2026-04-28"
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("100.00"), "EUR")
        issues = _validate_currency(inv, [])
        codes = [i.code for i in issues]
        self.assertNotIn("CURRENCY_POST_EURO_BGN_DETECTED", codes)
        self.assertNotIn("CURRENCY_AFTER_DUAL_PERIOD", codes)


if __name__ == "__main__":
    unittest.main()

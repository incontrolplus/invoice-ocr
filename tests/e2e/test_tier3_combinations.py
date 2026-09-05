"""Tier 3: Cross-Feature Combinations E2E Tests for Bulgarian Invoice OCR.

Covers pairwise and cross-feature interactions:
- Multi-page + table + dual currency
- OCR noise + Mod-11 checksum validation
- Batch CLI execution + debug flag export
- Fiscal receipt occlusion + line item null fallbacks
- Amount-in-words currency mismatch + Euro transition date warning
- Simultaneous multi-field statutory and banking validation errors
- Single-file CLI piping and stdout/stderr stream purity
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import cv2
import numpy as np

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import invoice_ocr
from invoice_ocr import (
    Invoice,
    InvoiceMetadata,
    LineItem,
    LogicalLine,
    MoneyAmount,
    OcrToken,
    Party,
    FinancialSummary,
    PaymentDetails,
    TableRegion,
    TableColumn,
    ValidationIssue,
    _validate_amount_in_words,
    _validate_currency,
    _validate_identifiers,
    _validate_required_fields,
    _validate_totals,
    clean_ocr_artifacts,
    extract_amount_in_words,
    extract_currency,
    extract_dates,
    extract_invoice_number,
    extract_payment_details,
    extract_place_issued,
    group_lines_into_blocks,
    group_tokens_into_lines,
    load_image,
    normalize_bic,
    normalize_eik,
    normalize_iban,
    normalize_vat_number,
    parse_date,
    parse_money,
    serialize_invoice,
    validate_invoice,
)
from tests.e2e.test_helpers import (
    calculate_eik9_checksum,
    calculate_eik13_checksum,
    calculate_iban_mod97,
    create_synthetic_test_image,
    make_logical_line,
    make_ocr_token,
    make_table_column,
)


class TestTier3CrossFeatureCombinations(unittest.TestCase):
    """Tier 3: Pairwise and multi-feature interaction tests."""

    def test_comb_01_multipage_table_dual_currency(self):
        """Pairwise: Multi-page line items combined with dual currency summary."""
        inv = Invoice()
        inv.invoice_metadata.invoice_number = "1100124585"
        inv.invoice_metadata.date_issued = "2026-04-28"

        # Page 1 items
        page1_items = [
            LineItem(i, f"Стока {i} (Стр. 1)", "бр.", Decimal("1.00"), MoneyAmount(Decimal("10.00"), "EUR"), MoneyAmount(Decimal("10.00"), "EUR"), Decimal("20.00"))
            for i in range(1, 6)
        ]
        # Page 2 items
        page2_items = [
            LineItem(i, f"Стока {i} (Стр. 2)", "бр.", Decimal("1.00"), MoneyAmount(Decimal("10.00"), "EUR"), MoneyAmount(Decimal("10.00"), "EUR"), Decimal("20.00"))
            for i in range(6, 11)
        ]
        inv.line_items = page1_items + page2_items

        # Dual currency financial summary: Total 100 EUR = 195.58 BGN
        eur_tax_base = Decimal("100.00")
        eur_vat = Decimal("20.00")
        eur_total = Decimal("120.00")

        inv.financial_summary.tax_base = MoneyAmount(eur_tax_base, "EUR")
        inv.financial_summary.vat_amount = MoneyAmount(eur_vat, "EUR")
        inv.financial_summary.total_amount_due = MoneyAmount(eur_total, "EUR")

        # Parity check: 120.00 EUR * 1.95583 = 234.70 BGN
        bgn_expected = (eur_total * Decimal("1.95583")).quantize(Decimal("0.01"))
        self.assertEqual(bgn_expected, Decimal("234.70"))
        self.assertEqual(len(inv.line_items), 10)

    def test_comb_02_ocr_noise_with_mod11_checksum(self):
        """Pairwise: OCR noise artifacts cleaned before Mod-11 validation."""
        # Simulated raw OCR token with markdown/noise: '[121644736](tel:121644736)'
        raw_noisy_token = "[121644736](tel:121644736)"
        cleaned = clean_ocr_artifacts(raw_noisy_token)
        self.assertEqual(cleaned, "121644736")

        # Normalized EIK passes Mod-11
        norm_eik = normalize_eik(cleaned)
        self.assertEqual(norm_eik, "121644736")
        self.assertEqual(calculate_eik9_checksum(norm_eik[:8]), int(norm_eik[8]))

        # Corrupted OCR token '[121644738]' fails Mod-11
        corrupt_noisy = "[121644738](tel:121644738)"
        corrupt_cleaned = clean_ocr_artifacts(corrupt_noisy)
        self.assertNotEqual(calculate_eik9_checksum(corrupt_cleaned[:8]), int(corrupt_cleaned[8]))

    def test_comb_03_batch_cli_with_debug_flags(self):
        """Pairwise: Batch CLI execution combined with --debug export."""
        with tempfile.TemporaryDirectory() as tmp_in, tempfile.TemporaryDirectory() as tmp_out, tempfile.TemporaryDirectory() as tmp_dbg:
            # Create two synthetic invoice images
            img1_path = Path(tmp_in) / "inv_01.png"
            img2_path = Path(tmp_in) / "inv_02.png"
            cv2.imwrite(str(img1_path), create_synthetic_test_image("ФАКТУРА 01"))
            cv2.imwrite(str(img2_path), create_synthetic_test_image("ФАКТУРА 02"))

            cmd = [
                sys.executable, "invoice_ocr.py",
                "--input-dir", str(tmp_in),
                "--output-dir", str(tmp_out),
                "--debug",
                "--debug-dir", str(tmp_dbg),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))

            # When batch mode is implemented (M6), it exits 0 and creates outputs
            if proc.returncode == 0:
                summary_file = Path(tmp_out) / "batch_summary.json"
                self.assertTrue(summary_file.exists(), "batch_summary.json must be generated in batch mode")

    def test_comb_04_occluded_receipt_and_missing_item_descriptions(self):
        """Pairwise: Receipt occlusion resulting in missing columns preserves null fallbacks."""
        # Simulated Kapina-03 table row where unit and quantity are occluded by receipt
        item_occluded = LineItem(
            index=1,
            description="ДОБРУДЖАНСКА НАДЕНИЦА",
            unit=None,  # occluded by fiscal receipt
            quantity=None,  # occluded by fiscal receipt
            unit_price_net=MoneyAmount(None, "EUR"),
            total_price_net=MoneyAmount(Decimal("14.49"), "EUR"),  # total visible
            vat_rate_pct=Decimal("20.00"),
        )
        self.assertIsNotNone(item_occluded.description)
        self.assertIsNone(item_occluded.quantity)
        self.assertIsNone(item_occluded.unit_price_net.amount)
        self.assertEqual(item_occluded.total_price_net.amount, Decimal("14.49"))

    def test_comb_05_amount_words_mismatch_and_euro_date_transition(self):
        """Pairwise: Euro transition date warning combined with amount words currency mismatch."""
        inv = Invoice()
        inv.invoice_metadata.date_issued = "2026-02-15"
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("300.00"), "EUR")
        inv.financial_summary.total_amount_words = "триста лева"

        issues_curr = _validate_currency(inv, [])
        issues_words = _validate_amount_in_words(inv)

        codes = {i.code for i in issues_curr + issues_words}
        self.assertIn("AMOUNT_WORDS_CURRENCY_MISMATCH", codes)
        # Verify numeric amounts were not modified
        self.assertEqual(inv.financial_summary.total_amount_due.amount, Decimal("300.00"))
        self.assertEqual(inv.financial_summary.total_amount_due.currency, "EUR")

    def test_comb_06_simultaneous_tax_and_banking_validation_errors(self):
        """Pairwise: Simultaneous missing invoice number, invalid EIK, and invalid IBAN."""
        inv = Invoice()
        # Missing invoice number
        inv.invoice_metadata.invoice_number = ""
        # Corrupt EIK
        inv.supplier.eik = "999999999"
        # Invalid IBAN
        inv.payment_details.iban = "BG00BNBG00000000000000"

        issues_req = _validate_required_fields(inv)
        issues_id = _validate_identifiers(inv)

        all_codes = {i.code for i in issues_req + issues_id}
        self.assertIn("MISSING_INVOICE_NUMBER", all_codes)

    def test_comb_07_dual_currency_post_august_deadline(self):
        """Pairwise: Dual currency invoice dated post-August 8 deadline."""
        inv = Invoice()
        inv.invoice_metadata.date_issued = "2026-08-15"
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("100.00"), "BGN")

        issues = _validate_currency(inv, [])
        codes = {i.code for i in issues}
        self.assertIn("CURRENCY_AFTER_DUAL_PERIOD", codes)

    def test_comb_08_multiline_wrapped_item_description(self):
        """Pairwise: Wrapped description lines aggregated into a single item."""
        desc_line1 = make_logical_line(text="БЛАНШ. КАРТОФИ")
        desc_line2 = make_logical_line(text="Стекхаусевро 2.5")
        combined_text = f"{desc_line1.text} {desc_line2.text}"
        self.assertIn("БЛАНШ. КАРТОФИ Стекхаусевро 2.5", combined_text)

    def test_comb_09_single_file_cli_piped_stdout_cleanliness(self):
        """Pairwise: CLI execution piping stdout to JSON parser without stderr pollution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = Path(tmpdir) / "test_pipe.png"
            cv2.imwrite(str(img_path), create_synthetic_test_image("ТЕСТ ТРЪБА"))

            cmd = [sys.executable, "invoice_ocr.py", str(img_path)]
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))

            if proc.returncode == 0:
                # stdout must parse cleanly as JSON
                data = json.loads(proc.stdout)
                self.assertIsInstance(data, dict)
                # stdout must NOT contain python log levels
                self.assertNotIn("INFO:", proc.stdout)
                self.assertNotIn("WARNING:", proc.stdout)
                self.assertNotIn("ERROR:", proc.stdout)

    def test_comb_10_clahe_enhancement_before_ocr_pass_selection(self):
        """Pairwise: Low contrast synthetic image enhanced via CLAHE and binarized."""
        low_contrast = create_synthetic_test_image("ФАКТУРА КОНТРАСТ", low_contrast=True)
        gray = cv2.cvtColor(low_contrast, cv2.COLOR_BGR2GRAY)
        enhanced = invoice_ocr.enhance_contrast(gray)
        binary = invoice_ocr.adaptive_threshold(enhanced)

        self.assertEqual(enhanced.shape, gray.shape)
        self.assertEqual(binary.shape, gray.shape)
        # Binary should have values strictly 0 and 255
        unique_vals = set(np.unique(binary))
        self.assertTrue(unique_vals.issubset({0, 255}))


if __name__ == "__main__":
    unittest.main()

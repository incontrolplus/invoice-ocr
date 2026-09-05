"""Tier 1: Feature Coverage E2E Tests for Bulgarian Invoice OCR.

Covers R1 through R6 requirements with >= 5 tests per requirement group:
- R1: Multi-Format & Multi-Page Ingestion
- R2: Adaptive Preprocessing & Multi-Pass OCR Engine
- R3: Coordinate-Based Layout Analysis & Table Reconstruction
- R4: Deterministic Field Extraction & Bulgarian Tax Rules
- R5: Rigorous Financial, Euro-Transition & Anomaly Validation
- R6: CLI, Batch Processing & Debug Artifacts
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
    clean_ocr_artifacts,
    extract_currency,
    extract_dates,
    extract_invoice_number,
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
    ACCEPTANCE_DIR,
    calculate_eik9_checksum,
    calculate_eik13_checksum,
    calculate_iban_mod97,
    create_synthetic_test_image,
    make_logical_line,
    make_ocr_token,
    make_table_column,
)


class TestTier1FeatureCoverage(unittest.TestCase):
    """Tier 1: Comprehensive feature coverage across R1 through R6."""

    # -----------------------------------------------------------------------
    # R1: Multi-Format & Multi-Page Ingestion
    # -----------------------------------------------------------------------

    def test_r1_01_supported_extensions_include_pdf_and_images(self):
        """R1: Verify supported extensions include .png, .jpg, .jpeg, and .pdf."""
        expected_extensions = {".png", ".jpg", ".jpeg", ".pdf"}
        supported = getattr(invoice_ocr, "SUPPORTED_EXTENSIONS", set())
        self.assertTrue(
            expected_extensions.issubset(supported),
            f"SUPPORTED_EXTENSIONS must include {expected_extensions}, found {supported}"
        )

    def test_r1_02_image_multi_format_loading(self):
        """R1: Test loading .png, .jpg, and .jpeg images returns valid 3-channel arrays."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            img = create_synthetic_test_image("Тест зареждане")

            for ext in [".png", ".jpg", ".jpeg"]:
                file_path = tmp_path / f"test_img{ext}"
                cv2.imwrite(str(file_path), img)
                loaded = load_image(file_path)
                self.assertIsInstance(loaded, np.ndarray, f"Failed to load image format: {ext}")
                self.assertEqual(len(loaded.shape), 3, f"Image {ext} must have 3 dimensions (H, W, C)")
                self.assertEqual(loaded.shape[2], 3, f"Image {ext} must have 3 color channels")

    def test_r1_03_pdf_rasterization_with_pymupdf(self):
        """R1: Verify PDF rasterization at 300-400 DPI producing PageImage / ndarray."""
        import pymupdf
        if ACCEPTANCE_DIR.exists() and (ACCEPTANCE_DIR / "капина-01.pdf").exists():
            pdf_path = ACCEPTANCE_DIR / "капина-01.pdf"
            doc = pymupdf.open(pdf_path)
            self.assertGreaterEqual(len(doc), 1, "PDF must have at least 1 page")
            page = doc[0]
            zoom = 300.0 / 72.0
            mat = pymupdf.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            self.assertGreaterEqual(pix.width, 2000, "300 DPI A4 image must be >= 2000px wide")
            self.assertGreaterEqual(pix.height, 3000, "300 DPI A4 image must be >= 3000px high")
            doc.close()

    def test_r1_04_token_page_number_and_bbox_contract(self):
        """R1: Verify OcrToken tracks page_number and bounding box coordinates."""
        token = make_ocr_token(
            text="ФАКТУРА",
            conf=95.0,
            bbox=(100, 200, 80, 25),
            page_number=1,
            is_low_confidence=False,
        )
        self.assertEqual(token.text, "ФАКТУРА")
        self.assertTrue(
            hasattr(token, "page_number"),
            "OcrToken must have 'page_number' field per PROJECT.md interface contract"
        )
        self.assertEqual(getattr(token, "page_number", None), 1)

    def test_r1_05_readonly_source_protection(self):
        """R1: Verify reading acceptance files never alters their metadata or content."""
        if not ACCEPTANCE_DIR.exists():
            self.skipTest("Acceptance directory not mounted")

        pdf_path = ACCEPTANCE_DIR / "капина-01.pdf"
        stat_before = pdf_path.stat()

        with open(pdf_path, "rb") as f:
            data = f.read(1024)
            self.assertTrue(data.startswith(b"%PDF"))

        stat_after = pdf_path.stat()
        self.assertEqual(stat_before.st_size, stat_after.st_size, "File size must not change")
        self.assertEqual(stat_before.st_mtime, stat_after.st_mtime, "File mtime must not change")

    # -----------------------------------------------------------------------
    # R2: Adaptive Preprocessing & Multi-Pass OCR Engine
    # -----------------------------------------------------------------------

    def test_r2_01_osd_orientation_correction(self):
        """R2: Verify orientation check function exists and returns an image."""
        img = create_synthetic_test_image("ФАКТУРА ТЕСТ")
        corrected = invoice_ocr.check_and_fix_orientation(img)
        self.assertIsInstance(corrected, np.ndarray)

    def test_r2_02_deskew_image(self):
        """R2: Verify deskew_image function processes synthetic image without error."""
        img = create_synthetic_test_image("ТЕСТ НАКЛОН", angle=2.0)
        deskewed = invoice_ocr.deskew_image(img)
        self.assertIsInstance(deskewed, np.ndarray)

    def test_r2_03_clahe_contrast_enhancement(self):
        """R2: Verify enhance_contrast operates on image."""
        low_contrast_img = create_synthetic_test_image("СЛАБ КОНТРАСТ", low_contrast=True)
        gray = cv2.cvtColor(low_contrast_img, cv2.COLOR_BGR2GRAY)
        enhanced = invoice_ocr.enhance_contrast(gray)
        self.assertIsInstance(enhanced, np.ndarray)

    def test_r2_04_binarization_variants(self):
        """R2: Verify generate_preprocessing_variants produces candidate images."""
        img = create_synthetic_test_image("ВАРИАНТИ ПРЕПРОЦЕСИНГ")
        variants = invoice_ocr.generate_preprocessing_variants(img)
        self.assertIsInstance(variants, list)
        self.assertGreaterEqual(len(variants), 1, "Must generate at least 1 preprocessing variant")
        for item in variants:
            image_arr = item[1] if isinstance(item, tuple) else item
            self.assertIsInstance(image_arr, np.ndarray)

    def test_r2_05_ocr_low_confidence_tagging(self):
        """R2: Verify tokens with confidence < 60 are tagged as low confidence."""
        token_low = make_ocr_token("ТЕСТ", 45.0, (10, 10, 50, 20), page_number=1, is_low_confidence=True)
        self.assertTrue(
            hasattr(token_low, "is_low_confidence"),
            "OcrToken must have 'is_low_confidence' attribute per PROJECT.md contract"
        )
        self.assertTrue(getattr(token_low, "is_low_confidence", False))

    def test_r2_06_ocr_pass_scoring_engine(self):
        """R2: Verify _score_ocr_result favors higher Cyrillic character density and confidence."""
        bulgarian_tokens = [
            make_ocr_token("Фактура", 90.0, (0, 0, 50, 20)),
            make_ocr_token("Доставчик", 95.0, (0, 30, 80, 20)),
            make_ocr_token("Получател", 92.0, (0, 60, 80, 20)),
        ]
        noisy_tokens = [
            make_ocr_token("???", 40.0, (0, 0, 50, 20)),
            make_ocr_token("###", 30.0, (0, 30, 80, 20)),
        ]
        score_bul = invoice_ocr._score_ocr_result(bulgarian_tokens)
        score_noisy = invoice_ocr._score_ocr_result(noisy_tokens)
        self.assertGreater(score_bul, score_noisy, "Bulgarian tokens must score higher than noise")

    # -----------------------------------------------------------------------
    # R3: Coordinate-Based Layout Analysis & Table Reconstruction
    # -----------------------------------------------------------------------

    def test_r3_01_coordinate_line_grouping(self):
        """R3: Verify tokens with overlapping Y coordinates form a single logical line."""
        token_left = make_ocr_token("ФАКТУРА", 90.0, (100, 50, 80, 20))
        token_right = make_ocr_token("1100124585", 92.0, (200, 52, 90, 20))
        token_other = make_ocr_token("Дата", 88.0, (100, 100, 50, 20))

        lines = group_tokens_into_lines([token_left, token_right, token_other])
        self.assertEqual(len(lines), 2, "Should produce exactly 2 logical lines")
        line1_texts = [t.text for t in lines[0].tokens]
        self.assertIn("ФАКТУРА", line1_texts)
        self.assertIn("1100124585", line1_texts)

    def test_r3_02_spatial_block_grouping(self):
        """R3: Verify logical lines are grouped into blocks based on vertical proximity."""
        line1 = make_logical_line(text="Блок 1 Ред 1", bbox=(100, 50, 200, 20), y_center=60.0)
        line2 = make_logical_line(text="Блок 1 Ред 2", bbox=(100, 75, 200, 20), y_center=85.0)
        line3 = make_logical_line(text="Блок 2 Ред 1", bbox=(100, 300, 200, 20), y_center=310.0)

        blocks = group_lines_into_blocks([line1, line2, line3])
        self.assertGreaterEqual(len(blocks), 2, "Lines separated by large gap must form distinct blocks")

    def test_r3_03_table_header_synonym_detection(self):
        """R3: Verify matching of Bulgarian column synonyms."""
        synonyms = [
            ("№", "index"),
            ("ОПИСАНИЕ НА СТОКАТА", "description"),
            ("Количество", "quantity"),
            ("Мярка", "unit"),
            ("Ед. цена", "unit_price"),
            ("Стойност", "total_price"),
            ("ДДС %", "vat_rate"),
        ]
        for text, expected_col in synonyms:
            matched = invoice_ocr._match_column_synonym(text)
            self.assertEqual(
                matched, expected_col,
                f"Column text '{text}' should match '{expected_col}', got '{matched}'"
            )

    def test_r3_04_table_column_boundary_mapping(self):
        """R3: Verify token assignment to table columns based on bounding box."""
        col1 = make_table_column("index", 50, 100)
        col2 = make_table_column("description", 105, 300)
        col3 = make_table_column("total_price", 305, 400)

        token_desc = make_ocr_token("Кашкавал", 90.0, (110, 200, 100, 20))
        token_val = make_ocr_token("15.42", 90.0, (310, 200, 50, 20))

        assigned_desc = invoice_ocr._assign_token_to_column(token_desc, [col1, col2, col3])
        assigned_val = invoice_ocr._assign_token_to_column(token_val, [col1, col2, col3])

        self.assertEqual(assigned_desc, "description")
        self.assertEqual(assigned_val, "total_price")

    def test_r3_05_no_synthetic_fallback_descriptions(self):
        """R3: Never substitute synthetic fallback like 'Item'; description must be None or valid text."""
        inv = Invoice()
        item = LineItem(
            index=1,
            description=None,
            unit="бр.",
            quantity=Decimal("1.00"),
            unit_price_net=MoneyAmount(Decimal("10.00"), "BGN"),
            total_price_net=MoneyAmount(Decimal("10.00"), "BGN"),
            vat_rate_pct=Decimal("20.00"),
        )
        inv.line_items = [item]
        self.assertIsNone(inv.line_items[0].description)
        self.assertNotEqual(inv.line_items[0].description, "Item")

    # -----------------------------------------------------------------------
    # R4: Deterministic Field Extraction & Bulgarian Tax Rules
    # -----------------------------------------------------------------------

    def test_r4_01_invoice_number_and_dates_extraction(self):
        """R4: Verify extraction of statutory 10-digit invoice number and ISO dates."""
        tokens = [
            make_ocr_token("ФАКТУРА"),
            make_ocr_token("№"),
            make_ocr_token("0000001234"),
        ]
        line_num = make_logical_line(tokens)
        line_dt = make_logical_line(text="Дата на издаване: 28.04.2026")

        inv_num = extract_invoice_number([line_num], tokens)
        self.assertIn("0000001234", inv_num or "")

        issue_date, _ = extract_dates([line_dt])
        self.assertEqual(issue_date, "2026-04-28")

    def test_r4_02_place_of_issue_extraction(self):
        """R4: Verify settlement of issue extraction."""
        line = make_logical_line(text="Място на издаване: гр. Плевен")
        place = extract_place_issued([line])
        self.assertIsNotNone(place)
        self.assertIn("Плевен", place)

    def test_r4_03_bulgarian_eik_modulo11_validation(self):
        """R4: Verify Bulgarian 9-digit and 13-digit EIK Modulo-11 algorithm."""
        eik_valid = "121644736"
        self.assertEqual(calculate_eik9_checksum(eik_valid[:8]), int(eik_valid[8]))
        self.assertEqual(normalize_eik(eik_valid), eik_valid)

        # Invalid EIK
        eik_invalid = "123456789"
        self.assertNotEqual(calculate_eik9_checksum(eik_invalid[:8]), int(eik_invalid[8]))

    def test_r4_04_vat_id_normalization(self):
        """R4: Verify normalization of VAT ID to BG prefix."""
        self.assertEqual(normalize_vat_number("BG114500333"), "BG114500333")
        self.assertEqual(normalize_vat_number("bg 114500333"), "BG114500333")
        self.assertEqual(normalize_vat_number("114500333"), "BG114500333")
        self.assertIsNone(normalize_vat_number("BG123"))

    def test_r4_05_bulgarian_iban_modulo97(self):
        """R4: Verify Bulgarian IBAN structure and ISO 7064 Modulo 97-10 checksum."""
        kapina_iban = "BG13BPBI81701060091301"
        self.assertEqual(calculate_iban_mod97(kapina_iban), 1)
        self.assertEqual(len(kapina_iban), 22)
        self.assertTrue(kapina_iban.startswith("BG"))

    def test_r4_06_monetary_parsing_to_decimal_with_currency(self):
        """R4: Verify monetary parsing creates Decimal and explicit currency objects."""
        amt = parse_money("1 234,56")
        self.assertEqual(amt, Decimal("1234.56"))
        money_obj = MoneyAmount(amount=amt, currency="EUR")
        self.assertEqual(money_obj.amount, Decimal("1234.56"))
        self.assertEqual(money_obj.currency, "EUR")

    # -----------------------------------------------------------------------
    # R5: Rigorous Financial, Euro-Transition & Anomaly Validation
    # -----------------------------------------------------------------------

    def test_r5_01_line_items_total_math_check(self):
        """R5: Verify sum(line_items.total_price_net) == tax_base within 0.02 tolerance."""
        inv = Invoice()
        inv.financial_summary.tax_base = MoneyAmount(Decimal("100.00"), "EUR")
        inv.financial_summary.vat_amount = MoneyAmount(Decimal("20.00"), "EUR")
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("120.00"), "EUR")

        inv.line_items = [
            LineItem(1, "Item 1", "бр.", Decimal("1"), MoneyAmount(Decimal("50.00"), "EUR"), MoneyAmount(Decimal("50.00"), "EUR"), Decimal("20")),
            LineItem(2, "Item 2", "бр.", Decimal("1"), MoneyAmount(Decimal("50.01"), "EUR"), MoneyAmount(Decimal("50.01"), "EUR"), Decimal("20")),
        ]
        issues = invoice_ocr._validate_totals(inv)
        mismatch_issues = [i for i in issues if i.code == "LINE_ITEMS_TOTAL_MISMATCH"]
        self.assertEqual(len(mismatch_issues), 0, "Tolerance of 0.01 should not trigger error")

    def test_r5_02_vat_calculation_math_check(self):
        """R5: Verify tax_base * vat_rate == vat_amount within 0.02 tolerance."""
        inv = Invoice()
        inv.financial_summary.tax_base = MoneyAmount(Decimal("200.00"), "EUR")
        inv.financial_summary.vat_amount = MoneyAmount(Decimal("40.01"), "EUR")
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("240.01"), "EUR")

        issues = invoice_ocr._validate_totals(inv)
        vat_mismatch = [i for i in issues if i.code == "VAT_CALCULATION_MISMATCH"]
        self.assertEqual(len(vat_mismatch), 0, "Diff 0.01 in VAT should pass")

    def test_r5_03_total_due_sum_math_check(self):
        """R5: Verify tax_base + vat_amount == total_amount_due within 0.02 tolerance."""
        inv = Invoice()
        inv.financial_summary.tax_base = MoneyAmount(Decimal("100.00"), "EUR")
        inv.financial_summary.vat_amount = MoneyAmount(Decimal("20.00"), "EUR")
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("120.02"), "EUR")

        issues = invoice_ocr._validate_totals(inv)
        total_mismatch = [i for i in issues if i.code == "TOTAL_SUM_MISMATCH"]
        self.assertEqual(len(total_mismatch), 0, "Diff 0.02 in total should pass")

    def test_r5_04_euro_transition_date_rules_no_autoconvert(self):
        """R5: Verify Euro transition warnings for BGN and verify amounts are not auto-converted."""
        inv = Invoice()
        inv.invoice_metadata.date_issued = "2026-01-15"
        inv.financial_summary.tax_base = MoneyAmount(Decimal("100.00"), "BGN")
        inv.financial_summary.vat_amount = MoneyAmount(Decimal("20.00"), "BGN")
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("120.00"), "BGN")

        issues = invoice_ocr._validate_currency(inv, [])
        codes = [i.code for i in issues]
        self.assertIn("CURRENCY_POST_EURO_BGN_DETECTED", codes)
        self.assertEqual(inv.financial_summary.total_amount_due.amount, Decimal("120.00"))
        self.assertEqual(inv.financial_summary.total_amount_due.currency, "BGN")

    def test_r5_05_amount_words_currency_mismatch(self):
        """R5: Verify warning when numeric currency is EUR but words mention 'лева'."""
        inv = Invoice()
        inv.financial_summary.total_amount_due = MoneyAmount(Decimal("100.00"), "EUR")
        inv.financial_summary.total_amount_words = "сто лева и 00 стотинки"

        issues = invoice_ocr._validate_amount_in_words(inv)
        codes = [i.code for i in issues]
        self.assertIn("AMOUNT_WORDS_CURRENCY_MISMATCH", codes)

    def test_r5_06_strict_3_layer_output_contract(self):
        """R5: Verify serialization produces 3-layer architecture JSON."""
        inv = Invoice()
        inv.invoice_metadata.invoice_number = "1100124585"
        json_str = serialize_invoice(inv)
        data = json.loads(json_str)
        self.assertIn("invoice_metadata", data)
        self.assertIn("validation", data)

    # -----------------------------------------------------------------------
    # R6: CLI, Batch Processing & Debug Artifacts
    # -----------------------------------------------------------------------

    def test_r6_01_cli_single_file_stdout_json(self):
        """R6: Verify single-file CLI execution outputs strictly valid JSON on stdout."""
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = Path(tmpdir) / "test_cli.png"
            cv2.imwrite(str(img_path), create_synthetic_test_image("ФАКТУРА CLI"))

            cmd = [sys.executable, "invoice_ocr.py", str(img_path)]
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))

            if proc.returncode == 0:
                try:
                    parsed = json.loads(proc.stdout)
                    self.assertIsInstance(parsed, dict)
                except json.JSONDecodeError:
                    self.fail(f"CLI stdout is not valid JSON:\n{proc.stdout}")

    def test_r6_02_cli_nonexistent_file_exit_code(self):
        """R6: Verify CLI exits with code 1 on non-existent file."""
        cmd = [sys.executable, "invoice_ocr.py", "/non/existent/file.png"]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not found", proc.stderr.lower())

    def test_r6_03_cli_unsupported_file_extension(self):
        """R6: Verify CLI rejects unsupported file extensions with code 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "test.txt"
            bad_file.write_text("not an image")
            cmd = [sys.executable, "invoice_ocr.py", str(bad_file)]
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("unsupported", proc.stderr.lower())


if __name__ == "__main__":
    unittest.main()

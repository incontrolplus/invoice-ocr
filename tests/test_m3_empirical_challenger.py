"""Empirical Challenger Test Suite for Milestone 3: Spatial Layout Analysis & Table Reconstruction.

Tests:
1. Token retention contract in process_invoice for scanned documents (non-digital PDFs / images).
2. Multi-page table continuation, continuous 1-based indexing, and transfer line filtering on метро.pdf and метро-2.pdf.
3. 17-item table reconstruction, occlusion handling, and receipt block isolation on капина-03.pdf.
4. Acceptance verification on капина-01.pdf and капина-02.pdf.
5. Immutability audit of /Volumes/NO NAME/_ФАКТУРИ.
"""

from decimal import Decimal
from pathlib import Path
import re
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import invoice_ocr as iocr
from invoice_ocr import (
    OcrToken,
    LogicalLine,
    LogicalBlock,
    TableRegion,
    TableColumn,
    LineItem,
    MoneyAmount,
    Invoice,
    detect_table_regions,
    extract_line_items,
    group_lines_into_blocks,
    group_tokens_into_lines,
    detect_receipt_regions,
    is_transfer_or_header_line,
    _match_column_synonym,
    _is_summary_line,
)

KAPINA_DIR = Path("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026")
METRO_DIR = Path("/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025")


# =====================================================================
# 1. Pipeline Token Retention Contract (Bug 1 Reproducer)
# =====================================================================

class TestTokenRetentionContract:
    """Verifies that process_invoice properly populates raw_ocr_evidence and all_tokens

    for documents without embedded PDF text streams (such as scanned invoices and images).
    """

    @pytest.mark.skipif(not METRO_DIR.exists(), reason="Acceptance dataset not mounted")
    def test_process_invoice_retains_ocr_tokens_on_scanned_pdf(self):
        """Scanned PDFs without embedded text streams must not produce OCR_NO_TOKENS.

        Current Bug: invoice_ocr.py lines 3456-3466 omits all_tokens.extend(page_tokens),
        causing all_tokens to remain empty and failing with OCR_NO_TOKENS.
        """
        metro_path = METRO_DIR / "метро.pdf"
        inv = iocr.process_invoice(metro_path)
        error_codes = [e.code for e in inv.validation.errors]
        assert "OCR_NO_TOKENS" not in error_codes, (
            "process_invoice emitted OCR_NO_TOKENS because all_tokens was never populated from page_tokens!"
        )
        assert inv.raw_ocr_evidence is not None
        assert inv.raw_ocr_evidence.get("total_tokens", 0) > 0, (
            f"Expected > 0 raw tokens in evidence, got {inv.raw_ocr_evidence.get('total_tokens')}"
        )


# =====================================================================
# 2. Multi-Page Continuation & Table Reconstruction on Metro
# =====================================================================

class TestMetroMultiPageTableReconstruction:
    """Adversarial and acceptance verification of table continuation on multi-page Metro files."""

    @pytest.mark.skipif(not METRO_DIR.exists(), reason="Acceptance dataset not mounted")
    def test_metro_2_table_detection_and_extraction(self):
        """метро-2.pdf (2 pages) must detect table regions and extract line items.

        Current Bug: Page 1 column header has OCR typos ('Сува вето', 'Гуна ДАС') matching only 2 columns,
        and Page 2 column header contains 'ОБЩО' which triggers false positive _is_summary_line(),
        resulting in 0 detected tables and 0 extracted items.
        """
        metro2_path = METRO_DIR / "метро-2.pdf"
        pages = iocr.load_document(metro2_path)
        all_tokens = []
        for p in pages:
            norm_page, _ = iocr.normalize_page_geometry(p)
            variants = iocr.generate_preprocessing_variants(norm_page.image)
            toks = iocr.run_multiple_ocr_passes(variants)
            for t in toks:
                t.page_number = p.page_number
            all_tokens.extend(toks)

        tokens = iocr.normalize_ocr_tokens(all_tokens)
        lines = iocr.group_tokens_into_lines(tokens)
        tables = iocr.detect_table_regions(lines, tokens)

        assert len(tables) > 0, "detect_table_regions found 0 tables on метро-2.pdf!"
        items = iocr.extract_line_items(tables, lines)
        assert len(items) >= 5, f"Expected at least 5 line items from метро-2.pdf, got {len(items)}"

    @pytest.mark.skipif(not METRO_DIR.exists(), reason="Acceptance dataset not mounted")
    def test_metro_continuous_indexing_across_pages(self):
        """Line items continuing across pages must have continuous 1-based indexing.

        Current Bug: extract_line_items overwrites item.index with int(idx_match.group())
        from random digits in the row (e.g. 400, 124, 1484) instead of sequential 1-based indexing.
        """
        metro_path = METRO_DIR / "метро.pdf"
        pages = iocr.load_document(metro_path)
        all_tokens = []
        for p in pages:
            norm_page, _ = iocr.normalize_page_geometry(p)
            variants = iocr.generate_preprocessing_variants(norm_page.image)
            toks = iocr.run_multiple_ocr_passes(variants)
            for t in toks:
                t.page_number = p.page_number
            all_tokens.extend(toks)

        tokens = iocr.normalize_ocr_tokens(all_tokens)
        lines = iocr.group_tokens_into_lines(tokens)
        tables = iocr.detect_table_regions(lines, tokens)
        items = iocr.extract_line_items(tables, lines)

        assert len(items) > 0, "Expected line items from метро.pdf"
        expected_indices = list(range(1, len(items) + 1))
        actual_indices = [it.index for it in items]
        assert actual_indices == expected_indices, (
            f"Line item indices are not continuous 1-based! Sample: {actual_indices[:15]}..."
        )

    @pytest.mark.skipif(not METRO_DIR.exists(), reason="Acceptance dataset not mounted")
    def test_metro_page_3_has_no_fake_table_projected(self):
        """Page 3 of метро.pdf contains only seller/buyer info and receipt; no table items must be extracted.

        Current Bug: detect_table_regions blindly projects a table onto page 3 and extracts
        buyer/seller text and phone numbers as invoice line items.
        """
        metro_path = METRO_DIR / "метро.pdf"
        pages = iocr.load_document(metro_path)
        all_tokens = []
        for p in pages:
            norm_page, _ = iocr.normalize_page_geometry(p)
            variants = iocr.generate_preprocessing_variants(norm_page.image)
            toks = iocr.run_multiple_ocr_passes(variants)
            for t in toks:
                t.page_number = p.page_number
            all_tokens.extend(toks)

        tokens = iocr.normalize_ocr_tokens(all_tokens)
        lines = iocr.group_tokens_into_lines(tokens)
        tables = iocr.detect_table_regions(lines, tokens)
        items = iocr.extract_line_items(tables, lines)

        p3_items = [it for it in items if it.page_number == 3]
        for it in p3_items:
            desc = (it.description or "").lower()
            assert "продавач" not in desc, f"Seller metadata falsely extracted as line item: {it.description}"
            assert "получател" not in desc, f"Buyer metadata falsely extracted as line item: {it.description}"
            assert "телефон" not in desc, f"Phone line falsely extracted as line item: {it.description}"
        assert len(p3_items) == 0, f"Expected 0 line items from Page 3 of метро.pdf, got {len(p3_items)}"


# =====================================================================
# 3. Kapina-03 Acceptance: 17 Items & Receipt Box Isolation
# =====================================================================

class TestKapina03AcceptanceAndOcclusion:
    """Acceptance tests for капина-03.pdf: 17 line items, null fallback, and receipt block isolation."""

    @pytest.mark.skipif(not KAPINA_DIR.exists(), reason="Acceptance dataset not mounted")
    def test_kapina_03_extracts_17_line_items(self):
        """капина-03.pdf must extract exactly 17 line items without collapsing occluded rows into prior items.

        Current Bug: Rows whose numeric columns are occluded by the thermal receipt have
        qty=None, price=None, total=None. extract_line_items treats them as continuation rows
        and appends them to previous items, reducing 17 items to 11 items.
        """
        p = KAPINA_DIR / "капина-03.pdf"
        inv = iocr.process_invoice(p)
        assert len(inv.line_items) == 17, (
            f"Expected exactly 17 line items for капина-03.pdf, got {len(inv.line_items)}"
        )

    @pytest.mark.skipif(not KAPINA_DIR.exists(), reason="Acceptance dataset not mounted")
    def test_kapina_03_receipt_block_isolation(self):
        """Thermal cash receipt must be isolated into its own LogicalBlock(block_type='receipt').

        Current Bug: group_lines_into_blocks creates a giant 40-line block covering the entire page
        (header, parties, table, receipt) and marks the entire invoice as block_type='receipt'.
        """
        p = KAPINA_DIR / "капина-03.pdf"
        pages = iocr.load_document(p)
        norm_page, _ = iocr.normalize_page_geometry(pages[0])
        variants = iocr.generate_preprocessing_variants(norm_page.image)
        tokens = iocr.run_multiple_ocr_passes(variants)
        lines = iocr.group_tokens_into_lines(tokens)
        blocks = iocr.group_lines_into_blocks(lines)

        receipt_blocks = [b for b in blocks if b.block_type == "receipt"]
        assert len(receipt_blocks) >= 1, "Expected at least one receipt block"
        for rb in receipt_blocks:
            # The receipt box should NOT contain the invoice title and header lines
            assert "фактура" not in rb.text_lower, (
                f"Receipt block swallowed invoice header! Text: {rb.text[:100]}"
            )
            assert rb.height < 1500, f"Receipt block height too large ({rb.height}px); whole page swallowed!"


# =====================================================================
# 4. Acceptance on Kapina-01 and Kapina-02
# =====================================================================

class TestKapina01And02Acceptance:
    """Acceptance tests for капина-01.pdf and капина-02.pdf."""

    @pytest.mark.skipif(not KAPINA_DIR.exists(), reason="Acceptance dataset not mounted")
    def test_kapina_01_line_items_and_eik(self):
        """капина-01.pdf extracts 13-14 items and correct party EIKs."""
        p = KAPINA_DIR / "капина-01.pdf"
        inv = iocr.process_invoice(p)
        assert len(inv.line_items) in (13, 14), f"Expected 13-14 items, got {len(inv.line_items)}"
        assert inv.supplier.eik == "114500333", f"Expected supplier EIK 114500333, got {inv.supplier.eik}"
        assert inv.recipient.eik == "207930830", f"Expected recipient EIK 207930830, got {inv.recipient.eik}"

    @pytest.mark.skipif(not KAPINA_DIR.exists(), reason="Acceptance dataset not mounted")
    def test_kapina_02_line_items_and_multi_line_descriptions(self):
        """капина-02.pdf extracts 18 items with multi-line wrapped descriptions."""
        p = KAPINA_DIR / "капина-02.pdf"
        inv = iocr.process_invoice(p)
        assert len(inv.line_items) == 18, f"Expected 18 items, got {len(inv.line_items)}"
        assert inv.supplier.eik == "114500333", f"Expected supplier EIK 114500333, got {inv.supplier.eik}"
        assert inv.recipient.eik == "207930830", f"Expected recipient EIK 207930830, got {inv.recipient.eik}"


# =====================================================================
# 5. External Volume Zero-Touch Protection
# =====================================================================

class TestExternalVolumeImmutability:
    """Verifies that /Volumes/NO NAME/_ФАКТУРИ has strictly zero modifications."""

    @pytest.mark.skipif(not Path("/Volumes/NO NAME/_ФАКТУРИ").exists(), reason="Acceptance dataset not mounted")
    def test_no_files_modified_in_external_volume(self):
        """No files in the external dataset volume were modified after initial copy."""
        import subprocess
        cmd = ["find", "/Volumes/NO NAME/_ФАКТУРИ", "!", "-path", "*/00_РМ_КАСКАДА_2026_ЕООД*", "-newerct", "2026-09-02"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0
        assert res.stdout.strip() == "", f"Files modified in external volume: {res.stdout}"

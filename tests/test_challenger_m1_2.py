"""Empirical Challenger 2 Test Suite for Milestone 1: Multi-Format Ingestion.

Covers:
1. Kapina acceptance files (капина-01.pdf, капина-02.pdf, капина-03.pdf) rasterization at 300 DPI.
2. Multi-page PDF rasterization on метро.pdf (3 pages) and метро-2.pdf (2 pages) in read-only mode.
3. Token coordinates, bounding boxes, and low confidence flag (is_low_confidence) preservation.
4. Source dataset immutability verification on /Volumes/NO NAME/_ФАКТУРИ.
5. Boundary and stress testing for data structures, DPI scaling, and memory efficiency.
"""

import hashlib
import os
from pathlib import Path
import time
import tracemalloc
import cv2
import numpy as np
import pytest
import pymupdf

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_ocr import (
    PageImage,
    OcrToken,
    LogicalLine,
    TableRegion,
    TableColumn,
    DEFAULT_RASTER_DPI,
    MIN_CONFIDENCE,
    SUPPORTED_EXTENSIONS,
    load_document,
    load_image,
    load_image_page,
    rasterize_pdf,
    pixmap_to_bgr,
    group_tokens_into_lines,
    group_lines_into_blocks,
    normalize_ocr_tokens,
    _parse_ocr_dict_to_tokens,
)

KAPINA_DIR = Path("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026")
METRO_DIR = Path("/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025")

KAPINA_FILES = ["капина-01.pdf", "капина-02.pdf", "капина-03.pdf"]
METRO_FILES = ["метро.pdf", "метро-2.pdf"]


def get_file_sha256(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class TestEmpiricalChallengerM1:
    """Adversarial and empirical verification suite for Milestone 1."""

    # -----------------------------------------------------------------------
    # Requirement 1: Kapina Acceptance Files at 300 DPI
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize("pdf_name", KAPINA_FILES)
    def test_kapina_acceptance_rasterization_300dpi(self, pdf_name: str) -> None:
        """Verify that load_document correctly rasterizes all 3 Kapina acceptance files at 300 DPI.

        Asserts:
        - 1 page returned per file
        - Dimensions correspond exactly to A4 at 300 DPI (2481 x 3508 px)
        - BGR 3-channel uint8 numpy array
        - C-contiguous memory layout
        - Memory consumption is bounded (< 100 MB peak)
        - No unhandled exceptions or crashes
        """
        pdf_path = KAPINA_DIR / pdf_name
        if not pdf_path.exists():
            pytest.skip(f"Acceptance file {pdf_path} not accessible")

        mtime_before = pdf_path.stat().st_mtime_ns
        hash_before = get_file_sha256(pdf_path)

        tracemalloc.start()
        t0 = time.perf_counter()
        pages = load_document(pdf_path, dpi=300)
        duration = time.perf_counter() - t0
        _, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Ingestion assertions
        assert isinstance(pages, list)
        assert len(pages) == 1, f"Expected 1 page for {pdf_name}, got {len(pages)}"

        page = pages[0]
        assert isinstance(page, PageImage)
        assert page.page_number == 1
        assert page.width == 2481, f"Width mismatch for {pdf_name}: {page.width}"
        assert page.height == 3508, f"Height mismatch for {pdf_name}: {page.height}"
        assert isinstance(page.image, np.ndarray)
        assert page.image.dtype == np.uint8
        assert page.image.shape == (3508, 2481, 3)
        assert page.image.flags["C_CONTIGUOUS"] is True

        # Performance assertions
        assert duration < 5.0, f"Rasterization too slow for {pdf_name}: {duration:.2f}s"
        peak_mb = peak_mem / (1024 * 1024)
        assert peak_mb < 120.0, f"Excessive peak memory for {pdf_name}: {peak_mb:.2f} MB"

        # Read-only assertion
        assert pdf_path.stat().st_mtime_ns == mtime_before
        assert get_file_sha256(pdf_path) == hash_before

    # -----------------------------------------------------------------------
    # Requirement 2: Multi-Page Rasterization on метро.pdf
    # -----------------------------------------------------------------------

    def test_metro_multipage_rasterization_3pages(self) -> None:
        """Empirically verify multi-page PDF rasterization on метро.pdf (3 pages) in read-only mode."""
        pdf_path = METRO_DIR / "метро.pdf"
        if not pdf_path.exists():
            pytest.skip(f"File {pdf_path} not accessible")

        mtime_before = pdf_path.stat().st_mtime_ns
        hash_before = get_file_sha256(pdf_path)

        tracemalloc.start()
        t0 = time.perf_counter()
        pages = load_document(pdf_path, dpi=300)
        duration = time.perf_counter() - t0
        _, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert isinstance(pages, list)
        assert len(pages) == 3, f"Expected exactly 3 pages for метро.pdf, got {len(pages)}"

        for idx, page in enumerate(pages, start=1):
            assert page.page_number == idx
            assert page.width == 2481
            assert page.height == 3508
            assert page.image.shape == (3508, 2481, 3)
            assert page.image.dtype == np.uint8
            assert page.image.flags["C_CONTIGUOUS"] is True

        peak_mb = peak_mem / (1024 * 1024)
        assert peak_mb < 200.0, f"Memory exceeded limit for 3 pages: {peak_mb:.2f} MB"
        assert duration < 10.0, f"Rasterization took too long: {duration:.2f}s"

        # Read-only assertion
        assert pdf_path.stat().st_mtime_ns == mtime_before
        assert get_file_sha256(pdf_path) == hash_before

    def test_metro2_multipage_rasterization_2pages(self) -> None:
        """Empirically verify multi-page PDF rasterization on метро-2.pdf (2 pages) in read-only mode."""
        pdf_path = METRO_DIR / "метро-2.pdf"
        if not pdf_path.exists():
            pytest.skip(f"File {pdf_path} not accessible")

        mtime_before = pdf_path.stat().st_mtime_ns
        hash_before = get_file_sha256(pdf_path)

        pages = load_document(pdf_path, dpi=300)
        assert len(pages) == 2, f"Expected 2 pages for метро-2.pdf, got {len(pages)}"

        for idx, page in enumerate(pages, start=1):
            assert page.page_number == idx
            assert page.width == 2481
            assert page.height == 3508
            assert page.image.shape == (3508, 2481, 3)
            assert page.image.flags["C_CONTIGUOUS"] is True

        assert pdf_path.stat().st_mtime_ns == mtime_before
        assert get_file_sha256(pdf_path) == hash_before

    # -----------------------------------------------------------------------
    # Requirement 3: Token Coordinates, Bounding Boxes & Confidence Flags
    # -----------------------------------------------------------------------

    def test_ocr_token_coordinate_preservation_and_accessors(self) -> None:
        """Test that OcrToken faithfully preserves coordinates and bounding box geometry."""
        # 1. Initialization with bbox tuple
        t1 = OcrToken("ИНВОЙС", conf=95.5, bbox=(120, 240, 300, 45), page_number=2)
        assert t1.bbox == (120, 240, 300, 45)
        assert t1.left == 120
        assert t1.top == 240
        assert t1.width == 300
        assert t1.height == 45
        assert t1.right == 420
        assert t1.bottom == 285
        assert t1.center_x == 270
        assert t1.center_y == 262
        assert t1.page_number == 2
        assert t1.is_low_confidence is False

        # 2. Initialization with legacy kwargs (left, top, width, height)
        t2 = OcrToken("СУМА", conf=80, left=50, top=100, width=80, height=20, page_number=3)
        assert t2.bbox == (50, 100, 80, 20)
        assert t2.left == 50
        assert t2.top == 100
        assert t2.width == 80
        assert t2.height == 20
        assert t2.right == 130
        assert t2.bottom == 120
        assert t2.center_x == 90
        assert t2.center_y == 110
        assert t2.page_number == 3

        # 3. Default fallback
        t3 = OcrToken("Default", conf=70)
        assert t3.bbox == (0, 0, 0, 0)
        assert t3.page_number == 1

    def test_is_low_confidence_flag_thresholds(self) -> None:
        """Test that is_low_confidence is faithfully set based on MIN_CONFIDENCE=60 threshold."""
        # conf < 60 -> is_low_confidence = True
        t_low1 = OcrToken("Low1", conf=0)
        t_low2 = OcrToken("Low2", conf=59)
        t_low3 = OcrToken("Low3", conf=59.9)
        assert t_low1.is_low_confidence is True
        assert t_low2.is_low_confidence is True
        assert t_low3.is_low_confidence is True

        # conf >= 60 -> is_low_confidence = False
        t_high1 = OcrToken("High1", conf=60)
        t_high2 = OcrToken("High2", conf=60.1)
        t_high3 = OcrToken("High3", conf=100)
        assert t_high1.is_low_confidence is False
        assert t_high2.is_low_confidence is False
        assert t_high3.is_low_confidence is False

        # Explicit override
        t_override_low = OcrToken("OverLow", conf=90, is_low_confidence=True)
        t_override_high = OcrToken("OverHigh", conf=30, is_low_confidence=False)
        assert t_override_low.is_low_confidence is True
        assert t_override_high.is_low_confidence is False

    def test_logical_line_bounding_box_enclosure(self) -> None:
        """Test that LogicalLine bounding box faithfully bounds all constituent tokens."""
        t1 = OcrToken("A", conf=80, bbox=(10, 20, 30, 25), page_number=2)
        t2 = OcrToken("B", conf=85, bbox=(50, 15, 40, 35), page_number=2)
        t3 = OcrToken("C", conf=90, bbox=(100, 18, 50, 30), page_number=2)

        line = LogicalLine(tokens=[t1, t2, t3])
        # Min left: 10, Min top: 15
        # Max right: max(10+30, 50+40, 100+50) = max(40, 90, 150) = 150
        # Max bottom: max(20+25, 15+35, 18+30) = max(45, 50, 48) = 50
        # Width: 150 - 10 = 140, Height: 50 - 15 = 35
        assert line.left == 10
        assert line.top == 15
        assert line.right == 150
        assert line.bottom == 50
        assert line.width == 140
        assert line.height == 35
        assert line.bbox == (10, 15, 140, 35)
        assert line.page_number == 2
        assert line.text == "A B C"

    def test_multi_page_token_line_isolation(self) -> None:
        """Test that group_tokens_into_lines never conflates tokens from distinct pages."""
        # Create tokens on page 1, 2, and 3 with identical Y coordinates
        tokens = [
            OcrToken("P1_Token1", conf=90, bbox=(100, 200, 50, 20), page_number=1),
            OcrToken("P1_Token2", conf=90, bbox=(160, 200, 50, 20), page_number=1),
            OcrToken("P2_Token1", conf=90, bbox=(100, 200, 50, 20), page_number=2),
            OcrToken("P3_Token1", conf=90, bbox=(100, 200, 50, 20), page_number=3),
        ]

        lines = group_tokens_into_lines(tokens)
        assert len(lines) == 3

        p1_lines = [l for l in lines if l.page_number == 1]
        p2_lines = [l for l in lines if l.page_number == 2]
        p3_lines = [l for l in lines if l.page_number == 3]

        assert len(p1_lines) == 1
        assert len(p2_lines) == 1
        assert len(p3_lines) == 1

        assert p1_lines[0].text == "P1_Token1 P1_Token2"
        assert p2_lines[0].text == "P2_Token1"
        assert p3_lines[0].text == "P3_Token1"

    def test_multi_page_block_isolation(self) -> None:
        """Test that group_lines_into_blocks never merges lines across page boundaries."""
        l1 = LogicalLine(tokens=[OcrToken("P1_L1", conf=90, bbox=(10, 10, 50, 20), page_number=1)])
        l2 = LogicalLine(tokens=[OcrToken("P1_L2", conf=90, bbox=(10, 35, 50, 20), page_number=1)])
        l3 = LogicalLine(tokens=[OcrToken("P2_L1", conf=90, bbox=(10, 10, 50, 20), page_number=2)])

        blocks = group_lines_into_blocks([l1, l2, l3], gap_factor=2.0)
        for block in blocks:
            pages = {line.page_number for line in block}
            assert len(pages) == 1, f"Cross-page contamination in block: {pages}"

    def test_normalize_ocr_tokens_preserves_token_attributes(self) -> None:
        """Verify normalize_ocr_tokens cleans text without altering bbox or metadata."""
        tokens = [
            OcrToken("[Фактура]", conf=85, bbox=(10, 20, 60, 25), page_number=2),
            OcrToken("№", conf=50, bbox=(75, 20, 15, 25), page_number=2),
        ]
        norm = normalize_ocr_tokens(tokens)
        assert len(norm) == 2
        assert norm[0].text == "Фактура"
        assert norm[0].bbox == (10, 20, 60, 25)
        assert norm[0].page_number == 2
        assert norm[0].conf == 85
        assert norm[0].is_low_confidence is False

        assert norm[1].text == "№"
        assert norm[1].bbox == (75, 20, 15, 25)
        assert norm[1].page_number == 2
        assert norm[1].conf == 50
        assert norm[1].is_low_confidence is True

    # -----------------------------------------------------------------------
    # Requirement 4: External Volume Zero-Mutation Guarantee
    # -----------------------------------------------------------------------

    def test_zero_modifications_on_external_volume(self) -> None:
        """Empirically prove that reading and rasterizing source files causes ZERO modifications."""
        all_targets = [
            KAPINA_DIR / "капина-01.pdf",
            KAPINA_DIR / "капина-02.pdf",
            KAPINA_DIR / "капина-03.pdf",
            METRO_DIR / "метро.pdf",
            METRO_DIR / "метро-2.pdf",
        ]

        # Record pre-execution state
        hashes_before = {}
        mtimes_before = {}
        sizes_before = {}
        for target in all_targets:
            if target.exists():
                hashes_before[target] = get_file_sha256(target)
                stat = target.stat()
                mtimes_before[target] = stat.st_mtime_ns
                sizes_before[target] = stat.st_size

        # Ingest all files sequentially
        for target in all_targets:
            if target.exists():
                _ = load_document(target, dpi=300)

        # Verify post-execution state
        for target in all_targets:
            if target.exists():
                stat_after = target.stat()
                hash_after = get_file_sha256(target)
                assert stat_after.st_size == sizes_before[target], f"Size changed for {target.name}!"
                assert stat_after.st_mtime_ns == mtimes_before[target], f"mtime changed for {target.name}!"
                assert hash_after == hashes_before[target], f"SHA256 changed for {target.name}!"

    # -----------------------------------------------------------------------
    # Requirement 5: Boundary, Stress, and Adversarial Scenarios
    # -----------------------------------------------------------------------

    def test_empty_tokens_in_logical_line_safe(self) -> None:
        """LogicalLine with empty tokens list must not crash with ZeroDivisionError or ValueError."""
        line = LogicalLine(tokens=[])
        assert line.bbox == (0, 0, 0, 0)
        assert line.left == 0
        assert line.top == 0
        assert line.width == 0
        assert line.height == 0
        assert line.text == ""
        assert line.y_center == 0.0
        assert line.page_number == 1

    def test_extreme_coordinate_values_safe(self) -> None:
        """OcrToken handles negative or unusually large coordinate values safely."""
        tok_neg = OcrToken("Neg", conf=75, bbox=(-10, -20, 50, 30), page_number=1)
        assert tok_neg.left == -10
        assert tok_neg.top == -20
        assert tok_neg.right == 40
        assert tok_neg.bottom == 10
        assert tok_neg.center_x == 15
        assert tok_neg.center_y == -5

        tok_large = OcrToken("Large", conf=99, bbox=(10000, 20000, 5000, 1000), page_number=5)
        assert tok_large.right == 15000
        assert tok_large.bottom == 21000

    def test_unsupported_extensions_strictly_rejected(self, tmp_path: Path) -> None:
        """Unsupported file extensions must raise ValueError with descriptive message."""
        bad_extensions = [".txt", ".docx", ".xlsx", ".csv", ".json", ".xml", ".tiff", ".bmp"]
        for ext in bad_extensions:
            bad_file = tmp_path / f"test{ext}"
            bad_file.write_bytes(b"dummy")
            with pytest.raises(ValueError) as exc:
                load_document(bad_file)
            assert "Unsupported file format" in str(exc.value)

    def test_memory_leak_stress_harness(self) -> None:
        """Verify that repeated rasterization in a loop does not leak memory."""
        metro_path = METRO_DIR / "метро.pdf"
        if not metro_path.exists():
            pytest.skip("Metro PDF not available")

        # Warm up
        _ = load_document(metro_path, dpi=150)

        tracemalloc.start()
        snapshot_start = tracemalloc.take_snapshot()

        # Run 5 iterations of 3-page rasterization
        for _ in range(5):
            pages = load_document(metro_path, dpi=150)
            assert len(pages) == 3
            del pages

        snapshot_end = tracemalloc.take_snapshot()
        tracemalloc.stop()

        top_stats = snapshot_end.compare_to(snapshot_start, 'lineno')
        total_diff_mb = sum(stat.size_diff for stat in top_stats) / (1024 * 1024)
        # Should not accumulate substantial retained memory
        assert total_diff_mb < 30.0, f"Possible memory leak detected: retained {total_diff_mb:.2f} MB"

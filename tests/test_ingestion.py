"""Unit tests for Milestone 1: Multi-Format Ingestion in invoice_ocr.py.

Covers:
- Single-page PDF ingestion
- Multi-page PDF ingestion
- PNG/JPG image ingestion
- Unsupported file types rejection
- Missing files error handling
- Corrupted/empty PDF and image rejection
- Password-protected PDF rejection
- Read-only source file integrity protection
- DPI scaling proportionality
- Path and str argument acceptance
- Backward-compatible load_image()
- Contiguous BGR pixmap conversion
- OcrToken bbox, properties, and low-confidence flagging
- Page-aware line and block grouping
- Real acceptance PDF read-only ingestion verification
"""

import hashlib
import os
from pathlib import Path
import stat
import sys
import cv2
import numpy as np
import pytest
import pymupdf

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_ocr import (
    PageImage,
    OcrToken,
    LogicalLine,
    TableRegion,
    SUPPORTED_EXTENSIONS,
    DEFAULT_RASTER_DPI,
    pixmap_to_bgr,
    rasterize_pdf,
    load_image_page,
    load_document,
    load_image,
    group_tokens_into_lines,
    group_lines_into_blocks,
)


class TestMultiFormatIngestion:
    """Comprehensive test suite for document ingestion and multi-page data models."""

    def test_single_page_pdf_ingestion(self, tmp_path: Path) -> None:
        """Verify single-page PDF ingestion returns 1 PageImage with valid dimensions."""
        pdf_path = tmp_path / "single_page.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)  # Standard A4 in points
        page.insert_text((50, 100), "Single Page Invoice Test")
        doc.save(str(pdf_path))
        doc.close()

        # Ingest at 300 DPI
        pages = load_document(pdf_path, dpi=300)

        assert isinstance(pages, list)
        assert len(pages) == 1

        p1 = pages[0]
        assert isinstance(p1, PageImage)
        assert p1.page_number == 1
        assert isinstance(p1.image, np.ndarray)
        assert p1.image.dtype == np.uint8
        assert p1.image.ndim == 3
        assert p1.image.shape[2] == 3  # BGR 3-channel
        assert p1.image.flags["C_CONTIGUOUS"] is True
        assert p1.width == p1.image.shape[1]
        assert p1.height == p1.image.shape[0]
        # At 300 DPI: 595 pt * 300/72 ~ 2479 px, 842 pt * 300/72 ~ 3508 px
        assert 2470 <= p1.width <= 2490
        assert 3500 <= p1.height <= 3520

    def test_multi_page_pdf_ingestion(self, tmp_path: Path) -> None:
        """Verify multi-page PDF ingestion returns N PageImages with sequential 1-based page numbers."""
        pdf_path = tmp_path / "multi_page.pdf"
        doc = pymupdf.open()
        for i in range(3):
            p = doc.new_page(width=595, height=842)
            p.insert_text((50, 50), f"Page {i + 1} Content")
        doc.save(str(pdf_path))
        doc.close()

        pages = load_document(pdf_path, dpi=300)

        assert len(pages) == 3
        for idx, p in enumerate(pages, start=1):
            assert p.page_number == idx
            assert p.width == p.image.shape[1]
            assert p.height == p.image.shape[0]
            assert p.image.ndim == 3
            assert p.image.shape[2] == 3
            assert p.image.flags["C_CONTIGUOUS"] is True

    def test_image_files_png_and_jpg(self, tmp_path: Path) -> None:
        """Verify image files (.png, .jpg, .jpeg) return 1 PageImage with page_number=1."""
        # 1. Test PNG
        png_path = tmp_path / "invoice_scan.png"
        dummy_png = np.zeros((400, 300, 3), dtype=np.uint8)
        dummy_png[:, :, 0] = 255  # Blue tint
        cv2.imwrite(str(png_path), dummy_png)

        png_pages = load_document(png_path)
        assert len(png_pages) == 1
        assert png_pages[0].page_number == 1
        assert png_pages[0].width == 300
        assert png_pages[0].height == 400
        assert png_pages[0].image.shape == (400, 300, 3)

        # 2. Test JPG
        jpg_path = tmp_path / "invoice_photo.jpg"
        dummy_jpg = np.ones((500, 350, 3), dtype=np.uint8) * 200
        cv2.imwrite(str(jpg_path), dummy_jpg)

        jpg_pages = load_document(jpg_path)
        assert len(jpg_pages) == 1
        assert jpg_pages[0].page_number == 1
        assert jpg_pages[0].width == 350
        assert jpg_pages[0].height == 500

    def test_unsupported_file_types_raise_clean_value_error(self, tmp_path: Path) -> None:
        """Verify unsupported file types (.txt, .docx, .csv) raise clean ValueError."""
        txt_path = tmp_path / "notes.txt"
        txt_path.write_text("This is an invoice text note.")

        with pytest.raises(ValueError) as exc_info:
            load_document(txt_path)
        assert "Unsupported file format" in str(exc_info.value)
        assert ".txt" in str(exc_info.value)

        docx_path = tmp_path / "contract.docx"
        docx_path.write_bytes(b"PK\x03\x04fake docx data")
        with pytest.raises(ValueError) as exc_info:
            load_document(docx_path)
        assert "Unsupported file format" in str(exc_info.value)
        assert ".docx" in str(exc_info.value)

    def test_missing_files_raise_file_not_found_error(self, tmp_path: Path) -> None:
        """Verify missing files raise FileNotFoundError."""
        missing_pdf = tmp_path / "non_existent.pdf"
        with pytest.raises(FileNotFoundError):
            load_document(missing_pdf)

        missing_png = tmp_path / "non_existent.png"
        with pytest.raises(FileNotFoundError):
            load_document(missing_png)

    def test_corrupted_pdf_and_image_raise_value_error(self, tmp_path: Path) -> None:
        """Verify corrupted or zero-byte files raise clean ValueError."""
        # 1. Zero-byte PDF
        empty_pdf = tmp_path / "empty.pdf"
        empty_pdf.write_bytes(b"")
        with pytest.raises(ValueError) as exc_info:
            load_document(empty_pdf)
        assert "Failed to open PDF" in str(exc_info.value)

        # 2. Corrupted PDF (invalid binary header)
        corrupt_pdf = tmp_path / "corrupt.pdf"
        corrupt_pdf.write_bytes(b"NOT_A_VALID_PDF_STREAM_12345")
        with pytest.raises(ValueError) as exc_info:
            load_document(corrupt_pdf)
        assert "Failed to open PDF" in str(exc_info.value)

        # 3. Zero-byte image
        empty_img = tmp_path / "empty.png"
        empty_img.write_bytes(b"")
        with pytest.raises(ValueError) as exc_info:
            load_document(empty_img)
        assert "Failed to decode image" in str(exc_info.value)

        # 4. Corrupted image file
        corrupt_img = tmp_path / "corrupt.png"
        corrupt_img.write_bytes(b"NOT_A_VALID_PNG_STREAM")
        with pytest.raises(ValueError) as exc_info:
            load_document(corrupt_img)
        assert "Failed to decode image" in str(exc_info.value)

    def test_password_protected_pdf_raises_value_error(self, tmp_path: Path) -> None:
        """Verify encrypted/password-protected PDFs raise clean ValueError."""
        enc_pdf = tmp_path / "protected.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(
            str(enc_pdf),
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            user_pw="password123",
            owner_pw="password123",
        )
        doc.close()

        with pytest.raises(ValueError) as exc_info:
            load_document(enc_pdf)
        err_msg = str(exc_info.value).lower()
        assert "password" in err_msg or "encrypted" in err_msg

    def test_read_only_guarantee_on_source_files(self, tmp_path: Path) -> None:
        """Verify strict read-only guarantee: permissions, mtime, and hashes remain untouched."""
        ro_dir = tmp_path / "protected_store"
        ro_dir.mkdir()
        pdf_path = ro_dir / "invoice.pdf"

        # Create sample PDF
        doc = pymupdf.open()
        p = doc.new_page(width=595, height=842)
        p.insert_text((50, 100), "Read-Only Security Verification")
        doc.save(str(pdf_path))
        doc.close()

        # Capture baseline file signature
        initial_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        initial_stat = pdf_path.stat()
        initial_mtime = initial_stat.st_mtime_ns
        dir_entries_before = set(os.listdir(ro_dir))

        # Apply read-only mode to both file (0444) and directory (0555)
        os.chmod(pdf_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.chmod(ro_dir, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

        try:
            # Execute load_document on read-only target
            pages = load_document(pdf_path, dpi=300)
            assert len(pages) == 1
            assert pages[0].image.shape[0] > 0

            # Re-read file signature
            final_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            final_stat = pdf_path.stat()
            final_mtime = final_stat.st_mtime_ns
            dir_entries_after = set(os.listdir(ro_dir))

            # Strictly assert invariant integrity
            assert initial_hash == final_hash, "Violation: File SHA-256 hash changed during load_document!"
            assert initial_mtime == final_mtime, "Violation: File modification timestamp changed!"
            assert dir_entries_before == dir_entries_after, "Violation: Extraneous or temporary files created in directory!"
        finally:
            # Restore permissions for pytest cleanup
            os.chmod(ro_dir, stat.S_IRWXU)
            os.chmod(pdf_path, stat.S_IRUSR | stat.S_IWUSR)

    def test_dpi_scaling_proportionality(self, tmp_path: Path) -> None:
        """Verify that DPI parameter scales rasterized image dimensions proportionally."""
        pdf_path = tmp_path / "dpi_test.pdf"
        doc = pymupdf.open()
        doc.new_page(width=200, height=400)  # 200x400 pt
        doc.save(str(pdf_path))
        doc.close()

        pages_150 = load_document(pdf_path, dpi=150)
        pages_300 = load_document(pdf_path, dpi=300)

        # 300 DPI should be roughly 2x dimensions of 150 DPI
        ratio_w = pages_300[0].width / pages_150[0].width
        ratio_h = pages_300[0].height / pages_150[0].height
        assert 1.95 <= ratio_w <= 2.05
        assert 1.95 <= ratio_h <= 2.05

        # Verify non-positive DPI raises ValueError
        with pytest.raises(ValueError, match="Invalid rasterization DPI: 0"):
            load_document(pdf_path, dpi=0)
        with pytest.raises(ValueError, match="Invalid rasterization DPI: -50"):
            load_document(pdf_path, dpi=-50)

    def test_accepts_both_str_and_path_inputs(self, tmp_path: Path) -> None:
        """Verify load_document works identically with str and Path arguments."""
        pdf_path = tmp_path / "type_test.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(pdf_path))
        doc.close()

        res_path = load_document(pdf_path)
        res_str = load_document(str(pdf_path))

        assert len(res_path) == len(res_str) == 1
        assert res_path[0].width == res_str[0].width
        assert res_path[0].height == res_str[0].height

    def test_load_image_backward_compatibility(self, tmp_path: Path) -> None:
        """Verify load_image returns a single OpenCV BGR numpy array."""
        pdf_path = tmp_path / "compat.pdf"
        doc = pymupdf.open()
        doc.new_page(width=300, height=500)
        doc.save(str(pdf_path))
        doc.close()

        img = load_image(pdf_path)
        assert isinstance(img, np.ndarray)
        assert img.ndim == 3
        assert img.shape[2] == 3

    def test_pixmap_to_bgr_contiguity(self) -> None:
        """Verify pixmap_to_bgr produces C-contiguous BGR arrays for RGB, Grayscale, and RGBA."""
        doc = pymupdf.open()
        page = doc.new_page(width=100, height=100)

        # RGB
        pix_rgb = page.get_pixmap(colorspace=pymupdf.csRGB, alpha=False)
        bgr_rgb = pixmap_to_bgr(pix_rgb)
        assert bgr_rgb.flags["C_CONTIGUOUS"] is True
        assert bgr_rgb.shape == (pix_rgb.height, pix_rgb.width, 3)

        # Grayscale
        pix_gray = page.get_pixmap(colorspace=pymupdf.csGRAY, alpha=False)
        bgr_gray = pixmap_to_bgr(pix_gray)
        assert bgr_gray.flags["C_CONTIGUOUS"] is True
        assert bgr_gray.shape == (pix_gray.height, pix_gray.width, 3)

        # RGBA
        pix_rgba = page.get_pixmap(colorspace=pymupdf.csRGB, alpha=True)
        bgr_rgba = pixmap_to_bgr(pix_rgba)
        assert bgr_rgba.flags["C_CONTIGUOUS"] is True
        assert bgr_rgba.shape == (pix_rgba.height, pix_rgba.width, 3)

        # CMYK
        pix_cmyk = pymupdf.Pixmap(pymupdf.csCMYK, pymupdf.IRect(0, 0, 50, 50), False)
        bgr_cmyk = pixmap_to_bgr(pix_cmyk)
        assert bgr_cmyk.flags["C_CONTIGUOUS"] is True
        assert bgr_cmyk.shape == (50, 50, 3)

        doc.close()

    def test_ocr_token_bbox_and_properties(self) -> None:
        """Verify OcrToken bbox, backward-compatible properties, and low-confidence flagging."""
        # High confidence token
        t_high = OcrToken("Фактура", conf=92, left=10, top=20, width=50, height=30, page_number=1)
        assert t_high.bbox == (10, 20, 50, 30)
        assert t_high.left == 10
        assert t_high.top == 20
        assert t_high.width == 50
        assert t_high.height == 30
        assert t_high.right == 60
        assert t_high.bottom == 50
        assert t_high.center_x == 35
        assert t_high.center_y == 35
        assert t_high.page_number == 1
        assert t_high.is_low_confidence is False

        # Low confidence token (conf < 60)
        t_low = OcrToken("ДДС", conf=45, bbox=(100, 150, 40, 20), page_number=2)
        assert t_low.bbox == (100, 150, 40, 20)
        assert t_low.left == 100
        assert t_low.top == 150
        assert t_low.right == 140
        assert t_low.bottom == 170
        assert t_low.page_number == 2
        assert t_low.is_low_confidence is True

    def test_logical_line_and_grouping_page_separation(self) -> None:
        """Verify group_tokens_into_lines and group_lines_into_blocks respect page boundaries."""
        # Tokens on Page 1 and Page 2 at identical coordinates
        t1 = OcrToken("Page1_Line1", conf=90, bbox=(100, 200, 50, 20), page_number=1)
        t2 = OcrToken("Page1_Line2", conf=90, bbox=(100, 250, 50, 20), page_number=1)
        t3 = OcrToken("Page2_Line1", conf=90, bbox=(100, 200, 50, 20), page_number=2)

        lines = group_tokens_into_lines([t1, t2, t3])
        assert len(lines) == 3

        # First two lines should be page 1, third line page 2
        p1_lines = [line for line in lines if line.page_number == 1]
        p2_lines = [line for line in lines if line.page_number == 2]
        assert len(p1_lines) == 2
        assert len(p2_lines) == 1
        assert p1_lines[0].page_number == 1
        assert p2_lines[0].page_number == 2

        # Blocks should never cross page boundaries
        blocks = group_lines_into_blocks(lines, gap_factor=1.0)
        for block in blocks:
            pages = {line.page_number for line in block}
            assert len(pages) == 1, "Block must contain lines from only a single page!"

        # Verify LogicalLine contract: tokens, bbox, text, page_number, y_center
        line_contract = LogicalLine([t1], (100, 200, 50, 20), "Explicit Text", 1, 210.0)
        assert line_contract.tokens == [t1]
        assert line_contract.bbox == (100, 200, 50, 20)
        assert line_contract.text == "Explicit Text"
        assert line_contract.page_number == 1
        assert line_contract.y_center == 210.0

        # Verify default auto-calculation of bbox, text, y_center when not provided
        line_default = LogicalLine([t1])
        assert line_default.tokens == [t1]
        assert line_default.bbox == (100, 200, 50, 20)
        assert line_default.text == "Page1_Line1"
        assert line_default.page_number == 1
        assert line_default.y_center == 210.0

    def test_acceptance_kapina_pdf_read_only_ingestion(self) -> None:
        """Verify real Kapina acceptance PDF can be ingested read-only without modifying anything."""
        kapina_path = Path("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf")
        if not kapina_path.exists():
            pytest.skip("Acceptance volume /Volumes/NO NAME/_ФАКТУРИ is not mounted")

        initial_stat = kapina_path.stat()
        initial_mtime = initial_stat.st_mtime_ns
        initial_size = initial_stat.st_size

        pages = load_document(kapina_path, dpi=DEFAULT_RASTER_DPI)
        assert len(pages) == 1
        assert pages[0].page_number == 1
        assert pages[0].width == 2481
        assert pages[0].height == 3508
        assert pages[0].image.shape == (3508, 2481, 3)

        # Strictly verify zero file changes
        final_stat = kapina_path.stat()
        assert initial_mtime == final_stat.st_mtime_ns, "CRITICAL: Source file mtime was modified!"
        assert initial_size == final_stat.st_size, "CRITICAL: Source file size was modified!"

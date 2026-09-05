"""Adversarial stress testing suite for Milestone 1 (Multi-Format Ingestion).

Conducted by Challenger 1.
Tests:
1. Corrupted byte streams, truncated PDFs, missing xref tables.
2. Multi-page PDFs with varying dimensions and mixed orientations.
3. Zero-byte files, non-PDF files disguised with .pdf extension.
4. File descriptor and memory leak stress tests.
5. Ungraceful crash reproductions on malformed page structures and extreme media boxes.
6. Read-only verification of source datasets.
"""

import os
import sys
import gc
import subprocess
from pathlib import Path
import cv2
import numpy as np
import pytest
import pymupdf

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_ocr import (
    load_document,
    rasterize_pdf,
    PageImage,
    DEFAULT_RASTER_DPI,
    SUPPORTED_EXTENSIONS,
)


class TestAdversarialCorruptedStreams:
    """Stress testing corrupted byte streams, truncated PDFs, and missing xref tables."""

    @pytest.mark.parametrize("size", [1, 7, 64, 512, 4096, 65536])
    def test_random_noise_streams_raise_clean_value_error(self, tmp_path: Path, size: int) -> None:
        """Verify arbitrary random byte streams disguised as .pdf raise clean ValueError."""
        noise_file = tmp_path / f"noise_{size}.pdf"
        noise_file.write_bytes(os.urandom(size))

        with pytest.raises(ValueError) as exc_info:
            load_document(noise_file)
        assert "Failed to open PDF" in str(exc_info.value) or "Failed to rasterize" in str(exc_info.value)

    def test_truncated_pdf_header_and_missing_xref_raises_value_error(self, tmp_path: Path) -> None:
        """Verify truncated PDF containing partial header but no catalog/pages raises clean ValueError."""
        p = tmp_path / "truncated_header.pdf"
        p.write_bytes(b"%PDF-1.4\n%FakeBinary\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\n")

        with pytest.raises(ValueError) as exc_info:
            load_document(p)
        assert isinstance(exc_info.value, ValueError)

    def test_pdf_with_corrupted_xref_offset_repaired_or_raises_value_error(self, tmp_path: Path) -> None:
        """Verify PDF with corrupted startxref offset either self-repairs or raises clean ValueError."""
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 50), "Xref Corruption Test")
        valid_bytes = doc.tobytes()
        doc.close()

        xref_idx = valid_bytes.rfind(b"startxref")
        assert xref_idx != -1
        corrupt_bytes = valid_bytes[:xref_idx] + b"startxref\n999999999\n%%EOF"

        p = tmp_path / "corrupt_xref.pdf"
        p.write_bytes(corrupt_bytes)

        try:
            pages = load_document(p)
            assert len(pages) == 1
            assert isinstance(pages[0], PageImage)
        except Exception as exc:
            assert isinstance(exc, ValueError), f"Expected clean ValueError, got {type(exc).__name__}: {exc}"

    def test_pdf_with_corrupted_flate_stream(self, tmp_path: Path) -> None:
        """Verify PDF with corrupt compressed stream does not crash ungracefully."""
        pdf_content = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>\nendobj\n"
            b"4 0 obj\n<< /Length 40 /Filter /FlateDecode >>\nstream\n"
            b"INVALID_NON_ZLIB_CORRUPTED_STREAM_DATA_12345\nendstream\nendobj\n"
            b"xref\n0 5\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n"
            b"0000000115 00000 n \n0000000201 00000 n \ntrailer\n<< /Size 5 /Root 1 0 R >>\n"
            b"startxref\n330\n%%EOF\n"
        )
        p = tmp_path / "corrupt_flate.pdf"
        p.write_bytes(pdf_content)

        try:
            pages = load_document(p)
            assert len(pages) == 1
        except Exception as exc:
            assert isinstance(exc, ValueError), f"Expected clean ValueError, got {type(exc).__name__}: {exc}"


class TestAdversarialDisguisedAndZeroByteFiles:
    """Stress testing disguised extensions, zero-byte files, and non-PDF payloads."""

    def test_zero_byte_pdf(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.pdf"
        p.write_bytes(b"")
        with pytest.raises(ValueError):
            load_document(p)

    def test_zero_byte_images(self, tmp_path: Path) -> None:
        for ext in [".png", ".jpg", ".jpeg"]:
            p = tmp_path / f"empty{ext}"
            p.write_bytes(b"")
            with pytest.raises(ValueError):
                load_document(p)

    def test_plain_text_disguised_as_pdf_raises_value_error(self, tmp_path: Path) -> None:
        p = tmp_path / "disguised_text.pdf"
        p.write_text("This is an invoice text file, not a real PDF binary document.")
        with pytest.raises(ValueError):
            load_document(p)

    def test_zip_archive_disguised_as_pdf_raises_value_error(self, tmp_path: Path) -> None:
        p = tmp_path / "disguised_zip.pdf"
        p.write_bytes(b"PK\x03\x04\x14\x00\x00\x00" + b"X" * 128)
        with pytest.raises(ValueError):
            load_document(p)

    def test_binary_executable_disguised_as_pdf_raises_value_error(self, tmp_path: Path) -> None:
        p = tmp_path / "disguised_bin.pdf"
        p.write_bytes(b"\x7fELF" + b"\x00" * 256)
        with pytest.raises(ValueError):
            load_document(p)

    def test_non_image_disguised_as_png_and_jpg_raises_value_error(self, tmp_path: Path) -> None:
        for ext in [".png", ".jpg", ".jpeg"]:
            p = tmp_path / f"disguised{ext}"
            p.write_bytes(b"%PDF-1.4 Fake PDF Header in image extension")
            with pytest.raises(ValueError):
                load_document(p)


class TestAdversarialMultiPageDimensionsAndOrientations:
    """Stress testing multi-page PDFs with varying dimensions and mixed orientations."""

    def test_varying_dimensions_and_rotations(self, tmp_path: Path) -> None:
        """Verify multi-page PDF with 5 different aspect ratios and rotations produces valid PageImages."""
        p = tmp_path / "mixed_pages.pdf"
        doc = pymupdf.open()

        specs = [
            (595, 842, 0, "A4 Portrait 0"),
            (842, 595, 0, "A4 Landscape 0"),
            (612, 792, 90, "Letter 90"),
            (300, 300, 180, "Square 180"),
            (200, 1200, 270, "Till roll 270"),
        ]

        for w, h, rot, label in specs:
            page = doc.new_page(width=w, height=h)
            page.set_rotation(rot)
            page.insert_text((50, 50), label)

        doc.save(str(p))
        doc.close()

        pages = load_document(p, dpi=300)
        assert len(pages) == 5

        for idx, page in enumerate(pages, 1):
            assert page.page_number == idx
            assert page.image.ndim == 3
            assert page.image.shape[2] == 3
            assert page.image.flags["C_CONTIGUOUS"] is True
            assert page.width == page.image.shape[1]
            assert page.height == page.image.shape[0]

        # Verify page 1 vs page 2 orientation differences
        assert pages[0].height > pages[0].width  # Page 1 portrait
        assert pages[1].width > pages[1].height  # Page 2 landscape
        # Page 3: Letter rotated 90 deg -> width > height
        assert pages[2].width > pages[2].height
        # Page 5: 200x1200 rotated 270 deg -> width 5000, height 834
        assert pages[4].width > pages[4].height


class TestAdversarialResourceLeaks:
    """Stress testing file descriptor leaks and memory leaks under repeated ingestion."""

    def test_file_descriptor_leak_invariance(self, tmp_path: Path) -> None:
        """Verify open file descriptor count does not drift after repeated successful & failed ingestions."""
        pdf_path = tmp_path / "leak_test.pdf"
        doc = pymupdf.open()
        for i in range(3):
            p = doc.new_page()
            p.insert_text((50, 50), f"Leak test page {i}")
        doc.save(str(pdf_path))
        doc.close()

        png_path = tmp_path / "leak_test.png"
        cv2.imwrite(str(png_path), np.zeros((100, 100, 3), dtype=np.uint8))

        corrupt_path = tmp_path / "corrupt_leak.pdf"
        corrupt_path.write_bytes(b"Corrupt data for leak test")

        missing_path = tmp_path / "missing_leak.pdf"

        gc.collect()
        fd_dir = Path("/dev/fd")
        if not fd_dir.exists():
            pytest.skip("/dev/fd not available on this platform")

        baseline_fds = len(os.listdir("/dev/fd"))

        # Ingest 100 valid PDFs
        for _ in range(100):
            _ = load_document(pdf_path)

        # Ingest 100 valid PNGs
        for _ in range(100):
            _ = load_document(png_path)

        # Ingest 100 corrupted files
        for _ in range(100):
            try:
                _ = load_document(corrupt_path)
            except ValueError:
                pass

        # Ingest 100 missing files
        for _ in range(100):
            try:
                _ = load_document(missing_path)
            except FileNotFoundError:
                pass

        gc.collect()
        final_fds = len(os.listdir("/dev/fd"))

        assert final_fds <= baseline_fds + 1, (
            f"File descriptor leak detected! Baseline: {baseline_fds}, Final: {final_fds}"
        )

    def test_memory_leak_bounded_growth(self, tmp_path: Path) -> None:
        """Verify resident memory consumption remains bounded across repeated multi-page rasterizations."""
        pdf_path = tmp_path / "mem_test.pdf"
        doc = pymupdf.open()
        for i in range(4):
            p = doc.new_page(width=595, height=842)
            p.insert_text((100, 100), f"Mem Page {i}")
        doc.save(str(pdf_path))
        doc.close()

        def get_current_rss_mb() -> float:
            pid = os.getpid()
            out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)]).decode().strip()
            return int(out) / 1024.0

        # Warmup (establish process working set buffer for 4 uncompressed 300 DPI pages ~ 104 MB)
        for _ in range(5):
            p = load_document(pdf_path)
            del p
        gc.collect()
        rss_warmed = get_current_rss_mb()

        # Run 80 iterations
        for i in range(80):
            pages = load_document(pdf_path)
            del pages
            if (i + 1) % 20 == 0:
                gc.collect()

        gc.collect()
        rss_final = get_current_rss_mb()
        drift = rss_final - rss_warmed

        # Bounded drift across 80 iterations (320 pages) must be < 10 MB
        assert drift < 10.0, f"Memory drift exceeds 10 MB: {drift:.2f} MB across 80 rasterizations"


class TestAdversarialUngracefulCrashBugs:
    """Adversarial stress tests exposing unhandled exceptions in rasterize_pdf."""

    def test_corrupted_page_tree_count_mismatch_must_raise_clean_value_error(self, tmp_path: Path) -> None:
        """Verify PDF with corrupted /Count header exceeding actual Kids objects raises ValueError, not IndexError.

        VULNERABILITY CHALLENGE:
        If total_pages = len(doc) returns 2 based on /Count 2, but Kids contains only 1 page object,
        accessing doc[1] raises `IndexError: page 1 not in document` which escapes unhandled!
        """
        pdf_content = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 2 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>\nendobj\n"
            b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n"
            b"0000000115 00000 n \ntrailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n192\n%%EOF\n"
        )
        p = tmp_path / "bad_kids_count.pdf"
        p.write_bytes(pdf_content)

        with pytest.raises(ValueError, match="Failed to (open|rasterize) PDF") as exc_info:
            load_document(p)
        assert isinstance(exc_info.value, ValueError)

    def test_extreme_mediabox_must_raise_clean_value_error(self, tmp_path: Path) -> None:
        """Verify PDF with extreme/invalid MediaBox raises clean ValueError, not FzErrorLimit.

        VULNERABILITY CHALLENGE:
        PyMuPDF raises `pymupdf.mupdf.FzErrorLimit: code=5: Overly large image` when rasterizing
        extreme dimensions. Because rasterize_pdf does not catch exceptions in get_pixmap(),
        this escapes as an unhandled FzErrorLimit.
        """
        pdf_content = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10000000 10000000] >>\nendobj\n"
            b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n"
            b"0000000115 00000 n \ntrailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n205\n%%EOF\n"
        )
        p = tmp_path / "extreme_box.pdf"
        p.write_bytes(pdf_content)

        with pytest.raises(ValueError, match="Failed to (open|rasterize) PDF") as exc_info:
            load_document(p)
        assert isinstance(exc_info.value, ValueError)


class TestAdversarialAdditionalEdgeCases:
    """Additional edge cases requested for Iteration 2: DPI boundaries and malformed xrefs."""

    @pytest.mark.parametrize("bad_dpi", [0, -1, -72, -300])
    def test_zero_and_negative_dpi_raises_value_error(self, tmp_path: Path, bad_dpi: int) -> None:
        """Verify non-positive DPI raises clean ValueError in both load_document and rasterize_pdf."""
        dummy_pdf = tmp_path / "valid_dummy.pdf"
        doc = pymupdf.open()
        doc.new_page(width=200, height=200)
        doc.save(str(dummy_pdf))
        doc.close()

        with pytest.raises(ValueError, match="Invalid rasterization DPI.*Must be a positive integer"):
            load_document(dummy_pdf, dpi=bad_dpi)

        with pytest.raises(ValueError, match="Invalid rasterization DPI.*Must be a positive integer"):
            rasterize_pdf(dummy_pdf, dpi=bad_dpi)

    def test_malformed_xref_table_syntax_raises_clean_value_error(self, tmp_path: Path) -> None:
        """Verify PDF with malformed non-numeric xref lines raises clean ValueError."""
        pdf_content = (
            b"%PDF-1.4\n"
            b"xref\n"
            b"0 10\n"
            b"corrupt_xref_entry_not_standard_length_and_format\n"
            b"trailer\n<< /Size 10 >>\n"
            b"startxref\n9\n%%EOF\n"
        )
        p = tmp_path / "malformed_xref_syntax.pdf"
        p.write_bytes(pdf_content)

        with pytest.raises(ValueError, match="Failed to (open|rasterize) PDF") as exc_info:
            load_document(p)
        assert isinstance(exc_info.value, ValueError)

    def test_corrupted_xref_stream_raises_clean_value_error(self, tmp_path: Path) -> None:
        """Verify PDF with corrupted compressed XRef stream raises clean ValueError."""
        pdf_content = (
            b"%PDF-1.5\n"
            b"1 0 obj\n<< /Type /XRef /Size 5 /Filter /FlateDecode >>\nstream\n"
            b"CORRUPTED_NON_ZLIB_XREF_STREAM_BYTES_12345\nendstream\nendobj\n"
            b"startxref\n9\n%%EOF\n"
        )
        p = tmp_path / "corrupt_xref_stream.pdf"
        p.write_bytes(pdf_content)

        with pytest.raises(ValueError, match="(Failed to (open|rasterize) PDF|PDF document contains 0 pages)") as exc_info:
            load_document(p)
        assert isinstance(exc_info.value, ValueError)

    def test_cyclic_page_tree_reference_raises_clean_value_error(self, tmp_path: Path) -> None:
        """Verify cyclic loop in page tree raises clean ValueError rather than unhandled MuPDF crash."""
        pdf_content = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [2 0 R] /Count 1 >>\nendobj\n"
            b"xref\n0 3\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000058 00000 n \n"
            b"trailer\n<< /Size 3 /Root 1 0 R >>\n"
            b"startxref\n115\n%%EOF\n"
        )
        p = tmp_path / "cyclic_page_tree.pdf"
        p.write_bytes(pdf_content)

        with pytest.raises(ValueError, match="Failed to (open|rasterize) PDF.*cycle in page tree") as exc_info:
            load_document(p)
        assert isinstance(exc_info.value, ValueError)

    def test_out_of_bounds_startxref_raises_clean_value_error(self, tmp_path: Path) -> None:
        """Verify out-of-bounds startxref with missing trailer raises clean ValueError."""
        pdf_content = (
            b"%PDF-1.4\n"
            b"startxref\n999999999\n%%EOF\n"
        )
        p = tmp_path / "oob_startxref.pdf"
        p.write_bytes(pdf_content)

        with pytest.raises(ValueError, match="Failed to (open|rasterize) PDF") as exc_info:
            load_document(p)
        assert isinstance(exc_info.value, ValueError)


class TestAdversarialSourceDatasetProtection:
    """Verify zero-touch immutability of primary acceptance dataset."""

    def test_source_dataset_strictly_unmodified(self) -> None:
        dataset_dir = Path("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026")
        if not dataset_dir.exists():
            pytest.skip("Dataset path /Volumes/NO NAME/_ФАКТУРИ not mounted")

        expected_files = {
            "капина-01.pdf": 7516207,
            "капина-02.pdf": 8148645,
            "капина-03.pdf": 7218503,
        }

        actual_files = list(dataset_dir.iterdir())
        actual_names = {f.name: f.stat().st_size for f in actual_files if not f.name.startswith(".")}

        for name, exp_size in expected_files.items():
            assert name in actual_names, f"File {name} missing from source dataset!"
            assert actual_names[name] == exp_size, f"File {name} size modified! Exp: {exp_size}, got: {actual_names[name]}"

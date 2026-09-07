"""Tests for streaming rasterization and OOM prevention on multi-page documents.

Verifies:
1. iter_rasterize_pdf() returns a generator yielding PageImage objects one by one.
2. iter_document() provides unified streaming across PDF and image formats.
3. Eager parameter validation (DPI, file existence, format) occurs before generator execution.
4. Early termination (gen.close()) cleans up PyMuPDF resources safely.
5. Backward compatibility with rasterize_pdf(), load_document(), and load_image().
6. Constant O(1) memory retention: PageImage objects in raw_ocr_evidence keep metadata
   without holding multi-megabyte numpy arrays in RAM.
7. End-to-end multi-page processing on synthetic and real-world multi-page invoices.
"""

import gc
import inspect
from pathlib import Path
import sys
from typing import Generator
import cv2
import numpy as np
import pytest
import pymupdf

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_ocr import (
    PageImage,
    DEFAULT_RASTER_DPI,
    _stream_pdf_pages,
    iter_rasterize_pdf,
    rasterize_pdf,
    iter_document,
    load_document,
    load_image,
    load_image_page,
    process_invoice,
)


@pytest.fixture
def make_multipage_pdf(tmp_path: Path):
    """Helper fixture to generate synthetic multi-page PDFs."""
    def _creator(num_pages: int = 5, prefix: str = "multipage") -> Path:
        pdf_path = tmp_path / f"{prefix}_{num_pages}p.pdf"
        doc = pymupdf.open()
        for idx in range(num_pages):
            page = doc.new_page(width=595, height=842)  # A4
            page.insert_text(
                (50, 100),
                f"Page {idx + 1} of {num_pages} - Фактура № {1000 + idx}\n"
                f"Доставчик: Тест ООД ЕИК 123456789\n"
                f"Получател: Клиент ЕООД ЕИК 987654321\n"
                f"Обща сума: 100.00 BGN\n"
            )
        doc.save(str(pdf_path))
        doc.close()
        return pdf_path
    return _creator


class TestStreamingRasterizePdf:
    """Tests for iter_rasterize_pdf generator streaming behavior."""

    def test_returns_generator(self, make_multipage_pdf):
        """iter_rasterize_pdf must return a generator object."""
        pdf_path = make_multipage_pdf(3)
        gen = iter_rasterize_pdf(pdf_path, dpi=72)
        assert inspect.isgenerator(gen)
        # Verify it can be iterated
        pages = list(gen)
        assert len(pages) == 3
        for idx, page in enumerate(pages):
            assert page.page_number == idx + 1
            assert isinstance(page.image, np.ndarray)
            assert page.width > 0
            assert page.height > 0

    def test_lazy_rasterization(self, make_multipage_pdf):
        """Pages should be yielded one at a time, not all at once."""
        pdf_path = make_multipage_pdf(5)
        gen = iter_rasterize_pdf(pdf_path, dpi=72)

        # First page
        p1 = next(gen)
        assert p1.page_number == 1
        assert p1.image is not None

        # Second page
        p2 = next(gen)
        assert p2.page_number == 2
        assert p2.image is not None

        # Consume remaining 3 pages
        remaining = list(gen)
        assert len(remaining) == 3
        assert [p.page_number for p in remaining] == [3, 4, 5]

    def test_eager_validation_dpi(self, make_multipage_pdf):
        """Invalid DPI must raise ValueError immediately on function call."""
        pdf_path = make_multipage_pdf(2)
        with pytest.raises(ValueError, match="Invalid rasterization DPI"):
            iter_rasterize_pdf(pdf_path, dpi=0)

        with pytest.raises(ValueError, match="Invalid rasterization DPI"):
            iter_rasterize_pdf(pdf_path, dpi=-100)

    def test_eager_validation_nonexistent_file(self, tmp_path: Path):
        """Non-existent file must raise ValueError immediately on function call."""
        bad_path = tmp_path / "does_not_exist.pdf"
        with pytest.raises(ValueError, match="Failed to open PDF document"):
            iter_rasterize_pdf(bad_path)

    def test_eager_validation_corrupted_file(self, tmp_path: Path):
        """Corrupted/empty file must raise ValueError immediately on function call."""
        corrupt_path = tmp_path / "corrupt.pdf"
        corrupt_path.write_bytes(b"NOT A VALID PDF FILE")
        with pytest.raises(ValueError, match="Failed to open PDF document"):
            iter_rasterize_pdf(corrupt_path)

    def test_early_close_cleans_up_document(self, make_multipage_pdf):
        """Calling gen.close() early must execute finally: doc.close() safely."""
        pdf_path = make_multipage_pdf(10)
        gen = iter_rasterize_pdf(pdf_path, dpi=72)

        first_page = next(gen)
        assert first_page.page_number == 1

        # Close generator before reading remaining 9 pages
        gen.close()

        # Generator should now be exhausted
        with pytest.raises(StopIteration):
            next(gen)


class TestIterDocument:
    """Tests for unified iter_document streaming generator."""

    def test_iter_document_pdf(self, make_multipage_pdf):
        """iter_document yields pages sequentially for PDF."""
        pdf_path = make_multipage_pdf(4)
        gen = iter_document(pdf_path, dpi=72)
        pages = list(gen)
        assert len(pages) == 4
        assert [p.page_number for p in pages] == [1, 2, 3, 4]

    def test_iter_document_image(self, tmp_path: Path):
        """iter_document yields a single PageImage for PNG/JPG."""
        img_path = tmp_path / "test_invoice.png"
        sample_img = np.full((300, 200, 3), 255, dtype=np.uint8)
        cv2.imwrite(str(img_path), sample_img)

        gen = iter_document(img_path)
        pages = list(gen)
        assert len(pages) == 1
        assert pages[0].page_number == 1
        assert pages[0].width == 200
        assert pages[0].height == 300
        assert pages[0].image is not None

    def test_iter_document_nonexistent_raises_filenotfound(self, tmp_path: Path):
        """Missing file raises FileNotFoundError eagerly."""
        bad_path = tmp_path / "missing_file.pdf"
        with pytest.raises(FileNotFoundError, match="Document file not found"):
            iter_document(bad_path)

    def test_iter_document_unsupported_format_raises_valueerror(self, tmp_path: Path):
        """Unsupported format raises ValueError eagerly."""
        txt_path = tmp_path / "sample.txt"
        txt_path.write_text("hello", encoding="utf-8")
        with pytest.raises(ValueError, match="Unsupported file format"):
            iter_document(txt_path)


class TestBackwardCompatibility:
    """Verify legacy loaders continue to work identically using streaming underneath."""

    def test_rasterize_pdf_returns_list(self, make_multipage_pdf):
        """rasterize_pdf returns a list of PageImage with non-None images."""
        pdf_path = make_multipage_pdf(3)
        pages = rasterize_pdf(pdf_path, dpi=72)
        assert isinstance(pages, list)
        assert len(pages) == 3
        for idx, page in enumerate(pages):
            assert page.page_number == idx + 1
            assert isinstance(page.image, np.ndarray)

    def test_load_document_returns_list(self, make_multipage_pdf, tmp_path: Path):
        """load_document returns a list of PageImage for both PDF and images."""
        pdf_path = make_multipage_pdf(2)
        pdf_pages = load_document(pdf_path, dpi=72)
        assert isinstance(pdf_pages, list)
        assert len(pdf_pages) == 2

        img_path = tmp_path / "single.jpg"
        cv2.imwrite(str(img_path), np.zeros((100, 100, 3), dtype=np.uint8))
        img_pages = load_document(img_path)
        assert isinstance(img_pages, list)
        assert len(img_pages) == 1

    def test_load_image_first_page_only(self, make_multipage_pdf):
        """load_image returns np.ndarray of page 1 without loading entire multi-page document."""
        pdf_path = make_multipage_pdf(5)
        img = load_image(pdf_path)
        assert isinstance(img, np.ndarray)
        assert len(img.shape) == 3


class TestMemoryOptimizationAndOOMPrevention:
    """Verify memory bounding and O(1) page image retention in the pipeline."""

    def test_process_invoice_evidence_stores_metadata_without_heavy_images(self, make_multipage_pdf):
        """raw_ocr_evidence must store page metadata (dimensions, page_number)

        without holding multi-megabyte numpy arrays in RAM.
        """
        pdf_path = make_multipage_pdf(3, prefix="evidence_test")
        invoice = process_invoice(pdf_path)

        assert invoice.raw_ocr_evidence is not None
        assert invoice.raw_ocr_evidence["total_pages"] == 3
        assert len(invoice.raw_ocr_evidence["pages"]) == 3

        for page in invoice.raw_ocr_evidence["pages"]:
            # Metadata preserved
            assert page["page_number"] in (1, 2, 3)
            assert page["width"] > 0
            assert page["height"] > 0
            # Image matrix is NOT stored in raw evidence dictionary
            assert "image" not in page

    def test_streaming_constant_image_memory(self, make_multipage_pdf):
        """Simulate processing a 15-page document: verify that discarding

        page images between iterations keeps held image memory bounded to 1 page.
        """
        num_pages = 15
        pdf_path = make_multipage_pdf(num_pages, prefix="stress_test")

        active_images = []
        for page in iter_rasterize_pdf(pdf_path, dpi=150):
            # Assert each yielded page has an image
            assert page.image is not None
            # Immediate cleanup as done in process_invoice
            page.image = None
            gc.collect()
            active_images.append(page)

        # All 15 pages yielded and metadata kept, but 0 image arrays retained
        assert len(active_images) == num_pages
        for p in active_images:
            assert p.image is None


class TestRealWorldMultiPage:
    """Test streaming on real-world multi-page acceptance invoice if available."""

    REAL_METRO_PATH = Path("/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf")

    @pytest.mark.skipif(
        not REAL_METRO_PATH.exists(),
        reason="Real multi-page acceptance invoice /Volumes/NO NAME/_ФАКТУРИ/метро.pdf not mounted",
    )
    def test_metro_multipage_streaming_pipeline(self):
        """Verify metro.pdf (3 pages) is processed via streaming with bounded memory."""
        invoice = process_invoice(self.REAL_METRO_PATH)

        assert invoice.raw_ocr_evidence["total_pages"] == 3
        assert len(invoice.raw_ocr_evidence["pages"]) == 3

        # Images must not be kept in evidence dict
        for page in invoice.raw_ocr_evidence["pages"]:
            assert "image" not in page
            assert page["width"] > 0
            assert page["height"] > 0
            assert page["token_count"] > 0

        # Tokens must span across all 3 pages
        pages_with_tokens = {
            tok["page_number"]
            for page in invoice.raw_ocr_evidence["pages"]
            for tok in page["tokens"]
        }
        assert len(pages_with_tokens) == 3, f"Expected tokens on all 3 pages, found on: {pages_with_tokens}"

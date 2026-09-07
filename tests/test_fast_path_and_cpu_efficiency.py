"""Unit and integration tests for Digital Vector PDF Fast Path and CPU Efficiency.

Verifies:
1. Digital vector PDFs bypass Tesseract OCR entirely (zero OCR calls).
2. Fast Path processes invoices in sub-second time (< 0.5s).
3. Graceful fallback to OCR when PDF lacks digital text or fails validation.
4. parse_date() supports Bulgarian space-separated dates (e.g. "31 07 2025", "10 04. 2026").
5. Debug artifacts are preserved when debug_dir is active during Fast Path.
6. Real-world digital acceptance invoices pass via Fast Path when volume is mounted.
"""

from decimal import Decimal
import inspect
from pathlib import Path
import sys
import time
import unittest.mock as mock
import cv2
import numpy as np
import pytest
import pymupdf
import pytesseract

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_ocr import (
    process_invoice,
    try_digital_pdf_fast_path,
    parse_date,
    detect_orientation,
    Invoice,
)


@pytest.fixture
def make_digital_pdf(tmp_path: Path):
    """Fixture to generate clean digital vector PDF invoices with statutory text."""
    def _create(
        inv_no: str = "1100124585",
        date_str: str = "28.08.2026",
        tax_base: str = "100.00",
        vat: str = "20.00",
        total: str = "120.00",
        supplier_eik: str = "123456789",
        recipient_eik: str = "987654321",
    ) -> Path:
        pdf_path = tmp_path / f"digital_inv_{inv_no}.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)  # A4

        # Register system font for proper Cyrillic text layer
        font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
        if Path(font_path).exists():
            page.insert_font(fontname="cyr", fontfile=font_path)
            fn = "cyr"
        else:
            fn = None

        # Header & Supplier on Left (x=50)
        left_text = (
            f"ФАКТУРА № {inv_no}\n"
            f"Дата на издаване: {date_str}\n"
            f"Дата на данъчно събитие: {date_str}\n"
            f"Доставчик: Тест Логистик ООД\n"
            f"ЕИК: {supplier_eik}\n"
            f"ДДС №: BG{supplier_eik}\n"
            f"Град: София\n"
        )
        page.insert_text((50, 80), left_text, fontname=fn, fontsize=11)

        # Recipient on Right (x=320)
        right_text = (
            f"ОРИГИНАЛ\n"
            f"Получател: Партньор Трейд ЕООД\n"
            f"ЕИК: {recipient_eik}\n"
            f"ДДС №: BG{recipient_eik}\n"
            f"Град: Пловдив\n"
        )
        page.insert_text((320, 80), right_text, fontname=fn, fontsize=11)

        # Table columns aligned properly
        page.insert_text((50, 240), "№", fontname=fn, fontsize=10)
        page.insert_text((80, 240), "Описание", fontname=fn, fontsize=10)
        page.insert_text((260, 240), "Мярка", fontname=fn, fontsize=10)
        page.insert_text((320, 240), "Количество", fontname=fn, fontsize=10)
        page.insert_text((400, 240), "Ед. цена", fontname=fn, fontsize=10)
        page.insert_text((470, 240), "Стойност", fontname=fn, fontsize=10)

        page.insert_text((50, 260), "1", fontname=fn, fontsize=10)
        page.insert_text((80, 260), "Транспортни услуги", fontname=fn, fontsize=10)
        page.insert_text((260, 260), "бр.", fontname=fn, fontsize=10)
        page.insert_text((340, 260), "1", fontname=fn, fontsize=10)
        page.insert_text((410, 260), tax_base, fontname=fn, fontsize=10)
        page.insert_text((480, 260), tax_base, fontname=fn, fontsize=10)

        # Financial Summary
        fin_text = (
            f"Данъчна основа: {tax_base} лв.\n"
            f"ДДС 20%: {vat} лв.\n"
            f"Обща сума за плащане: {total} EUR\n"
        )
        page.insert_text((50, 320), fin_text, fontname=fn, fontsize=11)

        doc.save(str(pdf_path))
        doc.close()
        return pdf_path
    return _create


@pytest.fixture
def make_scanned_pdf(tmp_path: Path):
    """Fixture to generate image-only scanned PDF (no digital text layer)."""
    def _create() -> Path:
        pdf_path = tmp_path / "scanned_image_only.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)

        # Render text into a bitmap image
        img = np.full((1200, 800, 3), 255, dtype=np.uint8)
        cv2.putText(img, "FAKTURA 12345", (100, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 2)
        _, img_bytes = cv2.imencode(".png", img)

        page.insert_image(page.rect, stream=img_bytes.tobytes())
        doc.save(str(pdf_path))
        doc.close()
        return pdf_path
    return _create


class TestDigitalPdfFastPath:
    """Verify Fast Path bypasses OCR and processes digital PDFs in < 0.5s."""

    def test_fast_path_bypasses_ocr_completely(self, make_digital_pdf):
        """Digital vector PDF must be processed without invoking Tesseract."""
        pdf_path = make_digital_pdf()

        # Mock pytesseract calls to fail if invoked
        with mock.patch("pytesseract.image_to_data") as mock_data, \
             mock.patch("pytesseract.image_to_osd") as mock_osd:

            mock_data.side_effect = AssertionError("Tesseract image_to_data must NOT be called on digital PDF!")
            mock_osd.side_effect = AssertionError("Tesseract image_to_osd must NOT be called on digital PDF!")

            invoice = process_invoice(pdf_path)

            assert invoice.validation.is_valid is True
            assert invoice.invoice_metadata.invoice_number == "1100124585"
            assert invoice.financial_summary.total_amount_due.amount == Decimal("120.00")
            assert invoice.financial_summary.tax_base.amount == Decimal("100.00")
            assert invoice.financial_summary.vat_amount.amount == Decimal("20.00")
            assert invoice.supplier.eik == "123456789"
            assert invoice.recipient.eik == "987654321"

            # Assert 0 Tesseract calls
            mock_data.assert_not_called()
            mock_osd.assert_not_called()

    def test_fast_path_latency_under_500ms(self, make_digital_pdf):
        """Fast Path processing of a digital invoice must complete in under 500 ms."""
        pdf_path = make_digital_pdf()

        start = time.perf_counter()
        invoice = process_invoice(pdf_path)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert invoice.validation.is_valid is True
        assert elapsed_ms < 500.0, f"Expected < 500ms, took {elapsed_ms:.2f}ms"

    def test_scanned_pdf_rejects_fast_path(self, make_scanned_pdf):
        """PDF without text layer must return None from try_digital_pdf_fast_path."""
        scanned_pdf = make_scanned_pdf()
        result = try_digital_pdf_fast_path(scanned_pdf)
        assert result is None, "Scanned PDF without text layer must not trigger Fast Path"

    def test_fast_path_preserves_debug_artifacts_when_requested(self, make_digital_pdf, tmp_path: Path):
        """When debug_dir is provided, Fast Path must save page images."""
        pdf_path = make_digital_pdf()
        debug_dir = tmp_path / "debug_fast"

        invoice = process_invoice(pdf_path, debug_dir=debug_dir)
        assert invoice.validation.is_valid is True

        raw_debug = debug_dir / f"{pdf_path.stem}_page_1_raw.png"
        norm_debug = debug_dir / f"{pdf_path.stem}_page_1_norm.png"
        assert raw_debug.exists(), "Fast Path must create raw debug image when requested"
        assert norm_debug.exists(), "Fast Path must create norm debug image when requested"


class TestDateParsingEnhancements:
    """Verify parse_date handles both standard and space-separated Bulgarian dates."""

    def test_standard_dates(self):
        assert parse_date("28.08.2026") == "2026-08-28"
        assert parse_date("28/08/2026") == "2026-08-28"
        assert parse_date("2026-08-28") == "2026-08-28"

    def test_space_separated_dates(self):
        assert parse_date("31 07 2025") == "2025-07-31"
        assert parse_date("10 04. 2026") == "2026-04-10"
        assert parse_date("01 02 2024") == "2024-02-01"
        assert parse_date("15 / 03 / 2025") == "2025-03-15"

    def test_invalid_dates(self):
        assert parse_date("invalid") is None
        assert parse_date("") is None
        assert parse_date(None) is None
        assert parse_date("32.13.2026") is None


class TestOsdOptimization:
    """Verify detect_orientation optimization on large images."""

    def test_detect_orientation_large_image(self):
        """detect_orientation downscales images > 1600px without crashing."""
        large_img = np.full((2400, 1800), 255, dtype=np.uint8)
        cv2.putText(large_img, "ТЕСТ НА ОРИЕНТАЦИЯ НА ТЕКСТА", (200, 500), cv2.FONT_HERSHEY_SIMPLEX, 2, 0, 2)
        rot = detect_orientation(large_img)
        assert rot in (0, 90, 180, 270)


class TestRealWorldAcceptanceFastPath:
    """Verify Fast Path on actual acceptance corpus files if mounted."""

    ELIKO_PATH = Path("/Volumes/NO NAME/_ФАКТУРИ/03_ЕЛИКО_143_ЕООД/Елико 2025/елико.pdf")

    @pytest.mark.skipif(not ELIKO_PATH.exists(), reason="Real invoice corpus not mounted")
    def test_eliko_processes_via_fast_path(self):
        """елико.pdf must process via Fast Path without Tesseract in < 1s."""
        with mock.patch("pytesseract.image_to_data") as mock_data:
            mock_data.side_effect = AssertionError("Tesseract should NOT be called on digital invoice елико.pdf!")

            start = time.perf_counter()
            inv = process_invoice(self.ELIKO_PATH)
            duration_ms = (time.perf_counter() - start) * 1000

            assert inv.validation.is_valid is True
            assert inv.invoice_metadata.invoice_number == "0885727402"
            assert inv.financial_summary.total_amount_due.amount == Decimal("76.00")
            assert duration_ms < 1000.0
            mock_data.assert_not_called()

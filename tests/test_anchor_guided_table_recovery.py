"""
tests/test_anchor_guided_table_recovery.py
=========================================
Tests for Стълб 1 (P0): Anchor-Guided Table Recovery & X-Projection Profiles.

Covers:
1. clean_table_crop: morphological table grid line removal.
2. build_x_projection_profile: 1D occupancy histogram and semantic column detection.
3. _parse_row_tokens_anchor_guided: candidate line item extraction from borderless rows.
4. recover_anchor_guided_table: in-memory Phase 1 recovery & targeted Phase 2 clean crop recovery.
5. End-to-end acceptance tests on archive invoices 14.pdf, 48.pdf, 51.pdf.
"""
from decimal import Decimal
from pathlib import Path
import sys
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_ocr import (
    clean_table_crop,
    build_x_projection_profile,
    recover_anchor_guided_table,
    _parse_row_tokens_anchor_guided,
    process_invoice,
    OcrToken,
    LogicalLine,
    LineItem,
    MoneyAmount,
    FinancialSummary,
)

ARCHIVE_DIR = Path("/Volumes/NO NAME/_ФАКТУРИ/00_РМ_КАСКАДА_2026_ЕООД")


class TestCleanTableCrop:
    """Unit tests for morphological table grid line removal."""

    def test_clean_table_crop_removes_lines(self):
        img = np.full((200, 400), 255, dtype=np.uint8)
        img[100, 20:380] = 0
        img[20:180, 200] = 0

        cleaned = clean_table_crop(img)
        assert cleaned.shape == img.shape
        assert cleaned[100, 200] == 255
        assert cleaned[100, 50] == 255
        assert cleaned[50, 200] == 255

    def test_clean_table_crop_handles_empty_or_small(self):
        empty = np.zeros((0, 0), dtype=np.uint8)
        assert clean_table_crop(empty).size == 0

        small = np.zeros((10, 10), dtype=np.uint8)
        assert clean_table_crop(small).shape == (10, 10)

        none_img = None
        assert clean_table_crop(none_img) is None


class TestXProjectionProfile:
    """Unit tests for 1D occupancy profile column detection."""

    def test_build_x_projection_profile_detects_columns(self):
        tokens = [
            OcrToken(text="КАФЕ", conf=95, bbox=(300, 100, 80, 25)),
            OcrToken(text="ЛАВАЦА", conf=95, bbox=(400, 100, 100, 25)),
            OcrToken(text="1.000", conf=95, bbox=(1420, 100, 60, 25)),
            OcrToken(text="18.32", conf=95, bbox=(1820, 100, 60, 25)),
            OcrToken(text="20.00%", conf=95, bbox=(2000, 100, 60, 25)),
            OcrToken(text="18.32", conf=95, bbox=(2220, 100, 60, 25)),
        ]

        cols = build_x_projection_profile(tokens, page_width=2500)
        assert len(cols) >= 3

        sem_types = [c.semantic_type for c in cols]
        assert "description" in sem_types
        assert any(t in sem_types for t in ["total_price", "unit_price", "vat_rate"])

    def test_build_x_projection_profile_empty_tokens(self):
        cols = build_x_projection_profile([])
        assert cols == []


class TestParseRowTokensAnchorGuided:
    """Unit tests for row token extraction heuristics."""

    def test_parse_row_tokens_with_vat_and_price(self):
        tokens = [
            OcrToken(text="ПИЛЕШКИ", conf=90, bbox=(400, 100, 120, 25)),
            OcrToken(text="ДРОБЧЕТА", conf=90, bbox=(540, 100, 130, 25)),
            OcrToken(text="3.38", conf=90, bbox=(1800, 100, 60, 25)),
            OcrToken(text="20.00%", conf=90, bbox=(2000, 100, 60, 25)),
            OcrToken(text="270.67", conf=90, bbox=(2250, 100, 80, 25)),
        ]
        item = _parse_row_tokens_anchor_guided(tokens, Decimal("543.56"))
        assert item is not None
        assert "ПИЛЕШКИ ДРОБЧЕТА" in item.description
        assert item.total_price_net.amount == Decimal("270.67")
        assert item.unit_price_net.amount == Decimal("3.38")
        assert item.quantity == Decimal("80.080")


class TestRecoverAnchorGuidedTable:
    """Unit tests for Anchor-Guided Table Recovery algorithm."""

    def test_recover_in_memory_complement_single_item(self):
        existing_item = LineItem(
            index=1,
            description="ПИЛЕШКИ СЪРЦА ОХЛАДЕНИ",
            quantity=Decimal("1.000"),
            unit_price_net=MoneyAmount(Decimal("272.89")),
            total_price_net=MoneyAmount(Decimal("272.89")),
            bbox=(400, 1170, 1900, 30),
            page_number=1,
        )

        tokens = [
            OcrToken(text="Стока", conf=95, bbox=(400, 950, 80, 25)),
            OcrToken(text="Стойност", conf=95, bbox=(2200, 950, 100, 25)),
            OcrToken(text="ПИЛЕШКИ", conf=92, bbox=(400, 1115, 120, 25)),
            OcrToken(text="ДРОБЧЕТА", conf=92, bbox=(540, 1115, 130, 25)),
            OcrToken(text="ОХЛАДЕНИ", conf=92, bbox=(690, 1115, 130, 25)),
            OcrToken(text="3.38", conf=90, bbox=(1800, 1115, 60, 25)),
            OcrToken(text="20.00%", conf=90, bbox=(2000, 1115, 60, 25)),
            OcrToken(text="270.67", conf=96, bbox=(2250, 1115, 80, 25)),
            OcrToken(text="Данъчна", conf=95, bbox=(1200, 1300, 100, 25)),
            OcrToken(text="основа", conf=95, bbox=(1320, 1300, 90, 25)),
            OcrToken(text="543.56", conf=98, bbox=(2200, 1300, 80, 25)),
        ]

        lines = [
            LogicalLine(tokens=[tokens[0], tokens[1]]),
            LogicalLine(tokens=tokens[2:8]),
            LogicalLine(tokens=tokens[8:]),
        ]

        fs = FinancialSummary(
            tax_base=MoneyAmount(Decimal("543.56"), "EUR"),
            total_amount_due=MoneyAmount(Decimal("652.27"), "EUR"),
        )

        recovered = recover_anchor_guided_table(
            items=[existing_item],
            lines=lines,
            tokens=tokens,
            financial_summary=fs,
        )

        assert len(recovered) == 2
        total_sum = sum(it.total_price_net.amount for it in recovered)
        assert total_sum == Decimal("543.56")
        assert recovered[0].description == "ПИЛЕШКИ ДРОБЧЕТА ОХЛАДЕНИ"
        assert recovered[0].total_price_net.amount == Decimal("270.67")
        assert recovered[1].description == "ПИЛЕШКИ СЪРЦА ОХЛАДЕНИ"
        assert recovered[1].total_price_net.amount == Decimal("272.89")

    def test_filter_carry_over_items_explicit_keywords(self):
        from invoice_ocr import filter_carry_over_items

        items = [
            LineItem(index=1, description="СИРЕНЕ КРАВЕ", total_price_net=MoneyAmount(Decimal("120.00"))),
            LineItem(index=2, description="КАШКАВАЛ ВИТОША", total_price_net=MoneyAmount(Decimal("80.00"))),
            LineItem(index=3, description="ПРЕНОС КЪМ СТР. 2", total_price_net=MoneyAmount(Decimal("200.00"))),
            LineItem(index=4, description="ОТ ПРЕНОС", total_price_net=MoneyAmount(Decimal("200.00"))),
            LineItem(index=5, description="МАСЛО КРАВЕ", total_price_net=MoneyAmount(Decimal("50.00"))),
        ]

        filtered = filter_carry_over_items(items)
        assert len(filtered) == 3
        assert [it.description for it in filtered] == ["СИРЕНЕ КРАВЕ", "КАШКАВАЛ ВИТОША", "МАСЛО КРАВЕ"]
        total = sum(it.total_price_net.amount for it in filtered)
        assert total == Decimal("250.00")

    def test_filter_carry_over_items_implicit_running_subtotal(self):
        from invoice_ocr import filter_carry_over_items

        items = [
            LineItem(index=1, description="МАСА ТРАПЕЗНА", total_price_net=MoneyAmount(Decimal("350.00"))),
            LineItem(index=2, description="СТОЛ ТРАПЕЗЕН", total_price_net=MoneyAmount(Decimal("150.00"))),
            LineItem(index=3, description="ЗА ПРЕНАСЯНЕ", total_price_net=MoneyAmount(Decimal("500.00"))),
            LineItem(index=4, description="ШКАФ ЗА ОБУВКИ", total_price_net=MoneyAmount(Decimal("220.00"))),
        ]

        filtered = filter_carry_over_items(items)
        assert len(filtered) == 3
        assert [it.description for it in filtered] == ["МАСА ТРАПЕЗНА", "СТОЛ ТРАПЕЗЕН", "ШКАФ ЗА ОБУВКИ"]
        total = sum(it.total_price_net.amount for it in filtered)
        assert total == Decimal("720.00")

    def test_multi_page_anchor_guided_recovery(self):
        # Page 1 has 1 item already extracted: 200.00
        p1_item = LineItem(
            index=1,
            description="АРТИКУЛ СТР 1",
            total_price_net=MoneyAmount(Decimal("200.00")),
            bbox=(100, 400, 600, 30),
            page_number=1,
        )

        # Page 2 has lines and tokens for a missing row: 150.00
        p2_tokens = [
            OcrToken("АРТИКУЛ", 95, (400, 300, 120, 25), page_number=2),
            OcrToken("СТР", 95, (540, 300, 60, 25), page_number=2),
            OcrToken("2", 95, (620, 300, 30, 25), page_number=2),
            OcrToken("1", 95, (1600, 300, 30, 25), page_number=2),
            OcrToken("150.00", 95, (1850, 300, 80, 25), page_number=2),
            OcrToken("150.00", 95, (2250, 300, 80, 25), page_number=2),
        ]
        p2_line = LogicalLine(
            tokens=p2_tokens,
            text="АРТИКУЛ СТР 2 1 150.00 150.00",
            bbox=(400, 300, 1930, 25),
            page_number=2,
            y_center=312.0,
        )

        header_l2 = LogicalLine(
            tokens=[OcrToken("Наименование", 95, (100, 150, 100, 20), page_number=2)],
            text="Наименование Количество Ед. цена Стойност",
            bbox=(100, 150, 600, 20),
            page_number=2,
            y_center=160.0,
        )
        totals_l2 = LogicalLine(
            tokens=[OcrToken("Всичко", 95, (100, 800, 60, 20), page_number=2)],
            text="Данъчна основа 350.00 Сума за плащане 420.00",
            bbox=(100, 800, 600, 20),
            page_number=2,
            y_center=810.0,
        )

        fs = FinancialSummary(
            tax_base=MoneyAmount(Decimal("350.00"), "BGN"),
            total_amount_due=MoneyAmount(Decimal("420.00"), "BGN"),
        )

        all_lines = [header_l2, p2_line, totals_l2]
        all_tokens = p2_tokens

        recovered = recover_anchor_guided_table(
            items=[p1_item],
            lines=all_lines,
            tokens=all_tokens,
            financial_summary=fs,
            page_number=None,  # Multi-page discovery
        )

        assert len(recovered) == 2
        recovered_sum = sum(it.total_price_net.amount for it in recovered)
        assert recovered_sum == Decimal("350.00")
        assert any("СТР" in (it.description or "") for it in recovered)


class TestArchiveAcceptanceP0:
    """Acceptance tests for real archive invoices 51.pdf, 48.pdf, 14.pdf."""

    @pytest.mark.skipif(not (ARCHIVE_DIR / "51.pdf").is_file(), reason="Archive 51.pdf not accessible")
    def test_acceptance_51_pdf(self):
        inv = process_invoice(ARCHIVE_DIR / "51.pdf")
        assert inv.validation.is_valid is True
        mismatch_errors = [e for e in inv.validation.errors if e.code == "LINE_ITEMS_TOTAL_MISMATCH"]
        assert len(mismatch_errors) == 0
        assert len(inv.line_items) == 2
        items_sum = sum(it.total_price_net.amount for it in inv.line_items)
        assert items_sum == Decimal("543.56")

    @pytest.mark.skipif(not (ARCHIVE_DIR / "48.pdf").is_file(), reason="Archive 48.pdf not accessible")
    def test_acceptance_48_pdf(self):
        inv = process_invoice(ARCHIVE_DIR / "48.pdf")
        assert inv.validation.is_valid is True
        mismatch_errors = [e for e in inv.validation.errors if e.code == "LINE_ITEMS_TOTAL_MISMATCH"]
        assert len(mismatch_errors) == 0
        assert len(inv.line_items) == 2
        items_sum = sum(it.total_price_net.amount for it in inv.line_items)
        assert items_sum == Decimal("50.58")

    @pytest.mark.skipif(not (ARCHIVE_DIR / "14.pdf").is_file(), reason="Archive 14.pdf not accessible")
    def test_acceptance_14_pdf(self):
        inv = process_invoice(ARCHIVE_DIR / "14.pdf")
        assert inv.validation.is_valid is True
        mismatch_errors = [e for e in inv.validation.errors if e.code == "LINE_ITEMS_TOTAL_MISMATCH"]
        assert len(mismatch_errors) == 0
        assert len(inv.line_items) == 8
        items_sum = sum(it.total_price_net.amount for it in inv.line_items)
        assert abs(items_sum - Decimal("64.49")) <= Decimal("0.05")

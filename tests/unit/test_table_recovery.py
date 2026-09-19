"""Unit tests for table recovery, projection profile, and line item extraction."""

from decimal import Decimal
from pathlib import Path
import sys
import unittest

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from invoice_core.extraction.models import (
    FinancialSummary,
    LineItem,
    LogicalLine,
    MoneyAmount,
    OcrToken,
    Party,
    TableColumn,
    TableRegion,
)
from invoice_core.ocr.table_recovery import (
    _sanitize_line_item_candidate,
    build_x_projection_profile,
    clean_table_crop,
    filter_carry_over_items,
    is_transfer_or_header_line,
    synthesize_service_line_item,
)
from tests.fixtures.helpers import make_ocr_token, make_logical_line


class TestTableRecovery(unittest.TestCase):
    """Test suite for table recovery algorithms and line item synthesis."""

    def test_sanitize_line_item_candidate(self):
        """Verify extraction and sanitization of monetary values from table cells."""
        # Simple valid price
        self.assertEqual(_sanitize_line_item_candidate("120.50"), Decimal("120.50"))
        self.assertEqual(_sanitize_line_item_candidate("120,50 лв."), Decimal("120.50"))

        # Strip dot-matrix prefixes or noise
        self.assertEqual(_sanitize_line_item_candidate("т15.00"), Decimal("15.00"))

        # Quantity parsing with is_qty=True
        self.assertEqual(_sanitize_line_item_candidate("5.000", is_qty=True), Decimal("5.000"))

        # Reject pure noise
        self.assertIsNone(_sanitize_line_item_candidate("|||"))
        self.assertIsNone(_sanitize_line_item_candidate(""))

    def test_is_transfer_or_header_line(self):
        """Verify detection of table header or carry-over/transfer lines."""
        line_carry = make_logical_line(text="ПРЕНОС")
        self.assertTrue(is_transfer_or_header_line(line_carry))

        line_carry2 = make_logical_line(text="Междинна сума")
        self.assertTrue(is_transfer_or_header_line(line_carry2))

        line_item = make_logical_line(text="1 Кашкавал Витоша 2.000 кг 25.00 50.00")
        self.assertFalse(is_transfer_or_header_line(line_item))

    def test_synthesize_service_line_item(self):
        """Verify synthesis of a single line item for service invoices lacking grid tables."""
        lines = [
            make_logical_line(text="ФАКТУРА 0000000001"),
            make_logical_line(text="Абонаментна поддръжка софтуер за м. Август 2026"),
            make_logical_line(text="Данъчна основа: 500.00 лв. ДДС 20%: 100.00 лв."),
            make_logical_line(text="Сума за плащане: 600.00 лв."),
        ]
        tokens = [t for l in lines for t in l.tokens]

        fin_summary = FinancialSummary(
            tax_base=MoneyAmount(Decimal("500.00"), "BGN"),
            vat_amount=MoneyAmount(Decimal("100.00"), "BGN"),
            total_amount_due=MoneyAmount(Decimal("600.00"), "BGN"),
        )
        supplier = Party(name="СОФТУЕР ИНВЕСТ ЕООД", eik="121644736")

        item = synthesize_service_line_item(
            lines=lines,
            tokens=tokens,
            financial_summary=fin_summary,
            supplier=supplier,
        )

        self.assertIsNotNone(item)
        self.assertEqual(item.quantity, Decimal("1"))
        self.assertEqual(item.total_price_net.amount, Decimal("500.00"))
        self.assertEqual(item.total_price_net.currency, "BGN")
        self.assertEqual(item.vat_rate_pct, Decimal("20"))

    def test_build_x_projection_profile(self):
        """Verify x-projection profile creation from tokens list."""
        tokens = [
            make_ocr_token("Описание", bbox=(50, 100, 200, 30)),
            make_ocr_token("Мярка", bbox=(300, 100, 50, 30)),
            make_ocr_token("Количество", bbox=(400, 100, 80, 30)),
            make_ocr_token("Цена", bbox=(550, 100, 60, 30)),
            make_ocr_token("Сума", bbox=(680, 100, 60, 30)),
        ]
        cols = build_x_projection_profile(tokens, page_width=1000)
        self.assertIsInstance(cols, list)
        self.assertTrue(len(cols) > 0)

    def test_filter_carry_over_items(self):
        """Verify filtering out carry-over / subtotal duplicates from line items."""
        item1 = LineItem(
            index=1,
            description="Артикул 1",
            quantity=Decimal("1"),
            unit="бр.",
            unit_price_net=MoneyAmount(Decimal("10.00"), "BGN"),
            total_price_net=MoneyAmount(Decimal("10.00"), "BGN"),
        )
        item_carry = LineItem(
            index=2,
            description="ПРЕНОС",
            quantity=Decimal("0"),
            unit="",
            unit_price_net=MoneyAmount(Decimal("0.00"), "BGN"),
            total_price_net=MoneyAmount(Decimal("10.00"), "BGN"),
        )

        filtered = filter_carry_over_items([item1, item_carry])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].description, "Артикул 1")


if __name__ == "__main__":
    unittest.main()

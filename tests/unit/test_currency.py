"""Unit tests for statutory currency conversion and dual-currency parity (EUR/BGN under ЗВЕ)."""

from decimal import Decimal
from pathlib import Path
import sys
import unittest

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from invoice_core.currency import (
    FIXED_EUR_BGN_RATE,
    convert_bgn_to_eur,
    convert_eur_to_bgn,
    verify_dual_currency_parity,
)


class TestCurrency(unittest.TestCase):
    """Test suite for currency conversion and parity rules."""

    def test_fixed_rate_statutory_value(self):
        """Fixed rate must be exactly 1.95583 as mandated by Bulgarian law."""
        self.assertEqual(FIXED_EUR_BGN_RATE, Decimal("1.95583"))

    def test_convert_eur_to_bgn(self):
        """Verify EUR to BGN conversion using statutory formula."""
        # 100 EUR * 1.95583 = 195.583 -> 195.58 BGN
        self.assertEqual(convert_eur_to_bgn(Decimal("100.00")), Decimal("195.58"))
        self.assertEqual(convert_eur_to_bgn("100.00"), Decimal("195.58"))
        self.assertEqual(convert_eur_to_bgn(100), Decimal("195.58"))
        self.assertEqual(convert_eur_to_bgn(100.0), Decimal("195.58"))

        # 50.55 EUR * 1.95583 = 98.8672065 -> 98.87 BGN
        self.assertEqual(convert_eur_to_bgn(Decimal("50.55")), Decimal("98.87"))

    def test_convert_bgn_to_eur(self):
        """Verify BGN to EUR conversion using statutory formula."""
        # 195.58 BGN / 1.95583 = 99.99846... -> 100.00 EUR
        self.assertEqual(convert_bgn_to_eur(Decimal("195.58")), Decimal("100.00"))
        self.assertEqual(convert_bgn_to_eur("195.58"), Decimal("100.00"))

        # 10.00 BGN / 1.95583 = 5.1129... -> 5.11 EUR
        self.assertEqual(convert_bgn_to_eur(Decimal("10.00")), Decimal("5.11"))

    def test_verify_dual_currency_parity(self):
        """Verify parity verification with default and custom tolerances."""
        # Exact match
        self.assertTrue(verify_dual_currency_parity(Decimal("100.00"), Decimal("195.58")))

        # Within default 0.02 BGN tolerance
        self.assertTrue(verify_dual_currency_parity(Decimal("100.00"), Decimal("195.60")))
        self.assertTrue(verify_dual_currency_parity(Decimal("100.00"), Decimal("195.56")))

        # Beyond default 0.02 BGN tolerance
        self.assertFalse(verify_dual_currency_parity(Decimal("100.00"), Decimal("195.61")))
        self.assertFalse(verify_dual_currency_parity(Decimal("100.00"), Decimal("195.55")))

        # Custom tolerance
        self.assertTrue(
            verify_dual_currency_parity(Decimal("100.00"), Decimal("195.65"), tolerance=Decimal("0.10"))
        )
        self.assertFalse(
            verify_dual_currency_parity(Decimal("100.00"), Decimal("195.70"), tolerance=Decimal("0.10"))
        )

    def test_edge_cases_zero_and_negative(self):
        """Test zero and negative amounts."""
        self.assertEqual(convert_eur_to_bgn(Decimal("0.00")), Decimal("0.00"))
        self.assertEqual(convert_bgn_to_eur(Decimal("0.00")), Decimal("0.00"))
        self.assertTrue(verify_dual_currency_parity(Decimal("0.00"), Decimal("0.00")))

        # Negative (e.g. credit note / storno)
        self.assertEqual(convert_eur_to_bgn(Decimal("-100.00")), Decimal("-195.58"))
        self.assertEqual(convert_bgn_to_eur(Decimal("-195.58")), Decimal("-100.00"))
        self.assertTrue(verify_dual_currency_parity(Decimal("-100.00"), Decimal("-195.58")))


if __name__ == "__main__":
    unittest.main()

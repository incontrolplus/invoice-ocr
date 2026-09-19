"""Unit tests for Bulgarian text, date, currency, and identifier normalizers."""

from decimal import Decimal
from pathlib import Path
import sys
import unittest

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from invoice_core.extraction.normalizers import (
    clean_ocr_artifacts,
    correct_eik_checksum,
    is_recipient_keyword,
    is_supplier_keyword,
    is_valid_eik13,
    is_valid_eik9,
    normalize_bic,
    normalize_eik,
    normalize_iban,
    normalize_vat_number,
    parse_date,
    parse_money,
    repair_eik_mod11,
    sanitize_vat_rate,
    validate_eik,
    validate_iban_modulo97,
)


class TestNormalizers(unittest.TestCase):
    """Test suite for normalizers and checksum validation."""

    def test_eik9_checksum_validation(self):
        """Verify 9-digit EIK Mod-11 checksum validation."""
        # Known valid 9-digit Bulgarian EIKs
        valid_eiks = ["121644736", "114500333", "114631464", "114540185", "206062202"]
        for eik in valid_eiks:
            self.assertTrue(is_valid_eik9(eik), f"EIK {eik} should be valid")
            self.assertTrue(validate_eik(eik), f"EIK {eik} should validate")

        # Invalid checksums
        self.assertFalse(is_valid_eik9("121644737"))
        self.assertFalse(is_valid_eik9("123456789"))
        self.assertFalse(is_valid_eik9("114500334"))

        # Invalid lengths
        self.assertFalse(is_valid_eik9("12345678"))
        self.assertFalse(is_valid_eik9("1234567890"))
        self.assertFalse(is_valid_eik9(None))

    def test_eik13_checksum_validation(self):
        """Verify 13-digit EIK Mod-11 checksum validation."""
        # 114500333 is valid 9-digit EIK. Sub-digits: 0006 -> valid 13th digit
        valid_13 = "1145003330006"
        self.assertTrue(is_valid_eik13(valid_13))
        self.assertTrue(validate_eik(valid_13))

        # Invalid 13th digit
        self.assertFalse(is_valid_eik13("1145003330007"))
        # Invalid first 9 digits
        self.assertFalse(is_valid_eik13("1145003340006"))

    def test_eik_repair_and_correction(self):
        """Verify single-digit OCR corruptions can be repaired using Mod-11."""
        # 121644736: Corrupt last digit to 0 -> should correct to 6
        repaired = correct_eik_checksum("121644730")
        self.assertEqual(repaired, "121644736")

        # repair_eik_mod11: 721644736 with '7'<->'1' confusion -> repairs to 121644736
        res = repair_eik_mod11("721644736")
        self.assertEqual(res, "121644736")

    def test_normalize_eik(self):
        """Verify EIK normalization strips prefixes, spaces, punctuation."""
        self.assertEqual(normalize_eik("BG 121644736"), "121644736")
        self.assertEqual(normalize_eik("ЕИК: 121-644-736"), "121644736")
        self.assertEqual(normalize_eik("  121 644 736  "), "121644736")

    def test_iban_mod97_validation(self):
        """Verify ISO 7064 Modulo 97-10 IBAN validation."""
        # Valid Bulgarian IBAN satisfying Modulo 97-10
        valid_iban = "BG59STSA93000020265432"
        self.assertTrue(validate_iban_modulo97(valid_iban))
        self.assertEqual(normalize_iban("bg59 stsa 9300 0020 2654 32"), "BG59STSA93000020265432")

        # Corrupted IBAN
        self.assertFalse(validate_iban_modulo97("BG59STSA93000020265433"))
        self.assertFalse(validate_iban_modulo97("INVALID_IBAN"))

    def test_normalize_bic(self):
        """Verify BIC/SWIFT normalization."""
        self.assertEqual(normalize_bic("stsa bg sf"), "STSABGSF")
        self.assertEqual(normalize_bic("STSA BG SF XXX"), "STSABGSFXXX")
        self.assertIsNone(normalize_bic("INVALID_BIC_TOO_LONG_12345"))

    def test_normalize_vat_number(self):
        """Verify VAT number normalization with BG prefix."""
        self.assertEqual(normalize_vat_number("121644736"), "BG121644736")
        self.assertEqual(normalize_vat_number("BG121644736"), "BG121644736")
        self.assertEqual(normalize_vat_number("bg 121644736"), "BG121644736")

    def test_parse_money(self):
        """Verify robust monetary value parsing."""
        self.assertEqual(parse_money("123,45"), Decimal("123.45"))
        self.assertEqual(parse_money("123.45"), Decimal("123.45"))
        self.assertEqual(parse_money("1 234,56 лв."), Decimal("1234.56"))
        self.assertEqual(parse_money("1.234,56 EUR"), Decimal("1234.56"))
        self.assertEqual(parse_money("1,234.56"), Decimal("1234.56"))
        self.assertEqual(parse_money("-50.00"), Decimal("-50.00"))
        self.assertEqual(parse_money("(120.00)"), Decimal("-120.00"))
        self.assertIsNone(parse_money("невалидно"))
        self.assertIsNone(parse_money(""))

    def test_parse_date(self):
        """Verify Bulgarian and ISO date parsing."""
        self.assertEqual(parse_date("2026-08-31"), "2026-08-31")
        self.assertEqual(parse_date("31.08.2026"), "2026-08-31")
        self.assertEqual(parse_date("31/08/2026"), "2026-08-31")
        self.assertEqual(parse_date("31.8.2026"), "2026-08-31")

    def test_clean_ocr_artifacts(self):
        """Verify removing markdown links, tel links, and stray symbols."""
        raw = "Доставчик: [121644736](tel:121644736) [Сайт](https://example.com)"
        cleaned = clean_ocr_artifacts(raw)
        self.assertNotIn("tel:", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertIn("121644736", cleaned)

    def test_party_keywords(self):
        """Verify detection of supplier and recipient keywords."""
        self.assertTrue(is_supplier_keyword("ДОСТАВЧИК:"))
        self.assertTrue(is_supplier_keyword("ПРОДАВАЧ:"))
        self.assertTrue(is_recipient_keyword("ПОЛУЧАТЕЛ:"))
        self.assertTrue(is_recipient_keyword("КЛИЕНТ:"))


if __name__ == "__main__":
    unittest.main()

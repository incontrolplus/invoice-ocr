"""Unit and regression tests for invoice number extraction and counterparty EIK disambiguation.

Verifies compliance with Art. 6 Accountancy Act (ЗСч) and Art. 114 VAT Act (ЗДДС),
preventing party UIC/EIK numbers (e.g. 202262252, 208230838) from being padded
with leading zeroes and substituted for statutory invoice numbers.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from invoice_ocr import (
    Invoice,
    OcrToken,
    LogicalLine,
    Party,
    group_tokens_into_lines,
    extract_invoice_number,
    extract_party,
    validate_eik,
    is_valid_eik9,
    _validate_identifiers,
)


class TestInvoiceNumberEikDisambiguation:
    """Test strict separation of invoice numbers and counterparty EIKs."""

    def test_anda_01_supplier_eik_not_extracted_as_invoice_number(self):
        """In анда-01.pdf, invoice number is 0000106705, supplier EIK is 202262252.

        Homep 0000106705 (with Latin 'Homep') must be extracted as 0000106705,
        and supplier EIK 202262252 must NOT be zero-padded to 0202262252.
        """
        tokens = [
            # Line 1: Document title
            OcrToken(text="Фактура", conf=92.0, bbox=(444, 175, 294, 65), page_number=1),
            # Line 2: Latin 'Homep' + invoice number + date
            OcrToken(text="Homep", conf=91.0, bbox=(1474, 188, 123, 38), page_number=1),
            OcrToken(text="0000106705", conf=96.0, bbox=(1610, 187, 209, 30), page_number=1),
            OcrToken(text="Дата", conf=96.0, bbox=(2011, 190, 90, 36), page_number=1),
            OcrToken(text="10.04.2026", conf=96.0, bbox=(2115, 189, 183, 29), page_number=1),
            # Supplier section tokens
            OcrToken(text="Доставчик", conf=96.0, bbox=(1235, 295, 176, 34), page_number=1),
            OcrToken(text="Анда", conf=92.0, bbox=(1499, 297, 92, 33), page_number=1),
            OcrToken(text="2012", conf=91.0, bbox=(1602, 296, 82, 31), page_number=1),
            OcrToken(text="Анко", conf=92.0, bbox=(1722, 297, 90, 30), page_number=1),
            OcrToken(text="Петров", conf=96.0, bbox=(1825, 298, 127, 38), page_number=1),
            OcrToken(text="ЕООД", conf=96.0, bbox=(1964, 298, 111, 36), page_number=1),
            OcrToken(text="ЕИК", conf=99.0, bbox=(1233, 330, 100, 55), page_number=1),
            OcrToken(text="202262252", conf=99.0, bbox=(1483, 330, 225, 55), page_number=1),
            # Line with stray '#' followed by EIK
            OcrToken(text="#", conf=99.0, bbox=(1458, 377, 27, 60), page_number=1),
            OcrToken(text="202262252", conf=99.0, bbox=(1558, 369, 281, 69), page_number=1),
        ]
        lines = group_tokens_into_lines(tokens)
        inv_num = extract_invoice_number(lines, tokens, supplier_eik="202262252", recipient_eik="207930830")
        assert inv_num == "0000106705", f"Expected 0000106705, got {inv_num}"
        assert inv_num != "0202262252", "Supplier EIK was mistakenly padded and used as invoice number"

    def test_metro_recipient_eik_not_extracted_as_invoice_number(self):
        """In метро.pdf, invoice number is 2208418424 on Page 1, recipient EIK is 208230838 on Page 3.

        ФАКТУРА Н:2208418424 on Page 1 must be extracted as 2208418424.
        ИА номер: 208230838 on Page 3 must NOT be zero-padded to 0208230838 as the invoice number.
        """
        tokens = [
            # Page 1 Supplier & Header
            OcrToken(text="МЕТРО КЕШ ЕНД КЕРИ", conf=90.0, bbox=(100, 200, 300, 30), page_number=1),
            OcrToken(text="БЪЛГАРИЯ ЕООД", conf=90.0, bbox=(100, 240, 200, 30), page_number=1),
            OcrToken(text="ЕИК 121644736", conf=90.0, bbox=(100, 280, 200, 30), page_number=1),
            OcrToken(text="ОРИГИНАЛ", conf=64.0, bbox=(944, 731, 134, 31), page_number=1),
            OcrToken(text="ФАКТУРА", conf=76.0, bbox=(941, 782, 120, 30), page_number=1),
            OcrToken(text="Н:2208418424", conf=68.0, bbox=(1080, 782, 208, 30), page_number=1),
            OcrToken(text="footer_marker", conf=90.0, bbox=(100, 3450, 10, 10), page_number=1),
            # Page 3 Buyer block (standard 300 DPI A4 page)
            OcrToken(text="КУПУВАЧ:", conf=80.0, bbox=(76, 1100, 150, 30), page_number=3),
            OcrToken(text="ГМ2025", conf=85.0, bbox=(240, 1100, 100, 30), page_number=3),
            OcrToken(text="ЕООД", conf=85.0, bbox=(350, 1100, 80, 30), page_number=3),
            OcrToken(text="ИА", conf=74.0, bbox=(76, 1222, 31, 34), page_number=3),
            OcrToken(text="номер:", conf=25.0, bbox=(130, 1230, 96, 30), page_number=3),
            OcrToken(text="208230838", conf=42.0, bbox=(250, 1222, 156, 31), page_number=3),
            OcrToken(text="footer_marker_p3", conf=90.0, bbox=(100, 3450, 10, 10), page_number=3),
        ]
        lines = group_tokens_into_lines(tokens)
        inv_num = extract_invoice_number(lines, tokens, supplier_eik="121644736", recipient_eik="208230838")
        assert inv_num == "2208418424", f"Expected 2208418424, got {inv_num}"
        assert inv_num != "0208230838", "Recipient EIK was mistakenly padded and used as invoice number"

        # Also verify recipient EIK extraction recognizes ИА номер: 208230838
        rec_party = extract_party(lines, tokens, "recipient")
        assert rec_party.eik == "208230838"

    def test_eik_modulo11_disqualification(self):
        """Any 9-digit number satisfying Modulo-11 UIC/BULSTAT checksum must be rejected as invoice number."""
        valid_eiks = [
            "202262252",  # АНДА 2012 АНКО ПЕТРОВ ЕООД
            "208230838",  # ГМ2025 ЕООД
            "114500333",  # КАПИНА 71 ООД
            "121644736",  # МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД
            "207930830",  # ФАСТ ТОП ФУУДС ЕООД
            "104586266",  # ГРЕСТОКОМЕРС ЕООД
        ]
        for eik in valid_eiks:
            assert is_valid_eik9(eik), f"Expected {eik} to be valid 9-digit EIK"
            tokens = [
                OcrToken(text="#", conf=90.0, bbox=(100, 200, 20, 20), page_number=1),
                OcrToken(text=eik, conf=90.0, bbox=(130, 200, 150, 20), page_number=1),
            ]
            lines = group_tokens_into_lines(tokens)
            result = extract_invoice_number(lines, tokens)
            assert result is None or result != ("0" + eik), (
                f"Valid EIK {eik} was incorrectly accepted/padded as invoice number: {result}"
            )

    def test_latin_homoglyph_homep_variations(self):
        """Variations of Latin and Cyrillic 'Homep' must correctly extract invoice numbers."""
        variations = [
            "Homep 0000106705",      # Full Latin 'Homep'
            "Номер 0000106705",      # Full Cyrillic 'Номер'
            "HOMEP 0000106705",      # Uppercase Latin
            "НОМЕР 0000106705",      # Uppercase Cyrillic
            "Homep: 0000106705",     # With colon
            "Номер: 0000106705",     # Cyrillic with colon
        ]
        for text in variations:
            line = LogicalLine(
                tokens=[OcrToken(text=text, conf=95.0, bbox=(1400, 180, 400, 30), page_number=1)],
                bbox=(1400, 180, 400, 30),
                text=text,
                page_number=1,
                y_center=195.0,
            )
            res = extract_invoice_number([line], line.tokens)
            assert res == "0000106705", f"Failed for variation '{text}': got {res}"

    def test_metro_style_invoice_prefixes(self):
        """Metro-style invoice number prefixes must be supported."""
        metro_lines = [
            "ФАКТУРА Н:2208418424",
            "ФАКТУРА Н: 2208418424",
            "ФАКТУРА N: 2208418424",
            "ФАКТУРА H: 2208418424",
            "ФАКТУРА 221192614",
        ]
        expected_numbers = [
            "2208418424",
            "2208418424",
            "2208418424",
            "2208418424",
            "0221192614",  # 9-digit padded to 10
        ]
        for line_str, exp in zip(metro_lines, expected_numbers):
            line = LogicalLine(
                tokens=[OcrToken(text=line_str, conf=90.0, bbox=(900, 780, 400, 30), page_number=1)],
                bbox=(900, 780, 400, 30),
                text=line_str,
                page_number=1,
                y_center=795.0,
            )
            res = extract_invoice_number([line], line.tokens)
            assert res == exp, f"Failed for '{line_str}': expected {exp}, got {res}"

    def test_spaced_invoice_numbers(self):
        """Invoice numbers with internal kerning spaces must be correctly consolidated."""
        cases = [
            ("Номер 00001 06854", "0000106854"),
            ("А/о 00005 75339", "0000575339"),
        ]
        for line_str, exp in cases:
            line = LogicalLine(
                tokens=[OcrToken(text=line_str, conf=90.0, bbox=(1400, 180, 400, 30), page_number=1)],
                bbox=(1400, 180, 400, 30),
                text=line_str,
                page_number=1,
                y_center=195.0,
            )
            res = extract_invoice_number([line], line.tokens)
            assert res == exp, f"Failed for '{line_str}': expected {exp}, got {res}"

    def test_phone_numbers_rejected_as_invoice_numbers(self):
        """Bulgarian telephone numbers must never be extracted as invoice numbers."""
        phone_cases = [
            "Телефон: 0885727402",
            "Тел. 0887845607",
            "GSM: 0899123456",
            "Телефон 0888979000",                    # Without colon
            "Телефон Телефон 0888979000",            # Repeated token without colon
            "Доставчик: Клийн Системс Тел 0888979000", # Embedded within line
            "МОЛ: Пламен Николов GSM 0878123456",     # GSM within party details
            "Факс 029876543",                        # Fax number
        ]
        for text in phone_cases:
            line = LogicalLine(
                tokens=[OcrToken(text=text, conf=90.0, bbox=(1200, 700, 300, 30), page_number=1)],
                bbox=(1200, 700, 300, 30),
                text=text,
                page_number=1,
                y_center=715.0,
            )
            res = extract_invoice_number([line], line.tokens)
            assert res is None, f"Telephone line '{text}' incorrectly extracted as invoice number: {res}"

    def test_pass3_fallback_disqualifies_bulgarian_mobile_numbers(self):
        """In Pass 3 (fallback without explicit labels), Bulgarian mobile numbers (087/088/089/098) must be rejected."""
        mobile_numbers = [
            "0888979000",  # A1 Bulgaria mobile
            "0878123456",  # Vivacom mobile
            "0899654321",  # Yettel mobile
            "0988112233",  # Bulsatcom / alternative mobile
        ]
        for mob in mobile_numbers:
            # Line without any keywords, purely testing Pass 3 fallback
            line = LogicalLine(
                tokens=[OcrToken(text=mob, conf=95.0, bbox=(1200, 700, 200, 30), page_number=1)],
                bbox=(1200, 700, 200, 30),
                text=mob,
                page_number=1,
                y_center=715.0,
            )
            res = extract_invoice_number([line], line.tokens)
            assert res is None, f"Mobile phone '{mob}' was incorrectly accepted as fallback invoice number: {res}"

    def test_leading_zero_invoice_number_not_rejected_as_noise(self):
        """Statutory 10-digit invoice numbers starting with multiple zeros (e.g. 0000006960) must be preserved."""
        tokens = [
            OcrToken(text="Номер:", conf=96.0, bbox=(1703, 373, 145, 53), page_number=1),
            OcrToken(text="0000006960", conf=92.0, bbox=(2069, 373, 248, 40), page_number=1),
        ]
        lines = group_tokens_into_lines(tokens)
        res = extract_invoice_number(lines, tokens)
        assert res == "0000006960", f"Expected 0000006960, got {res}"

    def test_dotmatrix_homoglyph_decoding_neacn(self):
        """Dot-matrix font homoglyphs (n->0, e->6, a->9, c->6) following 'Номер:' must decode to '0000006960'."""
        cases = [
            "Номер: nnnnnneacn",       # Cyrillic Номер: with lowercase dot-matrix string
            "Homep: NNNNNNEACN",       # Latin Homep: with uppercase dot-matrix string
            "Фактура №: nnnnnneacn",   # Faktura label with dot-matrix string
            "Номер: nn0000eacn",       # Mixed numbers and dot-matrix letters
        ]
        for text in cases:
            line = LogicalLine(
                tokens=[OcrToken(text=text, conf=88.0, bbox=(1700, 370, 450, 40), page_number=1)],
                bbox=(1700, 370, 450, 40),
                text=text,
                page_number=1,
                y_center=390.0,
            )
            res = extract_invoice_number([line], line.tokens)
            assert res == "0000006960", f"Expected 0000006960 for '{text}', got {res}"

        # Test two-line structure: Line 1 'Номер:', Line 2 'nnnnnneacn'
        l1 = LogicalLine(
            tokens=[OcrToken(text="Номер:", conf=95.0, bbox=(1700, 370, 150, 40), page_number=1)],
            bbox=(1700, 370, 150, 40),
            text="Номер:",
            page_number=1,
            y_center=390.0,
        )
        l2 = LogicalLine(
            tokens=[OcrToken(text="nnnnnneacn", conf=85.0, bbox=(2050, 370, 250, 40), page_number=1)],
            bbox=(2050, 370, 250, 40),
            text="nnnnnneacn",
            page_number=1,
            y_center=390.0,
        )
        res_twoline = extract_invoice_number([l1, l2], l1.tokens + l2.tokens)
        assert res_twoline == "0000006960", f"Expected 0000006960 for two-line, got {res_twoline}"

    def test_validation_issue_emitted_if_invoice_number_matches_eik(self):
        """Layer 3 validation must flag an error if invoice number matches counterparty EIK."""
        inv = Invoice()
        inv.invoice_metadata.invoice_number = "0202262252"
        inv.supplier = Party(name="Анда 2012 Анко Петров ЕООД", eik="202262252")
        inv.recipient = Party(name="ФАСТ ТОП ФУУДС ЕООД", eik="207930830")

        issues = _validate_identifiers(inv)
        error_codes = [i.code for i in issues if i.severity == "error"]
        assert "INVOICE_NUMBER_MATCHES_EIK" in error_codes, (
            f"Expected INVOICE_NUMBER_MATCHES_EIK error issue, got: {issues}"
        )

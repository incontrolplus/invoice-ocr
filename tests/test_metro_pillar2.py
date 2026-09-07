"""Pillar 2 Test Suite: Metro Cash & Carry Invoicing & Multi-Page Document Understanding.

Verifies:
1. Party Separation Invariant (supplier.eik != recipient.eik, no self-invoicing/duplication).
2. Metro Branch / Postal Code Normalization (separating clean 9-digit EIK 121644736 from fused postal codes).
3. 13-digit Bulgarian Modulo-11 statutory checksum logic.
4. ZDDS Art. 26 tolerance (0.03 EUR tolerance for >15 items / volume discounts / EUR dual pricing).
5. Dynamic multi-page recipient extraction above fiscal receipt ("СИСТЕМЕН БОН").
6. Real PDF acceptance tests on target files (02, 03, 04, 05, 06, 07, 42, 63).
"""
import os
from decimal import Decimal
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

from invoice_ocr import (
    Invoice,
    InvoiceMetadata,
    Party,
    LineItem,
    FinancialSummary,
    MoneyAmount,
    ValidationIssue,
    OcrToken,
    LogicalLine,
    is_valid_eik13,
    validate_eik,
    normalize_eik,
    _validate_identifiers,
    _validate_totals,
    _validate_line_items,
    _extract_metro_recipient,
    _is_metro_document,
    process_invoice,
    ZDDS_DISCOUNT_TOLERANCE,
    VAT_TOLERANCE,
)

DATASET_DIR = "/Volumes/NO NAME/_ФАКТУРИ/00_РМ_КАСКАДА_2026_ЕООД"
HAS_DATASET = os.path.isdir(DATASET_DIR)


class TestMetroBranchEikDisambiguation:
    """Test resolution of fused Metro 13-digit strings and 13-digit Modulo-11 checksums."""

    def test_valid_eik13_statutory_algorithm(self):
        # A valid 9-digit EIK extended with a valid 4-digit branch checksum
        # E.g. 121644736 + valid 4 digits
        # Under Modulo 11: weights [2, 7, 3, 5] then [4, 9, 5, 7]
        # Metro Pleven 1216447365800 has 5800 as postal code, NOT valid check digits
        assert not is_valid_eik13("1216447365800")
        assert not is_valid_eik13("1216447361784")  # Sofia postal code 1784
        assert not is_valid_eik13("1216447364000")  # Plovdiv postal code 4000

    def test_normalize_fused_metro_strings(self):
        # OCR frequently concatenates Metro EIK and store postal codes
        fused_cases = [
            ("1216447365800", "121644736"),
            ("1216447361784", "121644736"),
            ("1216447364000", "121644736"),
            ("BG1216447365800", "121644736"),
            ("121644736 5800", "121644736"),
        ]
        for raw, expected in fused_cases:
            assert normalize_eik(raw) == expected, f"Failed for {raw}"
            # Once normalized, it must pass 9-digit EIK validation
            assert validate_eik(normalize_eik(raw))

    def test_raw_fused_eik_rejected_as_eik13(self):
        # The fused 13-digit string must fail validate_eik if not normalized
        assert not validate_eik("1216447365800")
        assert not validate_eik("1216447361784")


class TestPartySeparationInvariant:
    """Test that supplier and recipient cannot share the same EIK."""

    def test_party_collision_same_eik_detected(self):
        inv = Invoice(
            supplier=Party(name="МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД", eik="121644736"),
            recipient=Party(name="МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД", eik="121644736"),
        )
        issues = _validate_identifiers(inv)
        collision_issues = [i for i in issues if i.code == "PARTY_COLLISION_SAME_EIK"]
        assert len(collision_issues) == 1
        assert collision_issues[0].severity == "error"
        assert collision_issues[0].field == "recipient.eik"

    def test_distinct_parties_pass_separation(self):
        inv = Invoice(
            supplier=Party(name="МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД", eik="121644736"),
            recipient=Party(name="РМ КАСКАДА 2026 ЕООД", eik="208380135"),
        )
        issues = _validate_identifiers(inv)
        collision_issues = [i for i in issues if i.code == "PARTY_COLLISION_SAME_EIK"]
        assert len(collision_issues) == 0


class TestZddsArt26Tolerance:
    """Test 0.03 EUR validation tolerance for multi-item invoices (>15 items), volume discounts, or EUR currency."""

    def test_totals_tolerance_applied_for_eur_multi_item(self):
        # 16 line items, cumulative rounding error of 0.03 between line items sum and tax base
        items = [
            LineItem(
                index=i,
                description=f"Item {i}",
                quantity=Decimal("1"),
                unit_price_net=MoneyAmount(Decimal("10.00"), "EUR"),
                total_price_net=MoneyAmount(Decimal("10.00"), "EUR"),
                vat_rate_pct=Decimal("20"),
            )
            for i in range(16)
        ]
        # Sum is 160.00 EUR, but tax base is 160.03 EUR (difference = 0.03)
        fs = FinancialSummary(
            tax_base=MoneyAmount(Decimal("160.03"), "EUR"),
            vat_amount=MoneyAmount(Decimal("32.01"), "EUR"),
            total_amount_due=MoneyAmount(Decimal("192.04"), "EUR"),
        )
        inv = Invoice(
            line_items=items,
            financial_summary=fs,
            supplier=Party(name="МЕТРО КЕШ ЕНД КЕРИ", eik="121644736"),
            recipient=Party(name="РМ КАСКАДА 2026 ЕООД", eik="208380135"),
        )
        issues = _validate_totals(inv)
        # Should not have LINE_ITEMS_TOTAL_MISMATCH or TOTAL_SUM_MISMATCH because diff is 0.03 <= 0.03
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 0

    def test_line_item_tolerance_applied_for_discounts(self):
        # Line item with volume discount where quantity * unit_price differs by 0.03
        item = LineItem(
            index=1,
            description="Промоция Метро отстъпка",
            quantity=Decimal("3"),
            unit_price_net=MoneyAmount(Decimal("33.33"), "EUR"),
            total_price_net=MoneyAmount(Decimal("100.02"), "EUR"),  # 3 * 33.33 = 99.99, diff = 0.03
        )
        setattr(item, "discount_pct", Decimal("5"))
        inv = Invoice(
            line_items=[item],
            financial_summary=FinancialSummary(tax_base=MoneyAmount(Decimal("100.02"), "EUR")),
        )
        issues = _validate_line_items(inv)
        mismatches = [i for i in issues if i.code == "LINE_ITEM_CALC_MISMATCH"]
        assert len(mismatches) == 0


class TestMultiPageMetroRecipientExtraction:
    """Test recipient extraction across multi-page Metro documents."""

    def test_metro_recipient_extracted_on_page_2_above_receipt(self):
        # Simulate tokens across 3 pages:
        # Page 1: Metro Sofia header
        # Page 2: Buyer box
        # Page 3: Fiscal receipt
        tokens = [
            # Page 1
            OcrToken("МЕТРО", 95, (100, 100, 80, 20), page_number=1, is_low_confidence=False),
            OcrToken("КЕШ", 95, (190, 100, 50, 20), page_number=1, is_low_confidence=False),
            OcrToken("ЕНД", 95, (250, 100, 50, 20), page_number=1, is_low_confidence=False),
            OcrToken("КЕРИ", 95, (310, 100, 60, 20), page_number=1, is_low_confidence=False),
            OcrToken("ЦАРИГРАДСКО", 95, (100, 130, 120, 20), page_number=1, is_low_confidence=False),
            OcrToken("ШОСЕ", 95, (230, 130, 60, 20), page_number=1, is_low_confidence=False),
            OcrToken("7-11", 95, (300, 130, 50, 20), page_number=1, is_low_confidence=False),
            OcrToken("KM", 95, (360, 130, 30, 20), page_number=1, is_low_confidence=False),
            OcrToken("1784", 95, (400, 130, 50, 20), page_number=1, is_low_confidence=False),
            OcrToken("СОФИЯ", 95, (460, 130, 60, 20), page_number=1, is_low_confidence=False),
            OcrToken("ЕИК", 95, (100, 160, 50, 20), page_number=1, is_low_confidence=False),
            OcrToken("121644736", 95, (160, 160, 100, 20), page_number=1, is_low_confidence=False),

            # Page 2: Customer box
            OcrToken("КУПУВАЧ:", 95, (100, 200, 90, 20), page_number=2, is_low_confidence=False),
            OcrToken("Клиент", 95, (200, 200, 70, 20), page_number=2, is_low_confidence=False),
            OcrToken("N:12345", 95, (280, 200, 80, 20), page_number=2, is_low_confidence=False),
            OcrToken("РМ", 95, (100, 230, 40, 20), page_number=2, is_low_confidence=False),
            OcrToken("КАСКАДА", 95, (150, 230, 90, 20), page_number=2, is_low_confidence=False),
            OcrToken("2026", 95, (250, 230, 50, 20), page_number=2, is_low_confidence=False),
            OcrToken("ЕООД", 95, (310, 230, 60, 20), page_number=2, is_low_confidence=False),
            OcrToken("Адрес:", 95, (100, 260, 50, 20), page_number=2, is_low_confidence=False),
            OcrToken("гр. Плевен", 95, (160, 260, 100, 20), page_number=2, is_low_confidence=False),
            OcrToken("ЕИК:", 95, (100, 290, 50, 20), page_number=2, is_low_confidence=False),
            OcrToken("208380135", 95, (160, 290, 100, 20), page_number=2, is_low_confidence=False),
            OcrToken("ДДС", 95, (100, 320, 50, 20), page_number=2, is_low_confidence=False),
            OcrToken("N:BG208380135", 95, (160, 320, 120, 20), page_number=2, is_low_confidence=False),

            # Page 3: Fiscal receipt
            OcrToken("СИСТЕМЕН", 95, (100, 100, 90, 20), page_number=3, is_low_confidence=False),
            OcrToken("БОН", 95, (200, 100, 50, 20), page_number=3, is_low_confidence=False),
        ]
        # Build logical lines
        lines = [
            LogicalLine(tokens=tokens[0:4], text="МЕТРО КЕШ ЕНД КЕРИ", bbox=(100, 100, 270, 20), page_number=1),
            LogicalLine(tokens=tokens[4:10], text="ЦАРИГРАДСКО ШОСЕ 7-11 KM 1784 СОФИЯ", bbox=(100, 130, 420, 20), page_number=1),
            LogicalLine(tokens=tokens[10:12], text="ЕИК 121644736", bbox=(100, 160, 160, 20), page_number=1),
            LogicalLine(tokens=tokens[12:15], text="КУПУВАЧ: Клиент N:12345", bbox=(100, 200, 260, 20), page_number=2),
            LogicalLine(tokens=tokens[15:19], text="РМ КАСКАДА 2026 ЕООД", bbox=(100, 230, 270, 20), page_number=2),
            LogicalLine(tokens=tokens[19:21], text="Адрес: гр. Плевен", bbox=(100, 260, 160, 20), page_number=2),
            LogicalLine(tokens=tokens[21:23], text="ЕИК: 208380135", bbox=(100, 290, 160, 20), page_number=2),
            LogicalLine(tokens=tokens[23:25], text="ДДС N:BG208380135", bbox=(100, 320, 180, 20), page_number=2),
            LogicalLine(tokens=tokens[25:27], text="СИСТЕМЕН БОН", bbox=(100, 100, 150, 20), page_number=3),
        ]

        recipient = _extract_metro_recipient(tokens, lines)
        assert recipient is not None
        assert recipient.name == "РМ КАСКАДА 2026 ЕООД"
        assert recipient.eik == "208380135"
        assert recipient.vat_number == "BG208380135"
        assert recipient.address == "гр. Плевен"
        assert recipient.eik != "121644736"


@pytest.mark.skipif(not HAS_DATASET, reason="Real invoice dataset on /Volumes/NO NAME is not mounted")
class TestMetroRealInvoicesPillar2:
    """Acceptance tests verifying Pillar 2 solutions against actual Metro PDFs."""

    @pytest.mark.parametrize("pdf_num", ["02", "03", "04", "05", "06", "07", "42", "63"])
    def test_metro_target_invoices(self, pdf_num):
        pdf_path = os.path.join(DATASET_DIR, f"{pdf_num}.pdf")
        assert os.path.exists(pdf_path), f"File {pdf_path} does not exist"

        invoice = process_invoice(pdf_path)

        # 1. Supplier must be Metro Cash & Carry
        assert invoice.supplier is not None
        assert "МЕТРО" in (invoice.supplier.name or "").upper()
        assert invoice.supplier.eik == "121644736"
        assert invoice.supplier.vat_number == "BG121644736"

        # 2. Recipient must be distinct from supplier (Party Separation Invariant)
        assert invoice.recipient is not None
        assert invoice.recipient.eik is not None
        assert invoice.recipient.eik != invoice.supplier.eik
        assert invoice.recipient.eik != "121644736"

        # 3. No party collision errors
        collision_errors = [
            iss for iss in invoice.validation.errors
            if iss.code == "PARTY_COLLISION_SAME_EIK"
        ]
        assert len(collision_errors) == 0, f"Party collision error found in {pdf_num}.pdf: {collision_errors}"

        # 4. No VAT calculation mismatch errors
        vat_calc_errors = [
            iss for iss in invoice.validation.errors
            if iss.code == "VAT_CALCULATION_MISMATCH"
        ]
        assert len(vat_calc_errors) == 0, f"VAT calculation mismatch in {pdf_num}.pdf: {vat_calc_errors}"

        # 5. Financial summary must have positive amounts
        assert invoice.financial_summary.tax_base.amount is not None
        assert invoice.financial_summary.tax_base.amount > Decimal("0.00")
        assert invoice.financial_summary.total_amount_due.amount is not None
        assert invoice.financial_summary.total_amount_due.amount > Decimal("0.00")

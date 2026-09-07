"""Tests for Critical Problem #6: Date anomalies and currency transition heuristics.

Covers:
1. Future date detection and validation (FUTURE_DATE error).
2. OCR year digit sanitization (e.g., misread 9 for 5: 2029 -> 2025).
3. Evidence-based currency resolution during the Euro transition period (post-2026 BGN vs EUR).
4. Dual-display informational EUR discounting.
"""

import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_ocr import (
    FIXED_EUR_BGN_RATE,
    FinancialSummary,
    Invoice,
    InvoiceMetadata,
    LogicalLine,
    MoneyAmount,
    OcrToken,
    PaymentDetails,
    _sanitize_extracted_date,
    _validate_currency,
    _validate_dates,
    convert_bgn_to_eur,
    convert_eur_to_bgn,
    extract_currency,
    extract_dates,
    extract_due_date,
    parse_date,
    process_invoice,
    verify_dual_currency_parity,
)


class TestDateValidationAndSanitization(unittest.TestCase):
    """Test date extraction, OCR anomaly sanitization, and future date validation."""

    def test_sanitize_extracted_date_misread_9_for_5(self):
        """Sanitizer should convert future year ending in 9 (2029) to past year (2025)."""
        # Given today is 2026-09-05, 2029-07-05 is in the future.
        # Replacing 9 with 5 gives 2025-07-05 <= today.
        sanitized = _sanitize_extracted_date("2029-07-05")
        self.assertEqual(sanitized, "2025-07-05")

    def test_sanitize_extracted_date_past_date_untouched(self):
        """Valid past and present dates should not be modified."""
        self.assertEqual(_sanitize_extracted_date("2025-07-05"), "2025-07-05")
        self.assertEqual(_sanitize_extracted_date("2026-01-15"), "2026-01-15")
        self.assertIsNone(_sanitize_extracted_date(None))

    def test_validate_dates_valid_past_and_present(self):
        """Dates in the past or on today should produce no validation issues."""
        today = datetime.date.today().isoformat()
        inv = Invoice()
        inv.invoice_metadata.date_issued = today
        inv.invoice_metadata.date_tax_event = "2025-12-31"

        issues = _validate_dates(inv)
        self.assertEqual(issues, [])

    def test_validate_dates_future_date_issued_triggers_error(self):
        """Invoice date in the future must trigger a FUTURE_DATE error."""
        inv = Invoice()
        inv.invoice_metadata.date_issued = "2030-01-01"

        issues = _validate_dates(inv)
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue.code, "FUTURE_DATE")
        self.assertEqual(issue.severity, "error")
        self.assertEqual(issue.field, "invoice_metadata.date_issued")
        self.assertIn("2030-01-01", issue.message)

    def test_validate_dates_future_date_tax_event_triggers_error(self):
        """Tax event date in the future must trigger a FUTURE_DATE error."""
        inv = Invoice()
        inv.invoice_metadata.date_issued = "2025-05-01"
        inv.invoice_metadata.date_tax_event = "2029-12-31"

        issues = _validate_dates(inv)
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue.code, "FUTURE_DATE")
        self.assertEqual(issue.severity, "error")
        self.assertEqual(issue.field, "invoice_metadata.date_tax_event")

    def test_validate_dates_invalid_calendar_date(self):
        """Invalid calendar date format (e.g. 2026-02-30) triggers INVALID_DATE error."""
        inv = Invoice()
        inv.invoice_metadata.date_issued = "2026-02-30"

        issues = _validate_dates(inv)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, "INVALID_DATE")
        self.assertEqual(issues[0].severity, "error")

    def test_extract_dates_with_ocr_misread(self):
        """extract_dates should automatically sanitize OCR year misrecognitions."""
        tokens = [
            OcrToken("Дата", 90, bbox=(10, 10, 50, 20)),
            OcrToken("на", 90, bbox=(55, 10, 75, 20)),
            OcrToken("издаване:", 90, bbox=(80, 10, 150, 20)),
            OcrToken("05.07.2029", 90, bbox=(160, 10, 250, 20)),
        ]
        line = LogicalLine(tokens=tokens)
        date_issued, date_tax = extract_dates([line])
        self.assertEqual(date_issued, "2025-07-05")

    def test_sanitize_extracted_date_month_homoglyph_09_to_08(self):
        """Sanitizer should convert dot-matrix 16.09.2026 (misread 8 as 9) to 16.08.2026 when <= today."""
        # Simulated today = 2026-09-06. Date 2026-09-16 is 10 days in the future.
        # Replacing month 9 with 8 yields 2026-08-16, which is <= 2026-09-06.
        today = datetime.date(2026, 9, 6)
        sanitized = _sanitize_extracted_date("2026-09-16", today=today)
        self.assertEqual(sanitized, "2026-08-16")

    def test_sanitize_extracted_date_month_homoglyph_10_to_08(self):
        """Sanitizer should convert future October date within 30 days to August if candidate <= today."""
        # Simulated today = 2026-09-06. Date 2026-10-02 is 26 days in the future.
        # Replacing month 10 with 8 yields 2026-08-02, which is <= 2026-09-06.
        today = datetime.date(2026, 9, 6)
        sanitized = _sanitize_extracted_date("2026-10-02", today=today)
        self.assertEqual(sanitized, "2026-08-02")

    def test_sanitize_extracted_date_distant_future_untouched(self):
        """Dates > 30 days in the future should not be modified by month homoglyph corrector."""
        today = datetime.date(2026, 9, 6)
        # 2026-10-25 is 49 days in the future (> 30 days)
        sanitized = _sanitize_extracted_date("2026-10-25", today=today)
        self.assertEqual(sanitized, "2026-10-25")

    def test_parse_date_bulgarian_suffixes(self):
        """parse_date must correctly extract date even with Bulgarian suffixes like 'r.', 'г.', ' г.'."""
        self.assertEqual(parse_date("Дата: 27.07.2026r."), "2026-07-27")
        self.assertEqual(parse_date("Фактура от 08.07.2026г."), "2026-07-08")
        self.assertEqual(parse_date("16.08.2026 г."), "2026-08-16")
        self.assertEqual(parse_date("2026-07-28г"), "2026-07-28")


class TestCurrencyTransitionResolution(unittest.TestCase):
    """Test evidence-based currency extraction during the Euro transition period."""

    def test_post_2026_bgn_invoice_with_informational_eur(self):
        """Post-2026 invoice in BGN with informational EUR footer must resolve to BGN."""
        tokens = [
            OcrToken("ФАКТУРА", 95, bbox=(10, 10, 50, 20)),
            OcrToken("Сума", 95, bbox=(10, 30, 40, 40)),
            OcrToken("за", 95, bbox=(45, 30, 60, 40)),
            OcrToken("плащане:", 95, bbox=(65, 30, 120, 40)),
            OcrToken("100.00", 95, bbox=(125, 30, 160, 40)),
            OcrToken("лв.", 95, bbox=(165, 30, 185, 40)),
            OcrToken("Равностойност", 90, bbox=(10, 50, 90, 60)),
            OcrToken("в", 90, bbox=(95, 50, 105, 60)),
            OcrToken("EUR:", 90, bbox=(110, 50, 140, 60)),
            OcrToken("51.13", 90, bbox=(145, 50, 180, 60)),
            OcrToken("EUR", 90, bbox=(185, 50, 210, 60)),
            OcrToken("курс", 90, bbox=(215, 50, 245, 60)),
            OcrToken("1.95583", 90, bbox=(250, 50, 290, 60)),
            OcrToken("Словом:", 90, bbox=(10, 70, 60, 80)),
            OcrToken("сто", 90, bbox=(65, 70, 90, 80)),
            OcrToken("лева", 90, bbox=(95, 70, 130, 80)),
        ]
        line1 = LogicalLine(tokens=tokens[0:1])
        line2 = LogicalLine(tokens=tokens[1:6])
        line3 = LogicalLine(tokens=tokens[6:13])
        line4 = LogicalLine(tokens=tokens[13:16])
        lines = [line1, line2, line3, line4]

        # Date is in 2026
        curr = extract_currency(lines, tokens, date_issued="2026-02-15")
        self.assertEqual(curr, "BGN")

    def test_post_2026_eur_invoice_with_informational_bgn(self):
        """Post-2026 invoice in EUR with informational BGN footer must resolve to EUR."""
        tokens = [
            OcrToken("Сума", 95, bbox=(10, 30, 40, 40)),
            OcrToken("за", 95, bbox=(45, 30, 60, 40)),
            OcrToken("плащане:", 95, bbox=(65, 30, 120, 40)),
            OcrToken("50.00", 95, bbox=(125, 30, 160, 40)),
            OcrToken("EUR", 95, bbox=(165, 30, 195, 40)),
            OcrToken("Равностойност", 90, bbox=(10, 50, 90, 60)),
            OcrToken("в", 90, bbox=(95, 50, 105, 60)),
            OcrToken("лева:", 90, bbox=(110, 50, 140, 60)),
            OcrToken("97.79", 90, bbox=(145, 50, 180, 60)),
            OcrToken("лв.", 90, bbox=(185, 50, 210, 60)),
            OcrToken("Словом:", 90, bbox=(10, 70, 60, 80)),
            OcrToken("петдесет", 90, bbox=(65, 70, 120, 80)),
            OcrToken("евро", 90, bbox=(125, 70, 160, 80)),
        ]
        line1 = LogicalLine(tokens=tokens[0:5])
        line2 = LogicalLine(tokens=tokens[5:10])
        line3 = LogicalLine(tokens=tokens[10:13])
        lines = [line1, line2, line3]

        curr = extract_currency(lines, tokens, date_issued="2026-03-01")
        self.assertEqual(curr, "EUR")

    def test_currency_from_words_precedence(self):
        """Statutory legal words line takes precedence over secondary labels."""
        tokens = [
            OcrToken("Словом:", 90, bbox=(10, 10, 60, 20)),
            OcrToken("двеста", 90, bbox=(65, 10, 110, 20)),
            OcrToken("лева", 90, bbox=(115, 10, 150, 20)),
        ]
        line = LogicalLine(tokens=tokens)
        curr = extract_currency([line], tokens, date_issued="2026-05-01")
        self.assertEqual(curr, "BGN")

    def test_genuine_tie_date_heuristic(self):
        """When text evidence is tied and inconclusive, fallback to transition date."""
        # 1 BGN indicator, 1 EUR indicator, neither informational
        tokens = [
            OcrToken("цена", 90, bbox=(10, 10, 40, 20)),
            OcrToken("EUR", 90, bbox=(45, 10, 70, 20)),
            OcrToken("цена", 90, bbox=(75, 10, 105, 20)),
            OcrToken("BGN", 90, bbox=(110, 10, 135, 20)),
        ]
        line = LogicalLine(tokens=tokens)

        # Before Jan 1, 2026 -> defaults to BGN
        self.assertEqual(extract_currency([line], tokens, date_issued="2025-10-15"), "BGN")
        # On or after Jan 1, 2026 -> defaults to EUR
        self.assertEqual(extract_currency([line], tokens, date_issued="2026-01-01"), "EUR")

    def test_ocr_error_tolerance_bgn_artifacts(self):
        """Tesseract OCR distortions (BGR, BOM, Cyrillic ВОМ) must resolve to BGN."""
        # BGR distortion
        tokens_bgr = [
            OcrToken("Общо", 90, bbox=(10, 10, 50, 20)),
            OcrToken("150.00", 90, bbox=(55, 10, 100, 20)),
            OcrToken("BGR", 90, bbox=(105, 10, 135, 20)),
        ]
        line_bgr = LogicalLine(tokens=tokens_bgr)
        self.assertEqual(extract_currency([line_bgr], tokens_bgr, date_issued="2025-08-10"), "BGN")

        # BOM distortion (Latin)
        tokens_bom = [
            OcrToken("Сума:", 90, bbox=(10, 10, 50, 20)),
            OcrToken("75.50", 90, bbox=(55, 10, 95, 20)),
            OcrToken("BOM", 90, bbox=(100, 10, 130, 20)),
        ]
        line_bom = LogicalLine(tokens=tokens_bom)
        self.assertEqual(extract_currency([line_bom], tokens_bom, date_issued="2025-09-01"), "BGN")

        # ВОМ distortion (Cyrillic)
        tokens_vom = [
            OcrToken("Сума", 90, bbox=(10, 10, 50, 20)),
            OcrToken("ВОМ!", 90, bbox=(55, 10, 95, 20)),
        ]
        line_vom = LogicalLine(tokens=tokens_vom)
        self.assertEqual(extract_currency([line_vom], tokens_vom, date_issued="2025-06-20"), "BGN")

    def test_ocr_error_tolerance_eur_artifacts(self):
        """Tesseract OCR distortions (EOB, EUB, ЕШ?, glued words) must resolve to EUR."""
        # EOB distortion
        tokens_eob = [
            OcrToken("Сума", 90, bbox=(10, 10, 50, 20)),
            OcrToken("за", 90, bbox=(55, 10, 70, 20)),
            OcrToken("плащане:", 90, bbox=(75, 10, 130, 20)),
            OcrToken("200.00", 90, bbox=(135, 10, 180, 20)),
            OcrToken("EOB", 90, bbox=(185, 10, 215, 20)),
        ]
        line_eob = LogicalLine(tokens=tokens_eob)
        self.assertEqual(extract_currency([line_eob], tokens_eob, date_issued="2026-02-01"), "EUR")

        # EUB distortion
        tokens_eub = [
            OcrToken("Total", 90, bbox=(10, 10, 50, 20)),
            OcrToken("45.00", 90, bbox=(55, 10, 95, 20)),
            OcrToken("EUB", 90, bbox=(100, 10, 130, 20)),
        ]
        line_eub = LogicalLine(tokens=tokens_eub)
        self.assertEqual(extract_currency([line_eub], tokens_eub, date_issued="2026-03-15"), "EUR")

        # Cyrillic ЕШ? in words
        tokens_esh = [
            OcrToken("Словом:", 90, bbox=(10, 10, 60, 20)),
            OcrToken("тридесет", 90, bbox=(65, 10, 120, 20)),
            OcrToken("15ЕШ?", 90, bbox=(125, 10, 160, 20)),
        ]
        line_esh = LogicalLine(tokens=tokens_esh)
        self.assertEqual(extract_currency([line_esh], tokens_esh, date_issued="2026-04-07"), "EUR")

        # Glued 'петеврои' and 'е.ц,.' in words
        tokens_glued = [
            OcrToken("Словом:", 90, bbox=(10, 10, 60, 20)),
            OcrToken("Тристаи", 90, bbox=(65, 10, 120, 20)),
            OcrToken("петеврои", 90, bbox=(125, 10, 180, 20)),
            OcrToken("50", 90, bbox=(185, 10, 205, 20)),
            OcrToken("е.ц,.", 90, bbox=(210, 10, 240, 20)),
        ]
        line_glued = LogicalLine(tokens=tokens_glued)
        self.assertEqual(extract_currency([line_glued], tokens_glued, date_issued="2026-04-01"), "EUR")

    def test_statutory_accounting_fallback_art_5_para_1(self):
        """Statutory accounting fallback under Art. 5, Para. 1 of the Bulgarian Accountancy Act:

        When no explicit currency is detected, pre-2026 invoices MUST be BGN,
        and post-2026 invoices MUST be EUR.
        """
        # Standard invoice with only numbers in columns (no currency symbol anywhere)
        tokens_no_curr = [
            OcrToken("ФАКТУРА", 95, bbox=(10, 10, 70, 20)),
            OcrToken("Количество", 90, bbox=(10, 30, 80, 40)),
            OcrToken("Цена", 90, bbox=(85, 30, 120, 40)),
            OcrToken("Стойност", 90, bbox=(125, 30, 170, 40)),
            OcrToken("10", 90, bbox=(10, 50, 30, 60)),
            OcrToken("28.49", 90, bbox=(85, 50, 120, 60)),
            OcrToken("284.97", 90, bbox=(125, 50, 170, 60)),
        ]
        line1 = LogicalLine(tokens=tokens_no_curr[0:1])
        line2 = LogicalLine(tokens=tokens_no_curr[1:4])
        line3 = LogicalLine(tokens=tokens_no_curr[4:7])
        lines = [line1, line2, line3]

        # Prior to 01.01.2026 -> strictly BGN
        self.assertEqual(extract_currency(lines, tokens_no_curr, date_issued="2025-07-05"), "BGN")

        # On or after 01.01.2026 -> strictly EUR
        self.assertEqual(extract_currency(lines, tokens_no_curr, date_issued="2026-04-01"), "EUR")

        # When date is None -> defaults to BGN (historic Bulgarian statutory currency)
        self.assertEqual(extract_currency(lines, tokens_no_curr, date_issued=None), "BGN")

    def test_intermes_dual_currency_words_and_payment_resolution(self):
        """Verify dual-currency invoices (Intermes pattern) resolve to EUR payable without false word warnings."""
        tokens = [
            OcrToken("ФАКТУРА", 95, bbox=(10, 10, 70, 20)),
            OcrToken("Сума", 90, bbox=(10, 30, 40, 40)),
            OcrToken("за", 90, bbox=(45, 30, 60, 40)),
            OcrToken("плащане", 90, bbox=(65, 30, 120, 40)),
            OcrToken("33.32", 90, bbox=(125, 30, 165, 40)),
            OcrToken("Словом:", 90, bbox=(10, 50, 60, 60)),
            OcrToken("тридесет", 90, bbox=(65, 50, 120, 60)),
            OcrToken("и", 90, bbox=(125, 50, 135, 60)),
            OcrToken("три", 90, bbox=(140, 50, 165, 60)),
            OcrToken("32ЕЦ", 90, bbox=(170, 50, 205, 60)),
            OcrToken("Сума", 90, bbox=(210, 50, 240, 60)),
            OcrToken("вОМ:", 90, bbox=(245, 50, 275, 60)),
            OcrToken("65.17", 90, bbox=(280, 50, 320, 60)),
        ]
        line1 = LogicalLine(tokens=tokens[0:1])
        line2 = LogicalLine(tokens=tokens[1:5])
        line3 = LogicalLine(tokens=tokens[5:13])
        lines = [line1, line2, line3]

        curr = extract_currency(lines, tokens, date_issued="2026-04-16")
        self.assertEqual(curr, "EUR")


class TestTargetFourInvoicesResolution(unittest.TestCase):
    """Regression test ensuring the 4 problematic corpus documents have explicit ISO-4217 currencies and 0 UNKNOWN."""

    RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"

    def test_four_problem_documents_currency_resolved(self):
        """All four documents (метро-2, оскари-02, интермес-01, интермес-02) must have non-null currencies."""
        expected = {
            "метро-2.json": "BGN",
            "оскари-02.json": "EUR",
            "интермес-01.json": "EUR",
            "интермес-02.json": "EUR",
        }
        for filename, exp_curr in expected.items():
            path = self.RESULTS_DIR / filename
            if not path.exists():
                continue
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            fin = data.get("financial_summary", {})
            total_curr = fin.get("total_amount_due", {}).get("currency")
            self.assertEqual(
                total_curr,
                exp_curr,
                f"Document {filename} must have currency {exp_curr}, got {total_curr}",
            )

    def test_batch_summary_has_no_unknown_currency(self):
        """batch_summary.json must have 0 documents under UNKNOWN currency."""
        summary_path = self.RESULTS_DIR / "batch_summary.json"
        if not summary_path.exists():
            return
        with open(summary_path, encoding="utf-8") as f:
            summary = json.load(f)
        fin_totals = summary.get("financial_totals_by_currency", {})
        self.assertNotIn("UNKNOWN", fin_totals, "batch_summary.json must not have UNKNOWN currency totals")
        self.assertIn("BGN", fin_totals)
        self.assertIn("EUR", fin_totals)
        total_docs = summary.get("summary", {}).get("total_documents", 0)
        curr_docs = sum(v.get("document_count", 0) for v in fin_totals.values())
        self.assertEqual(total_docs, curr_docs, "All documents in summary must be accounted for by valid ISO currencies")


class TestSemanticDateDisambiguation(unittest.TestCase):
    """Test semantic date separation: date of issue, tax event, and due date."""

    def test_extracted_dates_tuple_and_named_fields(self):
        """extract_dates returns an ExtractedDates tuple with date_issued, date_tax_event, and due_date."""
        tokens = [
            OcrToken("Дата:", 90, bbox=(10, 10, 50, 20)),
            OcrToken("27.07.2026r.", 90, bbox=(55, 10, 130, 20)),
            OcrToken("Срок", 90, bbox=(10, 30, 40, 40)),
            OcrToken("за", 90, bbox=(45, 30, 60, 40)),
            OcrToken("плащане:", 90, bbox=(65, 30, 120, 40)),
            OcrToken("28.07.2026г.", 90, bbox=(125, 30, 200, 40)),
        ]
        line1 = LogicalLine(tokens=tokens[0:2])
        line2 = LogicalLine(tokens=tokens[2:6])
        res = extract_dates([line1, line2])
        # Can unpack as 2-tuple for backward compatibility
        d_issued, d_tax = res
        self.assertEqual(d_issued, "2026-07-27")
        # In absence of separate tax event, statutory default is date_issued
        self.assertEqual(d_tax, "2026-07-27")
        self.assertEqual(res.due_date, "2026-07-28")

    def test_extract_due_date_patterns(self):
        """extract_due_date detects 'падеж', 'срок за плащане', 'due date'."""
        lines = [
            LogicalLine(tokens=[OcrToken("Падеж:", 90, bbox=(10, 10, 50, 20)), OcrToken("15.09.2026", 90, bbox=(55, 10, 120, 20))]),
            LogicalLine(tokens=[OcrToken("Срок за плащане до", 90, bbox=(10, 30, 120, 40)), OcrToken("20.10.2026", 90, bbox=(125, 30, 180, 40))]),
        ]
        self.assertEqual(extract_due_date([lines[0]]), "2026-09-15")
        self.assertEqual(extract_due_date([lines[1]]), "2026-10-20")

    def test_due_date_in_future_does_not_trigger_future_date_error(self):
        """Payment due dates legitimately extend into the future and must NOT emit FUTURE_DATE."""
        inv = Invoice()
        inv.invoice_metadata.date_issued = "2026-08-01"
        inv.invoice_metadata.date_tax_event = "2026-08-01"
        inv.invoice_metadata.due_date = "2026-10-31"  # Legitimate payment term in the future
        inv.payment_details.due_date = "2026-10-31"

        issues = _validate_dates(inv)
        future_issues = [i for i in issues if i.code == "FUTURE_DATE"]
        self.assertEqual(future_issues, [], "due_date in the future must not trigger FUTURE_DATE")

    def test_due_date_invalid_calendar_triggers_error(self):
        """Due date with invalid calendar date format must still trigger INVALID_DATE."""
        inv = Invoice()
        inv.invoice_metadata.date_issued = "2026-08-01"
        inv.invoice_metadata.due_date = "2026-02-30"

        issues = _validate_dates(inv)
        invalid_issues = [i for i in issues if i.code == "INVALID_DATE" and i.field == "invoice_metadata.due_date"]
        self.assertEqual(len(invalid_issues), 1)

    def test_shelf_life_and_batch_dates_excluded_from_document_dates(self):
        """Table line item shelf lives (срок на годност) and batches (партида) must not be extracted as invoice dates."""
        lines = [
            LogicalLine(tokens=[OcrToken("ФАКТУРА", 95, bbox=(10, 10, 70, 20)), OcrToken("№", 95, bbox=(75, 10, 85, 20)), OcrToken("100", 95, bbox=(90, 10, 120, 20))]),
            LogicalLine(tokens=[OcrToken("Дата:", 90, bbox=(10, 30, 50, 40)), OcrToken("08.07.2026г.", 90, bbox=(55, 30, 130, 40))]),
            # Grocery item line with expiration date
            LogicalLine(tokens=[
                OcrToken("132004", 90, bbox=(10, 50, 50, 60)),
                OcrToken("МОНАРХ", 90, bbox=(55, 50, 100, 60)),
                OcrToken("СЕКТОРНО", 90, bbox=(105, 50, 160, 60)),
                OcrToken("ТОПЕНО", 90, bbox=(165, 50, 210, 60)),
                OcrToken("20.10.2026", 90, bbox=(215, 50, 280, 60)),
            ]),
            # Line with explicit batch / expiration label
            LogicalLine(tokens=[
                OcrToken("Партида:", 90, bbox=(10, 70, 60, 80)),
                OcrToken("P100", 90, bbox=(65, 70, 95, 80)),
                OcrToken("срок", 90, bbox=(100, 70, 130, 80)),
                OcrToken("на", 90, bbox=(135, 70, 150, 80)),
                OcrToken("годност:", 90, bbox=(155, 70, 210, 80)),
                OcrToken("16.09.2026", 90, bbox=(215, 70, 280, 80)),
            ]),
        ]
        d_issued, d_tax = extract_dates(lines)
        self.assertEqual(d_issued, "2026-07-08")
        # Date of tax event defaults to date_issued, NOT 20.10.2026 or 16.09.2026
        self.assertEqual(d_tax, "2026-07-08")

    def test_delivery_address_line_excluded(self):
        """Delivery address lines with dates or numbers must not pollute date extraction."""
        lines = [
            LogicalLine(tokens=[OcrToken("Дата:", 90, bbox=(10, 10, 50, 20)), OcrToken("15.06.2026", 90, bbox=(55, 10, 120, 20))]),
            LogicalLine(tokens=[
                OcrToken("Адрес", 90, bbox=(10, 30, 50, 40)),
                OcrToken("на", 90, bbox=(55, 30, 70, 40)),
                OcrToken("доставка:", 90, bbox=(75, 30, 130, 40)),
                OcrToken("ул.", 90, bbox=(135, 30, 155, 40)),
                OcrToken("15.08.2026", 90, bbox=(160, 30, 220, 40)),
                OcrToken("тел.", 90, bbox=(225, 30, 250, 40)),
                OcrToken("0889", 90, bbox=(255, 30, 285, 40)),
                OcrToken("809", 90, bbox=(290, 30, 315, 40)),
                OcrToken("107", 90, bbox=(320, 30, 345, 40)),
            ]),
        ]
        d_issued, d_tax = extract_dates(lines)
        self.assertEqual(d_issued, "2026-06-15")
        self.assertEqual(d_tax, "2026-06-15")


class TestDualCurrencyParityAndCalculation(unittest.TestCase):
    """Test statutory dual-currency calculations, parity verification, and rounding under Bulgarian Euro transition law."""

    def test_statutory_fixed_rate(self):
        """Fixed exchange rate must be exactly 1.95583."""
        self.assertEqual(FIXED_EUR_BGN_RATE, Decimal("1.95583"))

    def test_convert_eur_to_bgn_statutory_rounding(self):
        """EUR to BGN conversion applies round(amount * 1.95583, 2) using ROUND_HALF_UP."""
        # 100 EUR = 195.583 -> 195.58 BGN
        self.assertEqual(convert_eur_to_bgn(Decimal("100.00")), Decimal("195.58"))
        # 120 EUR = 234.6996 -> 234.70 BGN
        self.assertEqual(convert_eur_to_bgn(Decimal("120.00")), Decimal("234.70"))
        # Cascade 58.pdf: 190.42 EUR * 1.95583 = 372.429148 -> 372.43 BGN
        self.assertEqual(convert_eur_to_bgn(Decimal("190.42")), Decimal("372.43"))
        # Cascade 60.pdf: 70.73 EUR * 1.95583 = 138.335857 -> 138.34 BGN
        self.assertEqual(convert_eur_to_bgn(Decimal("70.73")), Decimal("138.34"))

    def test_convert_bgn_to_eur_statutory_rounding(self):
        """BGN to EUR conversion applies round(amount / 1.95583, 2) using ROUND_HALF_UP."""
        self.assertEqual(convert_bgn_to_eur(Decimal("195.58")), Decimal("100.00"))
        self.assertEqual(convert_bgn_to_eur(Decimal("372.43")), Decimal("190.42"))
        self.assertEqual(convert_bgn_to_eur(Decimal("138.34")), Decimal("70.73"))

    def test_verify_dual_currency_parity_valid(self):
        """Parity check passes when BGN and EUR values are consistent within 0.02 tolerance."""
        self.assertTrue(verify_dual_currency_parity(Decimal("190.42"), Decimal("372.43")))
        self.assertTrue(verify_dual_currency_parity(Decimal("70.73"), Decimal("138.34")))
        # Difference within 0.02 tolerance
        self.assertTrue(verify_dual_currency_parity(Decimal("100.00"), Decimal("195.60")))

    def test_verify_dual_currency_parity_invalid(self):
        """Parity check fails when difference exceeds 0.02 tolerance."""
        self.assertFalse(verify_dual_currency_parity(Decimal("100.00"), Decimal("200.00")))
        self.assertFalse(verify_dual_currency_parity(Decimal("190.42"), Decimal("360.00")))

    def test_validate_currency_emits_parity_mismatch(self):
        """_validate_currency emits DUAL_CURRENCY_PARITY_MISMATCH warning on inconsistent dual totals."""
        inv = Invoice()
        inv.financial_summary = FinancialSummary(
            total_amount_due=MoneyAmount(Decimal("100.00"), "EUR"),
            total_amount_bgn=MoneyAmount(Decimal("250.00"), "BGN"),  # Mismatched! Expected ~195.58
        )
        issues = _validate_currency(inv)
        parity_issues = [i for i in issues if i.code == "DUAL_CURRENCY_PARITY_MISMATCH"]
        self.assertEqual(len(parity_issues), 1)
        self.assertEqual(parity_issues[0].severity, "warning")
        self.assertIn("195.58", parity_issues[0].message)

    def test_validate_currency_passes_on_correct_parity(self):
        """_validate_currency emits no parity warning on correct dual totals."""
        inv = Invoice()
        inv.financial_summary = FinancialSummary(
            total_amount_due=MoneyAmount(Decimal("190.42"), "EUR"),
            total_amount_bgn=MoneyAmount(Decimal("372.43"), "BGN"),
        )
        issues = _validate_currency(inv)
        parity_issues = [i for i in issues if i.code == "DUAL_CURRENCY_PARITY_MISMATCH"]
        self.assertEqual(parity_issues, [])


class TestCascade58And60RealInvoices(unittest.TestCase):
    """End-to-end acceptance tests on 58.pdf and 60.pdf from 00_РМ_КАСКАДА_2026_ЕООД.

    Verifies:
    1. Zero false FUTURE_DATE alarms.
    2. Zero validation errors.
    3. Correct semantic dates (date_issued, date_tax_event, due_date).
    4. Dual currency EUR/BGN extraction and parity reconciliation.
    """

    CASCADE_DIR = Path("/Volumes/NO NAME/_ФАКТУРИ/00_РМ_КАСКАДА_2026_ЕООД")

    def setUp(self):
        if not self.CASCADE_DIR.exists():
            self.skipTest(f"Cascade directory not accessible: {self.CASCADE_DIR}")

    def test_cascade_58_pdf_no_future_date_and_valid_dual_currency(self):
        """58.pdf must process with 0 errors, correct July 2026 dates, and valid EUR/BGN totals."""
        file_path = self.CASCADE_DIR / "58.pdf"
        if not file_path.exists():
            self.skipTest(f"File not found: {file_path}")

        inv = process_invoice(file_path)

        # 1. Date checks
        self.assertEqual(inv.invoice_metadata.date_issued, "2026-07-27")
        self.assertEqual(inv.invoice_metadata.date_tax_event, "2026-07-27")
        self.assertEqual(inv.invoice_metadata.due_date, "2026-07-28")
        self.assertEqual(inv.payment_details.due_date, "2026-07-28")

        # 2. Currency checks
        fin = inv.financial_summary
        self.assertIsNotNone(fin.total_amount_due)
        self.assertEqual(fin.total_amount_due.currency, "EUR")
        self.assertEqual(fin.total_amount_due.amount, Decimal("190.42"))
        self.assertIsNotNone(fin.total_amount_bgn)
        self.assertEqual(fin.total_amount_bgn.amount, Decimal("372.43"))

        # 3. Validation checks: 0 errors, zero FUTURE_DATE issues
        error_codes = [e.code for e in inv.validation.errors]
        self.assertNotIn("FUTURE_DATE", error_codes)
        self.assertEqual(inv.validation.errors, [])

    def test_cascade_60_pdf_no_future_date_and_valid_dual_currency(self):
        """60.pdf must process with 0 errors, correct July 2026 dates, and valid EUR/BGN totals."""
        file_path = self.CASCADE_DIR / "60.pdf"
        if not file_path.exists():
            self.skipTest(f"File not found: {file_path}")

        inv = process_invoice(file_path)

        # 1. Date checks
        self.assertEqual(inv.invoice_metadata.date_issued, "2026-07-08")
        self.assertEqual(inv.invoice_metadata.date_tax_event, "2026-07-08")
        self.assertEqual(inv.invoice_metadata.due_date, "2026-07-09")
        self.assertEqual(inv.payment_details.due_date, "2026-07-09")

        # 2. Currency checks
        fin = inv.financial_summary
        self.assertIsNotNone(fin.total_amount_due)
        self.assertEqual(fin.total_amount_due.currency, "EUR")
        self.assertEqual(fin.total_amount_due.amount, Decimal("70.73"))
        self.assertIsNotNone(fin.total_amount_bgn)
        self.assertEqual(fin.total_amount_bgn.amount, Decimal("138.34"))

        # 3. Validation checks: 0 errors, zero FUTURE_DATE issues
        error_codes = [e.code for e in inv.validation.errors]
        self.assertNotIn("FUTURE_DATE", error_codes)
        self.assertEqual(inv.validation.errors, [])


if __name__ == "__main__":
    unittest.main()




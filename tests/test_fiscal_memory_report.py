"""Unit tests for Fiscal Memory Reports (Z-reports / monthly turnover under Наредба Н-18).

Statutory requirements:
- DocumentClassifier recognizes fiscal memory report titles and abbreviations
- Device serial number, fiscal memory number (ФП №), report scope, period, and tax groups extracted
- Taxpayer entity identified from fiscal header
- Math reconciliation: tax_base + vat == turnover
- Accounting export: NAP document code 81 in PRODAGBI.TXT, double entry Debit 501 / Credit 702 & 4532
- Excluded from purchase ledger (POKUPKI.TXT)
"""

from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

import invoice_ocr as iocr
from invoice_ocr import (
    DocumentType,
    DocumentClassifier,
    FiscalMemoryReportDetails,
    MoneyAmount,
    OcrToken,
    LogicalLine,
    extract_fiscal_memory_report,
    extract_taxpayer_from_fiscal_report,
    validate_fiscal_memory_report,
    Invoice,
    InvoiceMetadata,
    Party,
    FinancialSummary,
    ValidationResult,
)
import accounting_export as aexp


class TestFiscalMemoryReportClassification:
    """Test classification of fiscal memory reports."""

    def test_classify_standard_fiscal_report_title(self):
        tokens = [
            OcrToken(text="ОТЧЕТ", conf=0.95, bbox=(100, 50, 100, 30)),
            OcrToken(text="НА", conf=0.95, bbox=(210, 50, 40, 30)),
            OcrToken(text="ФИСКАЛНА", conf=0.95, bbox=(260, 50, 150, 30)),
            OcrToken(text="ПАМЕТ", conf=0.95, bbox=(420, 50, 100, 30)),
        ]
        res = DocumentClassifier.classify(tokens)
        assert res.document_type == DocumentType.FISCAL_MEMORY_REPORT
        assert res.matched_rule == "fiscal_memory_report_title"

    def test_classify_dotmatrix_homoglyph_title(self):
        tokens = [
            OcrToken(text="CCBKPATEH", conf=0.90, bbox=(100, 50, 150, 30)),
            OcrToken(text="ОТЧЕТ", conf=0.90, bbox=(260, 50, 100, 30)),
            OcrToken(text="ФИСКАЙНА", conf=0.88, bbox=(100, 90, 150, 30)),
            OcrToken(text="ПАМЕТ", conf=0.90, bbox=(260, 90, 100, 30)),
        ]
        res = DocumentClassifier.classify(tokens)
        assert res.document_type == DocumentType.FISCAL_MEMORY_REPORT

    def test_classify_daily_z_report_title(self):
        tokens = [
            OcrToken(text="ДНЕВЕН", conf=0.92, bbox=(100, 50, 100, 30)),
            OcrToken(text="ФИНАНСОВ", conf=0.92, bbox=(210, 50, 120, 30)),
            OcrToken(text="ОТЧЕТ", conf=0.92, bbox=(340, 50, 90, 30)),
            OcrToken(text="Z-ОТЧЕТ", conf=0.90, bbox=(440, 50, 100, 30)),
        ]
        res = DocumentClassifier.classify(tokens)
        assert res.document_type == DocumentType.FISCAL_MEMORY_REPORT


class TestFiscalMemoryReportExtraction:
    """Test extraction of device parameters and turnover numbers."""

    def test_extract_report_fields_from_lines(self):
        lines_text = [
            "РМ КАСКАДА 2026 ЕООД",
            "ЕИК: 208380135",
            "ИН по ДДС: BG208380135",
            "гр. Плевен, ул. Гривишко шосе 1",
            "СЪКРАТЕН ОТЧЕТ НА ФИСКАЛНА ПАМЕТ",
            "ОТ ДАТА: 01-08-2026 ДО ДАТА: 31-08-2026",
            "ИНДИВИДУАЛЕН № НА ФП: 79034158",
            "НОМЕР НА ИНДИВИДУАЛЕН ДИСК / УСТРОЙСТВО: 1189252",
            "ДАНЪЧНА ГРУПА Б 20.00 %",
            "ОБЩ ОБОРОТ ГРУПА Б: 91175.95",
            "ДАНЪЧНА ОСНОВА: 75979.92",
            "НАЧИСЛЕН ДДС 20%: 15196.03",
            "ОБЩ ОБОРОТ: 91175.95 BGN",
        ]
        tokens = []
        lines = []
        y = 100
        for lt in lines_text:
            lt_tokens = []
            x = 50
            for w in lt.split():
                tok = OcrToken(text=w, conf=0.95, bbox=(x, y, len(w) * 15, 25))
                lt_tokens.append(tok)
                tokens.append(tok)
                x += len(w) * 15 + 10
            lines.append(LogicalLine(tokens=lt_tokens, bbox=(50, y, x - 50, 25), page_number=1))
            y += 35

        fisc = extract_fiscal_memory_report(lines, tokens)
        assert fisc is not None
        assert fisc.fiscal_memory_number == "79034158"
        assert fisc.device_number == "1189252"
        assert fisc.report_scope in ("MONTHLY", "PERIODIC")
        assert fisc.period_from == "2026-08-01"
        assert fisc.period_to == "2026-08-31"
        assert fisc.turnover_total.amount == Decimal("91175.95")
        assert fisc.tax_base_total.amount == Decimal("75979.92")
        assert fisc.vat_total.amount == Decimal("15196.03")

        name, eik, vat_num, address = extract_taxpayer_from_fiscal_report(lines, tokens)
        assert name == "РМ КАСКАДА 2026 ЕООД"
        assert eik == "208380135"

    def test_validate_fiscal_memory_report_math(self):
        fisc = FiscalMemoryReportDetails(
            device_number="1189252",
            fiscal_memory_number="79034158",
            period_from="2026-08-01",
            period_to="2026-08-31",
            turnover_total=Decimal("91175.95"),
            tax_base_total=Decimal("75979.92"),
            vat_total=Decimal("15196.03"),
        )
        taxpayer = Party(name="РМ КАСКАДА 2026 ЕООД", eik="208380135")
        inv = Invoice(
            invoice_metadata=InvoiceMetadata(document_type=DocumentType.FISCAL_MEMORY_REPORT.value),
            supplier=taxpayer,
            recipient=Party(),
            financial_summary=FinancialSummary(
                total_amount_due=MoneyAmount(amount=Decimal("91175.95"), currency="BGN"),
                tax_base=MoneyAmount(amount=Decimal("75979.92"), currency="BGN"),
                vat_amount=MoneyAmount(amount=Decimal("15196.03"), currency="BGN"),
            ),
            line_items=[],
            validation=ValidationResult(is_valid=True, errors=[], warnings=[]),
            raw_ocr_evidence={},
            fiscal_report=fisc,
        )
        res = validate_fiscal_memory_report(inv, [])
        assert res.is_valid is True
        assert len(res.errors) == 0


class TestFiscalMemoryReportAccountingExport:
    """Test ERP and statutory accounting export of Fiscal Memory Reports."""

    @pytest.fixture
    def sample_fiscal_invoice(self):
        fisc = FiscalMemoryReportDetails(
            device_number="1189252",
            fiscal_memory_number="79034158",
            period_from="2026-08-01",
            period_to="2026-08-31",
            turnover_total=Decimal("91175.95"),
            tax_base_total=Decimal("75979.92"),
            vat_total=Decimal("15196.03"),
        )
        meta = InvoiceMetadata(
            document_type=DocumentType.FISCAL_MEMORY_REPORT.value,
            invoice_number="79034158",
            date_issued="2026-08-31",
            date_tax_event="2026-08-31",
        )
        taxpayer = Party(name="РМ КАСКАДА 2026 ЕООД", eik="208380135")
        fin = FinancialSummary(
            total_amount_due=MoneyAmount(amount=Decimal("91175.95"), currency="BGN"),
            tax_base=MoneyAmount(amount=Decimal("75979.92"), currency="BGN"),
            vat_amount=MoneyAmount(amount=Decimal("15196.03"), currency="BGN"),
        )
        return Invoice(
            invoice_metadata=meta,
            supplier=taxpayer,
            recipient=Party(name="ФИЗИЧЕСКИ ЛИЦА", eik="999999999999999"),
            financial_summary=fin,
            line_items=[],
            validation=ValidationResult(is_valid=True, errors=[], warnings=[]),
            raw_ocr_evidence={},
            fiscal_report=fisc,
        )

    def test_nap_sales_ledger_entry_code_81(self, sample_fiscal_invoice):
        sales_entry = aexp.invoice_to_nap_sales_entry(sample_fiscal_invoice)
        assert sales_entry.doc_type == "81"
        assert sales_entry.doc_number == "0079034158"
        assert sales_entry.contractor_id == "999999999999999"
        assert sales_entry.total_amount == Decimal("91175.95")
        assert sales_entry.tax_base_20 == Decimal("75979.92")
        assert sales_entry.vat_20 == Decimal("15196.03")

    def test_double_entry_bookkeeping_retail_revenue(self, sample_fiscal_invoice):
        # Debit 501 (Каса) / Credit 702 (Приходи) and 4532 (Начислен ДДС)
        je = aexp.invoice_to_journal_entry(sample_fiscal_invoice)
        assert je.doc_type == "Отчет от фискална памет"
        assert je.total_amount == Decimal("91175.95")
        assert je.is_balanced is True
        assert len(je.records) == 2
        r1, r2 = je.records
        assert r1.debit_account == "501"
        assert r1.credit_account == "702"
        assert r1.amount == Decimal("75979.92")
        assert r2.debit_account == "501"
        assert r2.credit_account == "4532"
        assert r2.amount == Decimal("15196.03")

    def test_pokupki_txt_safely_excludes_fiscal_report(self, sample_fiscal_invoice):
        # Fiscal reports must not appear in purchase register (POKUPKI.TXT)
        with pytest.raises(ValueError, match="Отчетите от фискална памет са документи за продажби"):
            aexp.invoice_to_nap_entry(sample_fiscal_invoice)

        # Batch export cleanly filters out fiscal reports without crashing
        txt = aexp.invoices_to_pokupki_txt([sample_fiscal_invoice], encoding=None)
        assert txt.strip() == ""

"""Unit tests for Bulgarian Goods Receipts (Стокови разписки).

Covers document classification, specialized field extraction (receipt number,
delivered/received by, line items), validation rules, and ERP/statutory
accounting export (Debit 304 / Credit 401, exclusion from VAT ledgers).
"""

from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

from invoice_ocr import (
    DocumentType,
    FinancialSummary,
    GoodsReceiptDetails,
    Invoice,
    InvoiceMetadata,
    LineItem,
    LogicalLine,
    MoneyAmount,
    OcrToken,
    Party,
    ValidationResult,
    DocumentClassifier,
    extract_goods_receipt,
    extract_goods_receipt_line_items,
    extract_parties_for_goods_receipt,
    validate_goods_receipt,
)
from accounting_export import (
    invoice_to_journal_entry,
    invoices_to_pokupki_txt,
    invoices_to_prodagbi_txt,
)


class TestGoodsReceiptClassification:
    """Test classification of Goods Receipts vs Invoices vs other docs."""

    def test_classify_stock_receipt_header(self):
        tokens = [
            OcrToken(text="СТОКОВА", conf=0.95, bbox=(100, 50, 100, 20)),
            OcrToken(text="РАЗПИСКА", conf=0.95, bbox=(210, 50, 120, 20)),
            OcrToken(text="№", conf=0.95, bbox=(340, 50, 20, 20)),
            OcrToken(text="0000357071", conf=0.95, bbox=(370, 50, 120, 20)),
            OcrToken(text="ПРЕДАЛ:", conf=0.95, bbox=(100, 500, 80, 20)),
            OcrToken(text="ПРИЕЛ:", conf=0.95, bbox=(300, 500, 80, 20)),
        ]
        res = DocumentClassifier.classify(tokens)
        assert res.document_type == DocumentType.GOODS_RECEIPT

    def test_invoice_takes_precedence_when_invoice_keyword_present(self):
        tokens = [
            OcrToken(text="ФАКТУРА", conf=0.95, bbox=(100, 50, 100, 20)),
            OcrToken(text="ОРИГИНАЛ", conf=0.95, bbox=(210, 50, 100, 20)),
            OcrToken(text="КЪМ", conf=0.95, bbox=(100, 80, 50, 20)),
            OcrToken(text="СТОКОВА", conf=0.95, bbox=(160, 80, 80, 20)),
            OcrToken(text="РАЗПИСКА", conf=0.95, bbox=(250, 80, 90, 20)),
        ]
        res = DocumentClassifier.classify(tokens)
        assert res.document_type == DocumentType.INVOICE


class TestGoodsReceiptExtraction:
    """Test specialized field extraction from Goods Receipts."""

    def test_extract_receipt_number_and_date(self):
        lines_text = [
            "ЕТ ПЕТРОВ 1974 - СТОЯН ПЕТРОВ",
            "СТОКОВА РАЗПИСКА № 0000357071",
            "Дата: 03.08.2026",
            "Получател: РМ КАСКАДА 2026 ЕООД",
            "Сума за плащане: 56.84 BGN",
            "Предал: Иван Петров",
            "Получил: Марин Маринов",
        ]
        tokens = []
        lines = []
        y = 50
        for lt in lines_text:
            lt_tokens = []
            x = 50
            for w in lt.split():
                tok = OcrToken(text=w, conf=0.95, bbox=(x, y, len(w) * 15, 20))
                lt_tokens.append(tok)
                tokens.append(tok)
                x += len(w) * 15 + 10
            lines.append(LogicalLine(tokens=lt_tokens, bbox=(50, y, x - 50, 20), page_number=1))
            y += 30

        gr = extract_goods_receipt(lines, tokens)
        assert gr.receipt_number == "0000357071"
        assert gr.receipt_date == "2026-08-03"
        assert gr.total_amount.amount == Decimal("56.84")
        assert "Иван Петров" in gr.delivered_by
        assert "Марин Маринов" in gr.received_by

    def test_extract_line_items(self):
        lines_text = [
            "1. Захар кристал 1кг x 10 бр = 22.50",
            "2. Олио Калиакра 1л x 6 бр = 19.80",
            "3. Брашно тип 500 x 5 кг = 14.54",
        ]
        tokens = []
        lines = []
        for lt in lines_text:
            lt_tokens = [OcrToken(text=w, conf=0.95, bbox=(0, 0, 10, 10)) for w in lt.split()]
            tokens.extend(lt_tokens)
            lines.append(LogicalLine(tokens=lt_tokens, bbox=(0, 0, 10, 10), page_number=1))

        items = extract_goods_receipt_line_items(lines, tokens)
        assert len(items) == 3
        assert items[0].quantity == Decimal("10")
        assert items[0].total_price_net.amount == Decimal("22.50")
        assert items[1].quantity == Decimal("6")
        assert items[1].total_price_net.amount == Decimal("19.80")

    def test_extract_parties(self):
        lines_text = [
            "Доставчик: ЕТ ПЕТРОВ 1974 - СТОЯН ПЕТРОВ",
            "ЕИК: 104555888",
            "Клиент: РМ КАСКАДА 2026 ЕООД",
            "ЕИК: 208380135",
        ]
        lines = [
            LogicalLine(tokens=[OcrToken(text=w, conf=0.95, bbox=(0, 0, 10, 10)) for w in lt.split()],
                        bbox=(0, 0, 10, 10), page_number=1)
            for lt in lines_text
        ]
        sup, rec = extract_parties_for_goods_receipt(lines, [])
        assert "ПЕТРОВ" in sup.name
        assert "КАСКАДА" in rec.name
        assert rec.eik == "208380135"


class TestGoodsReceiptValidation:
    """Test validation behavior of Goods Receipts."""

    def test_validate_goods_receipt_valid(self):
        gr = GoodsReceiptDetails(
            receipt_number="0000357071",
            receipt_date="2026-08-03",
            total_amount=MoneyAmount(Decimal("56.84"), "BGN"),
            delivered_by="Иван Петров",
            received_by="Марин Маринов",
        )
        inv = Invoice(
            invoice_metadata=InvoiceMetadata(document_type=DocumentType.GOODS_RECEIPT.value),
            supplier=Party(name="ЕТ ПЕТРОВ 1974"),
            recipient=Party(name="РМ КАСКАДА 2026 ЕООД", eik="208380135"),
            financial_summary=FinancialSummary(
                total_amount_due=MoneyAmount(Decimal("56.84"), "BGN"),
            ),
            line_items=[],
            validation=ValidationResult(is_valid=True, errors=[], warnings=[]),
            raw_ocr_evidence={},
            goods_receipt=gr,
        )
        res = validate_goods_receipt(inv, [])
        assert res.is_valid is True
        assert len(res.errors) == 0

    def test_validate_goods_receipt_missing_number_gives_warning_not_error(self):
        gr = GoodsReceiptDetails(
            receipt_number=None,
            total_amount=MoneyAmount(Decimal("56.84"), "BGN"),
        )
        inv = Invoice(
            invoice_metadata=InvoiceMetadata(document_type=DocumentType.GOODS_RECEIPT.value),
            supplier=Party(name="ЕТ ПЕТРОВ 1974"),
            recipient=Party(name="РМ КАСКАДА 2026 ЕООД", eik="208380135"),
            financial_summary=FinancialSummary(),
            line_items=[],
            validation=ValidationResult(is_valid=True, errors=[], warnings=[]),
            raw_ocr_evidence={},
            goods_receipt=gr,
        )
        res = validate_goods_receipt(inv, [])
        assert res.is_valid is True
        assert any(w.code == "MISSING_RECEIPT_NUMBER" for w in res.warnings)


class TestGoodsReceiptAccountingExport:
    """Test ERP double-entry and VAT exclusion for Goods Receipts."""

    @pytest.fixture
    def sample_receipt_invoice(self):
        gr = GoodsReceiptDetails(
            receipt_number="0000357071",
            receipt_date="2026-08-03",
            total_amount=MoneyAmount(Decimal("56.84"), "BGN"),
            delivered_by="Иван Петров",
            received_by="Марин Маринов",
        )
        return Invoice(
            invoice_metadata=InvoiceMetadata(
                invoice_number="0000357071",
                date_issued="2026-08-03",
                document_type=DocumentType.GOODS_RECEIPT.value,
            ),
            supplier=Party(name="ЕТ ПЕТРОВ 1974", eik="104555888"),
            recipient=Party(name="РМ КАСКАДА 2026 ЕООД", eik="208380135"),
            financial_summary=FinancialSummary(
                total_amount_due=MoneyAmount(Decimal("56.84"), "BGN"),
            ),
            line_items=[],
            validation=ValidationResult(is_valid=True, errors=[], warnings=[]),
            raw_ocr_evidence={},
            goods_receipt=gr,
        )

    def test_goods_receipt_journal_entry(self, sample_receipt_invoice):
        entry = invoice_to_journal_entry(sample_receipt_invoice)
        assert entry.doc_type in (DocumentType.GOODS_RECEIPT.value, "Стокова разписка")
        assert entry.total_amount == Decimal("56.84")

        # Warehouse receipt: Debit 304 (Стоки) / Credit 401 (Доставчици)
        assert entry.expense_account == "304"
        assert entry.payable_account == "401"
        assert entry.vat_amount == Decimal("0.00")

        debit_records = [r for r in entry.records if r.debit_account == "304"]
        credit_records = [r for r in entry.records if r.credit_account == "401"]

        assert len(debit_records) == 1
        assert debit_records[0].amount == Decimal("56.84")
        assert len(credit_records) == 1
        assert credit_records[0].amount == Decimal("56.84")

    def test_goods_receipt_excluded_from_nap_vat_ledgers(self, sample_receipt_invoice):
        pokupki_txt = invoices_to_pokupki_txt([sample_receipt_invoice], period="202608")
        prodagbi_txt = invoices_to_prodagbi_txt([sample_receipt_invoice], period="202608")

        # Non-tax warehouse document must NOT be reported to NAP in POKUPKI or PRODAGBI
        assert b"0000357071" not in pokupki_txt
        assert b"0000357071" not in prodagbi_txt

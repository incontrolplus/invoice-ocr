import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
"""Tests for DocumentClassifier, Budget Payment Orders, and Credit Notes (Pillar 1)."""

import os
from decimal import Decimal
from pathlib import Path
import pytest

from invoice_ocr import (
    DocumentType,
    DocumentClassifier,
    classify_document,
    DocumentClassificationResult,
    BudgetPaymentDetails,
    extract_budget_payment_order,
    validate_budget_payment_order,
    extract_credit_debit_note_reference,
    process_invoice,
    serialize_invoice,
    validate_invoice,
    Invoice,
    InvoiceMetadata,
    Party,
    LineItem,
    FinancialSummary,
    MoneyAmount,
    PaymentDetails,
    OcrToken,
    LogicalLine,
    normalize_ocr_tokens,
    group_tokens_into_lines,
    synthesize_service_line_item,
    format_batch_console_report,
    process_batch,
    parse_money,
)


# ===========================================================================
# 1. DOCUMENT CLASSIFIER TESTS
# ===========================================================================

def test_document_classifier_invoice():
    text = "ОРИГИНАЛ ФАКТУРА № 1000044073 Дата: 12.05.2026"
    res = DocumentClassifier.classify(text)
    assert res.document_type == DocumentType.INVOICE
    assert res.confidence >= 0.90
    assert "invoice_title" in res.matched_rule


def test_document_classifier_credit_note():
    text = "КРЕДИТНО ИЗВЕСТИЕ № 0000000123 Дата: 15.06.2026 Към фактура № 1000012345"
    res = DocumentClassifier.classify(text)
    assert res.document_type == DocumentType.CREDIT_NOTE
    assert res.confidence >= 0.90
    assert "credit_note" in res.matched_rule


def test_document_classifier_debit_note():
    text = "ДЕБИТНО ИЗВЕСТИЕ № 0000000045 Дата: 20.06.2026 Към фактура № 1000012345"
    res = DocumentClassifier.classify(text)
    assert res.document_type == DocumentType.DEBIT_NOTE
    assert res.confidence >= 0.90
    assert "debit_note" in res.matched_rule


def test_document_classifier_procredit_bank_not_credit_note():
    text = "ПроКредит Банк (България) ЕАД ФАКТУРА № 2208418424 Дата: 01.07.2026"
    res = DocumentClassifier.classify(text)
    assert res.document_type == DocumentType.INVOICE
    assert res.document_type != DocumentType.CREDIT_NOTE


def test_document_classifier_unicredit_bank_not_credit_note():
    text = "УниКредит Булбанк АД ФАКТУРА № 1000044073 Дата: 05.07.2026"
    res = DocumentClassifier.classify(text)
    assert res.document_type == DocumentType.INVOICE
    assert res.document_type != DocumentType.CREDIT_NOTE


def test_document_classifier_debit_card_not_debit_note():
    text = "ФАКТУРА № 1000099999 Плащане с дебитна карта Дата: 10.07.2026"
    res = DocumentClassifier.classify(text)
    assert res.document_type == DocumentType.INVOICE
    assert res.document_type != DocumentType.DEBIT_NOTE


def test_document_classifier_payment_order_nap():
    text = "ДЪЛЖИМИ ВНОСКИ КЪМ НАП Задължено лице: Фирма ООД ЕИК 123456789"
    res = DocumentClassifier.classify(text)
    assert res.document_type == DocumentType.PAYMENT_ORDER_NAP
    assert res.confidence >= 0.95


def test_document_classifier_budget_payment_order():
    text = "Бюджетно платежно нареждане за плащане към бюджета НАП ДОО параграф 112001"
    res = DocumentClassifier.classify(text)
    assert res.document_type == DocumentType.PAYMENT_ORDER_NAP
    assert res.confidence >= 0.95


def test_document_classifier_protocol_art_117():
    text = "ПРОТОКОЛ по чл. 117 от ЗДДС № 0000000010 Дата: 05.05.2026"
    res = DocumentClassifier.classify(text)
    assert res.document_type == DocumentType.PROTOCOL_CHL_117
    assert res.confidence >= 0.95


def test_document_classifier_pure_fiscal_receipt():
    text = "ФИСКАЛЕН БОН ЕИК 203012369 ДАТА: 01.07.2026 ТОТАЛ: 45.20 БРОЙ ФИСКАЛНА ПАМЕТ"
    res = DocumentClassifier.classify(text)
    assert res.document_type == DocumentType.FISCAL_RECEIPT


def test_document_classifier_invoice_with_attached_receipt():
    text = "ФАКТУРА № 1000012345 Доставчик ООД ... ПРИКАЧЕН ФИСКАЛЕН БОН"
    res = DocumentClassifier.classify(text)
    assert res.document_type == DocumentType.INVOICE


def test_document_classifier_first_50_tokens():
    tokens = ["текст"] * 10 + ["кредитно", "известие", "№", "0000000123"] + ["текст"] * 30
    res = DocumentClassifier.classify(tokens, token_limit=50)
    assert res.document_type == DocumentType.CREDIT_NOTE


# ===========================================================================
# 2. CREDIT NOTE REFERENCE EXTRACTION & VALIDATION TESTS
# ===========================================================================

def test_extract_credit_note_reference_combined():
    lines = [
        LogicalLine(text="Към документ: ЗПТ 1003123108/2026-07-08; Документ Общо: 70.73", page_number=1, bbox=(0,0,0,0), y_center=0, tokens=[]),
    ]
    ref_num, ref_date, ref_reason = extract_credit_debit_note_reference(lines, [])
    assert ref_num == "1003123108"
    assert ref_date == "2026-07-08"
    assert ref_reason is None


def test_extract_credit_note_reference_with_reason():
    lines = [
        LogicalLine(text="КРЕДИТНО ИЗВЕСТИЕ № 0000000555", page_number=1, bbox=(0,0,0,0), y_center=0, tokens=[]),
        LogicalLine(text="Към фактура № 1000045678 от 15.04.2026 г.", page_number=1, bbox=(0,0,0,0), y_center=0, tokens=[]),
        LogicalLine(text="Основание за корекция: Търговска отстъпка за обем", page_number=1, bbox=(0,0,0,0), y_center=0, tokens=[]),
    ]
    ref_num, ref_date, ref_reason = extract_credit_debit_note_reference(lines, [])
    assert ref_num == "1000045678"
    assert ref_date == "2026-04-15"
    assert "Търговска отстъпка" in ref_reason


def test_extract_credit_note_reference_padding():
    lines = [
        LogicalLine(text="Във връзка с фактура No 45678 dated 2026-03-01", page_number=1, bbox=(0,0,0,0), y_center=0, tokens=[]),
    ]
    ref_num, ref_date, _ = extract_credit_debit_note_reference(lines, [])
    assert ref_num == "0000045678"
    assert ref_date == "2026-03-01"


def test_credit_note_negative_line_items_valid():
    invoice = Invoice()
    invoice.invoice_metadata.invoice_number = "0000000100"
    invoice.invoice_metadata.date_issued = "2026-05-10"
    invoice.invoice_metadata.document_type = DocumentType.CREDIT_NOTE.value
    invoice.invoice_metadata.is_credit_note = True
    invoice.invoice_metadata.original_invoice_number = "1000012345"
    invoice.supplier = Party(name="Доставчик ООД", eik="121644736")
    invoice.recipient = Party(name="Клиент ЕООД", eik="208380135")
    invoice.line_items = [
        LineItem(
            index=1,
            description="Върната стока",
            quantity=Decimal("-2"),
            unit_price_net=MoneyAmount(Decimal("50.00"), "BGN"),
            total_price_net=MoneyAmount(Decimal("-100.00"), "BGN"),
            vat_rate_pct=Decimal("20"),
        )
    ]
    invoice.financial_summary = FinancialSummary(
        tax_base=MoneyAmount(Decimal("-100.00"), "BGN"),
        vat_amount=MoneyAmount(Decimal("-20.00"), "BGN"),
        total_amount_due=MoneyAmount(Decimal("-120.00"), "BGN"),
    )

    val = validate_invoice(invoice, [])
    assert val.is_valid is True
    # Ensure no false positive negative quantity / price warnings
    codes = [i.code for i in val.errors + val.warnings]
    assert "NEGATIVE_QUANTITY" not in codes
    assert "NEGATIVE_PRICE" not in codes
    assert "TOTAL_SUM_MISMATCH" not in codes
    assert "VAT_CALCULATION_MISMATCH" not in codes


def test_credit_note_missing_reference_warning():
    invoice = Invoice()
    invoice.invoice_metadata.invoice_number = "0000000100"
    invoice.invoice_metadata.date_issued = "2026-05-10"
    invoice.invoice_metadata.document_type = DocumentType.CREDIT_NOTE.value
    invoice.invoice_metadata.is_credit_note = True
    invoice.invoice_metadata.original_invoice_number = None  # Missing
    invoice.supplier = Party(name="Доставчик ООД", eik="121644736")
    invoice.recipient = Party(name="Клиент ЕООД", eik="208380135")
    invoice.financial_summary = FinancialSummary(
        tax_base=MoneyAmount(Decimal("-100.00"), "BGN"),
        vat_amount=MoneyAmount(Decimal("-20.00"), "BGN"),
        total_amount_due=MoneyAmount(Decimal("-120.00"), "BGN"),
    )

    val = validate_invoice(invoice, [])
    warn_codes = [w.code for w in val.warnings]
    assert "MISSING_CORRECTED_INVOICE_REFERENCE" in warn_codes


# ===========================================================================
# 3. BUDGET PAYMENT ORDER (PAYMENT_ORDER_NAP) TESTS
# ===========================================================================

def test_validate_budget_payment_order_valid():
    inv = Invoice()
    inv.invoice_metadata.document_type = DocumentType.PAYMENT_ORDER_NAP.value
    inv.budget_payment = BudgetPaymentDetails(
        nra_iban="BG97BNBG96618000112001",
        obligated_person_eik="208380135",
        obligated_person_name="РМ КАСКАДА 2026 ЕООД",
        payment_paragraph="112001",
        period_from="07.2026",
        period_to="07.2026",
        amount_transferred=MoneyAmount(Decimal("783.02"), "BGN"),
    )

    val = validate_invoice(inv, [])
    assert val.is_valid is True
    assert len(val.errors) == 0


def test_validate_budget_payment_order_invalid_iban():
    inv = Invoice()
    inv.invoice_metadata.document_type = DocumentType.PAYMENT_ORDER_NAP.value
    inv.budget_payment = BudgetPaymentDetails(
        nra_iban="BG97BNBG96618000112999",  # Invalid checksum
        obligated_person_eik="208380135",
        amount_transferred=MoneyAmount(Decimal("100.00"), "BGN"),
    )
    val = validate_budget_payment_order(inv, [])
    assert val.is_valid is False
    assert any(e.code == "INVALID_NRA_IBAN" for e in val.errors)


def test_validate_budget_payment_order_invalid_eik():
    inv = Invoice()
    inv.invoice_metadata.document_type = DocumentType.PAYMENT_ORDER_NAP.value
    inv.budget_payment = BudgetPaymentDetails(
        nra_iban="BG97BNBG96618000112001",
        obligated_person_eik="123456780",  # Invalid Modulo-11
        amount_transferred=MoneyAmount(Decimal("100.00"), "BGN"),
    )
    val = validate_budget_payment_order(inv, [])
    assert val.is_valid is False
    assert any(e.code == "INVALID_OBLIGATED_PERSON_EIK" for e in val.errors)


def test_validate_budget_payment_order_missing_amount():
    inv = Invoice()
    inv.invoice_metadata.document_type = DocumentType.PAYMENT_ORDER_NAP.value
    inv.budget_payment = BudgetPaymentDetails(
        nra_iban="BG97BNBG96618000112001",
        obligated_person_eik="208380135",
        amount_transferred=MoneyAmount(None, "BGN"),
    )
    val = validate_budget_payment_order(inv, [])
    assert val.is_valid is False
    assert any(e.code == "MISSING_TRANSFERRED_AMOUNT" for e in val.errors)


# ===========================================================================
# 4. END-TO-END VERIFICATION: 59.pdf (CRITICAL AUDIT CASE)
# ===========================================================================

@pytest.mark.skipif(
    not Path("/Volumes/NO NAME/_ФАКТУРИ/00_РМ_КАСКАДА_2026_ЕООД/59.pdf").exists(),
    reason="59.pdf not mounted on USB drive",
)
def test_end_to_end_59_pdf_payment_order_nap():
    pdf_path = Path("/Volumes/NO NAME/_ФАКТУРИ/00_РМ_КАСКАДА_2026_ЕООД/59.pdf")
    inv = process_invoice(pdf_path)

    # 1. Document classification must detect budget payment order
    assert inv.invoice_metadata.document_type == DocumentType.PAYMENT_ORDER_NAP.value

    # 2. Must be 100% valid with 0 errors
    assert inv.validation.is_valid is True
    assert len(inv.validation.errors) == 0

    # 3. Critical budget details extracted
    bp = inv.budget_payment
    assert bp is not None
    assert bp.nra_iban == "BG97BNBG96618000112001"
    assert bp.obligated_person_eik == "208380135"
    assert bp.payment_paragraph == "112001"
    assert bp.period_from == "07.2026"
    assert bp.period_to == "07.2026"
    assert bp.amount_transferred.amount == Decimal("783.02")
    assert bp.amount_transferred.currency == "BGN"

    # 4. JSON serialization compatibility
    serialized = serialize_invoice(inv)
    assert "PAYMENT_ORDER_NAP" in serialized
    assert "BG97BNBG96618000112001" in serialized
    assert "783.02" in serialized


# ===========================================================================
# 5. BATCH SUMMARY INTEGRATION TEST
# ===========================================================================

def test_batch_summary_formatting_with_document_types():
    dummy_summary = {
        "summary": {
            "total_documents": 5,
            "processed_successfully": 5,
            "failed": 0,
            "valid_documents": 5,
            "invalid_documents": 0,
            "validation_pass_rate_pct": 100.0,
            "total_line_items": 12,
            "document_types": {
                "INVOICE": 3,
                "CREDIT_NOTE": 1,
                "PAYMENT_ORDER_NAP": 1,
            },
            "total_duration_seconds": 1.25,
            "average_duration_seconds": 0.25,
        },
        "financial_totals_by_currency": {
            "BGN": {
                "document_count": 5,
                "total_tax_base": "1000.00",
                "total_vat_amount": "200.00",
                "total_amount_due": "1200.00",
            }
        },
        "suppliers": [],
        "validation_issues_summary": {"errors": {}, "warnings": {}},
    }
    text = format_batch_console_report(dummy_summary)
    assert "РАЗПРЕДЕЛЕНИЕ ПО ВИДОВЕ ДОКУМЕНТИ:" in text
    assert "INVOICE" in text
    assert "CREDIT_NOTE" in text
    assert "PAYMENT_ORDER_NAP" in text

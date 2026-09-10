"""Unit tests for AccountingEngine and Microinvest Delta Pro operations."""
from decimal import Decimal
import os
import pytest

from invoice_core.accounting_engine import (
    AccountingEngine,
    JournalEntryRow,
    AccountingOperation,
    parse_transfer_log,
    export_delta_csv,
    generate_supabase_accounting_sql,
    ACCT_STOCK_MERCHANDISE,
    ACCT_EXTERNAL_SERVICES,
    ACCT_OTHER_EXPENSES,
    ACCT_MATERIALS,
    ACCT_VAT_PURCHASES,
    ACCT_CASH_BGN,
    ACCT_SUPPLIERS,
    DOC_TYPE_INVOICE,
    DOC_TYPE_CREDIT_NOTE,
)
from invoice_core.models import (
    Invoice,
    InvoiceMetadata,
    Party,
    FinancialSummary,
    PaymentDetails,
    MoneyAmount,
    LineItem,
)
from invoice_core.pipeline import serialize_invoice


@pytest.fixture
def engine():
    return AccountingEngine()


def test_standard_purchase_metro(engine):
    inv_data = {
        "invoice_number": "2205445374",
        "date_issued": "2025-01-23",
        "vendorName": "МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД",
        "vendorEik": "121644736",
        "subtotal": "145.07",
        "taxAmount": "29.01",
        "totalAmount": "174.08",
        "payment_method": "CASH",
    }
    op = engine.create_operation(inv_data, operation_id=1)

    assert op.operation_id == 1
    assert op.document_number == "2205445374"
    assert op.document_type == DOC_TYPE_INVOICE
    assert op.document_type_label == "Ф-ра"
    assert op.tax_base == Decimal("145.07")
    assert op.vat_amount == Decimal("29.01")
    assert op.total_amount == Decimal("174.08")
    assert op.is_balanced is True
    assert op.reason == "стоки"

    # Verify rows
    assert len(op.rows) == 4
    # Line 1: 304 / 501
    r1_cr = [r for r in op.rows if r.line_number == 1 and r.direction == "CREDIT"][0]
    r1_db = [r for r in op.rows if r.line_number == 1 and r.direction == "DEBIT"][0]
    assert r1_cr.account == ACCT_CASH_BGN
    assert r1_cr.amount == Decimal("-145.07")
    assert r1_db.account == ACCT_STOCK_MERCHANDISE
    assert r1_db.amount == Decimal("145.07")

    # Line 2: 4531 / 501
    r2_cr = [r for r in op.rows if r.line_number == 2 and r.direction == "CREDIT"][0]
    r2_db = [r for r in op.rows if r.line_number == 2 and r.direction == "DEBIT"][0]
    assert r2_cr.account == ACCT_CASH_BGN
    assert r2_cr.amount == Decimal("-29.01")
    assert r2_db.account == ACCT_VAT_PURCHASES
    assert r2_db.amount == Decimal("29.01")


def test_external_services_vivacom_bank(engine):
    inv_data = {
        "invoice_number": "1145678901",
        "date_issued": "2025-01-15",
        "vendorName": "БТК ЕАД",
        "vendorEik": "831642181",
        "subtotal": "50.00",
        "taxAmount": "10.00",
        "totalAmount": "60.00",
        "payment_method": "BANK",
    }
    op = engine.create_operation(inv_data, operation_id=2)

    assert op.is_balanced is True
    assert op.reason == "телекомуникации"
    # Deferred payment should use account 401 (Доставчици)
    r1_cr = [r for r in op.rows if r.line_number == 1 and r.direction == "CREDIT"][0]
    r1_db = [r for r in op.rows if r.line_number == 1 and r.direction == "DEBIT"][0]
    assert r1_cr.account == ACCT_SUPPLIERS
    assert r1_cr.amount == Decimal("-50.00")
    assert r1_db.account == ACCT_EXTERNAL_SERVICES
    assert r1_db.amount == Decimal("50.00")


def test_consumables_sbb_group(engine):
    inv_data = {
        "invoice_number": "0000012345",
        "date_issued": "2025-01-18",
        "vendorName": "СББ ГРУП ЕООД",
        "vendorEik": "200388915",
        "subtotal": "120.00",
        "taxAmount": "24.00",
        "totalAmount": "144.00",
        "payment_method": "CASH",
    }
    op = engine.create_operation(inv_data, operation_id=3)

    assert op.is_balanced is True
    assert op.reason == "консумативи"
    r1_db = [r for r in op.rows if r.line_number == 1 and r.direction == "DEBIT"][0]
    assert r1_db.account == ACCT_OTHER_EXPENSES
    assert r1_db.amount == Decimal("120.00")


def test_non_vat_supplier_cookielicious(engine):
    inv_data = {
        "invoice_number": "0207822235",
        "date_issued": "2025-01-20",
        "vendorName": "КУКИЛИШЪС ЕООД",
        "vendorEik": "207822235",
        "subtotal": "187.65",
        "taxAmount": "0.00",
        "totalAmount": "187.65",
        "payment_method": "CASH",
    }
    op = engine.create_operation(inv_data, operation_id=4)

    assert op.is_balanced is True
    # Non-VAT supplier should only have 1 line (2 journal rows) without VAT line
    assert len(op.rows) == 2
    assert op.vat_amount == Decimal("0.00")
    r1_db = op.rows[1]
    assert r1_db.account == ACCT_STOCK_MERCHANDISE
    assert r1_db.amount == Decimal("187.65")


def test_credit_note_storno_arpak(engine):
    inv_data = {
        "invoice_number": "2001480033",
        "date_issued": "2025-01-09",
        "vendorName": "АРПАК ЕООД",
        "vendorEik": "114690224",
        "subtotal": "26.40",
        "taxAmount": "5.28",
        "totalAmount": "31.68",
        "is_credit_note": True,
        "payment_method": "CASH",
    }
    op = engine.create_operation(inv_data, operation_id=6)

    assert op.is_credit_note is True
    assert op.document_type == DOC_TYPE_CREDIT_NOTE
    assert op.document_type_label == "КИ"
    assert op.is_balanced is True

    # Credit Note should have negative debit amounts (червено сторно)
    r1_db = [r for r in op.rows if r.line_number == 1 and r.direction == "DEBIT"][0]
    r1_cr = [r for r in op.rows if r.line_number == 1 and r.direction == "CREDIT"][0]
    assert r1_db.amount == Decimal("-26.40")
    assert r1_cr.amount == Decimal("26.40")

    r2_db = [r for r in op.rows if r.line_number == 2 and r.direction == "DEBIT"][0]
    r2_cr = [r for r in op.rows if r.line_number == 2 and r.direction == "CREDIT"][0]
    assert r2_db.amount == Decimal("-5.28")
    assert r2_cr.amount == Decimal("5.28")


def test_invoice_object_integration(engine):
    invoice = Invoice(
        invoice_metadata=InvoiceMetadata(
            invoice_number="2208612033",
            date_issued="2025-01-23",
            currency="BGN",
        ),
        supplier=Party(
            name="МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД",
            eik="121644736",
            vat_number="BG121644736",
        ),
        recipient=Party(
            name="СИКРЕТ ЛЕДЖЪНД ЕООД",
            eik="208139865",
            vat_number="BG208139865",
        ),
        financial_summary=FinancialSummary(
            tax_base=MoneyAmount(Decimal("138.66"), "BGN"),
            vat_amount=MoneyAmount(Decimal("27.73"), "BGN"),
            total_amount_due=MoneyAmount(Decimal("166.39"), "BGN"),
        ),
        payment_details=PaymentDetails(method="CASH"),
    )

    op = engine.create_operation(invoice, operation_id=13)
    assert op.document_number == "2208612033"
    assert op.tax_base == Decimal("138.66")
    assert op.vat_amount == Decimal("27.73")
    assert op.total_amount == Decimal("166.39")
    assert op.is_balanced is True

    # Attach to invoice and test serialization
    invoice.accounting_operation = op
    json_str = serialize_invoice(invoice)
    assert "accounting_operation" in json_str
    assert "2208612033" in json_str


def test_delta_csv_and_supabase_sql_generation(engine):
    inv_data = {
        "invoice_number": "2205445374",
        "date_issued": "2025-01-23",
        "vendorName": "МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД",
        "vendorEik": "121644736",
        "subtotal": "145.07",
        "taxAmount": "29.01",
        "totalAmount": "174.08",
        "payment_method": "CASH",
    }
    op = engine.create_operation(inv_data, operation_id=1)

    # Test Delta Pro CSV Export
    csv_bytes = export_delta_csv([op])
    csv_text = csv_bytes.decode("cp1251")
    assert "2205445374" in csv_text
    assert "304" in csv_text
    assert "501" in csv_text
    assert "145.07" in csv_text

    # Test Supabase SQL generation
    sql = generate_supabase_accounting_sql([op])
    assert "INSERT INTO accounting.transactions" in sql
    assert "INSERT INTO accounting.journal_entries" in sql
    assert "OP-00001" in sql
    assert "121644736" in sql


def test_parse_transfer_log_file_if_available():
    log_path = "/Volumes/NO NAME/Маджестик Смоук/TRANSFER.LOG"
    if os.path.exists(log_path):
        ops = parse_transfer_log(log_path)
        assert len(ops) == 205
        # Verify first operation
        first_op = ops[0]
        assert first_op.operation_id == 1
        assert first_op.is_balanced is True
        # Verify credit note
        cn_ops = [o for o in ops if o.is_credit_note]
        assert len(cn_ops) >= 1
        assert cn_ops[0].document_type_label == "КИ"
        assert cn_ops[0].is_balanced is True

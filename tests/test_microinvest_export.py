"""Tests for Microinvest Sklad Pro & Delta Pro TransferData XML Exporters."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decimal import Decimal
import xml.etree.ElementTree as ET
import tempfile
import pytest
from fastapi.testclient import TestClient

from api_server import app
from invoice_ocr import (
    Invoice,
    InvoiceMetadata,
    Party,
    LineItem,
    FinancialSummary,
    MoneyAmount,
)
from accounting_export import (
    generate_microinvest_sklad_xml,
    generate_microinvest_delta_xml,
    export_microinvest_package,
)

TRANSFER_NS = "urn:Transfer"


@pytest.fixture
def sample_invoice():
    return Invoice(
        invoice_metadata=InvoiceMetadata(
            invoice_number="0000123456",
            date_issued="2026-08-16",
            document_type="INVOICE",
        ),
        supplier=Party(
            name="МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД",
            eik="121644736",
            vat_number="BG121644736",
            address="гр. София 1784, бул. Цариградско шосе 7-11 км",
        ),
        recipient=Party(
            name="ТЕСТ КЛИЕНТ ЕООД",
            eik="208380135",
            vat_number="BG208380135",
            address="гр. Плевен, ул. Васил Левски 10",
        ),
        line_items=[
            LineItem(
                index=1,
                article_code="SKU-001",
                description="Офис консумативи",
                quantity=Decimal("5.0000"),
                unit="бр.",
                unit_price_net=Decimal("20.00"),
                vat_rate_pct=Decimal("20.00"),
                total_price_net=Decimal("100.00"),
            ),
        ],
        financial_summary=FinancialSummary(
            tax_base=MoneyAmount(Decimal("100.00"), "BGN"),
            vat_amount=MoneyAmount(Decimal("20.00"), "BGN"),
            total_amount_due=MoneyAmount(Decimal("120.00"), "BGN"),
        ),
    )


@pytest.fixture
def sample_credit_note():
    return Invoice(
        invoice_metadata=InvoiceMetadata(
            invoice_number="0000123457",
            date_issued="2026-08-17",
            document_type="CREDIT_NOTE",
            is_credit_note=True,
        ),
        supplier=Party(
            name="МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД",
            eik="121644736",
            vat_number="BG121644736",
        ),
        recipient=Party(
            name="ТЕСТ КЛИЕНТ ЕООД",
            eik="208380135",
        ),
        line_items=[
            LineItem(
                index=1,
                article_code="SKU-001",
                description="Върната стока",
                quantity=Decimal("1.0000"),
                unit="бр.",
                unit_price_net=Decimal("20.00"),
                vat_rate_pct=Decimal("20.00"),
                total_price_net=Decimal("20.00"),
            ),
        ],
        financial_summary=FinancialSummary(
            tax_base=MoneyAmount(Decimal("20.00"), "BGN"),
            vat_amount=MoneyAmount(Decimal("4.00"), "BGN"),
            total_amount_due=MoneyAmount(Decimal("24.00"), "BGN"),
        ),
    )


# ---------------------------------------------------------------------------
# Sklad Pro XML Tests
# ---------------------------------------------------------------------------

def test_generate_sklad_xml_from_invoice(sample_invoice):
    xml_str = generate_microinvest_sklad_xml(sample_invoice)
    assert xml_str.startswith("<?xml")
    root = ET.fromstring(xml_str)
    assert root.tag == "Invoice"
    assert root.find("DocumentNumber").text == "0000123456"
    assert root.find("DocumentDate").text == "2026-08-16"
    assert root.find("DocumentType").text == "Purchase"

    vendor = root.find("Vendor")
    assert vendor.find("Name").text == "МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД"
    assert vendor.find("TaxNumber").text == "121644736"
    assert vendor.find("VATNumber").text == "BG121644736"

    recipient = root.find("Recipient")
    assert recipient.find("Name").text == "ТЕСТ КЛИЕНТ ЕООД"
    assert recipient.find("TaxNumber").text == "208380135"

    items = root.find("Items")
    assert len(items.findall("Item")) == 1
    item = items.find("Item")
    assert item.find("Code").text == "SKU-001"
    assert item.find("UnitPrice").text == "20.00"
    assert item.find("VATRate").text == "20.00"

    totals = root.find("Totals")
    assert totals.find("Subtotal").text == "100.00"
    assert totals.find("VATAmount").text == "20.00"
    assert totals.find("TotalAmount").text == "120.00"


def test_generate_sklad_xml_from_dict():
    inv_dict = {
        "invoice_number": "1000000001",
        "date_issued": "2026-08-20",
        "currency": "EUR",
        "supplier": {"name": "Test Supplier", "eik": "111111111"},
        "recipient": {"name": "Test Buyer", "eik": "222222222"},
        "financial_summary": {
            "tax_base": Decimal("50.00"),
            "vat_amount": Decimal("10.00"),
            "total_amount_due": Decimal("60.00"),
        },
        "line_items": [],
    }
    xml_str = generate_microinvest_sklad_xml(inv_dict)
    root = ET.fromstring(xml_str)
    assert root.find("DocumentNumber").text == "1000000001"
    assert root.find("Currency").text == "EUR"
    # Auto-synthesized item
    assert len(root.find("Items").findall("Item")) == 1


# ---------------------------------------------------------------------------
# Delta Pro TransferData XML Tests
# ---------------------------------------------------------------------------

def test_generate_delta_xml_standard_purchase(sample_invoice):
    xml_str = generate_microinvest_delta_xml(sample_invoice)
    assert TRANSFER_NS in xml_str
    root = ET.fromstring(xml_str)

    accs = root.find(f"{{{TRANSFER_NS}}}Accountings")
    assert accs is not None
    acc_list = accs.findall(f"{{{TRANSFER_NS}}}Accounting")
    assert len(acc_list) == 1

    acc = acc_list[0]
    assert acc.attrib.get("Term") == "Покупка"
    assert acc.attrib.get("VatTerm") == "1"

    doc = acc.find(f"{{{TRANSFER_NS}}}Document")
    assert doc.attrib.get("Number") == "0000123456"
    assert doc.attrib.get("DocumentType") == "1"

    details = acc.find(f"{{{TRANSFER_NS}}}AccountingDetails").findall(f"{{{TRANSFER_NS}}}AccountingDetail")
    # Expected rows: 602 Debit (100.00), 453/1 Debit (20.00), 401 Credit (120.00)
    assert len(details) == 3

    debit_sum = Decimal("0")
    credit_sum = Decimal("0")
    for d in details:
        amt = Decimal(d.attrib.get("Amount"))
        if d.attrib.get("Direction") == "Debit":
            debit_sum += amt
        else:
            credit_sum += amt

    assert debit_sum == Decimal("120.00")
    assert credit_sum == Decimal("120.00")
    assert debit_sum == credit_sum, "TransferData record must balance exactly"


def test_generate_delta_xml_credit_note(sample_credit_note):
    xml_str = generate_microinvest_delta_xml(sample_credit_note)
    root = ET.fromstring(xml_str)
    acc = root.find(f"{{{TRANSFER_NS}}}Accountings").find(f"{{{TRANSFER_NS}}}Accounting")
    assert acc.attrib.get("Term") == "Кредитно известие"

    details = acc.find(f"{{{TRANSFER_NS}}}AccountingDetails").findall(f"{{{TRANSFER_NS}}}AccountingDetail")
    # Credit note reverses direction: 602 Credit (20.00), 453/1 Credit (4.00), 401 Debit (24.00)
    debit_sum = Decimal("0")
    credit_sum = Decimal("0")
    for d in details:
        amt = Decimal(d.attrib.get("Amount"))
        if d.attrib.get("Direction") == "Debit":
            debit_sum += amt
        else:
            credit_sum += amt

    assert debit_sum == Decimal("24.00")
    assert credit_sum == Decimal("24.00")
    assert debit_sum == credit_sum


def test_export_microinvest_package(sample_invoice, sample_credit_note):
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = export_microinvest_package([sample_invoice, sample_credit_note], tmp_dir)
        assert "delta_xml" in res
        assert Path(res["delta_xml"]).exists()
        assert "sklad_xml_0000123456" in res
        assert Path(res["sklad_xml_0000123456"]).exists()


# ---------------------------------------------------------------------------
# API Server Endpoints Tests
# ---------------------------------------------------------------------------

def test_api_microinvest_sklad_endpoint(sample_invoice):
    client = TestClient(app)
    payload = {
        "invoices": [
            {
                "invoice_number": "9999000001",
                "date_issued": "2026-08-25",
                "supplier": {"name": "Доставчик ООД", "eik": "123456789"},
                "recipient": {"name": "Купувач ЕООД", "eik": "987654321"},
                "financial_summary": {
                    "tax_base": "100.00",
                    "vat_amount": "20.00",
                    "total_amount_due": "120.00",
                },
            }
        ]
    }
    resp = client.post("/api/v1/export/microinvest/sklad", json=payload)
    assert resp.status_code == 200
    assert "application/xml" in resp.headers["content-type"]
    assert "<Invoice" in resp.text
    assert "<DocumentNumber>9999000001</DocumentNumber>" in resp.text


def test_api_microinvest_delta_endpoint(sample_invoice):
    client = TestClient(app)
    payload = {
        "invoices": [
            {
                "invoice_number": "9999000001",
                "date_issued": "2026-08-25",
                "supplier": {"name": "Доставчик ООД", "eik": "123456789"},
                "recipient": {"name": "Купувач ЕООД", "eik": "987654321"},
                "financial_summary": {
                    "tax_base": "100.00",
                    "vat_amount": "20.00",
                    "total_amount_due": "120.00",
                },
            }
        ],
        "default_expense_account": "602",
        "default_supplier_account": "401",
    }
    resp = client.post("/api/v1/export/microinvest/delta", json=payload)
    assert resp.status_code == 200
    assert "application/xml" in resp.headers["content-type"]
    assert "TransferData" in resp.text
    assert 'Account="602"' in resp.text
    assert 'Account="401"' in resp.text


def test_api_microinvest_empty_invoices():
    client = TestClient(app)
    resp = client.post("/api/v1/export/microinvest/sklad", json={"invoices": []})
    assert resp.status_code == 400

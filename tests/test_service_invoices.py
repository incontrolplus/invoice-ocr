"""Tests for Service Invoices without Table Grids (Critical Problem #5).

Under Bulgarian accounting standards (ЗДДС), service invoices (e.g. security fees,
rent, maintenance, transport) often omit a traditional multi-column table grid
(Quantity, Unit, Unit Price).
When no table grid is detected, but valid financial totals (tax base) exist, the system
must synthesize exactly one Service Line Item with:
- index = 1
- quantity = 1
- unit_price = tax_base
- total_price = tax_base
- vat_rate_pct = 20% (or inferred rate)
- descriptive text from document body or deal basis
"""

from decimal import Decimal
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

from invoice_ocr import (
    process_invoice,
    Invoice,
    LineItem,
    MoneyAmount,
    FinancialSummary,
    Party,
    LogicalLine,
    OcrToken,
    synthesize_service_line_item,
    extract_service_description,
    _validate_line_items,
    _validate_totals,
)

CORPUS_DIR = Path("/Volumes/NO NAME/_ФАКТУРИ")


class TestServiceInvoices:
    """Test suite for service invoices without table grids."""

    def test_synthesize_service_line_item_math(self):
        """Verify mathematical integrity of synthetic service line items."""
        fs = FinancialSummary(
            tax_base=MoneyAmount(Decimal("120.00"), "EUR"),
            vat_amount=MoneyAmount(Decimal("24.00"), "EUR"),
            total_amount_due=MoneyAmount(Decimal("144.00"), "EUR"),
        )
        supplier = Party(name="Експрес Секюрити СОД ЕООД", eik="114540185")
        item = synthesize_service_line_item(lines=[], tokens=[], financial_summary=fs, supplier=supplier)

        assert item is not None
        assert item.index == 1
        assert item.quantity == Decimal("1")
        assert item.unit == "бр."
        assert item.unit_price_net.amount == Decimal("120.00")
        assert item.unit_price_net.currency == "EUR"
        assert item.total_price_net.amount == Decimal("120.00")
        assert item.total_price_net.currency == "EUR"
        assert item.vat_rate_pct == Decimal("20")

        # Line item validation
        inv = Invoice(
            financial_summary=fs,
            supplier=supplier,
            recipient=Party(name="Купувач ООД", eik="207930830"),
            line_items=[item],
        )
        issues = _validate_line_items(inv)
        assert len(issues) == 0, f"Unexpected line item issues: {issues}"

        totals_issues = _validate_totals(inv)
        assert len(totals_issues) == 0, f"Unexpected totals issues: {totals_issues}"

    def test_synthesize_service_line_item_with_inferred_tax_base(self):
        """When tax_base is None but total_amount_due exists, tax_base is derived."""
        fs = FinancialSummary(
            tax_base=MoneyAmount(None, "BGN"),
            vat_amount=MoneyAmount(None, "BGN"),
            total_amount_due=MoneyAmount(Decimal("120.00"), "BGN"),
        )
        item = synthesize_service_line_item(lines=[], tokens=[], financial_summary=fs)
        assert item is not None
        assert item.total_price_net.amount == Decimal("100.00")
        assert fs.tax_base.amount == Decimal("100.00")

    def test_description_explicit_label(self):
        """Explicit basis label in invoice is extracted."""
        tok = OcrToken(text="Основание на сделката: Абонаментна поддръжка за софтуер", conf=95.0, bbox=(100, 1000, 600, 30))
        lines = [
            LogicalLine(
                tokens=[tok],
                page_number=1,
                bbox=(100, 1000, 600, 30),
            )
        ]
        tokens = lines[0].tokens
        desc, is_generic = extract_service_description(lines, tokens)
        assert "Абонаментна поддръжка" in desc
        assert not is_generic

    def test_description_supplier_context_fallback(self):
        """When no body text is found, supplier name provides contextual service description."""
        supp = Party(name="ЕКСПРЕС СЕКЮРИТИ СОД ЕООД")
        desc, is_generic = extract_service_description(lines=[], tokens=[], supplier=supp)
        assert desc == "Охранителни услуги"
        assert is_generic is True

    def test_description_generic_statutory_fallback(self):
        """When no context exists, statutory default is used without banned placeholder words."""
        desc, is_generic = extract_service_description(lines=[], tokens=[])
        assert desc == "Доставка на стоки / услуги"
        assert is_generic is True
        banned = {"item", "unknown", "placeholder", "n/a", "none", "артикул", "null", ""}
        assert desc.lower() not in banned

    @pytest.mark.skipif(not CORPUS_DIR.exists(), reason="External invoice corpus volume not mounted")
    def test_real_world_ekspres_security_service_invoice(self):
        """Verify real-world service invoice without table: експрес.pdf."""
        path = CORPUS_DIR / "11_ЕКСПРЕС_СЕКЮРИТИ_СОД_ЕООД/Експрес 2026/експрес.pdf"
        inv = process_invoice(path)
        assert len(inv.line_items) == 1
        item = inv.line_items[0]
        assert item.quantity == Decimal("1")
        assert item.unit_price_net.amount == Decimal("42.00")
        assert item.total_price_net.amount == Decimal("42.00")
        assert item.total_price_net.currency == "EUR"
        assert any(kw in item.description.lower() for kw in ["охрана", "сод", "такса"])
        assert inv.validation.is_valid
        assert len(inv.validation.errors) == 0

    @pytest.mark.skipif(not CORPUS_DIR.exists(), reason="External invoice corpus volume not mounted")
    def test_real_world_nendv_service_invoice(self):
        """Verify real-world invoice without table headers: нендв.pdf."""
        path = CORPUS_DIR / "09_НЕНДВ_ООД/НендВ 2026/нендв.pdf"
        inv = process_invoice(path)
        assert len(inv.line_items) == 1
        item = inv.line_items[0]
        assert item.quantity == Decimal("1")
        assert item.unit_price_net.amount == Decimal("750.00")
        assert item.total_price_net.amount == Decimal("750.00")
        assert item.total_price_net.currency == "EUR"
        assert any(kw in item.description.lower() for kw in ["хлебни", "изделия", "доставка"])
        assert inv.validation.is_valid
        assert len(inv.validation.errors) == 0

    @pytest.mark.skipif(not CORPUS_DIR.exists(), reason="External invoice corpus volume not mounted")
    def test_real_world_kapina_service_invoice(self):
        """Verify real-world invoice without table columns: капина.pdf."""
        path = CORPUS_DIR / "02_КАПИНА_71_ООД/Капина 2025/капина.pdf"
        inv = process_invoice(path)
        assert len(inv.line_items) == 1
        item = inv.line_items[0]
        assert item.quantity == Decimal("1")
        assert item.unit_price_net.amount == Decimal("156.83")
        assert item.total_price_net.amount == Decimal("156.83")
        assert item.total_price_net.currency == "BGN"
        assert inv.validation.is_valid
        assert len(inv.validation.errors) == 0

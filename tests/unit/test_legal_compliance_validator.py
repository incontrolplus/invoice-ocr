"""Test suite for Goal 5: Automated Statutory Requisites Control under ЗСч (чл. 6 и 7) and ЗДДС (чл. 114).

Covers:
1. Bank requisites extraction, ISO 7064 Modulo 97-10 IBAN validation, BIC checks, and Bulgarian bank recognition.
2. Zero / non-charged VAT mandatory legal grounds validation under ЗДДС (обратно начисляване по чл. 163а за скрап и зърно,
   чл. 82 ал. 2, тристранни операции по чл. 141, износ по чл. 28, ВОД по чл. 53, нерегистрирано лице по чл. 113 ал. 9).
3. Signatories compliance under чл. 6, ал. 1, т. 5 и чл. 7 от Закона за счетоводството (съставител / МОЛ).
4. Complete structured legal compliance report (legal_compliance_report) in JSON serialization and API endpoints.
"""
from __future__ import annotations

from decimal import Decimal
import json
import pytest

from invoice_core.legal_compliance import (
    BULGARIAN_BANKS,
    audit_legal_compliance,
    detect_vat_exemption_grounds,
    find_bank_by_bic,
    find_bank_by_code,
    find_bank_by_name,
    validate_bank_requisites,
    validate_signatories_compliance,
)
from invoice_core.models import (
    FinancialSummary,
    Invoice,
    InvoiceMetadata,
    LineItem,
    MoneyAmount,
    Party,
    PaymentDetails,
)
from invoice_core.pipeline import serialize_invoice
from invoice_core.validation import validate_invoice


# ===========================================================================
# 1. Bank Requisites & Servicing Bank Recognition Tests
# ===========================================================================

class TestBankRequisitesAndServicingBankRecognition:
    """Verify Bulgarian IBAN Mod-97 checksum, BIC validation, and bank recognition."""

    def test_valid_bulgarian_iban_dsk(self):
        """Test valid DSK Bank IBAN with Mod-97 checksum."""
        iban = "BG10STSA93000027446545"
        bic = "STSABGSF"
        details, issues = validate_bank_requisites(iban=iban, bic=bic)

        assert details.is_iban_valid is True
        assert details.is_bic_valid is True
        assert details.bank_code == "STSA"
        assert details.bank_recognized is True
        assert "Банка ДСК" in details.servicing_bank
        assert not any(i.severity == "error" for i in issues)

    def test_valid_bulgarian_iban_unicredit(self):
        """Test valid UniCredit Bulbank IBAN with Mod-97 checksum."""
        iban = "BG21UNCR70001524316086"
        bic = "UNCRBGSF"
        details, issues = validate_bank_requisites(iban=iban, bic=bic)

        assert details.is_iban_valid is True
        assert details.is_bic_valid is True
        assert details.bank_code == "UNCR"
        assert details.bank_recognized is True
        assert "УниКредит Булбанк" in details.servicing_bank
        assert not any(i.severity == "error" for i in issues)

    def test_valid_bulgarian_iban_fibank(self):
        """Test valid First Investment Bank (Fibank) IBAN."""
        iban = "BG30FINV91501017845321"
        bic = "FINVBGSF"
        details, issues = validate_bank_requisites(iban=iban, bic=bic)

        assert details.is_iban_valid is True
        assert details.bank_code == "FINV"
        assert details.bank_recognized is True
        assert "Първа инвестиционна банка" in details.servicing_bank

    def test_valid_bulgarian_iban_ubb(self):
        """Test valid United Bulgarian Bank (ОББ) IBAN."""
        iban = "BG69UBBS80021086890030"
        bic = "UBBSBGSF"
        details, issues = validate_bank_requisites(iban=iban, bic=bic)

        assert details.is_iban_valid is True
        assert details.bank_code == "UBBS"
        assert details.bank_recognized is True
        assert "Обединена българска банка" in details.servicing_bank

    def test_valid_bulgarian_iban_postbank(self):
        """Test valid Eurobank Bulgaria (Пощенска банка) IBAN."""
        iban = "BG87BPBI79401058204501"
        bic = "BPBIBGSF"
        details, issues = validate_bank_requisites(iban=iban, bic=bic)

        assert details.is_iban_valid is True
        assert details.bank_code == "BPBI"
        assert details.bank_recognized is True
        assert "Пощенска банка" in details.servicing_bank or "Юробанк" in details.servicing_bank

    def test_invalid_iban_modulo97_checksum(self):
        """Corrupted digit in IBAN must fail ISO 7064 Modulo 97-10 checksum."""
        corrupt_iban = "BG10STSA93000027446546"  # Changed last digit from 5 to 6
        details, issues = validate_bank_requisites(iban=corrupt_iban, bic="STSABGSF")

        assert details.is_iban_valid is False
        assert any(i.code == "INVALID_IBAN_MOD97" for i in issues)
        assert any("контролна сума" in i.message for i in issues)

    def test_invalid_iban_format(self):
        """Incorrect length or non-alphanumeric characters must fail format validation."""
        short_iban = "BG10STSA9300002744"  # 18 chars instead of 22
        details, issues = validate_bank_requisites(iban=short_iban, bic=None)

        assert details.is_iban_valid is False
        assert any(i.code == "INVALID_IBAN_FORMAT" for i in issues)

    def test_invalid_bic_format(self):
        """Malformed BIC code must emit INVALID_BIC_FORMAT."""
        details, issues = validate_bank_requisites(iban="BG10STSA93000027446545", bic="BAD_BIC")

        assert details.is_bic_valid is False
        assert any(i.code == "INVALID_BIC_FORMAT" for i in issues)

    def test_iban_bic_mismatch(self):
        """IBAN for UniCredit with BIC for DSK must trigger an error."""
        iban = "BG21UNCR70001524316086"  # UNCR
        bic = "STSABGSF"                  # STSA
        details, issues = validate_bank_requisites(iban=iban, bic=bic)

        assert details.is_iban_valid is True
        assert details.is_bic_valid is True
        assert any(i.code == "IBAN_BIC_BANK_MISMATCH" for i in issues)

    def test_missing_iban_for_bank_transfer(self):
        """Payment method 'банков превод' without IBAN must produce a warning."""
        details, issues = validate_bank_requisites(iban=None, bic=None, payment_method="банков превод")

        assert any(i.code == "MISSING_IBAN_FOR_BANK_TRANSFER" for i in issues)

    def test_lookup_bank_by_name_and_aliases(self):
        """Test directory lookup by name and aliases."""
        b1 = find_bank_by_name("ОББ")
        assert b1 is not None and b1.code == "UBBS"

        b2 = find_bank_by_name("Fibank")
        assert b2 is not None and b2.code == "FINV"

        b3 = find_bank_by_name("УниКредит")
        assert b3 is not None and b3.code == "UNCR"


# ===========================================================================
# 2. Specific Tax Regimes & Zero VAT Legal Grounds Tests
# ===========================================================================

class TestStatutoryTaxRegimesAndZeroVatCompliance:
    """Verify statutory tax regimes under Bulgarian VAT Act (ЗДДС)."""

    def test_reverse_charge_scrap_metal_chl_163a(self):
        """Test reverse charge for scrap/waste under Art. 163a(1) ЗДДС (Annex 2)."""
        invoice = Invoice(
            invoice_metadata=InvoiceMetadata(
                invoice_number="0100023456",
                date_issued="2026-05-10",
                date_tax_event="2026-05-10",
            ),
            supplier=Party(name="Метал Рециклинг ООД", eik="115829103"),
            recipient=Party(name="Стомана Индъстри АД", eik="113579124"),
            line_items=[
                LineItem(
                    index=1,
                    description="Отпадъци от черни метали (скрап). Обратно начисляване по чл. 163а, ал. 1 от ЗДДС.",
                    quantity=Decimal("12.50"),
                    total_price_net=MoneyAmount(Decimal("3750.00"), "BGN"),
                    vat_rate_pct=Decimal("0.00"),
                )
            ],
            financial_summary=FinancialSummary(
                tax_base=MoneyAmount(Decimal("3750.00"), "BGN"),
                vat_amount=MoneyAmount(Decimal("0.00"), "BGN"),
                total_amount_due=MoneyAmount(Decimal("3750.00"), "BGN"),
            ),
            payment_details=PaymentDetails(
                method="банков превод",
                iban="BG18UNCR70001524316086",
                bic="UNCRBGSF",
            ),
        )

        res = validate_invoice(invoice, tokens=[])
        report = res.legal_compliance_report

        assert report is not None
        assert report.vat_regime.is_zero_or_exempt is True
        assert report.vat_regime.is_valid_basis is True
        assert report.vat_regime.legal_basis_code == "CHL_163A_REVERSE_CHARGE_SCRAP"
        assert "163а" in report.vat_regime.legal_basis_article
        assert report.zdds_compliant is True
        assert not any(e.code == "MISSING_VAT_EXEMPTION_REASON" for e in res.errors)

    def test_reverse_charge_grain_crops_chl_163a(self):
        """Test reverse charge for grain and technical crops under Art. 163a(2) ЗДДС (Annex 2)."""
        invoice = Invoice(
            invoice_metadata=InvoiceMetadata(
                invoice_number="0000045678",
                date_issued="2026-07-20",
                date_tax_event="2026-07-20",
            ),
            supplier=Party(name="Агро Зърно ЕООД", eik="201485962"),
            recipient=Party(name="Мелница Плевен ООД", eik="114609731"),
            line_items=[
                LineItem(
                    index=1,
                    description="Хлебна пшеница 100 тона. Приложение № 2 от ЗДДС - обратно начисляване по чл. 163а, ал. 2.",
                    quantity=Decimal("100"),
                    total_price_net=MoneyAmount(Decimal("25000.00"), "BGN"),
                    vat_rate_pct=Decimal("0.00"),
                )
            ],
            financial_summary=FinancialSummary(
                tax_base=MoneyAmount(Decimal("25000.00"), "BGN"),
                vat_amount=MoneyAmount(Decimal("0.00"), "BGN"),
                total_amount_due=MoneyAmount(Decimal("25000.00"), "BGN"),
            ),
        )

        res = validate_invoice(invoice, tokens=[])
        report = res.legal_compliance_report

        assert report is not None
        assert report.vat_regime.is_zero_or_exempt is True
        assert report.vat_regime.is_valid_basis is True
        assert report.vat_regime.legal_basis_code == "CHL_163A_REVERSE_CHARGE_GRAIN"
        assert "163а" in report.vat_regime.legal_basis_article
        assert report.zdds_compliant is True

    def test_triangular_operations_chl_141(self):
        """Test triangular operations regime under Art. 141 ЗДДС / Directive 2006/112/EC."""
        invoice = Invoice(
            invoice_metadata=InvoiceMetadata(
                invoice_number="1000055443",
                date_issued="2026-06-15",
                date_tax_event="2026-06-15",
            ),
            supplier=Party(name="Балкан Трейдинг ООД", eik="131234567", vat_number="BG131234567"),
            recipient=Party(name="Hellas Import S.A.", vat_number="EL998877665"),
            line_items=[
                LineItem(
                    index=1,
                    description="Промишлено оборудване. Тристранна операция съгласно чл. 141 от ЗДДС.",
                    total_price_net=MoneyAmount(Decimal("15000.00"), "EUR"),
                    vat_rate_pct=Decimal("0.00"),
                )
            ],
            financial_summary=FinancialSummary(
                tax_base=MoneyAmount(Decimal("15000.00"), "EUR"),
                vat_amount=MoneyAmount(Decimal("0.00"), "EUR"),
                total_amount_due=MoneyAmount(Decimal("15000.00"), "EUR"),
            ),
        )

        res = validate_invoice(invoice, tokens=[])
        report = res.legal_compliance_report

        assert report is not None
        assert report.vat_regime.is_zero_or_exempt is True
        assert report.vat_regime.is_valid_basis is True
        assert report.vat_regime.legal_basis_code == "CHL_141_TRIANGULAR"
        assert "141" in report.vat_regime.legal_basis_article
        assert report.zdds_compliant is True

    def test_export_outside_eu_chl_28(self):
        """Test export of goods outside EU under Art. 28 ЗДДС (0% VAT)."""
        invoice = Invoice(
            invoice_metadata=InvoiceMetadata(
                invoice_number="0000088990",
                date_issued="2026-08-01",
                date_tax_event="2026-08-01",
            ),
            supplier=Party(name="Бул Експорт ЕООД", eik="175089345", vat_number="BG175089345"),
            recipient=Party(name="Swiss Trading GmbH (Zurich)", eik="CHE123456789"),
            line_items=[
                LineItem(
                    index=1,
                    description="Експорт на машини за Швейцария. Доставка по чл. 28, т. 1 от ЗДДС (износ извън ЕС).",
                    total_price_net=MoneyAmount(Decimal("50000.00"), "EUR"),
                    vat_rate_pct=Decimal("0.00"),
                )
            ],
            financial_summary=FinancialSummary(
                tax_base=MoneyAmount(Decimal("50000.00"), "EUR"),
                vat_amount=MoneyAmount(Decimal("0.00"), "EUR"),
                total_amount_due=MoneyAmount(Decimal("50000.00"), "EUR"),
            ),
        )

        res = validate_invoice(invoice, tokens=[])
        report = res.legal_compliance_report

        assert report is not None
        assert report.vat_regime.is_zero_or_exempt is True
        assert report.vat_regime.is_valid_basis is True
        assert report.vat_regime.legal_basis_code == "CHL_28_EXPORT"
        assert "28" in report.vat_regime.legal_basis_article
        assert report.zdds_compliant is True

    def test_intra_community_supply_vod_chl_53(self):
        """Test intra-community supply (ВОД) under Art. 53 ЗДДС (0% VAT)."""
        invoice = Invoice(
            invoice_metadata=InvoiceMetadata(
                invoice_number="0000099112",
                date_issued="2026-08-05",
                date_tax_event="2026-08-05",
            ),
            supplier=Party(name="Родина Трейд ООД", eik="102938475", vat_number="BG102938475"),
            recipient=Party(name="Deutsche Handel GmbH", vat_number="DE123456789"),
            line_items=[
                LineItem(
                    index=1,
                    description="Вътреобщностна доставка на стоки (ВОД) съгласно чл. 53 от ЗДДС.",
                    total_price_net=MoneyAmount(Decimal("8000.00"), "EUR"),
                    vat_rate_pct=Decimal("0.00"),
                )
            ],
            financial_summary=FinancialSummary(
                tax_base=MoneyAmount(Decimal("8000.00"), "EUR"),
                vat_amount=MoneyAmount(Decimal("0.00"), "EUR"),
                total_amount_due=MoneyAmount(Decimal("8000.00"), "EUR"),
            ),
        )

        res = validate_invoice(invoice, tokens=[])
        report = res.legal_compliance_report

        assert report is not None
        assert report.vat_regime.is_zero_or_exempt is True
        assert report.vat_regime.is_valid_basis is True
        assert report.vat_regime.legal_basis_code == "CHL_53_VOD"
        assert "53" in report.vat_regime.legal_basis_article
        assert report.zdds_compliant is True

    def test_non_vat_registered_supplier_chl_113_al_9(self):
        """Test supplier non-registered under VAT Act pursuant to Art. 113(9) ЗДДС."""
        invoice = Invoice(
            invoice_metadata=InvoiceMetadata(
                invoice_number="0000000015",
                date_issued="2026-03-12",
                date_tax_event="2026-03-12",
            ),
            supplier=Party(name="Консулт 77 ЕООД", eik="205849302"),
            recipient=Party(name="РМ КАСКАДА 2026 ЕООД", eik="208380135"),
            line_items=[
                LineItem(
                    index=1,
                    description="ИТ консултантски услуги. Основание за неначисляване: чл. 113, ал. 9 от ЗДДС (нерегистрирано лице).",
                    total_price_net=MoneyAmount(Decimal("600.00"), "BGN"),
                    vat_rate_pct=Decimal("0.00"),
                )
            ],
            financial_summary=FinancialSummary(
                tax_base=MoneyAmount(Decimal("600.00"), "BGN"),
                vat_amount=MoneyAmount(Decimal("0.00"), "BGN"),
                total_amount_due=MoneyAmount(Decimal("600.00"), "BGN"),
            ),
        )

        res = validate_invoice(invoice, tokens=[])
        report = res.legal_compliance_report

        assert report is not None
        assert report.vat_regime.is_zero_or_exempt is True
        assert report.vat_regime.is_valid_basis is True
        assert report.vat_regime.legal_basis_code == "CHL_113_AL_9_NOT_REGISTERED"
        assert "113" in report.vat_regime.legal_basis_article
        assert report.zdds_compliant is True

    def test_negative_zero_vat_missing_legal_grounds(self):
        """Domestic invoice charging 0% VAT WITHOUT statutory grounds must fail compliance."""
        invoice = Invoice(
            invoice_metadata=InvoiceMetadata(
                invoice_number="0000099999",
                date_issued="2026-08-10",
                date_tax_event="2026-08-10",
            ),
            supplier=Party(name="Строител ООД", eik="123456789", vat_number="BG123456789"),
            recipient=Party(name="Инвест 2026 ЕООД", eik="208380135"),
            line_items=[
                LineItem(
                    index=1,
                    description="Строително-монтажни работи на обект",
                    total_price_net=MoneyAmount(Decimal("5000.00"), "BGN"),
                    vat_rate_pct=Decimal("0.00"),
                )
            ],
            financial_summary=FinancialSummary(
                tax_base=MoneyAmount(Decimal("5000.00"), "BGN"),
                vat_amount=MoneyAmount(Decimal("0.00"), "BGN"),
                total_amount_due=MoneyAmount(Decimal("5000.00"), "BGN"),
            ),
        )

        res = validate_invoice(invoice, tokens=[])
        report = res.legal_compliance_report

        assert res.is_valid is False
        assert report is not None
        assert report.vat_regime.is_zero_or_exempt is True
        assert report.vat_regime.is_valid_basis is False
        assert report.zdds_compliant is False
        assert report.is_compliant is False

        # Must report MISSING_VAT_EXEMPTION_REASON error
        vat_errs = [e for e in res.errors if e.code == "MISSING_VAT_EXEMPTION_REASON"]
        assert len(vat_errs) == 1
        assert "чл. 114, ал. 1, т. 11" in vat_errs[0].message


# ===========================================================================
# 3. Signatories & Accountability Tests (ЗСч чл. 6, ал. 1, т. 5 и чл. 7)
# ===========================================================================

class TestSignatoriesAndAccountancyActCompliance:
    """Verify compiler and MOL requirements under Art. 6(1)(5) Accountancy Act (ЗСч)."""

    def test_signatory_with_compiler_name(self):
        """Invoice containing compiler name satisfies Art. 6(1)(5) ЗСч."""
        invoice = Invoice(
            invoice_metadata=InvoiceMetadata(
                invoice_number="0000012345",
                date_issued="2026-08-01",
                compiled_by="Петър Дражев",
            ),
            supplier=Party(name="Доставчик ООД", eik="114609731"),
            recipient=Party(name="Получател ЕООД", eik="208380135"),
            financial_summary=FinancialSummary(
                tax_base=MoneyAmount(Decimal("100.00"), "BGN"),
                vat_amount=MoneyAmount(Decimal("20.00"), "BGN"),
                total_amount_due=MoneyAmount(Decimal("120.00"), "BGN"),
            ),
            line_items=[LineItem(index=1, description="Стока", total_price_net=MoneyAmount(Decimal("100.00"), "BGN"))],
        )

        details, issues = validate_signatories_compliance(invoice)
        assert details.is_compliant is True
        assert details.compiled_by == "Петър Дражев"
        assert not any(i.code == "MISSING_ISSUER_NAME_OR_MOL" for i in issues)

    def test_signatory_with_supplier_mol(self):
        """Invoice containing supplier MOL satisfies Art. 6(1)(5) ЗСч."""
        invoice = Invoice(
            invoice_metadata=InvoiceMetadata(
                invoice_number="0000012346",
                date_issued="2026-08-01",
            ),
            supplier=Party(name="Доставчик ООД", eik="114609731", mol="Валентин Борисов"),
            recipient=Party(name="Получател ЕООД", eik="208380135"),
            financial_summary=FinancialSummary(
                tax_base=MoneyAmount(Decimal("100.00"), "BGN"),
                vat_amount=MoneyAmount(Decimal("20.00"), "BGN"),
                total_amount_due=MoneyAmount(Decimal("120.00"), "BGN"),
            ),
            line_items=[LineItem(index=1, description="Стока", total_price_net=MoneyAmount(Decimal("100.00"), "BGN"))],
        )

        details, issues = validate_signatories_compliance(invoice)
        assert details.is_compliant is True
        assert details.supplier_mol == "Валентин Борисов"
        assert not any(i.code == "MISSING_ISSUER_NAME_OR_MOL" for i in issues)

    def test_negative_missing_compiler_and_mol(self):
        """Invoice lacking both compiler name and supplier MOL emits MISSING_ISSUER_NAME_OR_MOL."""
        invoice = Invoice(
            invoice_metadata=InvoiceMetadata(
                invoice_number="0000012347",
                date_issued="2026-08-01",
                compiled_by=None,
            ),
            supplier=Party(name="Доставчик ООД", eik="114609731", mol=None),
            recipient=Party(name="Получател ЕООД", eik="208380135"),
            financial_summary=FinancialSummary(
                tax_base=MoneyAmount(Decimal("100.00"), "BGN"),
                vat_amount=MoneyAmount(Decimal("20.00"), "BGN"),
                total_amount_due=MoneyAmount(Decimal("120.00"), "BGN"),
            ),
            line_items=[LineItem(index=1, description="Стока", total_price_net=MoneyAmount(Decimal("100.00"), "BGN"))],
        )

        details, issues = validate_signatories_compliance(invoice)
        assert details.is_compliant is False
        assert any(i.code == "MISSING_ISSUER_NAME_OR_MOL" for i in issues)


# ===========================================================================
# 4. JSON Serialization & API Integration of legal_compliance_report
# ===========================================================================

class TestLegalComplianceReportSerializationAndApi:
    """Verify inclusion of legal_compliance_report in JSON output and REST endpoints."""

    def test_legal_compliance_report_json_serialization(self):
        """Test that serialize_invoice includes complete legal_compliance_report in JSON."""
        invoice = Invoice(
            invoice_metadata=InvoiceMetadata(
                invoice_number="0300032630",
                date_issued="2026-07-30",
                date_tax_event="2026-07-30",
                compiled_by="Валентин Борисов",
            ),
            supplier=Party(
                name="Валборген ООД",
                eik="114609731",
                vat_number="BG114609731",
                mol="Валентин Борисов",
            ),
            recipient=Party(
                name="ГМ2025 ЕООД",
                eik="208230838",
                vat_number="BG208230838",
            ),
            line_items=[
                LineItem(
                    index=1,
                    description="Пилешко месо",
                    total_price_net=MoneyAmount(Decimal("83.52"), "BGN"),
                    vat_rate_pct=Decimal("20.00"),
                )
            ],
            financial_summary=FinancialSummary(
                tax_base=MoneyAmount(Decimal("83.52"), "BGN"),
                vat_amount=MoneyAmount(Decimal("16.70"), "BGN"),
                total_amount_due=MoneyAmount(Decimal("100.22"), "BGN"),
            ),
            payment_details=PaymentDetails(
                method="банков превод",
                iban="BG10STSA93000027446545",
                bic="STSABGSF",
            ),
        )

        res = validate_invoice(invoice, tokens=[])
        assert res.is_valid is True

        json_str = serialize_invoice(invoice)
        data = json.loads(json_str)

        # 1. Top-level legal_compliance_report
        assert "legal_compliance_report" in data
        lcr = data["legal_compliance_report"]
        assert lcr["is_compliant"] is True
        assert lcr["zsch_compliant"] is True
        assert lcr["zdds_compliant"] is True

        # Bank requisites in report
        bank = lcr["bank_requisites"]
        assert bank["iban"] == "BG10STSA93000027446545"
        assert bank["bic"] == "STSABGSF"
        assert bank["is_iban_valid"] is True
        assert bank["bank_recognized"] is True
        assert "Банка ДСК" in bank["servicing_bank"]

        # VAT regime in report
        vat = lcr["vat_regime"]
        assert vat["is_zero_or_exempt"] is False
        assert vat["is_valid_basis"] is True

        # Signatories in report
        sign = lcr["signatories"]
        assert sign["is_compliant"] is True
        assert sign["compiled_by"] == "Валентин Борисов"

        # 2. Inside validation_results
        val_res = data["validation_results"]
        assert "legal_compliance_report" in val_res
        assert val_res["legal_compliance_report"]["is_compliant"] is True

    def test_api_validation_endpoint_with_legal_compliance(self):
        """Test that /api/v1/invoices/validate endpoint returns legal_compliance_report."""
        from fastapi.testclient import TestClient
        from api_server import app

        client = TestClient(app)
        payload = {
            "invoice_metadata": {
                "invoice_number": "0000011223",
                "date_issued": "2026-08-15",
                "compiled_by": "Иван Стоянов",
            },
            "supplier": {
                "name": "Агро Скрап ЕООД",
                "eik": "115829103",
                "vat_number": "BG115829103",
                "mol": "Иван Стоянов",
            },
            "recipient": {
                "name": "Метал Комерс ООД",
                "eik": "208380135",
                "vat_number": "BG208380135",
            },
            "line_items": [
                {
                    "index": 1,
                    "description": "Скрап от мед и алуминий. Обратно начисляване по чл. 163а от ЗДДС.",
                    "quantity": "5",
                    "unit_price_net": {"amount": "100.00", "currency": "BGN"},
                    "total_price_net": {"amount": "500.00", "currency": "BGN"},
                    "vat_rate_pct": "0.00",
                }
            ],
            "financial_summary": {
                "tax_base": {"amount": "500.00", "currency": "BGN"},
                "vat_amount": {"amount": "0.00", "currency": "BGN"},
                "total_amount_due": {"amount": "500.00", "currency": "BGN"},
            },
            "payment_details": {
                "method": "банков превод",
                "iban": "BG21UNCR70001524316086",
                "bic": "UNCRBGSF",
            },
        }

        resp = client.post("/api/v1/invoices/validate", json=payload)
        assert resp.status_code == 200
        res_data = resp.json()

        assert res_data["is_valid"] is True
        assert "legal_compliance_report" in res_data
        rep = res_data["legal_compliance_report"]
        assert rep["is_compliant"] is True
        assert rep["vat_regime"]["legal_basis_code"] == "CHL_163A_REVERSE_CHARGE_SCRAP"
        assert rep["bank_requisites"]["bank_recognized"] is True
        assert "УниКредит Булбанк" in rep["bank_requisites"]["servicing_bank"]

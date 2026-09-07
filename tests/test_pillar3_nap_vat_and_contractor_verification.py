import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
"""Comprehensive Test Suite for Pillar 3 (P1): Full НАП VAT Cycle & Online Contractor Verification.

Validates:
1. Real-Time Online Contractor Verification:
   - Bulgarian EIK Modulus 11 syntax and checksum
   - Commercial Register (Търговски регистър) legal status (Active, Bankruptcy, Liquidation, Deregistered)
   - NRA VAT Register (чл. 94 ЗДДС) registration/deregistration date bounds vs date_tax_event
   - Denial of tax credit when transaction precedes registration or follows deregistration
   - European Commission VIES verification for EU cross-border counterparties (Google, Meta, Adobe, AWS)
   - Persistent SQLite caching and TTL expiration
   - Offline fallback and test mock registry injection
   - Async (verify_contractor_async) and Sync (verify_contractor) functions

2. Statutory Protocols under Art. 117 of the Bulgarian VAT Act (ЗДДС):
   - Automated detection of Reverse Charge (Google, Meta, Adobe, AWS, foreign EU 0% VAT, reverse charge clauses)
   - Protocol generation: 20% VAT self-assessment, BNB exchange rate conversion (EUR -> BGN 1.95583)
   - Dual-entry reflection into both Purchase Ledger (POKUPKI.TXT) and Sales Ledger (PRODAGBI.TXT)
   - Accounting double-entry transactions (Debit 602/Credit 401 and Debit 4531/Credit 4532)

3. Statutory Sales Ledger (PRODAGBI.TXT - Приложение № 10 от ППЗДДС):
   - 19 statutory columns layout
   - 10-digit zero-padded numbers, date YYYY-MM-DD, contractor EIK/VAT
   - Fixed-width, TSV, and CSV formats
   - Windows-1251 (CP1251) and UTF-8 encodings with CRLF endings
   - Credit notes / storno negative amounts

4. Statutory VAT Declaration (DEKLAR.TXT - Приложение № 13 от ППЗДДС):
   - Sales section A (cells 01, 11-24), Purchases section B (cells 30-43), Result section C (cells 50-80)
   - Strict cross-ledger mathematical reconciliation (POKUPKI + PRODAGBI == DEKLAR)
   - Format validation and CRLF line breaks

5. Complete НАП Export Package (export_nap_package):
   - Full 3-file package (POKUPKI.TXT, PRODAGBI.TXT, DEKLAR.TXT)
   - ZIP archive generation for direct submission to the NRA electronic portal
   - Mixed domestic + reverse charge portfolio testing

6. REST API Endpoints in api_server.py:
   - POST /api/v1/verify/contractor
   - POST /api/v1/protocol-117/generate
   - POST /api/v1/export/prodagbi
   - POST /api/v1/export/deklar
   - POST /api/v1/export/nap-package (JSON and ZIP)
"""

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import zipfile
import pytest
from fastapi.testclient import TestClient

from accounting_export import (
    DEFAULT_BRANCH,
    NapLedgerEntry,
    NapSalesLedgerEntry,
    ProtocolChl117,
    VatDeclaration,
    export_all_accounting_files,
    export_nap_package,
    generate_protocol_chl_117,
    generate_vat_declaration,
    invoice_to_nap_entry,
    invoice_to_nap_sales_entry,
    invoices_to_pokupki_txt,
    invoices_to_prodagbi_txt,
    is_reverse_charge_or_vop,
)
from api_server import app
from contractor_verification import (
    CompanyStatus,
    ContractorCache,
    ContractorVerificationResult,
    ContractorVerifier,
    VatRegistrationStatus,
    verify_contractor,
    verify_contractor_async,
)
from invoice_ocr import (
    DocumentType,
    FinancialSummary,
    Invoice,
    InvoiceMetadata,
    LineItem,
    MoneyAmount,
    Party,
    process_batch,
    validate_contractor_eligibility,
    validate_invoice,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def sample_domestic_invoice() -> Invoice:
    return Invoice(
        invoice_metadata=InvoiceMetadata(
            invoice_number="124013",
            date_issued="2026-08-16",
            date_tax_event="2026-08-16",
            document_type=DocumentType.INVOICE.value,
        ),
        supplier=Party(
            name="МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД",
            eik="121644736",
            vat_number="BG121644736",
        ),
        recipient=Party(
            name="РМ КАСКАДА 2026 ЕООД",
            eik="208380135",
            vat_number="BG208380135",
        ),
        line_items=[
            LineItem(
                index=1,
                description="КАФЕ ЛАВАЦА КРЕМА",
                quantity=Decimal("10"),
                total_price_net=MoneyAmount(Decimal("250.00"), "BGN"),
                vat_rate_pct=Decimal("20.00"),
            )
        ],
        financial_summary=FinancialSummary(
            tax_base=MoneyAmount(Decimal("250.00"), "BGN"),
            vat_amount=MoneyAmount(Decimal("50.00"), "BGN"),
            total_amount_due=MoneyAmount(Decimal("300.00"), "BGN"),
        ),
    )


@pytest.fixture
def sample_google_reverse_charge_invoice() -> Invoice:
    return Invoice(
        invoice_metadata=InvoiceMetadata(
            invoice_number="INV-998877",
            date_issued="2026-08-10",
            date_tax_event="2026-08-10",
            document_type=DocumentType.INVOICE.value,
        ),
        supplier=Party(
            name="Google Cloud EMEA Limited",
            vat_number="IE6388047V",
        ),
        recipient=Party(
            name="РМ КАСКАДА 2026 ЕООД",
            eik="208380135",
            vat_number="BG208380135",
        ),
        line_items=[
            LineItem(
                index=1,
                description="Google Workspace Enterprise Cloud Subscription",
                quantity=Decimal("1"),
                total_price_net=MoneyAmount(Decimal("100.00"), "EUR"),
                vat_rate_pct=Decimal("0.00"),
            )
        ],
        financial_summary=FinancialSummary(
            tax_base=MoneyAmount(Decimal("100.00"), "EUR"),
            vat_amount=MoneyAmount(Decimal("0.00"), "EUR"),
            total_amount_due=MoneyAmount(Decimal("100.00"), "EUR"),
        ),
    )


@pytest.fixture
def sample_meta_reverse_charge_invoice() -> Invoice:
    return Invoice(
        invoice_metadata=InvoiceMetadata(
            invoice_number="META-445566",
            date_issued="2026-08-12",
            date_tax_event="2026-08-12",
            document_type=DocumentType.INVOICE.value,
        ),
        supplier=Party(
            name="Meta Platforms Ireland Limited",
            vat_number="IE9692928F",
        ),
        recipient=Party(
            name="РМ КАСКАДА 2026 ЕООД",
            eik="208380135",
            vat_number="BG208380135",
        ),
        line_items=[
            LineItem(
                index=1,
                description="Рекламна кампания Facebook Ads (Reverse charge)",
                quantity=Decimal("1"),
                total_price_net=MoneyAmount(Decimal("200.00"), "EUR"),
                vat_rate_pct=Decimal("0.00"),
            )
        ],
        financial_summary=FinancialSummary(
            tax_base=MoneyAmount(Decimal("200.00"), "EUR"),
            vat_amount=MoneyAmount(Decimal("0.00"), "EUR"),
            total_amount_due=MoneyAmount(Decimal("200.00"), "EUR"),
        ),
    )


# ---------------------------------------------------------------------------
# 1. Contractor Online Verification Tests
# ---------------------------------------------------------------------------

class TestContractorVerification:
    """Test suite for Bulgarian and EU contractor verification with NRA/VIES/TR rules."""

    def test_bulgarian_valid_contractor(self):
        # Метро Кеш енд Кери (ЕИК 121644736)
        res = verify_contractor("121644736", country_code="BG", date_tax_event="2026-08-16")
        assert res.legal_status == CompanyStatus.ACTIVE
        assert res.vat_status == VatRegistrationStatus.REGISTERED
        assert res.vat_registered_on_date is True
        assert res.is_valid_for_tax_credit is True
        assert len(res.issues) == 0

    def test_bulgarian_invalid_checksum_rejected(self):
        # 121644739 has invalid Modulus 11 checksum
        res = verify_contractor("121644739", country_code="BG")
        assert res.is_valid_for_tax_credit is False
        assert any("Модул 11" in issue for issue in res.issues)

    def test_bankrupt_company_flagged(self):
        # Registered mock bankrupt entity 111111113
        res = verify_contractor("111111113", country_code="BG", date_tax_event="2026-08-16")
        assert res.legal_status == CompanyStatus.BANKRUPTCY
        assert res.is_valid_for_tax_credit is False
        assert any("BANKRUPTCY" in issue for issue in res.issues)

    def test_deregistered_company_flagged(self):
        # Registered mock deleted trader 222222226
        res = verify_contractor("222222226", country_code="BG", date_tax_event="2026-08-16")
        assert res.legal_status == CompanyStatus.DEREGISTERED_DELETED
        assert res.is_valid_for_tax_credit is False
        assert any("ДЕРЕГИСТРИРАНА" in issue for issue in res.issues)

    def test_vat_registration_date_bounds_denial_of_tax_credit(self):
        # Mock entity 333333339 registered on 2026-08-20
        # Transaction on 2026-08-05 (BEFORE registration) -> Tax credit must be DENIED!
        res_early = verify_contractor("333333339", country_code="BG", date_tax_event="2026-08-05")
        assert res_early.vat_registered_on_date is False
        assert res_early.is_valid_for_tax_credit is False
        assert any("ПРЕДИ датата на регистрация" in issue for issue in res_early.issues)

        # Transaction on 2026-08-25 (AFTER registration) -> Valid!
        res_valid = verify_contractor("333333339", country_code="BG", date_tax_event="2026-08-25")
        assert res_valid.vat_registered_on_date is True
        assert res_valid.is_valid_for_tax_credit is True

    def test_bulgarian_date_formats_supported(self):
        # Support DD.MM.YYYY and DD/MM/YYYY formats
        res1 = verify_contractor("333333339", country_code="BG", date_tax_event="25.08.2026")
        assert res1.vat_registered_on_date is True
        assert res1.is_valid_for_tax_credit is True

        res2 = verify_contractor("333333339", country_code="BG", date_tax_event="05.08.2026")
        assert res2.vat_registered_on_date is False
        assert res2.is_valid_for_tax_credit is False

    def test_non_registered_vat_entity(self):
        # Mock entity 444444441 has NOT_REGISTERED status
        res = verify_contractor("444444441", country_code="BG", date_tax_event="2026-08-16")
        assert res.vat_status == VatRegistrationStatus.NOT_REGISTERED
        assert res.vat_registered_on_date is False
        assert res.is_valid_for_tax_credit is False
        assert any("НЯМА регистрация по ЗДДС" in issue for issue in res.issues)

    def test_eu_vies_verification_google_and_meta(self):
        # Google Ireland (IE6388047V)
        res_google = verify_contractor("6388047V", country_code="IE")
        assert res_google.country_code == "IE"
        assert res_google.vat_status == VatRegistrationStatus.REGISTERED
        assert res_google.is_valid_for_tax_credit is True
        assert "GOOGLE" in (res_google.company_name or "")

        # Meta Ireland (IE9692928F)
        res_meta = verify_contractor("IE9692928F")
        assert res_meta.country_code == "IE"
        assert res_meta.vat_status == VatRegistrationStatus.REGISTERED
        assert "META" in (res_meta.company_name or "")

    def test_persistent_sqlite_cache(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            cache = ContractorCache(db_path=tmp.name, ttl_seconds=60)
            rec = ContractorVerificationResult(
                country_code="BG",
                identifier="121644736",
                company_name="МЕТРО КЕШ ЕНД КЕРИ",
                legal_status=CompanyStatus.ACTIVE,
                vat_status=VatRegistrationStatus.REGISTERED,
            )
            cache.set(rec)
            cached = cache.get("BG", "121644736")
            assert cached is not None
            assert cached.company_name == "МЕТРО КЕШ ЕНД КЕРИ"
            assert "CACHE" in cached.source

    def test_cache_pollution_prevention(self):
        # Entity 333333339 registered on 2026-08-20
        # 1. Query with invalid early date -> cached as failed date
        res1 = verify_contractor("333333339", country_code="BG", date_tax_event="2026-08-05")
        assert res1.vat_registered_on_date is False
        assert res1.is_valid_for_tax_credit is False

        # 2. Query with valid date -> must be clean, valid, and without pollution from previous check
        res2 = verify_contractor("333333339", country_code="BG", date_tax_event="2026-08-25")
        assert res2.vat_registered_on_date is True
        assert res2.is_valid_for_tax_credit is True
        assert not any("ПРЕДИ датата" in iss for iss in res2.issues)

    @pytest.mark.anyio
    async def test_async_verification(self):
        res = await verify_contractor_async("121644736", country_code="BG", date_tax_event="2026-08-16")
        assert res.legal_status == CompanyStatus.ACTIVE
        assert res.vat_registered_on_date is True


# ---------------------------------------------------------------------------
# 2. Article 117 Protocols Tests (Reverse Charge & ВОП)
# ---------------------------------------------------------------------------

class TestArticle117Protocols:
    """Test suite for Reverse Charge detection and Art. 117 Protocol generation."""

    def test_reverse_charge_detection(self, sample_google_reverse_charge_invoice, sample_domestic_invoice):
        # Google invoice -> True
        assert is_reverse_charge_or_vop(sample_google_reverse_charge_invoice) is True
        # Domestic Metro invoice with 20% VAT -> False
        assert is_reverse_charge_or_vop(sample_domestic_invoice) is False

    def test_protocol_117_generation_math(self, sample_google_reverse_charge_invoice):
        proto = generate_protocol_chl_117(
            invoice=sample_google_reverse_charge_invoice,
            protocol_number="1",
            recipient_company={"name": "РМ КАСКАДА 2026 ЕООД", "eik": "208380135"},
        )
        assert proto.protocol_number == "0000000001"
        assert proto.currency == "EUR"
        # 100 EUR * 1.95583 = 195.58 BGN
        assert proto.tax_base_bgn == Decimal("195.58")
        # 195.58 * 0.20 = 39.12 BGN
        assert proto.vat_amount_bgn == Decimal("39.12")
        assert proto.total_amount_bgn == Decimal("234.70")
        assert proto.legal_basis == "чл. 82, ал. 2, т. 3 от ЗДДС (доставка на услуги)"

    def test_protocol_text_document_layout(self, sample_google_reverse_charge_invoice):
        proto = generate_protocol_chl_117(sample_google_reverse_charge_invoice)
        doc = proto.to_text_document()
        assert "ПРОТОКОЛ ПО ЧЛ. 117 ОТ ЗДДС" in doc
        assert "№ 0000000001" in doc
        assert "Google Cloud EMEA Limited" in doc
        assert "195.58 лв." in doc
        assert "39.12 лв." in doc
        assert "ДНЕВНИК ЗА ПРОДАЖБИТЕ" in doc
        assert "ДНЕВНИК ЗА ПОКУПКИТЕ" in doc

    def test_protocol_dual_entry_reflection(self, sample_google_reverse_charge_invoice):
        proto = generate_protocol_chl_117(sample_google_reverse_charge_invoice, protocol_number="42")

        # 1. Purchase Ledger Entry
        p_entry = proto.to_purchase_ledger_entry(period="202608")
        assert p_entry.doc_type == "09"
        assert p_entry.doc_number == "0000000042"
        assert p_entry.tax_base_20 == Decimal("195.58")
        assert p_entry.vat_20 == Decimal("39.12")
        assert p_entry.vat_special_art82 == Decimal("39.12")
        assert p_entry.total_amount == Decimal("234.70")

        # 2. Sales Ledger Entry
        s_entry = proto.to_sales_ledger_entry(period="202608")
        assert s_entry.doc_type == "09"
        assert s_entry.doc_number == "0000000042"
        assert s_entry.tax_base_art82 == Decimal("195.58")
        assert s_entry.vat_art82 == Decimal("39.12")
        assert s_entry.total_amount == Decimal("234.70")

    def test_protocol_double_entry_journal_entry(self, sample_google_reverse_charge_invoice):
        proto = generate_protocol_chl_117(sample_google_reverse_charge_invoice)
        jentry = proto.to_journal_entry()
        assert jentry.is_balanced is True
        assert len(jentry.records) == 2
        # Record 1: Expense (Debit 602 / Credit 401)
        assert jentry.records[0].debit_account == "602"
        assert jentry.records[0].credit_account == "401"
        assert jentry.records[0].amount == Decimal("195.58")
        # Record 2: VAT self-assessment (Debit 4531 / Credit 4532)
        assert jentry.records[1].debit_account == "4531"
        assert jentry.records[1].credit_account == "4532"
        assert jentry.records[1].amount == Decimal("39.12")


# ---------------------------------------------------------------------------
# 3. Statutory Sales Ledger Tests (PRODAGBI.TXT)
# ---------------------------------------------------------------------------

class TestSalesLedgerProdagbi:
    """Test suite for statutory PRODAGBI.TXT sales ledger."""

    def test_fixed_width_line_columns_and_encoding(self, sample_domestic_invoice):
        s_entry = invoice_to_nap_sales_entry(sample_domestic_invoice, period="202608")
        line = s_entry.to_fixed_width_line()
        assert line.startswith("2026")
        assert "124013" in line
        assert "250.00" in line
        assert "50.00" in line

        # Generate CP1251 bytes
        txt_bytes = invoices_to_prodagbi_txt([s_entry], format="fixed_width", encoding="cp1251")
        assert isinstance(txt_bytes, bytes)
        assert b"\r\n" in txt_bytes
        decoded = txt_bytes.decode("cp1251")
        assert "124013" in decoded

    def test_tsv_and_csv_formats(self, sample_domestic_invoice):
        tsv_str = invoices_to_prodagbi_txt([sample_domestic_invoice], format="tsv", encoding=None)
        assert "\t" in tsv_str
        parts = tsv_str.strip().split("\t")
        assert len(parts) == 19  # 19 statutory columns

        csv_str = invoices_to_prodagbi_txt([sample_domestic_invoice], format="csv", encoding=None)
        assert ";" in csv_str
        csv_lines = csv_str.strip().split("\r\n")
        assert len(csv_lines) == 2  # Header + 1 record


# ---------------------------------------------------------------------------
# 4. Statutory VAT Declaration Tests (DEKLAR.TXT)
# ---------------------------------------------------------------------------

class TestVatDeclarationDeklar:
    """Test suite for statutory VAT Declaration (DEKLAR.TXT) and cross-ledger audit."""

    def test_declaration_mathematical_reconciliation(self, sample_domestic_invoice, sample_google_reverse_charge_invoice):
        # Generate full package
        pkg = export_nap_package(
            purchase_invoices=[sample_domestic_invoice, sample_google_reverse_charge_invoice],
            period="202608",
        )
        assert pkg["status"] == "success"
        assert len(pkg["consistency_issues"]) == 0

        decl = pkg["declaration"]
        sales_vat = Decimal(decl["sales_section_A"]["cell_20_total_vat_charged"])
        pur_vat = Decimal(decl["purchases_section_B"]["cell_43_total_vat_credit"])
        vat_payable = Decimal(decl["result_section_C"]["cell_50_vat_payable"])
        vat_refundable = Decimal(decl["result_section_C"]["cell_60_vat_refundable"])

        # Google Protocol VAT = 39.12 BGN
        assert sales_vat == Decimal("39.12")
        # Purchase credit = 50.00 (Metro) + 39.12 (Google Protocol) = 89.12 BGN
        assert pur_vat == Decimal("89.12")
        # Net result: Refundable = 89.12 - 39.12 = 50.00 BGN
        assert vat_payable == Decimal("0.00")
        assert vat_refundable == Decimal("50.00")

    def test_deklar_txt_serialization(self):
        decl = generate_vat_declaration(period="202608")
        txt_bytes = decl.to_nap_deklar_txt(encoding="cp1251")
        assert isinstance(txt_bytes, bytes)
        assert b"[01]=" in txt_bytes
        assert b"[20]=" in txt_bytes
        assert b"[40]=" in txt_bytes
        assert b"[50]=" in txt_bytes
        assert b"[60]=" in txt_bytes
        assert b"\r\n" in txt_bytes


# ---------------------------------------------------------------------------
# 5. Complete NAP Package & File Export Tests
# ---------------------------------------------------------------------------

class TestCompleteNapPackage:
    """Test suite for full 3-file package + ZIP archive."""

    def test_export_nap_package_files_and_zip(self, sample_domestic_invoice, sample_google_reverse_charge_invoice, sample_meta_reverse_charge_invoice):
        with tempfile.TemporaryDirectory() as tmp_dir:
            res = export_nap_package(
                purchase_invoices=[
                    sample_domestic_invoice,
                    sample_google_reverse_charge_invoice,
                    sample_meta_reverse_charge_invoice,
                ],
                period="202608",
                output_dir=tmp_dir,
                format="fixed_width",
                encoding="cp1251",
                create_zip=True,
                auto_generate_protocols=True,
            )
            out_p = Path(tmp_dir)
            assert (out_p / "POKUPKI.TXT").exists()
            assert (out_p / "PRODAGBI.TXT").exists()
            assert (out_p / "DEKLAR.TXT").exists()
            assert (out_p / "NAP_202608.zip").exists()

            # Verify ZIP contents
            with zipfile.ZipFile(out_p / "NAP_202608.zip", "r") as zf:
                namelist = zf.namelist()
                assert "POKUPKI.TXT" in namelist
                assert "PRODAGBI.TXT" in namelist
                assert "DEKLAR.TXT" in namelist

            # Verify 2 protocols generated (Google + Meta)
            assert res["protocols_count"] == 2
            assert (out_p / "PROTOCOLS_CHL_117").exists()
            assert len(list((out_p / "PROTOCOLS_CHL_117").glob("*.txt"))) == 2

    def test_export_all_accounting_files_integration(self, sample_domestic_invoice):
        with tempfile.TemporaryDirectory() as tmp_dir:
            exported = export_all_accounting_files(
                invoices=[sample_domestic_invoice],
                output_dir=tmp_dir,
                nap_period="202608",
            )
            assert "pokupki_txt" in exported
            assert "journal_entries_csv" in exported
            assert "nap_prodagbi" in exported
            assert "nap_deklar" in exported
            assert "nap_zip_package" in exported


# ---------------------------------------------------------------------------
# 6. REST API Endpoints Tests
# ---------------------------------------------------------------------------

class TestPillar3RestApiEndpoints:
    """Test suite for FastAPI REST endpoints."""

    def test_api_verify_contractor(self, api_client):
        # 1. Valid Bulgarian entity
        r1 = api_client.post(
            "/api/v1/verify/contractor",
            json={"identifier": "121644736", "country_code": "BG", "date_tax_event": "2026-08-16"},
        )
        assert r1.status_code == 200
        data1 = r1.json()
        assert data1["legal_status"] == "ACTIVE"
        assert data1["vat_status"] == "REGISTERED"
        assert data1["is_valid_for_tax_credit"] is True

        # 2. Bankrupt entity
        r2 = api_client.post(
            "/api/v1/verify/contractor",
            json={"identifier": "111111113", "country_code": "BG", "date_tax_event": "2026-08-16"},
        )
        assert r2.status_code == 200
        data2 = r2.json()
        assert data2["legal_status"] == "BANKRUPTCY"
        assert data2["is_valid_for_tax_credit"] is False

    def test_api_generate_protocol_117(self, api_client):
        inv_payload = {
            "invoice_metadata": {"invoice_number": "AWS-7788", "date_issued": "2026-08-01"},
            "supplier": {"name": "Amazon Web Services EMEA SARL", "vat_number": "LU20260743"},
            "client": {"name": "РМ КАСКАДА 2026 ЕООД", "eik": "208380135"},
            "financial_summary": {"tax_base": {"amount": 100.0, "currency": "EUR"}, "vat_amount": {"amount": 0.0}},
            "line_items": [{"description": "AWS EC2 Hosting", "vat_rate_pct": 0.0}],
        }
        resp = api_client.post(
            "/api/v1/protocol-117/generate",
            json={"invoice": inv_payload, "protocol_number": "0000000009"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["protocol"]["protocol_number"] == "0000000009"
        assert data["protocol"]["tax_base_bgn"] == "195.58"
        assert data["protocol"]["vat_amount_bgn"] == "39.12"
        assert "ПРОТОКОЛ ПО ЧЛ. 117" in data["formatted_document"]

    def test_api_export_prodagbi(self, api_client):
        inv_payload = {
            "invoice_metadata": {"invoice_number": "1001", "date_issued": "2026-08-15"},
            "client": {"name": "КЛИЕНТ 1 ООД", "eik": "111222333"},
            "financial_summary": {"tax_base": {"amount": 500.0}, "vat_amount": {"amount": 100.0}, "total_amount_due": {"amount": 600.0}},
            "line_items": [{"description": "Продажба на стоки", "vat_rate_pct": 20.0}],
        }
        resp = api_client.post(
            "/api/v1/export/prodagbi?format=tsv&encoding=utf-8",
            json=[inv_payload],
        )
        assert resp.status_code == 200
        assert "1001" in resp.text
        assert "500.00" in resp.text

    def test_api_export_deklar(self, api_client):
        pur_inv = {
            "invoice_metadata": {"invoice_number": "5001", "date_issued": "2026-08-15"},
            "supplier": {"name": "ДОСТАВЧИК ООД", "eik": "121644736"},
            "financial_summary": {"tax_base": {"amount": 100.0}, "vat_amount": {"amount": 20.0}, "total_amount_due": {"amount": 120.0}},
            "line_items": [{"description": "Покупка", "vat_rate_pct": 20.0}],
        }
        resp = api_client.post(
            "/api/v1/export/deklar",
            json={"purchase_invoices": [pur_inv], "period": "202608"},
        )
        assert resp.status_code == 200
        assert b"[01]=" in resp.content
        assert b"[20]=" in resp.content

    def test_api_export_nap_package(self, api_client):
        pur_inv = {
            "invoice_metadata": {"invoice_number": "5001", "date_issued": "2026-08-15"},
            "supplier": {"name": "ДОСТАВЧИК ООД", "eik": "121644736"},
            "financial_summary": {"tax_base": {"amount": 100.0}, "vat_amount": {"amount": 20.0}, "total_amount_due": {"amount": 120.0}},
            "line_items": [{"description": "Покупка", "vat_rate_pct": 20.0}],
        }
        # 1. JSON summary
        resp_json = api_client.post(
            "/api/v1/export/nap-package",
            json={"purchase_invoices": [pur_inv], "period": "202608", "as_zip": False},
        )
        assert resp_json.status_code == 200
        assert resp_json.json()["status"] == "success"

        # 2. ZIP download
        resp_zip = api_client.post(
            "/api/v1/export/nap-package",
            json={"purchase_invoices": [pur_inv], "period": "202608", "as_zip": True},
        )
        assert resp_zip.status_code == 200
        assert resp_zip.headers["content-type"] == "application/zip"
        assert len(resp_zip.content) > 500


# ---------------------------------------------------------------------------
# 7. Batch Processing with Pillar 3 Flags Tests
# ---------------------------------------------------------------------------

class TestBatchProcessingPillar3:
    """Test suite for process_batch integration with Pillar 3 export flags."""

    def test_process_batch_with_nap_package_and_contractors(self, monkeypatch):
        # Mock _process_batch_file_worker to avoid running full OCR on real images
        from invoice_ocr import Invoice, InvoiceMetadata, Party, LineItem, FinancialSummary, MoneyAmount
        
        sample_inv1 = Invoice(
            invoice_metadata=InvoiceMetadata(invoice_number="10001", date_issued="2026-08-16", document_type="INVOICE"),
            supplier=Party(name="МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД", eik="121644736", vat_number="BG121644736"),
            recipient=Party(name="РМ КАСКАДА 2026 ЕООД", eik="208380135"),
            line_items=[LineItem(index=1, description="Стока 1", total_price_net=MoneyAmount(Decimal("100.00"), "BGN"), vat_rate_pct=Decimal("20.00"))],
            financial_summary=FinancialSummary(tax_base=MoneyAmount(Decimal("100.00"), "BGN"), vat_amount=MoneyAmount(Decimal("20.00"), "BGN"), total_amount_due=MoneyAmount(Decimal("120.00"), "BGN")),
        )
        sample_inv2 = Invoice(
            invoice_metadata=InvoiceMetadata(invoice_number="GOOGLE-01", date_issued="2026-08-18", document_type="INVOICE"),
            supplier=Party(name="Google Cloud EMEA Limited", vat_number="IE6388047V"),
            recipient=Party(name="РМ КАСКАДА 2026 ЕООД", eik="208380135"),
            line_items=[LineItem(index=1, description="Google Cloud Compute (Reverse Charge)", total_price_net=MoneyAmount(Decimal("50.00"), "EUR"), vat_rate_pct=Decimal("0.00"))],
            financial_summary=FinancialSummary(tax_base=MoneyAmount(Decimal("50.00"), "EUR"), vat_amount=MoneyAmount(Decimal("0.00"), "EUR"), total_amount_due=MoneyAmount(Decimal("50.00"), "EUR")),
        )

        dummy_files = ["inv1.pdf", "inv2.pdf"]

        def mock_worker(wargs):
            idx = wargs["file_idx"]
            inv = sample_inv1 if idx == 0 else sample_inv2
            return {
                "file_idx": idx,
                "file": dummy_files[idx],
                "relative_path": dummy_files[idx],
                "status": "success",
                "invoice": inv,
                "duration_seconds": 0.05,
                "output_file": "out.json",
            }

        monkeypatch.setattr("invoice_ocr._process_batch_file_worker", mock_worker)

        with tempfile.TemporaryDirectory() as tmp_in_dir, tempfile.TemporaryDirectory() as tmp_out_dir:
            in_p = Path(tmp_in_dir)
            out_p = Path(tmp_out_dir)
            # Create 2 dummy files
            (in_p / "inv1.pdf").write_bytes(b"%PDF-dummy")
            (in_p / "inv2.pdf").write_bytes(b"%PDF-dummy")

            summary = process_batch(
                input_dir=in_p,
                output_dir=out_p,
                workers=1,
                export_prodagbi=True,
                export_deklar=True,
                export_nap_package=True,
                verify_contractors=True,
                nap_period="202608",
            )

            assert summary["summary"]["total_documents"] == 2
            assert summary["summary"]["processed_successfully"] == 2

            acc_exports = summary.get("accounting_exports", {})
            assert "prodagbi" in acc_exports
            assert "deklar" in acc_exports
            assert "nap_pkg_zip_package" in acc_exports
            assert Path(acc_exports["prodagbi"]).exists()
            assert Path(acc_exports["deklar"]).exists()
            assert Path(acc_exports["nap_pkg_zip_package"]).exists()

            # Contractor verification audit
            assert "contractor_verifications" in summary
            assert "121644736" in summary["contractor_verifications"]
            metro_ver = summary["contractor_verifications"]["121644736"]
            assert metro_ver["legal_status"] == "ACTIVE"
            assert metro_ver["vat_status"] == "REGISTERED"

            # Check NAP package audit
            assert summary["nap_package_audit"]["status"] == "success"
            assert summary["nap_package_audit"]["protocols_count"] == 1  # Google auto-protocol


# ---------------------------------------------------------------------------
# 8. Statutory Contractor Eligibility & Validation Integration Tests
# ---------------------------------------------------------------------------

class TestContractorValidationIntegration:
    """Test suite verifying statutory contractor checks integrated into validation."""

    def test_validate_contractor_bankrupt_entity(self, sample_domestic_invoice):
        # Change supplier to bankrupt entity 111111113
        sample_domestic_invoice.supplier.eik = "111111113"
        issues = validate_contractor_eligibility(sample_domestic_invoice)
        assert len(issues) > 0
        assert any(i.code == "SUPPLIER_BANKRUPT_OR_INACTIVE" for i in issues)
        assert any(i.severity == "error" for i in issues)

        # Integrated into validate_invoice
        res = validate_invoice(sample_domestic_invoice, tokens=[], verify_contractors=True)
        assert res.is_valid is False
        assert any(e.code == "SUPPLIER_BANKRUPT_OR_INACTIVE" for e in res.errors)

    def test_validate_contractor_deregistered_entity(self, sample_domestic_invoice):
        # Change supplier to deregistered entity 222222226
        sample_domestic_invoice.supplier.eik = "222222226"
        issues = validate_contractor_eligibility(sample_domestic_invoice)
        assert len(issues) > 0
        assert any(i.code in ("SUPPLIER_VAT_DEREGISTERED", "SUPPLIER_BANKRUPT_OR_INACTIVE") for i in issues)

        res = validate_invoice(sample_domestic_invoice, tokens=[], verify_contractors=True)
        assert res.is_valid is False

    def test_validate_contractor_not_vat_registered(self, sample_domestic_invoice):
        # Entity 444444441 has no VAT registration
        sample_domestic_invoice.supplier.eik = "444444441"
        issues = validate_contractor_eligibility(sample_domestic_invoice)
        assert len(issues) > 0
        assert any(i.code == "SUPPLIER_NOT_VAT_REGISTERED" for i in issues)

        res = validate_invoice(sample_domestic_invoice, tokens=[], verify_contractors=True)
        assert res.is_valid is False
        assert any(e.code == "SUPPLIER_NOT_VAT_REGISTERED" for e in res.errors)

    def test_validate_contractor_transaction_before_registration(self, sample_domestic_invoice):
        # Entity 333333339 registered on 2026-08-20; invoice date 2026-08-05
        sample_domestic_invoice.supplier.eik = "333333339"
        sample_domestic_invoice.invoice_metadata.date_tax_event = "2026-08-05"
        sample_domestic_invoice.invoice_metadata.date_issued = "2026-08-05"

        issues = validate_contractor_eligibility(sample_domestic_invoice)
        assert len(issues) > 0
        assert any(i.code == "TAX_CREDIT_DENIED_BEFORE_REGISTRATION" for i in issues)

        res = validate_invoice(sample_domestic_invoice, tokens=[], verify_contractors=True)
        assert res.is_valid is False
        assert any(e.code == "TAX_CREDIT_DENIED_BEFORE_REGISTRATION" for e in res.errors)

    def test_validate_contractor_active_valid(self, sample_domestic_invoice):
        # Metro Cash & Carry (EIK 121644736) is active and registered
        issues = validate_contractor_eligibility(sample_domestic_invoice)
        assert len(issues) == 0

        res = validate_invoice(sample_domestic_invoice, tokens=[], verify_contractors=True)
        assert res.is_valid is True
        assert len(res.errors) == 0


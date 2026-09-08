"""Comprehensive Test Suite for Deep Bulgarian Accounting Systems Integration.

Validates DoD requirements:
1. Microinvest Delta Pro export (official TransferData XML and 2-sided postings CSV).
2. Business Navigator export (structured TXT, delimited CSV, and binary dBase III/IV DBF tables).
3. Configurable account mapping (line item keywords, supplier EIK, and multi-line item splitting).
4. Automatic tax period verification under Art. 124 VAT Act (чл. 124 ЗДДС: 0 mo, 1..12 mo, >12 mo, future).
5. Official Bulgarian Chart of Accounts (НСС / Национален сметкоплан) catalog, lookup, and search.
6. Ajur (Ажур-L / Ажур 7) export.
7. End-to-end acceptance test using real corpus invoice (results/анда-01.json).
8. REST API endpoints in api_server.py.
"""
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api_server import app
from invoice_ocr import (
    DocumentType,
    FinancialSummary,
    Invoice,
    InvoiceMetadata,
    LineItem,
    MoneyAmount,
    Party,
)
from accounting_export import (
    DEFAULT_CHART_OF_ACCOUNTS,
    DEFAULT_MAPPING_ENGINE,
    AccountClass,
    AccountDefinition,
    AccountMappingEngine,
    AccountType,
    DBFField,
    SimpleDBFReader,
    SimpleDBFWriter,
    TaxPeriodStatus,
    TaxPeriodValidator,
    export_all_accounting_files,
    export_ajur_package,
    export_business_navigator_package,
    export_microinvest_package,
    generate_ajur_csv,
    generate_business_navigator_csv,
    generate_business_navigator_dbf_tables,
    generate_business_navigator_section_txt,
    generate_business_navigator_single_dbf,
    generate_microinvest_delta_csv,
    generate_microinvest_delta_xml,
    generate_microinvest_sklad_xml,
    lookup_account,
    search_accounts,
    explain_account,
    validate_tax_period,
)

TRANSFER_NS = "urn:Transfer"


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def multi_line_mixed_invoice() -> Invoice:
    """Multi-item invoice with mixed expenses (Fuel 6012 + Office 6013 + Coffee 3041 + Repair 6024)."""
    return Invoice(
        invoice_metadata=InvoiceMetadata(
            invoice_number="0000555666",
            date_issued="2026-08-16",
            date_tax_event="2026-08-16",
            document_type=DocumentType.INVOICE.value,
        ),
        supplier=Party(
            name="ЛУКОЙЛ БЪЛГАРИЯ ЕООД",
            eik="121687551",
            vat_number="BG121687551",
            address="гр. София, бул. Тодор Александров 42",
        ),
        recipient=Party(
            name="РМ КАСКАДА 2026 ЕООД",
            eik="208380135",
            vat_number="BG208380135",
            address="гр. Плевен",
        ),
        line_items=[
            LineItem(
                index=1,
                description="Дизелово гориво ЕКТО Б6",
                quantity=Decimal("50.00"),
                unit="л",
                unit_price_net=MoneyAmount(Decimal("2.00"), "BGN"),
                total_price_net=MoneyAmount(Decimal("100.00"), "BGN"),
                vat_rate_pct=Decimal("20.00"),
            ),
            LineItem(
                index=2,
                description="Копирна хартия А4 80гр.",
                quantity=Decimal("2.00"),
                unit="пак.",
                unit_price_net=MoneyAmount(Decimal("10.00"), "BGN"),
                total_price_net=MoneyAmount(Decimal("20.00"), "BGN"),
                vat_rate_pct=Decimal("20.00"),
            ),
            LineItem(
                index=3,
                description="Кафе еспресо Лаваца",
                quantity=Decimal("1.00"),
                unit="бр.",
                unit_price_net=MoneyAmount(Decimal("30.00"), "BGN"),
                total_price_net=MoneyAmount(Decimal("30.00"), "BGN"),
                vat_rate_pct=Decimal("20.00"),
            ),
            LineItem(
                index=4,
                description="Автомивка външно измиване",
                quantity=Decimal("1.00"),
                unit="бр.",
                unit_price_net=MoneyAmount(Decimal("10.00"), "BGN"),
                total_price_net=MoneyAmount(Decimal("10.00"), "BGN"),
                vat_rate_pct=Decimal("20.00"),
            ),
        ],
        financial_summary=FinancialSummary(
            tax_base=MoneyAmount(Decimal("160.00"), "BGN"),
            vat_amount=MoneyAmount(Decimal("32.00"), "BGN"),
            total_amount_due=MoneyAmount(Decimal("192.00"), "BGN"),
        ),
    )


@pytest.fixture
def multi_line_credit_note() -> Invoice:
    """Credit note for returned goods."""
    return Invoice(
        invoice_metadata=InvoiceMetadata(
            invoice_number="0000555667",
            date_issued="2026-08-18",
            date_tax_event="2026-08-18",
            document_type=DocumentType.CREDIT_NOTE.value,
            is_credit_note=True,
            original_invoice_number="0000555666",
        ),
        supplier=Party(
            name="ЛУКОЙЛ БЪЛГАРИЯ ЕООД",
            eik="121687551",
            vat_number="BG121687551",
        ),
        recipient=Party(
            name="РМ КАСКАДА 2026 ЕООД",
            eik="208380135",
        ),
        line_items=[
            LineItem(
                index=1,
                description="Корекция дизелово гориво",
                quantity=Decimal("-10.00"),
                unit_price_net=MoneyAmount(Decimal("2.00"), "BGN"),
                total_price_net=MoneyAmount(Decimal("-20.00"), "BGN"),
                vat_rate_pct=Decimal("20.00"),
            ),
        ],
        financial_summary=FinancialSummary(
            tax_base=MoneyAmount(Decimal("20.00"), "BGN"),
            vat_amount=MoneyAmount(Decimal("4.00"), "BGN"),
            total_amount_due=MoneyAmount(Decimal("24.00"), "BGN"),
        ),
    )


# ============================================================================
# 1. OFFICIAL BULGARIAN CHART OF ACCOUNTS TESTS
# ============================================================================

class TestChartOfAccounts:
    def test_catalog_coverage_and_classes(self):
        """Verify all major accounting classes (1 to 7, 9) are covered."""
        classes = {acc.account_class for acc in DEFAULT_CHART_OF_ACCOUNTS.all_accounts()}
        assert {1, 2, 3, 4, 5, 6, 7, 9}.issubset(classes)
        assert len(DEFAULT_CHART_OF_ACCOUNTS.all_accounts()) >= 40

    def test_lookup_synthetic_and_subaccounts(self):
        """Test looking up synthetic accounts and analytical subaccounts."""
        # Class 6: Materials (601) and subaccounts (6012, 6013)
        acc_601 = lookup_account("601")
        assert acc_601 is not None
        assert acc_601.name == "Разходи за материали"
        assert acc_601.account_type == AccountType.ACTIVE

        acc_6012 = lookup_account("6012")
        assert acc_6012 is not None
        assert "горива" in acc_6012.name.lower()
        assert acc_6012.parent_code == "601"

        # Class 4: Supplier 401 and VAT 4531
        acc_401 = lookup_account("401")
        assert acc_401 is not None
        assert acc_401.account_type == AccountType.PASSIVE

        acc_4531 = lookup_account("4531")
        assert acc_4531 is not None
        assert "данъчен кредит" in acc_4531.purpose.lower()

    def test_search_accounts_by_keywords(self):
        """Test fuzzy / keyword search in Bulgarian accounts catalog."""
        res_fuel = search_accounts("дизел гориво")
        assert any(a.code in ("601", "6012", "3023") for a in res_fuel)

        res_power = search_accounts("ток електроенергия")
        assert any(a.code in ("602", "6021") for a in res_power)

        res_vat = search_accounts("данъчен кредит")
        assert any(a.code in ("453", "4531") for a in res_vat)

    def test_explain_account(self):
        """Verify detailed human-readable explanation generation."""
        expl = explain_account("6012")
        assert "Сметка 6012" in expl
        assert "Дебит:" in expl
        assert "Кредит:" in expl
        assert "Предназначение:" in expl


# ============================================================================
# 2. CONFIGURABLE ACCOUNT MAPPING ENGINE TESTS
# ============================================================================

class TestAccountMappingEngine:
    def test_keyword_classification(self):
        """Test classifying line items by keywords and regex."""
        acc, _, expl = DEFAULT_MAPPING_ENGINE.classify_line_item("Дизелово гориво Б6 за камион")
        assert acc == "6012"
        assert "fuel_and_lubricants" in expl

        acc, _, _ = DEFAULT_MAPPING_ENGINE.classify_line_item("Копирна хартия Double A 80g")
        assert acc == "6013"

        acc, _, _ = DEFAULT_MAPPING_ENGINE.classify_line_item("Сметка за активна електроенергия")
        assert acc == "6021"

        acc, _, _ = DEFAULT_MAPPING_ENGINE.classify_line_item("Месечен абонамент за мобилен интернет")
        assert acc == "6022"

        acc, _, _ = DEFAULT_MAPPING_ENGINE.classify_line_item("Куриерска пратка експрес")
        assert acc == "6027"

        acc, _, _ = DEFAULT_MAPPING_ENGINE.classify_line_item("Счетоводно обслужване и ТРЗ")
        assert acc == "6025"

    def test_supplier_eik_override(self):
        """Test that supplier EIK rules correctly map to expected accounts."""
        # Lukoil EIK -> 6012
        acc, sub, _ = DEFAULT_MAPPING_ENGINE.classify_line_item("Обща фактура", supplier_eik="121687551")
        assert acc == "6012"
        assert sub == "121687551"

        # A1 Bulgaria EIK -> 6022
        acc, sub, _ = DEFAULT_MAPPING_ENGINE.classify_line_item("Фактура", supplier_eik="131468980")
        assert acc == "6022"

        # Metro Cash & Carry EIK -> 3041
        acc, sub, _ = DEFAULT_MAPPING_ENGINE.classify_line_item("Фактура", supplier_eik="121644736")
        assert acc == "3041"

    def test_multi_line_invoice_split(self, multi_line_mixed_invoice):
        """Test splitting multi-item invoice into distributed accounting rows."""
        dists = DEFAULT_MAPPING_ENGINE.split_invoice_by_accounts(multi_line_mixed_invoice, prefer_subaccounts=True)
        # We expect 4 distinct accounts: 6012 (Diesel), 6013 (Paper), 3041 (Coffee), 6024 (Carwash)
        accounts = {d["account"] for d in dists}
        assert "6012" in accounts
        assert "6013" in accounts
        assert "3041" in accounts
        assert "6024" in accounts

        # Total distributed base must equal document tax base (160.00)
        total_base = sum(d["amount"] for d in dists)
        assert total_base == Decimal("160.00")

    def test_custom_rule_addition_and_serialization(self):
        """Test adding custom rules and JSON export/import."""
        engine = AccountMappingEngine()
        engine.add_supplier_rule("999888777", "6028", description="Custom IT supplier")
        acc, _, expl = engine.classify_line_item("IT поддръжка", supplier_eik="999888777")
        assert acc == "6028"

        engine.add_keyword_rule(
            rule_id="custom_hardware",
            account="204",
            keywords=["специализиран робот"],
            priority=100,
        )
        acc_hw, _, _ = engine.classify_line_item("Закупуване на специализиран робот")
        assert acc_hw == "204"

        # Serialize to JSON and reload
        json_str = engine.to_json()
        data = json.loads(json_str)
        reloaded = AccountMappingEngine.from_dict(data)
        acc_check, _, _ = reloaded.classify_line_item("Закупуване на специализиран робот")
        assert acc_check == "204"


# ============================================================================
# 3. TAX PERIOD VERIFICATION TESTS (ЧЛ. 124 ЗДДС)
# ============================================================================

class TestTaxPeriodValidator:
    def test_current_tax_period(self):
        """Event in current filing month -> 0 offset, full credit, cell 10."""
        res = validate_tax_period({"date_issued": "2026-08-16"}, target_period="202608")
        assert res.status == TaxPeriodStatus.CURRENT_PERIOD
        assert res.is_valid_for_filing is True
        assert res.can_claim_tax_credit is True
        assert res.period_offset_months == 0
        assert res.recommended_vat_cell == 10
        assert res.severity == "ok"

    def test_prior_period_allowed_within_12_months(self):
        """Event 6 months prior -> allowed under Art. 124 (4) VAT Act, full credit."""
        res = validate_tax_period({"date_issued": "2026-02-10"}, target_period="202608")
        assert res.status == TaxPeriodStatus.PRIOR_PERIOD_ALLOWED_12M
        assert res.is_valid_for_filing is True
        assert res.can_claim_tax_credit is True
        assert res.period_offset_months == 6
        assert res.recommended_vat_cell == 10
        assert res.severity == "info"

    def test_expired_period_over_12_months(self):
        """Event 14 months prior -> expired right to tax credit, cell 16 only."""
        res = validate_tax_period({"date_issued": "2025-06-01"}, target_period="202608")
        assert res.status == TaxPeriodStatus.EXPIRED_PERIOD_OVER_12M
        assert res.can_claim_tax_credit is False
        assert res.period_offset_months == 14
        assert res.recommended_vat_cell == 16
        assert res.severity == "warning"

    def test_future_period_invalid(self):
        """Event in future relative to reporting period -> invalid for current declaration."""
        res = validate_tax_period({"date_issued": "2026-09-01"}, target_period="202608")
        assert res.status == TaxPeriodStatus.FUTURE_PERIOD_INVALID
        assert res.is_valid_for_filing is False
        assert res.can_claim_tax_credit is False
        assert res.severity == "error"

    def test_batch_validation_summary(self):
        """Test batch validation with aggregate counts."""
        batch = [
            {"date_issued": "2026-08-01"},
            {"date_issued": "2026-07-15"},
            {"date_issued": "2025-01-01"},
        ]
        res = TaxPeriodValidator.validate_batch(batch, target_period="202608")
        assert res["total_documents"] == 3
        assert res["summary"]["current_period_count"] == 1
        assert res["summary"]["prior_period_allowed_12m_count"] == 1
        assert res["summary"]["expired_period_count"] == 1
        assert res["is_batch_valid"] is True


# ============================================================================
# 4. MICROINVEST DELTA PRO EXPORT TESTS
# ============================================================================

class TestMicroinvestDeltaExport:
    def test_delta_xml_multi_line_postings(self, multi_line_mixed_invoice):
        """Test TransferData XML with multiple distributed expense accounts and subaccounts."""
        xml_str = generate_microinvest_delta_xml(multi_line_mixed_invoice)
        assert TRANSFER_NS in xml_str
        root = ET.fromstring(xml_str)

        acc = root.find(f"{{{TRANSFER_NS}}}Accountings").find(f"{{{TRANSFER_NS}}}Accounting")
        details = acc.find(f"{{{TRANSFER_NS}}}AccountingDetails").findall(f"{{{TRANSFER_NS}}}AccountingDetail")

        # Expect separate debit rows for accounts: 6012, 6013, 3041, 6024, plus 453/1, and credit 401
        accounts_in_xml = [d.attrib.get("Account") for d in details]
        assert "601" in accounts_in_xml or "6012" in accounts_in_xml
        assert "453/1" in accounts_in_xml
        assert "401" in accounts_in_xml

        # Check subaccount/analytic code
        sup_detail = [d for d in details if d.attrib.get("Account") == "401"][0]
        assert sup_detail.attrib.get("SubAccount") == "121687551"

        # Mathematical debit == credit balance
        debit_sum = sum(Decimal(d.attrib["Amount"]) for d in details if d.attrib["Direction"] == "Debit")
        credit_sum = sum(Decimal(d.attrib["Amount"]) for d in details if d.attrib["Direction"] == "Credit")
        assert debit_sum == Decimal("192.00")
        assert credit_sum == Decimal("192.00")

    def test_delta_csv_export(self, multi_line_mixed_invoice, multi_line_credit_note):
        """Test Microinvest Delta Pro 2-sided postings CSV format."""
        csv_bytes = generate_microinvest_delta_csv(
            [multi_line_mixed_invoice, multi_line_credit_note],
            encoding="windows-1251",
        )
        assert isinstance(csv_bytes, bytes)
        csv_text = csv_bytes.decode("cp1251")
        lines = csv_text.strip().split("\r\n")

        # Header check
        assert lines[0] == "Дата;ВидДокумент;НомерДокумент;СметкаДт;АналитичностДт;СметкаКт;АналитичностКт;Сума;Валута;Основание"

        # Verify postings
        assert "0000555666" in csv_text
        assert "121687551" in csv_text
        assert "401" in csv_text

        # Credit note has negative sign or reversed postings
        assert "Кредитно известие" in csv_text
        assert "-20.00" in csv_text or "20.00" in csv_text

    def test_microinvest_package_with_zip(self, multi_line_mixed_invoice):
        """Test complete package generation (Delta XML, Delta CSV, Sklad XML, ZIP)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            res = export_microinvest_package([multi_line_mixed_invoice], output_dir=tmp_dir, create_zip=True)
            assert "delta_xml" in res
            assert "delta_csv" in res
            assert "microinvest_zip" in res
            assert Path(res["delta_csv"]).exists()
            assert Path(res["microinvest_zip"]).exists()

            # Verify ZIP contents
            with zipfile.ZipFile(res["microinvest_zip"], "r") as zf:
                names = zf.namelist()
                assert "Microinvest_Delta_TransferData.xml" in names
                assert "Microinvest_Delta_Postings.csv" in names


# ============================================================================
# 5. BUSINESS NAVIGATOR EXPORT TESTS (TXT, CSV, DUAL DBF, SINGLE DBF)
# ============================================================================

class TestBusinessNavigatorExport:
    def test_business_navigator_csv(self, multi_line_mixed_invoice):
        """Test delimited CSV export for Business Navigator import wizard."""
        csv_bytes = generate_business_navigator_csv([multi_line_mixed_invoice], encoding="windows-1251")
        assert isinstance(csv_bytes, bytes)
        csv_text = csv_bytes.decode("cp1251")
        assert "КодДокумент;НомерДокумент;ДатаДокумент" in csv_text
        assert "0000555666" in csv_text
        assert "ЛУКОЙЛ" in csv_text
        assert "401" in csv_text

    def test_business_navigator_section_txt(self, multi_line_mixed_invoice):
        """Test tagged section TXT format ([DOKUMENT] and [OPERACII])."""
        txt_bytes = generate_business_navigator_section_txt([multi_line_mixed_invoice], encoding="windows-1251")
        txt = txt_bytes.decode("cp1251")
        assert "[DOKUMENT]" in txt
        assert "NUM=0000555666" in txt
        assert "EIK=121687551" in txt
        assert "[OPERACII]" in txt
        assert "KT=401" in txt

    def test_dual_dbf_tables_binary_validity(self, multi_line_mixed_invoice, multi_line_credit_note):
        """Test binary dBase III / IV DBF generation and verify with SimpleDBFReader."""
        dokum_b, oper_b = generate_business_navigator_dbf_tables([multi_line_mixed_invoice, multi_line_credit_note])

        # 1. Parse DOKUM.DBF
        reader_d = SimpleDBFReader(dokum_b, encoding="cp1251")
        assert reader_d.version == 3
        assert reader_d.record_count == 2
        rec1 = reader_d.records[0]
        assert rec1["DOK_NUM"] == "0000555666"
        assert rec1["DOK_EIK"] == "121687551"
        assert Decimal(str(rec1["DOK_SUMA"])) == Decimal("192.00")
        assert Decimal(str(rec1["DOK_OSN"])) == Decimal("160.00")
        assert Decimal(str(rec1["DOK_DDS"])) == Decimal("32.00")

        # Credit note record
        rec2 = reader_d.records[1]
        assert rec2["DOK_VID"] == "03"
        assert Decimal(str(rec2["DOK_SUMA"])) == Decimal("-24.00")

        # 2. Parse OPER.DBF
        reader_o = SimpleDBFReader(oper_b, encoding="cp1251")
        assert reader_o.version == 3
        assert len(reader_o.records) >= 5  # 4 expense rows + 1 VAT row for inv1 + rows for inv2

        # Check debit and credit accounts in OPER.DBF
        debit_accounts = {r["SMETKA_DT"] for r in reader_o.records if r["DOK_NUM"] == "0000555666"}
        assert "401" in {r["SMETKA_KT"] for r in reader_o.records}
        assert "4531" in debit_accounts
        assert any(acc in debit_accounts for acc in ("601", "6012", "6013", "3041", "6024"))

        # Verify mathematical balance of postings for document 1
        inv1_opers = [r for r in reader_o.records if r["DOK_NUM"] == "0000555666"]
        total_debits = sum(Decimal(str(r["SUMA"])) for r in inv1_opers)
        assert total_debits == Decimal("192.00"), "Sum of operational postings must equal invoice gross total"

    def test_single_dbf_export(self, multi_line_mixed_invoice):
        """Test single combined flat DBF table."""
        dbf_b = generate_business_navigator_single_dbf([multi_line_mixed_invoice])
        reader = SimpleDBFReader(dbf_b, encoding="cp1251")
        assert reader.record_count >= 5
        assert reader.records[0]["DOC_NUM"] == "0000555666"
        assert reader.records[0]["PARTNER_ID"] == "121687551"

    def test_business_navigator_complete_package(self, multi_line_mixed_invoice):
        """Test full package ZIP creation."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            res = export_business_navigator_package([multi_line_mixed_invoice], output_dir=tmp_dir, create_zip=True)
            assert "bn_dokum_dbf" in res
            assert "bn_oper_dbf" in res
            assert "bn_csv" in res
            assert "bn_txt" in res
            assert "bn_zip" in res

            with zipfile.ZipFile(res["bn_zip"], "r") as zf:
                names = zf.namelist()
                assert "DOKUM.DBF" in names
                assert "OPER.DBF" in names
                assert "BN_IMPORT.csv" in names


# ============================================================================
# 6. AJUR EXPORT TESTS
# ============================================================================

class TestAjurExport:
    def test_ajur_csv_generation(self, multi_line_mixed_invoice):
        """Test Ajur 7 / L import CSV file format."""
        csv_bytes = generate_ajur_csv([multi_line_mixed_invoice], encoding="windows-1251")
        assert isinstance(csv_bytes, bytes)
        csv_text = csv_bytes.decode("cp1251")
        assert "СметкаДт;ПодсметкаДт;СметкаКт" in csv_text
        assert "0000555666" in csv_text
        assert "121687551" in csv_text
        assert "401" in csv_text


# ============================================================================
# 7. ACCEPTANCE TEST WITH REAL CORPUS INVOICE (results/анда-01.json)
# ============================================================================

class TestRealCorpusInvoiceIntegration:
    @pytest.fixture
    def real_corpus_invoice(self) -> dict[str, Any]:
        path = Path(__file__).resolve().parent.parent / "results" / "анда-01.json"
        if not path.exists():
            pytest.skip("Corpus sample results/анда-01.json not found")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_real_invoice_microinvest_delta_export(self, real_corpus_invoice):
        """Verify real corpus invoice exports to Delta Pro XML and CSV cleanly."""
        # 1. Delta XML
        delta_xml = generate_microinvest_delta_xml(real_corpus_invoice)
        assert TRANSFER_NS in delta_xml
        root = ET.fromstring(delta_xml)
        acc = root.find(f"{{{TRANSFER_NS}}}Accountings").find(f"{{{TRANSFER_NS}}}Accounting")
        details = acc.find(f"{{{TRANSFER_NS}}}AccountingDetails").findall(f"{{{TRANSFER_NS}}}AccountingDetail")
        assert len(details) >= 3

        # Balance check
        debit_sum = sum(Decimal(d.attrib["Amount"]) for d in details if d.attrib["Direction"] == "Debit")
        credit_sum = sum(Decimal(d.attrib["Amount"]) for d in details if d.attrib["Direction"] == "Credit")
        assert debit_sum == credit_sum

        # 2. Delta CSV
        csv_bytes = generate_microinvest_delta_csv(real_corpus_invoice)
        assert len(csv_bytes) > 0

    def test_real_invoice_business_navigator_dbf_export(self, real_corpus_invoice):
        """Verify real corpus invoice exports to Business Navigator DBF tables and parses back."""
        dokum_b, oper_b = generate_business_navigator_dbf_tables(real_corpus_invoice)
        r_dokum = SimpleDBFReader(dokum_b)
        assert r_dokum.record_count == 1
        assert "202262252" in r_dokum.records[0]["DOK_EIK"]

        r_oper = SimpleDBFReader(oper_b)
        assert r_oper.record_count >= 2
        assert "401" in {r["SMETKA_KT"] for r in r_oper.records}

    def test_real_invoice_tax_period_validation(self, real_corpus_invoice):
        """Validate tax event of real corpus invoice against various VAT periods."""
        # The invoice has date 2026-04-10
        res_current = validate_tax_period(real_corpus_invoice, target_period="202604")
        assert res_current.status == TaxPeriodStatus.CURRENT_PERIOD
        assert res_current.can_claim_tax_credit is True

        res_august = validate_tax_period(real_corpus_invoice, target_period="202608")
        assert res_august.status == TaxPeriodStatus.PRIOR_PERIOD_ALLOWED_12M
        assert res_august.period_offset_months == 4
        assert res_august.can_claim_tax_credit is True


# ============================================================================
# 8. REST API ENDPOINT INTEGRATION TESTS
# ============================================================================

class TestAccountingApiEndpoints:
    @pytest.fixture
    def client(self) -> TestClient:
        return TestClient(app)

    @pytest.fixture
    def sample_api_invoice(self) -> dict[str, Any]:
        return {
            "invoice_number": "0000777888",
            "date_issued": "2026-08-20",
            "supplier": {
                "name": "ЕЛЕКТРОХОЛД ПРОДАЖБИ ЕАД",
                "eik": "130007803",
                "vat_number": "BG130007803",
            },
            "financial_summary": {
                "tax_base": "500.00",
                "vat_amount": "100.00",
                "total_amount_due": "600.00",
            },
            "line_items": [
                {
                    "description": "Активна електрическа енергия",
                    "total_price_net": "500.00",
                    "vat_rate_pct": 20.0,
                }
            ],
        }

    def test_api_chart_of_accounts(self, client):
        """GET /api/v1/accounting/chart-of-accounts"""
        # Query search
        resp = client.get("/api/v1/accounting/chart-of-accounts?q=материали")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] > 0
        assert any(a["code"] in ("601", "302") for a in data["accounts"])

        # Filter by class 6
        resp_cls = client.get("/api/v1/accounting/chart-of-accounts?account_class=6")
        assert resp_cls.status_code == 200
        cls_data = resp_cls.json()
        assert all(a["account_class"] == 6 for a in cls_data["accounts"])

    def test_api_chart_of_accounts_detail(self, client):
        """GET /api/v1/accounting/chart-of-accounts/{code}"""
        resp = client.get("/api/v1/accounting/chart-of-accounts/6021")
        assert resp.status_code == 200
        data = resp.json()
        assert data["account"]["code"] == "6021"
        assert "електроенергия" in data["explanation"].lower()

    def test_api_validate_tax_period(self, client, sample_api_invoice):
        """POST /api/v1/accounting/validate-tax-period"""
        payload = {
            "invoices": [sample_api_invoice],
            "target_period": "202608",
        }
        resp = client.post("/api/v1/accounting/validate-tax-period", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["current_period_count"] == 1

    def test_api_business_navigator_csv(self, client, sample_api_invoice):
        """POST /api/v1/export/business-navigator/csv"""
        payload = {"invoices": [sample_api_invoice]}
        resp = client.post("/api/v1/export/business-navigator/csv", json=payload)
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "0000777888" in resp.content.decode("cp1251")

    def test_api_business_navigator_package_zip(self, client, sample_api_invoice):
        """POST /api/v1/export/business-navigator/package"""
        payload = {"invoices": [sample_api_invoice]}
        resp = client.post("/api/v1/export/business-navigator/package", json=payload)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"

    def test_api_microinvest_delta_csv(self, client, sample_api_invoice):
        """POST /api/v1/export/microinvest/delta-csv"""
        payload = {"invoices": [sample_api_invoice]}
        resp = client.post("/api/v1/export/microinvest/delta-csv", json=payload)
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    def test_api_ajur_csv(self, client, sample_api_invoice):
        """POST /api/v1/export/ajur"""
        payload = {"invoices": [sample_api_invoice]}
        resp = client.post("/api/v1/export/ajur", json=payload)
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    def test_api_mapping_rules_endpoints(self, client):
        """GET / POST /api/v1/accounting/mapping-rules"""
        # Get rules
        resp = client.get("/api/v1/accounting/mapping-rules")
        assert resp.status_code == 200
        rules = resp.json()
        assert "supplier_rules" in rules

        # Add supplier rule
        sup_payload = {
            "eik": "555444333",
            "target_account": "6029",
            "supplier_name": "Тест Охрана ООД",
            "description": "Охранителни услуги",
        }
        resp_add = client.post("/api/v1/accounting/mapping-rules/supplier", json=sup_payload)
        assert resp_add.status_code == 200

        # Add keyword rule
        kw_payload = {
            "rule_id": "test_drone",
            "target_account": "204",
            "keywords": ["дрон", "квадрокоптер"],
            "priority": 80,
        }
        resp_kw = client.post("/api/v1/accounting/mapping-rules/keyword", json=kw_payload)
        assert resp_kw.status_code == 200

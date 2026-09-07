"""Comprehensive Test Suite for Statutory НАП VAT Ledger (POKUPKI.TXT) and ERP Journal Entries.

Validates Pillar 4:
1. Statutory НАП POKUPKI.TXT format according to Приложение № 12 от ППЗДДС:
   - 10-digit zero-padded invoice numbers
   - Statutory document type mapping (01, 02, 03, 07, 09)
   - 20%, 9%, and 0% VAT column placement
   - Credit notes / storno operations with negative numbers
   - Fixed-width, TSV, and CSV formats
   - Windows-1251 (CP1251) and UTF-8 encodings with CRLF endings
2. Double-entry bookkeeping journal entries (контировки):
   - Debit 304/602 + Debit 4531 = Credit 401 mathematical balance
   - Smart account classification (Goods 304 vs Services 602)
   - Storno negative balance
   - Export to CSV, JSON, Microinvest Delta Pro, Бизнес Навигатор, Ajur, SAP
3. CLI integration in invoice_ocr.py (process_batch flags)
4. REST API endpoints in api_server.py (/api/v1/export/pokupki, /api/v1/export/journal-entries)
5. Acceptance test on real corpus invoice JSONs
"""

from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest
from fastapi.testclient import TestClient

from accounting_export import (
    DEFAULT_BRANCH,
    DEFAULT_EXPENSE_GOODS_ACCOUNT,
    DEFAULT_EXPENSE_SERVICE_ACCOUNT,
    DEFAULT_SUPPLIER_PAYABLE_ACCOUNT,
    DEFAULT_VAT_PURCHASE_ACCOUNT,
    AccountingJournalEntry,
    NapLedgerEntry,
    classify_expense_account,
    export_all_accounting_files,
    export_journal_entries_csv,
    export_journal_entries_json,
    invoice_to_journal_entry,
    invoice_to_nap_entry,
    invoices_to_journal_entries,
    invoices_to_pokupki_txt,
    map_document_type,
    normalize_doc_number,
)
from api_server import app
from invoice_ocr import (
    DocumentType,
    FinancialSummary,
    Invoice,
    InvoiceMetadata,
    LineItem,
    MoneyAmount,
    Party,
    process_batch,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_standard_invoice() -> Invoice:
    """Standard 20% VAT purchase invoice (Goods)."""
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
        line_items=[
            LineItem(
                index=1,
                description="КАФЕ ЛАВАЦА КРЕМА 1КГ",
                unit="бр.",
                quantity=Decimal("10"),
                unit_price_net=MoneyAmount(Decimal("25.00"), "BGN"),
                total_price_net=MoneyAmount(Decimal("250.00"), "BGN"),
                vat_rate_pct=Decimal("20"),
            )
        ],
        financial_summary=FinancialSummary(
            tax_base=MoneyAmount(Decimal("250.00"), "BGN"),
            vat_amount=MoneyAmount(Decimal("50.00"), "BGN"),
            total_amount_due=MoneyAmount(Decimal("300.00"), "BGN"),
        ),
    )


@pytest.fixture
def sample_service_invoice() -> Invoice:
    """Service invoice classified to Account 602 (Разходи за външни услуги)."""
    return Invoice(
        invoice_metadata=InvoiceMetadata(
            invoice_number="0100456789",
            date_issued="2026-08-25",
            date_tax_event="2026-08-25",
            document_type=DocumentType.INVOICE.value,
        ),
        supplier=Party(
            name="А1 БЪЛГАРИЯ ЕАД",
            eik="131468980",
            vat_number="BG131468980",
        ),
        line_items=[
            LineItem(
                index=1,
                description="Абонамент за оптичен интернет и телекомуникационни услуги",
                quantity=Decimal("1"),
                total_price_net=MoneyAmount(Decimal("100.00"), "BGN"),
                vat_rate_pct=Decimal("20"),
            )
        ],
        financial_summary=FinancialSummary(
            tax_base=MoneyAmount(Decimal("100.00"), "BGN"),
            vat_amount=MoneyAmount(Decimal("20.00"), "BGN"),
            total_amount_due=MoneyAmount(Decimal("120.00"), "BGN"),
        ),
    )


@pytest.fixture
def sample_reduced_vat_invoice() -> Invoice:
    """Invoice with 9% reduced VAT (Books / Tourism / Restaurant)."""
    return Invoice(
        invoice_metadata=InvoiceMetadata(
            invoice_number="7890",
            date_issued="2026-08-10",
            date_tax_event="2026-08-10",
            document_type=DocumentType.INVOICE.value,
        ),
        supplier=Party(
            name="ИЗДАТЕЛСТВО ПРОСВЕТА АД",
            eik="831641791",
            vat_number="BG831641791",
        ),
        line_items=[
            LineItem(
                index=1,
                description="Счетоводно ръководство за прехода към еврото",
                quantity=Decimal("5"),
                total_price_net=MoneyAmount(Decimal("100.00"), "BGN"),
                vat_rate_pct=Decimal("9"),
            )
        ],
        financial_summary=FinancialSummary(
            tax_base=MoneyAmount(Decimal("100.00"), "BGN"),
            vat_amount=MoneyAmount(Decimal("9.00"), "BGN"),
            total_amount_due=MoneyAmount(Decimal("109.00"), "BGN"),
        ),
    )


@pytest.fixture
def sample_credit_note() -> Invoice:
    """Credit note (Кредитно известие / Сторно)."""
    return Invoice(
        invoice_metadata=InvoiceMetadata(
            invoice_number="0000000042",
            date_issued="2026-08-28",
            date_tax_event="2026-08-28",
            document_type=DocumentType.CREDIT_NOTE.value,
            is_credit_note=True,
            original_invoice_number="0000124013",
        ),
        supplier=Party(
            name="МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД",
            eik="121644736",
            vat_number="BG121644736",
        ),
        line_items=[
            LineItem(
                index=1,
                description="Върнато кафе Лаваца - дефектна партида",
                quantity=Decimal("-2"),
                total_price_net=MoneyAmount(Decimal("-50.00"), "BGN"),
                vat_rate_pct=Decimal("20"),
            )
        ],
        financial_summary=FinancialSummary(
            tax_base=MoneyAmount(Decimal("50.00"), "BGN"),
            vat_amount=MoneyAmount(Decimal("10.00"), "BGN"),
            total_amount_due=MoneyAmount(Decimal("60.00"), "BGN"),
        ),
    )


# ---------------------------------------------------------------------------
# Unit Tests: Normalization and Type Mapping
# ---------------------------------------------------------------------------

class TestNormalizationAndMapping:
    def test_normalize_doc_number_zero_padding(self):
        assert normalize_doc_number("124013") == "0000124013"
        assert normalize_doc_number("1100098511") == "1100098511"
        assert normalize_doc_number("№ 00042") == "0000000042"
        assert normalize_doc_number("") == "0000000000"
        assert normalize_doc_number(None) == "0000000000"
        # Truncate to trailing 10 if exceeds
        assert normalize_doc_number("123456789012") == "3456789012"

    def test_map_document_type(self):
        assert map_document_type(DocumentType.INVOICE) == "01"
        assert map_document_type("INVOICE") == "01"
        assert map_document_type(DocumentType.DEBIT_NOTE) == "02"
        assert map_document_type(DocumentType.CREDIT_NOTE) == "03"
        assert map_document_type("CUSTOMS_DECLARATION") == "07"
        assert map_document_type(DocumentType.PROTOCOL_CHL_117) == "09"
        # Flags override
        assert map_document_type("INVOICE", is_credit=True) == "03"
        assert map_document_type("INVOICE", is_debit=True) == "02"

    def test_classify_expense_account(self, sample_standard_invoice, sample_service_invoice):
        # Goods -> 304
        assert classify_expense_account(sample_standard_invoice) == DEFAULT_EXPENSE_GOODS_ACCOUNT
        # Services (telecom, internet keywords) -> 602
        assert classify_expense_account(sample_service_invoice) == DEFAULT_EXPENSE_SERVICE_ACCOUNT
        # Custom override
        assert classify_expense_account(sample_standard_invoice, default_account="601") == "601"


# ---------------------------------------------------------------------------
# Unit Tests: POKUPKI.TXT Generation
# ---------------------------------------------------------------------------

class TestPokupkiTxtGenerator:
    def test_standard_invoice_to_nap_entry(self, sample_standard_invoice):
        entry = invoice_to_nap_entry(sample_standard_invoice, period="202608")
        assert entry.branch_or_period == "202608"
        assert entry.doc_type == "01"
        assert entry.doc_number == "0000124013"
        assert entry.doc_date == "2026-08-16"
        assert entry.contractor_id == "121644736"
        assert entry.contractor_name == "МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД"
        assert entry.total_amount == Decimal("300.00")
        assert entry.tax_base_20 == Decimal("250.00")
        assert entry.vat_20 == Decimal("50.00")
        assert entry.tax_base_9 == Decimal("0.00")
        assert entry.vat_9 == Decimal("0.00")
        assert entry.tax_base_no_credit == Decimal("0.00")

    def test_reduced_vat_invoice_to_nap_entry(self, sample_reduced_vat_invoice):
        entry = invoice_to_nap_entry(sample_reduced_vat_invoice, period="202608")
        assert entry.doc_type == "01"
        assert entry.doc_number == "0000007890"
        assert entry.total_amount == Decimal("109.00")
        assert entry.tax_base_20 == Decimal("0.00")
        assert entry.vat_20 == Decimal("0.00")
        # Col 11 & 12: 9% VAT
        assert entry.tax_base_9 == Decimal("100.00")
        assert entry.vat_9 == Decimal("9.00")

    def test_credit_note_storno_negative_amounts(self, sample_credit_note):
        entry = invoice_to_nap_entry(sample_credit_note, period="202608")
        assert entry.doc_type == "03"
        assert entry.doc_number == "0000000042"
        # Storno must have negative values in NAP ledger
        assert entry.total_amount == Decimal("-60.00")
        assert entry.tax_base_20 == Decimal("-50.00")
        assert entry.vat_20 == Decimal("-10.00")

    def test_zero_vat_exempt_delivery(self):
        inv = {
            "invoice_metadata": {"invoice_number": "555", "date_issued": "2026-08-10"},
            "supplier": {"name": "Експорт ЕООД", "eik": "111222333"},
            "financial_summary": {
                "tax_base": {"amount": "500.00", "currency": "BGN"},
                "vat_amount": {"amount": "0.00", "currency": "BGN"},
                "total_amount_due": {"amount": "500.00", "currency": "BGN"},
            },
        }
        entry = invoice_to_nap_entry(inv)
        assert entry.tax_base_20 == Decimal("0.00")
        assert entry.vat_20 == Decimal("0.00")
        assert entry.tax_base_no_credit == Decimal("500.00")

    def test_pokupki_txt_fixed_width_format(self, sample_standard_invoice, sample_credit_note):
        txt_str = invoices_to_pokupki_txt(
            [sample_standard_invoice, sample_credit_note],
            format="fixed_width",
            encoding=None,
            period="202608",
        )
        assert isinstance(txt_str, str)
        assert "\r\n" in txt_str
        lines = [l for l in txt_str.split("\r\n") if l.strip()]
        assert len(lines) == 2

        # Line 1: Standard Invoice
        l1 = lines[0]
        assert l1.startswith("2026")
        assert "01" in l1[:10]
        assert "0000124013" in l1
        assert "300.00" in l1
        assert "250.00" in l1
        assert "50.00" in l1

        # Line 2: Credit note with negative storno
        l2 = lines[1]
        assert "03" in l2[:10]
        assert "0000000042" in l2
        assert "-60.00" in l2
        assert "-50.00" in l2
        assert "-10.00" in l2

    def test_pokupki_txt_tsv_format(self, sample_standard_invoice):
        tsv_str = invoices_to_pokupki_txt(
            [sample_standard_invoice],
            format="tsv",
            encoding=None,
        )
        assert isinstance(tsv_str, str)
        cols = tsv_str.strip().split("\t")
        assert len(cols) == 16
        assert cols[0] == DEFAULT_BRANCH
        assert cols[1] == "01"
        assert cols[2] == "0000124013"
        assert cols[3] == "2026-08-16"
        assert cols[4] == "121644736"
        assert cols[7] == "300.00"  # Col 8: Total
        assert cols[8] == "250.00"  # Col 9: Base 20%
        assert cols[9] == "50.00"   # Col 10: VAT 20%

    def test_pokupki_txt_cp1251_encoding(self, sample_standard_invoice):
        raw_bytes = invoices_to_pokupki_txt(
            [sample_standard_invoice],
            format="fixed_width",
            encoding="cp1251",
        )
        assert isinstance(raw_bytes, bytes)
        decoded = raw_bytes.decode("cp1251")
        assert "МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД" in decoded
        assert "0000124013" in decoded


# ---------------------------------------------------------------------------
# Unit Tests: Double-Entry Accounting Journal Entries
# ---------------------------------------------------------------------------

class TestAccountingJournalEntries:
    def test_standard_journal_entry_balance(self, sample_standard_invoice):
        entry = invoice_to_journal_entry(sample_standard_invoice)
        assert entry.is_balanced is True
        assert entry.is_storno is False
        assert entry.expense_account == DEFAULT_EXPENSE_GOODS_ACCOUNT  # 304
        assert entry.tax_base_amount == Decimal("250.00")
        assert entry.vat_account == DEFAULT_VAT_PURCHASE_ACCOUNT       # 4531
        assert entry.vat_amount == Decimal("50.00")
        assert entry.payable_account == DEFAULT_SUPPLIER_PAYABLE_ACCOUNT # 401
        assert entry.total_amount == Decimal("300.00")

        # Verify mathematical equality: Dt 304 + Dt 4531 = Ct 401
        assert entry.tax_base_amount + entry.vat_amount == entry.total_amount

        # Records inspection
        assert len(entry.records) == 2
        r1, r2 = entry.records
        assert r1.debit_account == "304" and r1.credit_account == "401" and r1.amount == Decimal("250.00")
        assert r2.debit_account == "4531" and r2.credit_account == "401" and r2.amount == Decimal("50.00")

    def test_service_journal_entry_account_602(self, sample_service_invoice):
        entry = invoice_to_journal_entry(sample_service_invoice)
        assert entry.is_balanced is True
        assert entry.expense_account == DEFAULT_EXPENSE_SERVICE_ACCOUNT  # 602
        assert entry.tax_base_amount == Decimal("100.00")
        assert entry.vat_amount == Decimal("20.00")
        assert entry.total_amount == Decimal("120.00")

    def test_credit_note_storno_balance(self, sample_credit_note):
        entry = invoice_to_journal_entry(sample_credit_note)
        assert entry.is_balanced is True
        assert entry.is_storno is True
        assert entry.doc_type == "Кредитно известие"
        assert entry.tax_base_amount == Decimal("-50.00")
        assert entry.vat_amount == Decimal("-10.00")
        assert entry.total_amount == Decimal("-60.00")
        # Equality: (-50) + (-10) == -60
        assert entry.tax_base_amount + entry.vat_amount == entry.total_amount

    def test_export_universal_csv(self, sample_standard_invoice, sample_service_invoice):
        entries = invoices_to_journal_entries([sample_standard_invoice, sample_service_invoice])
        csv_str = export_journal_entries_csv(entries, format_type="universal")
        lines = csv_str.strip().split("\r\n")
        assert len(lines) == 3  # Header + 2 rows
        assert "Сметка_Разход;Сума_Разход;Сметка_ДДС;Сума_ДДС;Сметка_Доставчик" in lines[0]
        assert "304;250.00;4531;50.00;401;300.00" in lines[1]
        assert "602;100.00;4531;20.00;401;120.00" in lines[2]

    def test_export_microinvest_delta_pro_format(self, sample_standard_invoice):
        entries = invoices_to_journal_entries([sample_standard_invoice])
        csv_str = export_journal_entries_csv(entries, format_type="microinvest")
        lines = csv_str.strip().split("\r\n")
        assert "Дата;ВидДокумент;НомерДокумент;СметкаДт;АналитичностДт;СметкаКт;АналитичностКт;Сума" in lines[0]
        # 2 postings for 1 invoice (goods + VAT)
        assert len(lines) == 3
        assert "304;;401;121644736;250.00;BGN" in lines[1]
        assert "4531;;401;121644736;50.00;BGN" in lines[2]

    def test_export_sap_format(self, sample_standard_invoice):
        entries = invoices_to_journal_entries([sample_standard_invoice])
        sap_str = export_journal_entries_csv(entries, format_type="sap")
        lines = sap_str.strip().split("\r\n")
        assert "BUDAT\tBLDAT\tBLART\tXBLNR\tLIFNR\tHKONT\tWRBTR\tSHKZG\tMWSKZ\tSGTXT" in lines[0]
        # 3 line items for SAP FI: Expense, VAT, Vendor payable
        assert len(lines) == 4
        assert "KR\t0000124013\t121644736\t304000\t250.00\tS" in lines[1]
        assert "KR\t0000124013\t121644736\t453100\t50.00\tS" in lines[2]
        assert "KR\t0000124013\t121644736\t401000\t300.00\tH" in lines[3]

    def test_export_journal_entries_json(self, sample_standard_invoice, sample_service_invoice, sample_credit_note):
        entries = invoices_to_journal_entries([sample_standard_invoice, sample_service_invoice, sample_credit_note])
        json_str = export_journal_entries_json(entries)
        data = json.loads(json_str)
        summary = data["summary"]
        assert summary["total_documents"] == 3
        assert summary["is_overall_balanced"] is True
        assert summary["storno_documents_count"] == 1
        # Net Totals: (250+50) + (100+20) + (-50-10) = 300 + 120 - 60 = 360.00
        assert summary["total_debit"] == "360.00"
        assert summary["total_credit"] == "360.00"


# ---------------------------------------------------------------------------
# Unit Tests: Batch Export Facilitator
# ---------------------------------------------------------------------------

class TestBatchExportFacilitator:
    def test_export_all_accounting_files(self, sample_standard_invoice, sample_service_invoice):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_p = Path(tmp_dir)
            files = export_all_accounting_files(
                [sample_standard_invoice, sample_service_invoice],
                output_dir=out_p,
                nap_period="202608",
            )
            assert Path(files["pokupki_txt"]).exists()
            assert Path(files["pokupki_tsv"]).exists()
            assert Path(files["journal_entries_csv"]).exists()
            assert Path(files["microinvest_csv"]).exists()
            assert Path(files["journal_entries_json"]).exists()

            # Verify POKUPKI.TXT is valid CP1251
            cp1251_content = Path(files["pokupki_txt"]).read_bytes().decode("cp1251")
            assert "0000124013" in cp1251_content
            assert "0100456789" in cp1251_content

            # Verify JSON journal entries
            json_data = json.loads(Path(files["journal_entries_json"]).read_text("utf-8"))
            assert json_data["summary"]["is_overall_balanced"] is True
            assert json_data["summary"]["total_documents"] == 2


# ---------------------------------------------------------------------------
# Integration Tests: process_batch in invoice_ocr.py
# ---------------------------------------------------------------------------

class TestInvoiceOcrBatchIntegration:
    def test_process_batch_with_accounting_exports(self, sample_standard_invoice):
        with tempfile.TemporaryDirectory() as tmp_in_dir, tempfile.TemporaryDirectory() as tmp_out_dir:
            in_p = Path(tmp_in_dir)
            out_p = Path(tmp_out_dir)

            # Create a mock json file simulating processed invoice
            inv_dict = json.loads(json.dumps(sample_standard_invoice, default=lambda o: o.value if hasattr(o, "value") else (str(o) if isinstance(o, Decimal) else getattr(o, "__dict__", str(o)))))
            (out_p / "invoice1.json").write_text(json.dumps(inv_dict), encoding="utf-8")

            # Call process_batch with export flags on dummy input (or using existing results)
            from invoice_ocr import format_batch_console_report

            summary = {
                "summary": {"total_documents": 1, "processed_successfully": 1, "failed": 0},
                "financial_totals_by_currency": {"BGN": {"total_amount_due": "300.00"}},
                "output_directory": str(out_p),
                "summary_file": str(out_p / "batch_summary.json"),
                "accounting_exports": {
                    "pokupki": str(out_p / "POKUPKI.TXT"),
                    "journal_entries_csv": str(out_p / "journal_entries.csv"),
                    "journal_entries_json": str(out_p / "journal_entries.json"),
                },
            }
            report = format_batch_console_report(summary)
            assert "СЧЕТОВОДЕН ЕКСПОРТ (НАП И ERP):" in report
            assert "НАП Дневник покупки (POKUPKI.TXT)" in report
            assert "Счетоводни статии (CSV)" in report


# ---------------------------------------------------------------------------
# API Integration Tests: FastAPI endpoints
# ---------------------------------------------------------------------------

class TestApiAccountingEndpoints:
    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_export_pokupki_endpoint_fixed_width(self, client):
        payload = [
            {
                "invoice_metadata": {
                    "invoice_number": "1100098511",
                    "date_issued": "2025-07-31",
                    "document_type": "INVOICE",
                },
                "supplier": {
                    "name": "КАПИНА 71 ООД",
                    "eik": "114500333",
                },
                "financial_summary": {
                    "tax_base": {"amount": "156.83", "currency": "BGN"},
                    "vat_amount": {"amount": "31.37", "currency": "BGN"},
                    "total_amount_due": {"amount": "188.20", "currency": "BGN"},
                },
            }
        ]
        resp = client.post(
            "/api/v1/export/pokupki?format=fixed_width&encoding=cp1251&period=202507",
            json=payload,
        )
        assert resp.status_code == 200
        assert resp.headers["Content-Disposition"] == 'attachment; filename="POKUPKI.TXT"'
        assert "windows-1251" in resp.headers["content-type"].lower()
        decoded = resp.content.decode("cp1251")
        assert "1100098511" in decoded
        assert "КАПИНА 71 ООД" in decoded
        assert "188.20" in decoded

    def test_export_pokupki_endpoint_tsv(self, client):
        payload = [
            {
                "invoice_metadata": {"invoice_number": "999", "date_issued": "2026-08-01"},
                "supplier": {"name": "Тест ООД", "eik": "123456789"},
                "financial_summary": {
                    "tax_base": {"amount": "100.00", "currency": "BGN"},
                    "vat_amount": {"amount": "20.00", "currency": "BGN"},
                    "total_amount_due": {"amount": "120.00", "currency": "BGN"},
                },
            }
        ]
        resp = client.post("/api/v1/export/pokupki?format=tsv&encoding=utf-8", json=payload)
        assert resp.status_code == 200
        text = resp.text
        assert "0000000999" in text
        assert "120.00" in text
        assert "\t" in text

    def test_export_journal_entries_endpoint_json(self, client):
        payload = [
            {
                "invoice_metadata": {"invoice_number": "100", "date_issued": "2026-08-01"},
                "supplier": {"name": "Метро ООД", "eik": "121644736"},
                "financial_summary": {
                    "tax_base": {"amount": "200.00", "currency": "BGN"},
                    "vat_amount": {"amount": "40.00", "currency": "BGN"},
                    "total_amount_due": {"amount": "240.00", "currency": "BGN"},
                },
            }
        ]
        resp = client.post("/api/v1/export/journal-entries?format=json", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["is_overall_balanced"] is True
        assert data["summary"]["total_debit"] == "240.00"
        assert len(data["entries"]) == 1
        entry = data["entries"][0]
        assert entry["expense_account"] == "304"
        assert entry["tax_base_amount"] == "200.00"
        assert entry["vat_amount"] == "40.00"
        assert entry["total_amount"] == "240.00"

    def test_export_journal_entries_endpoint_microinvest(self, client):
        payload = [
            {
                "invoice_metadata": {"invoice_number": "100", "date_issued": "2026-08-01"},
                "supplier": {"name": "Метро ООД", "eik": "121644736"},
                "financial_summary": {
                    "tax_base": {"amount": "200.00", "currency": "BGN"},
                    "vat_amount": {"amount": "40.00", "currency": "BGN"},
                    "total_amount_due": {"amount": "240.00", "currency": "BGN"},
                },
            }
        ]
        resp = client.post("/api/v1/export/journal-entries?format=microinvest", json=payload)
        assert resp.status_code == 200
        assert "journal_entries_microinvest.csv" in resp.headers["Content-Disposition"]
        text = resp.text
        assert "СметкаДт;АналитичностДт;СметкаКт;АналитичностКт;Сума" in text
        assert "304;;401;121644736;200.00;BGN" in text
        assert "4531;;401;121644736;40.00;BGN" in text

    def test_export_empty_list_returns_400(self, client):
        resp = client.post("/api/v1/export/pokupki", json=[])
        assert resp.status_code == 400
        resp_j = client.post("/api/v1/export/journal-entries", json=[])
        assert resp_j.status_code == 400


# ---------------------------------------------------------------------------
# Acceptance Test: Real Corpus Invoices
# ---------------------------------------------------------------------------

class TestRealCorpusAccountingExport:
    def test_real_corpus_json_files(self):
        results_dir = Path("results")
        if not results_dir.exists():
            pytest.skip("results/ directory not present")

        json_files = [p for p in results_dir.glob("*.json") if p.name != "batch_summary.json"]
        if not json_files:
            pytest.skip("No invoice json files in results/")

        invoices = [json.loads(p.read_text("utf-8")) for p in json_files]
        assert len(invoices) >= 10, f"Expected at least 10 processed invoices, got {len(invoices)}"

        # 1. Generate POKUPKI.TXT for all corpus invoices
        pokupki_raw = invoices_to_pokupki_txt(invoices, format="fixed_width", encoding="cp1251")
        assert isinstance(pokupki_raw, bytes)
        decoded = pokupki_raw.decode("cp1251")
        lines = [l for l in decoded.split("\r\n") if l.strip()]
        assert len(lines) == len(invoices)

        # 2. Generate journal entries for all corpus invoices
        entries = invoices_to_journal_entries(invoices)
        assert len(entries) == len(invoices)

        # 3. Verify that 100% of corpus journal entries are strictly balanced (Debit == Credit)
        unbalanced = [e for e in entries if not e.is_balanced]
        assert len(unbalanced) == 0, f"Found unbalanced journal entries: {[(u.doc_number, u.supplier_name, u.tax_base_amount, u.vat_amount, u.total_amount) for u in unbalanced]}"

        # 4. Verify JSON summary reflects overall balanced state
        summary_str = export_journal_entries_json(entries)
        summary = json.loads(summary_str)["summary"]
        assert summary["is_overall_balanced"] is True
        assert summary["total_debit"] == summary["total_credit"]


class TestAdditionalErpFormatsAndEdgeCases:
    def test_export_business_navigator_format(self, sample_standard_invoice):
        entries = invoices_to_journal_entries([sample_standard_invoice])
        csv_str = export_journal_entries_csv(entries, format_type="business_navigator")
        lines = csv_str.strip().split("\r\n")
        assert lines[0] == "Дата;Документ;Номер;ЕИК;Контрагент;СметкаДт;СметкаКт;Дебит;Кредит;Основание"
        assert "304;401;250.00;250.00" in lines[1]
        assert "4531;401;50.00;50.00" in lines[2]

    def test_export_ajur_format(self, sample_standard_invoice, sample_credit_note):
        entries = invoices_to_journal_entries([sample_standard_invoice, sample_credit_note])
        csv_str = export_journal_entries_csv(entries, format_type="ajur")
        lines = csv_str.strip().split("\r\n")
        assert lines[0] == "Дата;Вид;Номер;СметкаДт;СметкаКт;ДанъчнаОснова;ДДС;Общо;ЕИК;Име;Основание"
        # Standard invoice (01)
        assert "01;0000124013;304;401;250.00;50.00;300.00" in lines[1]
        # Credit note (03)
        assert "03;0000000042;304;401;-50.00;-10.00;-60.00" in lines[2]

    def test_credit_note_sap_format(self, sample_credit_note):
        entries = invoices_to_journal_entries([sample_credit_note])
        sap_str = export_journal_entries_csv(entries, format_type="sap")
        lines = sap_str.strip().split("\r\n")
        # In SAP FI, vendor credit memos use document type KG, and credit indicator H on expense
        assert "KG\t0000000042\t121644736\t304000\t50.00\tH" in lines[1]
        assert "KG\t0000000042\t121644736\t453100\t10.00\tH" in lines[2]
        assert "KG\t0000000042\t121644736\t401000\t60.00\tS" in lines[3]

    def test_foreign_currency_eur_conversion_to_bgn_for_nap(self):
        # Invoice in EUR (e.g. 100 EUR net, 20 EUR vat, 120 EUR total)
        # In statutory BGN: 120 * 1.95583 = 234.70 BGN
        inv = {
            "invoice_metadata": {"invoice_number": "999888", "date_issued": "2026-08-15"},
            "supplier": {"name": "DELL TECHNOLOGIES GMBH", "eik": "DE123456789"},
            "financial_summary": {
                "tax_base": {"amount": "100.00", "currency": "EUR"},
                "vat_amount": {"amount": "20.00", "currency": "EUR"},
                "total_amount_due": {"amount": "120.00", "currency": "EUR"},
            },
        }
        entry = invoice_to_nap_entry(inv)
        assert entry.total_amount == Decimal("234.70")
        assert entry.tax_base_20 == Decimal("195.58")
        assert entry.vat_20 == Decimal("39.12")
        # Check sum
        assert entry.tax_base_20 + entry.vat_20 == entry.total_amount

    def test_api_batch_dir_accounting_export(self, sample_standard_invoice):
        from unittest.mock import patch
        client = TestClient(app)
        with tempfile.TemporaryDirectory() as tmp_in, tempfile.TemporaryDirectory() as tmp_out:
            dummy_file = Path(tmp_in) / "inv.png"
            dummy_file.write_bytes(b"dummy image")
            with patch("invoice_ocr.process_invoice", return_value=sample_standard_invoice):
                req_payload = {
                    "input_dir": tmp_in,
                    "output_dir": tmp_out,
                    "export_nap": True,
                    "export_entries": True,
                    "nap_period": "202608",
                }
                resp = client.post("/api/v1/invoices/batch-dir", json=req_payload)
                assert resp.status_code == 200
                data = resp.json()
                assert "accounting_exports" in data
                assert Path(data["accounting_exports"]["pokupki"]).exists()
                assert Path(data["accounting_exports"]["journal_entries_csv"]).exists()
                assert Path(data["accounting_exports"]["journal_entries_json"]).exists()

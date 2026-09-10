"""Unit and integration tests for partner verification and HITL routing.

Verifies:
1. Exact name match against accounting.partners -> auto-approved.
2. Minor OCR typo / Latin transliteration -> SIMILAR -> auto-approved.
3. Completely different company name with valid EIK -> DIVERGENT -> CRITICAL_MISMATCH -> blocked from auto-classification, routed to HITL.
4. Classification endpoint blocking on critical divergence.
5. Ingest email endpoint blocking on critical divergence.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from invoice_core.models import (
    DocumentType,
    FinancialSummary,
    Invoice,
    InvoiceMetadata,
    MoneyAmount,
    Party,
    ValidationResult,
)
from invoice_core.partner_verification import (
    compute_company_name_similarity,
    normalize_company_name,
    verify_invoice_parties,
    verify_party_against_partner,
)
from contractor_verification import (
    CompanyStatus,
    ContractorVerificationResult,
    VatRegistrationStatus,
)


class MockContractorVerifier:
    """Mock verifier providing canonical accounting.partners records."""

    def __init__(self):
        self.db = {
            "203818240": ContractorVerificationResult(
                country_code="BG",
                identifier="203818240",
                company_name="ДЖЕНТЪЛМЕН ГРУП ООД",
                legal_status=CompanyStatus.ACTIVE,
                vat_status=VatRegistrationStatus.REGISTERED,
                address="гр. София, бул. Черни връх 51",
                mol_name="Иван Иванов",
                raw_data={
                    "legal_name": "ДЖЕНТЪЛМЕН ГРУП ООД",
                    "transliteration": "Gentleman Group OOD",
                    "trade_name": "Gentleman",
                    "ocr_aliases": ["Gentleman Barbershop", "Джентълмен"],
                },
            ),
            "131129282": ContractorVerificationResult(
                country_code="BG",
                identifier="131129282",
                company_name="ШЕЛ БЪЛГАРИЯ ЕАД",
                legal_status=CompanyStatus.ACTIVE,
                vat_status=VatRegistrationStatus.REGISTERED,
                address="гр. София, бул. Цариградско шосе 141",
                mol_name="Петър Петров",
                raw_data={
                    "legal_name": "ШЕЛ БЪЛГАРИЯ ЕАД",
                    "transliteration": "Shell Bulgaria EAD",
                    "trade_name": "Shell",
                    "ocr_aliases": ["Шел"],
                },
            ),
            "121683623": ContractorVerificationResult(
                country_code="BG",
                identifier="121683623",
                company_name="ТОПЛИВО АД",
                legal_status=CompanyStatus.ACTIVE,
                vat_status=VatRegistrationStatus.REGISTERED,
                address="гр. София, ул. Солунска 2",
                mol_name="Георги Георгиев",
                raw_data={
                    "legal_name": "ТОПЛИВО АД",
                    "transliteration": "Toplivo AD",
                    "trade_name": "Toplivo",
                },
            ),
        }

    def verify_sync(self, ident: str, date_tax_event=None):
        clean = ident.replace("BG", "").strip()
        return self.db.get(clean)


@pytest.fixture
def mock_verifier():
    return MockContractorVerifier()


def test_normalize_company_name():
    assert normalize_company_name('„КАУФЛАНД БЪЛГАРИЯ" ЕООД ЕНД КО КД') == "кауфланд българия"
    assert normalize_company_name("Джентълмен Груп ООД") == "джентълмен груп"
    assert normalize_company_name("Shell Bulgaria EAD") == "shell bulgaria"


def test_similarity_exact_and_similar(mock_verifier):
    cand = ["ДЖЕНТЪЛМЕН ГРУП ООД", "Gentleman Group OOD", "Gentleman"]
    
    # 1. Exact Cyrillic
    score, status, match = compute_company_name_similarity("Джентълмен Груп ООД", cand)
    assert status == "EXACT"
    assert score >= 0.95

    # 2. Latin transliteration
    score, status, match = compute_company_name_similarity("Gentleman Group", cand)
    assert status in ("EXACT", "SIMILAR")
    assert score >= 0.75

    # 3. Typo in name
    score, status, match = compute_company_name_similarity("Дженталмен Груп", cand)
    assert status in ("EXACT", "SIMILAR")
    assert score >= 0.60


def test_similarity_divergent_different_entity(mock_verifier):
    cand = ["ДЖЕНТЪЛМЕН ГРУП ООД", "Gentleman Group OOD"]
    
    # Recognized name is Toplivo AD, but EIK belongs to Gentleman Group
    score, status, match = compute_company_name_similarity("ТОПЛИВО АД", cand)
    assert status == "DIVERGENT"
    assert score < 0.60

    # Shell Bulgaria vs Kaufland Bulgaria (should NOT match despite both having "България")
    kaufland_cand = ["КАУФЛАНД БЪЛГАРИЯ ЕООД ЕНД КО КД"]
    score, status, match = compute_company_name_similarity("Шел България ЕАД", kaufland_cand)
    assert status == "DIVERGENT"
    assert score < 0.60


def test_verify_party_against_partner_exact(mock_verifier):
    p = Party(name="Шел България ЕАД", eik="131129282", vat_number="BG131129282")
    res = verify_party_against_partner(p, role="supplier", verifier=mock_verifier)
    assert res.db_partner_found is True
    assert res.match_status == "EXACT"
    assert res.is_critical_mismatch is False
    assert res.db_canonical_name == "ШЕЛ БЪЛГАРИЯ ЕАД"


def test_verify_party_against_partner_critical_mismatch(mock_verifier):
    # EIK is 203818240 (Gentleman), but invoice text says "ТОПЛИВО АД"
    p = Party(name="ТОПЛИВО АД", eik="203818240", vat_number="BG203818240")
    res = verify_party_against_partner(p, role="supplier", verifier=mock_verifier)
    assert res.db_partner_found is True
    assert res.match_status == "DIVERGENT"
    assert res.is_critical_mismatch is True
    assert "Критично разминаване" in res.details


def test_verify_invoice_parties_consolidated_report(mock_verifier):
    sup_ok = Party(name="Шел България ЕАД", eik="131129282")
    rec_mismatch = Party(name="ТОПЛИВО АД", eik="203818240")  # Actually Gentleman

    report = verify_invoice_parties(sup_ok, rec_mismatch, verifier=mock_verifier)
    assert report.both_eiks_in_db is True
    assert report.has_critical_mismatch is True
    assert report.requires_hitl is True
    assert len(report.hitl_reasons) == 1
    assert any(iss.code == "CRITICAL_RECIPIENT_NAME_MISMATCH" for iss in report.validation_issues)


def test_classify_document_critical_divergence_blocks_classification(mock_verifier):
    from fastapi.testclient import TestClient
    from api_server import app
    from invoice_core.document_classifier import ClassificationResult, DocumentCategory

    client = TestClient(app)

    # Build dummy invoice with divergent supplier (Name: Топливо АД, but EIK: 203818240 which belongs to Джентълмен)
    inv = Invoice(
        invoice_metadata=InvoiceMetadata(invoice_number="1000000001", date_issued="2026-03-01"),
        supplier=Party(name="ТОПЛИВО АД", eik="203818240", vat_number="BG203818240"),
        recipient=Party(name="ШЕЛ БЪЛГАРИЯ ЕАД", eik="131129282", vat_number="BG131129282"),
        financial_summary=FinancialSummary(total_amount_due=MoneyAmount(Decimal("120.00"), "BGN")),
    )

    mock_pool = MagicMock()
    mock_pool.submit_ocr_async = AsyncMock(return_value={"status": "success", "invoice": inv})

    mock_cls_res = ClassificationResult(
        category=DocumentCategory.FAKTURI,
        confidence=0.98,
        matched_keywords=["фактура", "еик"],
    )

    with (
        patch("api_server.get_ocr_pool", return_value=mock_pool),
        patch("api_server.get_document_classifier") as mock_cls_getter,
        patch("contractor_verification.default_verifier", mock_verifier),
    ):
        mock_cls = MagicMock()
        mock_cls.classify_file.return_value = mock_cls_res
        mock_cls_getter.return_value = mock_cls

        files = {"file": ("invoice_test.pdf", b"%PDF-1.4 dummy", "application/pdf")}
        resp = client.post("/api/v1/classify/document?sync_supabase=false", files=files)

        assert resp.status_code == 200
        data = resp.json()

        # Must NOT be classified into FAKTURI or auto-routed to invoices!
        assert data["status"] == "needs_review"
        assert data["routing_action"] == "hitl_review"
        assert data["requires_hitl"] is True
        assert data["category_code"] == "UNCLASSIFIED_NEEDS_REVIEW"
        assert data["category"] == DocumentCategory.NEKLASIFITSIRANI.value
        assert len(data["hitl_reasons"]) > 0
        assert "Критично разминаване" in data["hitl_reasons"][0]


def test_classify_document_valid_partners_auto_routed(mock_verifier):
    from fastapi.testclient import TestClient
    from api_server import app
    from invoice_core.document_classifier import ClassificationResult, DocumentCategory

    client = TestClient(app)

    # Both supplier and recipient match their respective EIKs
    inv = Invoice(
        invoice_metadata=InvoiceMetadata(invoice_number="1000000002", date_issued="2026-03-01"),
        supplier=Party(name="ДЖЕНТЪЛМЕН ГРУП ООД", eik="203818240", vat_number="BG203818240"),
        recipient=Party(name="ШЕЛ БЪЛГАРИЯ ЕАД", eik="131129282", vat_number="BG131129282"),
        financial_summary=FinancialSummary(total_amount_due=MoneyAmount(Decimal("120.00"), "BGN")),
    )

    mock_pool = MagicMock()
    mock_pool.submit_ocr_async = AsyncMock(return_value={"status": "success", "invoice": inv})

    mock_cls_res = ClassificationResult(
        category=DocumentCategory.FAKTURI,
        confidence=0.98,
        matched_keywords=["фактура", "еик"],
    )

    with (
        patch("api_server.get_ocr_pool", return_value=mock_pool),
        patch("api_server.get_document_classifier") as mock_cls_getter,
        patch("contractor_verification.default_verifier", mock_verifier),
    ):
        mock_cls = MagicMock()
        mock_cls.classify_file.return_value = mock_cls_res
        mock_cls_getter.return_value = mock_cls

        files = {"file": ("invoice_good.pdf", b"%PDF-1.4 dummy", "application/pdf")}
        resp = client.post("/api/v1/classify/document?sync_supabase=false", files=files)

        assert resp.status_code == 200
        data = resp.json()

        assert data["status"] == "success"
        assert data["routing_action"] == "routed_to_invoices"
        assert data["category_code"] == "FAKTURI"
        assert data["requires_hitl"] is False


def test_invoices_process_critical_divergence_routes_to_hitl(mock_verifier):
    from fastapi.testclient import TestClient
    from api_server import app

    client = TestClient(app)

    # Invoice with divergent recipient (Name: Топливо АД, but EIK: 203818240 -> Джентълмен)
    inv = Invoice(
        invoice_metadata=InvoiceMetadata(invoice_number="1000000003", date_issued="2026-03-01"),
        supplier=Party(name="ШЕЛ БЪЛГАРИЯ ЕАД", eik="131129282", vat_number="BG131129282"),
        recipient=Party(name="ТОПЛИВО АД", eik="203818240", vat_number="BG203818240"),
        financial_summary=FinancialSummary(total_amount_due=MoneyAmount(Decimal("250.00"), "BGN")),
    )

    mock_pool = MagicMock()
    mock_pool.submit_ocr_async = AsyncMock(return_value={"status": "success", "invoice": inv})

    with (
        patch("api_server.get_ocr_pool", return_value=mock_pool),
        patch("contractor_verification.default_verifier", mock_verifier),
    ):
        files = {"file": ("inv_proc_mismatch.pdf", b"%PDF-1.4 dummy", "application/pdf")}
        resp = client.post("/api/v1/invoices/process", files=files)

        assert resp.status_code == 200
        data = resp.json()

        assert data["status"] == "needs_review"
        assert data["is_valid"] is False
        assert data["requires_hitl"] is True
        assert len(data["hitl_reasons"]) > 0
        assert "parties_verification" in data
        assert data["parties_verification"]["has_critical_mismatch"] is True

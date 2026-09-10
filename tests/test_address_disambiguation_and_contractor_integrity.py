"""Unit and Integration tests for Contractor Address Integrity and Prevention Architecture.

Verifies:
1. Наредба Н-18 header layout disambiguation (Registered Office vs Trade Outlet / Store).
2. Central Contractor Master Registry reconciliation in the extraction pipeline.
3. Strict enforcement of statutory registered office (чл. 114 ЗДДС) on party.address.
4. Routing of trade outlet addresses to metadata.place_issued and raw_ocr_evidence.
5. Full CompanyBook schema attributes accessibility in contractor verification results.
"""

from __future__ import annotations

from decimal import Decimal
import pytest

from contractor_verification import (
    ContractorVerificationResult,
    CompanyStatus,
    VatRegistrationStatus,
    verify_contractor,
)
from invoice_core.extraction import extract_party
from invoice_core.models import InvoiceMetadata, LogicalLine, OcrToken, Party
from invoice_core.pipeline import reconcile_party_with_contractor_master


def _create_token(
    text: str,
    left: int,
    top: int,
    width: int,
    height: int,
    page_number: int = 1,
) -> OcrToken:
    return OcrToken(
        text=text,
        conf=95.0,
        bbox=(left, top, width, height),
        page_number=page_number,
    )


def test_h18_header_layout_disambiguation():
    """Verify that lines preceded by 'МАГАЗИН:' / 'ОБЕКТ:' are not mistaken for the registered office."""
    # Simulated receipt/invoice header with both addresses
    lines = [
        LogicalLine([
            _create_token("„ДЖЕНТЪЛМЕН", 100, 100, 150, 25),
            _create_token("ГРУП“", 260, 100, 80, 25),
            _create_token("ЕООД", 350, 100, 60, 25),
        ]),
        LogicalLine([
            _create_token("гр.", 100, 130, 30, 20),
            _create_token("София,", 135, 130, 60, 20),
            _create_token("ул.", 200, 130, 30, 20),
            _create_token("Суходолска", 235, 130, 100, 20),
            _create_token("201", 340, 130, 40, 20),
        ]),
        LogicalLine([
            _create_token("МАГАЗИН:", 100, 160, 90, 20),
            _create_token("гр.", 195, 160, 30, 20),
            _create_token("София,", 230, 160, 60, 20),
            _create_token("бул.", 295, 160, 40, 20),
            _create_token("Патриарх", 340, 160, 90, 20),
            _create_token("Евтимий", 435, 160, 80, 20),
            _create_token("77", 520, 160, 30, 20),
        ]),
        LogicalLine([
            _create_token("ЕИК:", 100, 190, 50, 20),
            _create_token("203818240", 155, 190, 100, 20),
            _create_token("ИН", 270, 190, 30, 20),
            _create_token("по", 305, 190, 25, 20),
            _create_token("ЗДДС:", 335, 190, 50, 20),
            _create_token("BG203818240", 390, 190, 120, 20),
        ]),
    ]
    tokens = [t for line in lines for t in line.tokens]

    party = extract_party(lines, tokens, role="supplier")

    assert party.eik == "203818240"
    assert party.vat_number == "BG203818240"
    # Address must match the registered office line, NOT the store outlet
    assert "Суходолска" in (party.address or "")
    assert "Патриарх Евтимий" not in (party.address or "")


def test_reconcile_party_with_contractor_master_registered_office():
    """Verify master reconciliation strictly sets registered office and routes outlet."""
    party = Party(
        name="ОЖЕНТЪЛМЕН ГРУГ oy",  # Imperfect OCR name
        eik="203818240",
        address="гр. София, бул. Патриарх Евтимий 77",  # OCR captured the physical store
        mol=None,
    )
    metadata = InvoiceMetadata()
    raw_evidence = {}

    reconcile_party_with_contractor_master(
        party=party,
        role="supplier",
        metadata=metadata,
        raw_ocr_evidence=raw_evidence,
    )

    # 1. Registered office must be strictly enforced on party.address (чл. 114 ЗДДС)
    assert "Суходолска 201" in party.address
    assert "Патриарх Евтимий" not in party.address

    # 2. Trade outlet address must be preserved in place_issued & raw evidence
    assert metadata.place_issued == "гр. София, бул. Патриарх Евтимий 77"
    assert raw_evidence["trade_outlet_address"] == "гр. София, бул. Патриарх Евтимий 77"

    # 3. Canonical name and MOL must be aligned
    assert party.name == "ДЖЕНТЪЛМЕН ГРУП ЕООД"
    assert party.mol == "Георги Ангелов Георгиев"
    assert party.vat_number == "BG203818240"


def test_contractor_verification_result_companybook_fields():
    """Verify ContractorVerificationResult exposes full CompanyBook metadata."""
    res = verify_contractor("203818240")

    assert res.identifier == "203818240"
    assert res.company_name == "ДЖЕНТЪЛМЕН ГРУП ЕООД"
    assert res.legal_status == CompanyStatus.ACTIVE
    assert res.vat_status == VatRegistrationStatus.REGISTERED
    assert "Суходолска 201" in (res.seat_address or res.address)
    assert res.mol_name == "Георги Ангелов Георгиев"
    assert len(res.trade_outlets) > 0
    assert any("Патриарх Евтимий 77" in o.get("address", "") for o in res.trade_outlets)

    # Test serialization
    data = res.to_dict()
    assert data["seat_address"] is not None
    assert data["mol_name"] == "Георги Ангелов Георгиев"
    assert len(data["trade_outlets"]) >= 1


@pytest.mark.anyio
async def test_accounting_partners_schema_query():
    """Verify ContractorVerifier queries accounting.partners via PostgREST with Accept-Profile."""
    from unittest.mock import AsyncMock, patch, MagicMock
    from contractor_verification import ContractorVerifier

    verifier = ContractorVerifier(offline_mode=False)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [{
        "id": "11111111-2222-3333-4444-555555555555",
        "country_code": "BG",
        "eik": "203818240",
        "legal_name": "„ДЖЕНТЪЛМЕН ГРУП“ ЕООД",
        "legal_status": "ACTIVE",
        "vat_status": "REGISTERED",
        "seat_settlement": "гр. София",
        "seat_street": "ул. Суходолска",
        "seat_street_number": "201",
        "mol_name": "Георги Ангелов Георгиев",
        "trade_outlets": [{"name": "Магазин Nargile.bg", "address": "гр. София, бул. Патриарх Евтимий 77"}],
    }]

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        res = await verifier._query_supabase_contractor_async("BG", "203818240")

        assert res is not None
        assert res.identifier == "203818240"
        assert res.company_name == "„ДЖЕНТЪЛМЕН ГРУП“ ЕООД"
        assert res.mol_name == "Георги Ангелов Георгиев"
        assert "Суходолска 201" in res.seat_address
        assert len(res.trade_outlets) == 1
        # Verify call headers included Accept-Profile: accounting
        call_args = mock_get.call_args_list[0]
        assert call_args.kwargs["headers"].get("Accept-Profile") == "accounting"


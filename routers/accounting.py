"""FastAPI Router for Statutory Accounting Exports, NAP Ledgers, and Microinvest Delta Pro."""

import base64
from datetime import datetime
from decimal import Decimal
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Optional
import uuid
import zipfile

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import AuditTrailRecord, DocumentRecord, get_db, get_db_session

logger = logging.getLogger("invoice_ocr_api")
router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic Schemas for Accounting & Export Endpoints
# ---------------------------------------------------------------------------

class Protocol117GenerateRequest(BaseModel):
    invoice: dict[str, Any] = Field(..., description="Foreign/EU invoice JSON payload")
    protocol_number: str = Field(default="0000000001", description="10-digit protocol number")
    protocol_date: Optional[str] = Field(default=None, description="Protocol date YYYY-MM-DD (within 15 days of invoice)")
    recipient_company: Optional[dict[str, Any]] = Field(default=None, description="Bulgarian recipient company details")
    legal_basis: Optional[str] = Field(default=None, description="Statutory legal basis (e.g. чл. 82, ал. 2, т. 3 ЗДДС)")
    full_tax_credit: bool = Field(default=True, description="Right to full tax credit deduction")


class VatDeclarationRequest(BaseModel):
    purchase_invoices: list[dict[str, Any]] = Field(..., description="List of purchase invoice JSON payloads")
    sales_invoices: Optional[list[dict[str, Any]]] = Field(default=None, description="List of sales invoice JSON payloads")
    company_info: Optional[dict[str, Any]] = Field(default=None, description="Company details (name, EIK, VAT number)")
    period: str = Field(default="202608", description="VAT period YYYYMM")
    prior_vat_credit_cell_70: float = Field(default=0.0, description="Deductions from prior periods under Art. 92 ЗДДС")
    partial_credit_coefficient_cell_42: float = Field(default=1.0, description="Coefficient under Art. 73 ЗДДС")


class NapPackageExportRequest(BaseModel):
    purchase_invoices: list[dict[str, Any]] = Field(..., description="List of purchase invoice JSON payloads")
    sales_invoices: Optional[list[dict[str, Any]]] = Field(default=None, description="List of sales invoice JSON payloads")
    company_info: Optional[dict[str, Any]] = Field(default=None, description="Company details (name, EIK, VAT number)")
    period: str = Field(default="202608", description="VAT period YYYYMM")
    format: str = Field(default="fixed_width", description="Format: 'fixed_width', 'tsv', or 'csv'")
    encoding: str = Field(default="cp1251", description="Encoding: 'cp1251' or 'utf-8'")
    auto_generate_protocols: bool = Field(default=True, description="Auto-generate Art. 117 protocols for reverse charge")
    as_zip: bool = False


class MicroinvestExportRequest(BaseModel):
    invoices: list[dict[str, Any]] = Field(..., description="List of invoice JSON payloads")
    default_expense_account: str = Field(default="602", description="Default expense account for Delta Pro")
    default_goods_account: str = Field(default="304", description="Default goods account for Delta Pro")
    default_vat_account: str = Field(default="453/1", description="Default VAT account for Delta Pro")
    default_supplier_account: str = Field(default="401", description="Default supplier account for Delta Pro")


class BusinessNavigatorExportRequest(BaseModel):
    invoices: list[dict[str, Any]] = Field(..., description="List of invoice JSON payloads")
    encoding: str = Field(default="windows-1251", description="File encoding (windows-1251 or utf-8)")


class AjurExportRequest(BaseModel):
    invoices: list[dict[str, Any]] = Field(..., description="List of invoice JSON payloads")
    encoding: str = Field(default="windows-1251", description="File encoding (windows-1251 or utf-8)")


class TaxPeriodValidationApiRequest(BaseModel):
    invoices: list[dict[str, Any]] = Field(..., description="List of invoice JSON payloads to validate")
    target_period: Optional[str] = Field(default=None, description="Target VAT period (e.g. 202608 or 2026-08)")


class SupplierMappingRuleApiRequest(BaseModel):
    eik: str = Field(..., description="Supplier EIK / BULSTAT or VAT number")
    target_account: str = Field(..., description="Target accounting account (e.g. 6012, 6021, 3041)")
    target_subledger: Optional[str] = Field(default=None, description="Optional analytical subledger code")
    supplier_name: Optional[str] = Field(default="", description="Supplier name")
    description: Optional[str] = Field(default="", description="Description of the rule")


class KeywordMappingRuleApiRequest(BaseModel):
    rule_id: str = Field(..., description="Unique rule identifier")
    target_account: str = Field(..., description="Target accounting account (e.g. 6012, 6021, 3041)")
    target_subledger: Optional[str] = Field(default=None, description="Optional analytical subledger code")
    keywords: list[str] = Field(default_factory=list, description="List of keywords to match")
    regex_pattern: Optional[str] = Field(default=None, description="Optional regular expression pattern")
    description: Optional[str] = Field(default="", description="Description of the rule")
    priority: int = Field(default=10, description="Rule priority (higher value = higher precedence)")


class BatchTransferLogRequest(BaseModel):
    document_ids: Optional[list[str]] = Field(default=None, description="Optional list of document IDs to consolidate")
    client_eik: Optional[str] = Field(default="206062202", description="Client company EIK (default Building 11)")
    client_company_name: Optional[str] = Field(default="БИЛДИНГ 11 ООД", description="Client company legal name")
    period: Optional[str] = Field(default=None, description="Optional accounting period (e.g. 2026-08)")
    target_drop_dir: Optional[str] = Field(default=None, description="Optional directory to automatically sync TRANSFER.LOG/ldb")
    sync_to_obsidian: bool = Field(default=True, description="Automatically generate/update Obsidian dossier")
    response_format: str = Field(default="json", description="Response format: 'json', 'log', or 'zip'")


class DropSyncRequest(BaseModel):
    target_dir: str = Field(..., description="Target directory path to drop TRANSFER.LOG and TRANSFER.ldb")
    batch_id: Optional[str] = Field(default=None, description="Batch ID to sync")
    document_id: Optional[str] = Field(default=None, description="Single document ID to sync")


class ObsidianSyncRequest(BaseModel):
    client_eik: str = Field(default="206062202", description="Client company EIK")
    client_company_name: str = Field(default="БИЛДИНГ 11 ООД", description="Client company name")
    period: str = Field(default="2026-08", description="Period label (YYYY-MM)")
    document_ids: Optional[list[str]] = Field(default=None, description="Optional document IDs to include")


class UtmBridgeRequest(BaseModel):
    action: str = Field(default="status", description="Action: 'status', 'usb-list', 'usb-connect', 'usb-disconnect', 'stage'")
    vm_name: str = Field(default="Windows XP", description="Target UTM VM name")
    device_id: Optional[str] = Field(default="ABCD:1234", description="USB device VID:PID or location")
    firm_slug: str = Field(default="Building_11", description="Firm folder identifier")
    firm_eik: str = Field(default="206062202", description="Firm EIK identifier")
    drop_dir: Optional[str] = Field(default=None, description="Custom drop directory")


class DeltaProVmDispatchRequest(BaseModel):
    document_id: Optional[str] = Field(default=None, description="Optional document ID to dispatch")
    batch_id: Optional[str] = Field(default=None, description="Optional batch ID to dispatch")
    target_hot_folder: Optional[str] = Field(default=None, description="Configured shared folder / hot-folder path on VM")
    vm_name: str = Field(default="Windows XP", description="VM name (e.g. Windows XP, Windows 11 QEMU)")
    firm_slug: str = Field(default="Building_11", description="Firm folder slug identifier")
    firm_eik: str = Field(default="206062202", description="Firm EIK identifier")
    actor: str = Field(default="system", description="Actor performing the VM dispatch")
    transfer_log_bytes_b64: Optional[str] = Field(default=None, description="Optional raw base64-encoded TRANSFER.LOG bytes")


# ---------------------------------------------------------------------------
# Automated Accounting Pipeline & Microinvest Delta Pro Integration
# ---------------------------------------------------------------------------

def execute_accounting_pipeline_for_document(
    doc_id: str,
    invoice_dict: dict[str, Any],
    file_name: str,
    parties_rep: Any = None,
) -> Optional[dict[str, Any]]:
    """Execute automated accounting operation determination and Microinvest Delta Pro package generation.

    Strictly complies with Bulgarian Accounting Standards (НСС / Закон за счетоводството, ЗДДС):
    - Purchases: Дт 601/602/304, Дт 4531, Кт 401 (or 501 if cash)
    - Sales: Дт 411, Кт 701/702/703, Кт 4532
    - Credit notes: Red storno (червено сторно) with negative amounts
    - Exact currency preservation (EUR / BGN)
    - 100% offline-first operation matching against local MDB knowledge base
    - Generates binary TRANSFER.LOG (65KB) & TRANSFER.ldb (64B) for Microinvest Delta Pro
    """
    try:
        from invoice_core.historical_matcher import process_invoice_and_create_accounting_package
        bundle = process_invoice_and_create_accounting_package(invoice_dict)
        if not bundle:
            return None

        # Persist local binary files for Microinvest Delta Pro
        log_bytes = bundle.get("transfer_log_bytes")
        ldb_bytes = bundle.get("transfer_ldb_bytes")

        if log_bytes:
            client_co = bundle.get("accounting_operation", {}).get("client_company") or "Building_11"
            safe_co = "Building_11" if "11" in client_co else client_co.replace(" ", "_")

            # 1. Store in local repository directory under doc_id
            acc_dir = Path(f".stored_documents/accounting/{doc_id}")
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "TRANSFER.LOG").write_bytes(log_bytes)
            if ldb_bytes:
                (acc_dir / "TRANSFER.ldb").write_bytes(ldb_bytes)

            # 2. Store in active company folder (e.g. Building_11/TRANSFER.LOG)
            try:
                if "PYTEST_CURRENT_TEST" not in os.environ:
                    co_dir = Path(safe_co)
                    co_dir.mkdir(parents=True, exist_ok=True)
                    dest_file = co_dir / "TRANSFER.LOG"
                    # Protect existing multi-document batch files (> 64KB) from being overwritten by single-doc stream
                    if dest_file.exists() and dest_file.stat().st_size > 65536 and len(log_bytes) <= 65536:
                        (co_dir / "TRANSFER_LATEST.LOG").write_bytes(log_bytes)
                    else:
                        dest_file.write_bytes(log_bytes)
                    if ldb_bytes:
                        (co_dir / "TRANSFER.ldb").write_bytes(ldb_bytes)
            except Exception as co_err:
                logger.warning("Could not write to company directory %s: %s", safe_co, co_err)

            # 3. Store in persistent storage /data if running in container
            data_co_dir = Path(f"/data/accounting/{safe_co}")
            if Path("/data").exists():
                try:
                    data_co_dir.mkdir(parents=True, exist_ok=True)
                    (data_co_dir / "TRANSFER.LOG").write_bytes(log_bytes)
                    if ldb_bytes:
                        (data_co_dir / "TRANSFER.ldb").write_bytes(ldb_bytes)
                except Exception as data_err:
                    logger.warning("Could not write to /data/accounting: %s", data_err)

            # 4. Save accounting operation JSON
            try:
                (acc_dir / "accounting_operation.json").write_text(
                    json.dumps({
                        "file_name": file_name,
                        "accounting_operation": bundle.get("accounting_operation"),
                        "match_report": bundle.get("match_report"),
                    }, default=str, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as json_err:
                logger.warning("Could not save local accounting JSON for %s: %s", doc_id, json_err)

            logger.info("Saved Microinvest Delta Pro transfer files for %s to %s and %s", doc_id, acc_dir, safe_co)

            # 5. Store on USB if mounted
            usb_co_dir = Path(f"/Volumes/NO NAME/{safe_co}")
            if usb_co_dir.parent.exists():
                try:
                    usb_co_dir.mkdir(parents=True, exist_ok=True)
                    (usb_co_dir / "TRANSFER.LOG").write_bytes(log_bytes)
                    if ldb_bytes:
                        (usb_co_dir / "TRANSFER.ldb").write_bytes(ldb_bytes)
                    logger.info("Synced Microinvest Delta Pro transfer files to USB: %s", usb_co_dir)
                except Exception as usb_err:
                    logger.warning("Could not write to USB directory %s: %s", usb_co_dir, usb_err)

        return bundle
    except Exception as exc:
        logger.error("Error executing accounting pipeline for %s: %s", file_name, exc, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Endpoints: Statutory Exports & Declarations
# ---------------------------------------------------------------------------

@router.post(
    "/api/v1/export/pokupki",
    summary="Export NAP Purchase Ledger (POKUPKI.TXT)",
    tags=["Accounting"],
)
async def export_pokupki_ledger(
    invoices: list[dict[str, Any]],
    format: str = Query(default="fixed_width", pattern="^(fixed_width|tsv|csv)$", description="Export format: 'fixed_width', 'tsv', or 'csv'"),
    encoding: str = Query(default="cp1251", pattern="^(cp1251|utf-8)$", description="File encoding: 'cp1251' or 'utf-8'"),
    period: Optional[str] = Query(default=None, description="Tax period YYYYMM"),
    branch: str = Query(default="00", description="Branch code"),
):
    """Generate statutory NAP VAT purchase ledger (Дневник за покупки по Приложение № 12 от ППЗДДС)."""
    from accounting_export import invoices_to_pokupki_txt
    if not invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice list cannot be empty",
        )
    try:
        raw_output = invoices_to_pokupki_txt(
            invoices,
            format=format,  # type: ignore
            encoding=encoding,
            period=period,
            branch=branch,
        )
        media_type = "text/plain; charset=windows-1251" if encoding == "cp1251" else "text/plain; charset=utf-8"
        if format == "tsv":
            media_type = "text/tab-separated-values; charset=utf-8"
        elif format == "csv":
            media_type = "text/csv; charset=utf-8"

        content_bytes = raw_output if isinstance(raw_output, bytes) else raw_output.encode(encoding)
        return Response(
            content=content_bytes,
            media_type=media_type,
            headers={"Content-Disposition": 'attachment; filename="POKUPKI.TXT"'},
        )
    except Exception as exc:
        logger.error("Failed generating POKUPKI.TXT: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed generating POKUPKI.TXT: {exc}",
        )


@router.post(
    "/api/v1/export/journal-entries",
    summary="Export Accounting Journal Entries (Контировки)",
    tags=["Accounting"],
)
async def export_journal_entries_endpoint(
    invoices: list[dict[str, Any]],
    format: str = Query(default="json", pattern="^(json|universal|microinvest|business_navigator|ajur|sap)$", description="ERP format"),
    default_account: Optional[str] = Query(default=None, description="Default expense account (e.g. 304 or 602)"),
):
    """Generate double-entry bookkeeping journal entries (контировки)."""
    from accounting_export import (
        export_journal_entries_csv,
        export_journal_entries_json,
        invoices_to_journal_entries,
    )
    if not invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice list cannot be empty",
        )
    try:
        entries = invoices_to_journal_entries(invoices, default_expense_account=default_account)
        if format == "json":
            payload_str = export_journal_entries_json(entries)
            return JSONResponse(content=json.loads(payload_str))

        csv_text = export_journal_entries_csv(entries, format_type=format)  # type: ignore
        media_type = "text/csv; charset=utf-8"
        if format == "sap":
            media_type = "text/tab-separated-values; charset=utf-8"
        filename = f"journal_entries_{format}.csv"
        return Response(
            content=csv_text.encode("utf-8-sig"),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        logger.error("Failed generating journal entries: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed generating journal entries: {exc}",
        )


@router.post(
    "/api/v1/protocol-117/generate",
    summary="Generate Protocol Art. 117 ЗДДС (Reverse Charge & ВОП)",
    tags=["Accounting"],
)
async def generate_protocol_117_endpoint(request: Protocol117GenerateRequest):
    """Generate statutory Protocol under Art. 117 ЗДДС for foreign invoices (Google, Meta, Adobe, ВОП)."""
    from accounting_export import generate_protocol_chl_117
    try:
        proto = generate_protocol_chl_117(
            invoice=request.invoice,
            protocol_number=request.protocol_number,
            protocol_date=request.protocol_date,
            recipient_company=request.recipient_company,
            legal_basis=request.legal_basis,
            full_tax_credit=request.full_tax_credit,
        )
        return JSONResponse(content={
            "protocol": proto.to_dict(),
            "formatted_document": proto.to_text_document(),
            "purchase_ledger_entry": proto.to_purchase_ledger_entry().to_dict(),
            "sales_ledger_entry": proto.to_sales_ledger_entry().to_dict(),
            "journal_entry": proto.to_journal_entry().to_dict(),
        })
    except Exception as exc:
        logger.error("Failed generating Protocol 117: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed generating Protocol 117: {exc}",
        )


@router.post(
    "/api/v1/export/prodagbi",
    summary="Export NAP Sales Ledger (PRODAGBI.TXT)",
    tags=["Accounting"],
)
async def export_prodagbi_ledger(
    invoices: list[dict[str, Any]],
    format: str = Query(default="fixed_width", pattern="^(fixed_width|tsv|csv)$", description="Export format: 'fixed_width', 'tsv', or 'csv'"),
    encoding: str = Query(default="cp1251", pattern="^(cp1251|utf-8)$", description="File encoding: 'cp1251' or 'utf-8'"),
    period: Optional[str] = Query(default=None, description="Tax period YYYYMM"),
    branch: str = Query(default="00", description="Branch code"),
):
    """Generate statutory NAP VAT sales ledger (Дневник за продажби по Приложение № 10 от ППЗДДС)."""
    from accounting_export import invoices_to_prodagbi_txt
    if not invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice list cannot be empty",
        )
    try:
        raw_output = invoices_to_prodagbi_txt(
            invoices,
            format=format,  # type: ignore
            encoding=encoding,
            period=period,
            branch=branch,
        )
        media_type = "text/plain; charset=windows-1251" if encoding == "cp1251" else "text/plain; charset=utf-8"
        if format == "tsv":
            media_type = "text/tab-separated-values; charset=utf-8"
        elif format == "csv":
            media_type = "text/csv; charset=utf-8"

        content_bytes = raw_output if isinstance(raw_output, bytes) else raw_output.encode(encoding)
        return Response(
            content=content_bytes,
            media_type=media_type,
            headers={"Content-Disposition": 'attachment; filename="PRODAGBI.TXT"'},
        )
    except Exception as exc:
        logger.error("Failed generating PRODAGBI.TXT: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed generating PRODAGBI.TXT: {exc}",
        )


@router.post(
    "/api/v1/export/deklar",
    summary="Export NAP VAT Return Declaration (DEKLAR.TXT)",
    tags=["Accounting"],
)
async def export_deklar_endpoint(request: VatDeclarationRequest):
    """Generate statutory NAP VAT declaration (Справка-декларация по чл. 125 ЗДДС - Приложение № 13)."""
    from accounting_export import (
        generate_vat_declaration,
        invoice_to_nap_entry,
        invoice_to_nap_sales_entry,
    )
    try:
        pur_entries = [invoice_to_nap_entry(i, period=request.period) for i in request.purchase_invoices]
        sal_entries = [invoice_to_nap_sales_entry(i, period=request.period) for i in (request.sales_invoices or [])]

        decl = generate_vat_declaration(
            purchase_entries=pur_entries,
            sales_entries=sal_entries,
            company_info=request.company_info,
            period=request.period,
            prior_vat_credit_cell_70=Decimal(str(request.prior_vat_credit_cell_70)),
            partial_credit_coefficient_cell_42=Decimal(str(request.partial_credit_coefficient_cell_42)),
        )

        deklar_bytes = decl.to_nap_deklar_txt(encoding="cp1251")
        assert isinstance(deklar_bytes, bytes)
        return Response(
            content=deklar_bytes,
            media_type="text/plain; charset=windows-1251",
            headers={"Content-Disposition": 'attachment; filename="DEKLAR.TXT"'},
        )
    except Exception as exc:
        logger.error("Failed generating DEKLAR.TXT: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed generating DEKLAR.TXT: {exc}",
        )


@router.post(
    "/api/v1/export/nap-package",
    summary="Export Complete Statutory НАП VAT Package (POKUPKI, PRODAGBI, DEKLAR, ZIP)",
    tags=["Accounting"],
)
async def export_nap_package_endpoint(request: NapPackageExportRequest):
    """Generate complete 3-file statutory package for direct upload to NRA (НАП) electronic portal."""
    from accounting_export import export_nap_package
    if not request.purchase_invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Purchase invoices list cannot be empty",
        )
    try:
        temp_dir = tempfile.mkdtemp(prefix="nap_pkg_")
        res = export_nap_package(
            purchase_invoices=request.purchase_invoices,
            sales_invoices=request.sales_invoices,
            company_info=request.company_info,
            period=request.period,
            format=request.format,  # type: ignore
            encoding=request.encoding,
            auto_generate_protocols=request.auto_generate_protocols,
            create_zip=request.as_zip,
            output_dir=temp_dir,
        )

        if request.as_zip:
            zip_file = Path(temp_dir) / f"NAP_{request.period}.zip"
            if zip_file.exists():
                zip_bytes = zip_file.read_bytes()
                shutil.rmtree(temp_dir, ignore_errors=True)
                return Response(
                    content=zip_bytes,
                    media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="NAP_{request.period}.zip"'},
                )

        shutil.rmtree(temp_dir, ignore_errors=True)
        return JSONResponse(content=res)
    except Exception as exc:
        logger.error("Failed generating complete NAP package: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed generating complete NAP package: {exc}",
        )


@router.post(
    "/api/v1/export/microinvest/sklad",
    summary="Export to Microinvest Sklad Pro (Warehouse Pro) XML",
    tags=["Accounting", "Microinvest ERP"],
)
async def export_microinvest_sklad_endpoint(request: MicroinvestExportRequest):
    """Export invoices to Microinvest Sklad Pro (Warehouse Pro) Purchase XML."""
    from accounting_export import generate_microinvest_sklad_xml
    if not request.invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoices list cannot be empty",
        )
    xml_content = generate_microinvest_sklad_xml(request.invoices[0])
    return Response(content=xml_content, media_type="application/xml")


@router.post(
    "/api/v1/export/microinvest/delta",
    summary="Export to Microinvest Delta Pro TransferData XML",
    tags=["Accounting", "Microinvest ERP"],
)
async def export_microinvest_delta_endpoint(request: MicroinvestExportRequest):
    """Export invoices to Microinvest Delta Pro <TransferData xmlns="urn:Transfer"> XML."""
    from accounting_export import generate_microinvest_delta_xml
    if not request.invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoices list cannot be empty",
        )
    xml_content = generate_microinvest_delta_xml(
        request.invoices,
        default_expense_account=request.default_expense_account,
        default_goods_account=request.default_goods_account,
        default_vat_account=request.default_vat_account,
        default_supplier_account=request.default_supplier_account,
    )
    return Response(content=xml_content, media_type="application/xml")


@router.post(
    "/api/v1/export/microinvest/delta-csv",
    summary="Export to Microinvest Delta Pro Postings CSV",
    tags=["Accounting", "Microinvest ERP"],
)
async def export_microinvest_delta_csv_endpoint(request: MicroinvestExportRequest):
    """Export invoices to Microinvest Delta Pro double-entry postings CSV (CP1251)."""
    from invoice_core.microinvest_export import generate_microinvest_delta_csv
    if not request.invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoices list cannot be empty",
        )
    csv_bytes = generate_microinvest_delta_csv(
        request.invoices,
        default_expense_account=request.default_expense_account,
        default_goods_account=request.default_goods_account,
        default_vat_account=request.default_vat_account,
        default_supplier_account=request.default_supplier_account,
        encoding="windows-1251",
    )
    assert isinstance(csv_bytes, bytes)
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=Microinvest_Delta_Postings.csv"},
    )


@router.post(
    "/api/v1/export/business-navigator/csv",
    summary="Export to Business Navigator Delimited CSV",
    tags=["Accounting", "Business Navigator"],
)
async def export_business_navigator_csv_endpoint(request: BusinessNavigatorExportRequest):
    """Export invoices to Business Navigator delimited CSV format."""
    from invoice_core.business_navigator_export import generate_business_navigator_csv
    if not request.invoices:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoices list cannot be empty")
    csv_bytes = generate_business_navigator_csv(request.invoices, encoding=request.encoding)
    content = csv_bytes if isinstance(csv_bytes, bytes) else csv_bytes.encode(request.encoding)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=BN_IMPORT.csv"},
    )


@router.post(
    "/api/v1/export/business-navigator/txt",
    summary="Export to Business Navigator Section TXT",
    tags=["Accounting", "Business Navigator"],
)
async def export_business_navigator_txt_endpoint(request: BusinessNavigatorExportRequest):
    """Export invoices to Business Navigator section-based tagged TXT format."""
    from invoice_core.business_navigator_export import generate_business_navigator_section_txt
    if not request.invoices:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoices list cannot be empty")
    txt_bytes = generate_business_navigator_section_txt(request.invoices, encoding=request.encoding)
    content = txt_bytes if isinstance(txt_bytes, bytes) else txt_bytes.encode(request.encoding)
    return Response(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=BN_IMPORT.txt"},
    )


@router.post(
    "/api/v1/export/business-navigator/dbf",
    summary="Export to Business Navigator Single DBF",
    tags=["Accounting", "Business Navigator"],
)
async def export_business_navigator_dbf_endpoint(request: BusinessNavigatorExportRequest):
    """Export invoices to native binary dBase III / IV DBF file."""
    from invoice_core.business_navigator_export import generate_business_navigator_single_dbf
    if not request.invoices:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoices list cannot be empty")
    dbf_bytes = generate_business_navigator_single_dbf(request.invoices)
    return Response(
        content=dbf_bytes,
        media_type="application/x-dbf",
        headers={"Content-Disposition": "attachment; filename=BN_SINGLE.DBF"},
    )


@router.post(
    "/api/v1/export/business-navigator/package",
    summary="Export Complete Business Navigator Package (ZIP with DBF, CSV, TXT)",
    tags=["Accounting", "Business Navigator"],
)
async def export_business_navigator_package_endpoint(request: BusinessNavigatorExportRequest):
    """Export complete Business Navigator package with dual DBFs (DOKUM/OPER), CSV, TXT in a ZIP archive."""
    from invoice_core.business_navigator_export import export_business_navigator_package
    if not request.invoices:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoices list cannot be empty")
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = export_business_navigator_package(request.invoices, output_dir=tmp_dir, create_zip=True)
        zip_path = res["bn_zip"]
        zip_bytes = Path(zip_path).read_bytes()
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=Business_Navigator_Package.zip"},
    )


@router.post(
    "/api/v1/export/ajur",
    summary="Export to Ajur (Ажур-L / Ажур 7) CSV",
    tags=["Accounting", "Ajur ERP"],
)
async def export_ajur_endpoint(request: AjurExportRequest):
    """Export invoices to Ajur 7 / L import CSV format (CP1251)."""
    from invoice_core.ajur_export import generate_ajur_csv
    if not request.invoices:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoices list cannot be empty")
    csv_bytes = generate_ajur_csv(request.invoices, encoding=request.encoding)
    content = csv_bytes if isinstance(csv_bytes, bytes) else csv_bytes.encode(request.encoding)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=AJUR_IMPORT.csv"},
    )


# ---------------------------------------------------------------------------
# Official Bulgarian Chart of Accounts & Tax Period Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/api/v1/accounting/chart-of-accounts",
    summary="Query Official Bulgarian Chart of Accounts (НСС / Национален сметкоплан)",
    tags=["Accounting", "Chart of Accounts"],
)
async def get_chart_of_accounts_endpoint(
    q: Optional[str] = Query(default=None, description="Search query by code, name, keywords, or purpose"),
    account_class: Optional[int] = Query(default=None, ge=1, le=9, description="Filter by account class (1-9)"),
    limit: int = Query(default=50, ge=1, le=200, description="Max results to return"),
):
    """List or search accounts in the official Bulgarian National Chart of Accounts."""
    from invoice_core.chart_of_accounts import DEFAULT_CHART_OF_ACCOUNTS
    if q:
        results = DEFAULT_CHART_OF_ACCOUNTS.search(q, limit=limit)
    elif account_class:
        results = DEFAULT_CHART_OF_ACCOUNTS.get_by_class(account_class)[:limit]
    else:
        results = DEFAULT_CHART_OF_ACCOUNTS.all_accounts()[:limit]

    return {
        "count": len(results),
        "accounts": [a.to_dict() for a in results],
    }


@router.get(
    "/api/v1/accounting/chart-of-accounts/{code}",
    summary="Get Account Details and Purpose by Code",
    tags=["Accounting", "Chart of Accounts"],
)
async def get_account_detail_endpoint(code: str):
    """Get full details, legal purpose, and typical debit/credit conventions for an account."""
    from invoice_core.chart_of_accounts import DEFAULT_CHART_OF_ACCOUNTS
    acc = DEFAULT_CHART_OF_ACCOUNTS.get(code)
    if not acc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account '{code}' not found in the Bulgarian Chart of Accounts",
        )
    return {
        "account": acc.to_dict(),
        "explanation": DEFAULT_CHART_OF_ACCOUNTS.explain(code),
        "subaccounts": [s.to_dict() for s in DEFAULT_CHART_OF_ACCOUNTS.get_subaccounts(code)],
    }


@router.post(
    "/api/v1/accounting/validate-tax-period",
    summary="Validate Tax Period Compliance under Art. 124 VAT Act (чл. 124 ЗДДС)",
    tags=["Accounting", "Tax Period Validation"],
)
async def validate_tax_period_endpoint(request: TaxPeriodValidationApiRequest):
    """Check if document dates/tax events match the declared VAT period and statutory 12-month rule."""
    from invoice_core.tax_period_validator import TaxPeriodValidator
    if not request.invoices:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoices list cannot be empty")
    batch_res = TaxPeriodValidator.validate_batch(request.invoices, target_period=request.target_period)
    return batch_res


@router.get(
    "/api/v1/accounting/mapping-rules",
    summary="Get Active Account Mapping Engine Rules",
    tags=["Accounting", "Account Mapping"],
)
async def get_mapping_rules_endpoint():
    """Retrieve active supplier and keyword account mapping rules."""
    from invoice_core.account_mapping import DEFAULT_MAPPING_ENGINE
    return DEFAULT_MAPPING_ENGINE.to_dict()


@router.post(
    "/api/v1/accounting/mapping-rules/supplier",
    summary="Add or Update Supplier Account Mapping Rule",
    tags=["Accounting", "Account Mapping"],
)
async def add_supplier_mapping_rule_endpoint(request: SupplierMappingRuleApiRequest):
    """Register custom account mapping for a specific supplier EIK."""
    from invoice_core.account_mapping import DEFAULT_MAPPING_ENGINE
    DEFAULT_MAPPING_ENGINE.add_supplier_rule(
        eik=request.eik,
        account=request.target_account,
        subledger=request.target_subledger,
        supplier_name=request.supplier_name or "",
        description=request.description or "",
    )
    return {
        "status": "success",
        "message": f"Supplier rule registered for EIK {request.eik} -> Account {request.target_account}",
    }


@router.post(
    "/api/v1/accounting/mapping-rules/keyword",
    summary="Add or Update Keyword/Regex Account Mapping Rule",
    tags=["Accounting", "Account Mapping"],
)
async def add_keyword_mapping_rule_endpoint(request: KeywordMappingRuleApiRequest):
    """Register custom keyword or regex mapping rule for line item descriptions."""
    from invoice_core.account_mapping import DEFAULT_MAPPING_ENGINE
    DEFAULT_MAPPING_ENGINE.add_keyword_rule(
        rule_id=request.rule_id,
        account=request.target_account,
        keywords=request.keywords,
        regex_pattern=request.regex_pattern,
        subledger=request.target_subledger,
        priority=request.priority,
        description=request.description or "",
    )
    return {
        "status": "success",
        "message": f"Keyword rule '{request.rule_id}' registered -> Account {request.target_account}",
    }


# ---------------------------------------------------------------------------
# Microinvest Delta Pro Transfer File Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/api/v1/accounting/transfer-log/{document_id}",
    summary="Download Microinvest Delta Pro TRANSFER.LOG file",
    tags=["Accounting & Delta Pro"],
)
async def download_transfer_log(document_id: str, db: Session = Depends(get_db)):
    """Download native Microinvest Delta Pro Jet 2.0 binary TRANSFER.LOG file (65,536 bytes) for direct import."""
    acc_path = Path(f".stored_documents/accounting/{document_id}/TRANSFER.LOG")
    if acc_path.exists():
        return FileResponse(
            path=acc_path,
            filename="TRANSFER.LOG",
            media_type="application/octet-stream",
        )

    record = db.query(DocumentRecord).filter(DocumentRecord.id == document_id).first()
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    if record.ocr_result_json:
        try:
            ocr_data = json.loads(record.ocr_result_json)
            b64_log = (
                ocr_data.get("accounting_bundle", {}).get("transfer_files", {}).get("transfer_log_b64")
                or ocr_data.get("custom_metadata", {}).get("transfer_files", {}).get("transfer_log_b64")
            )
            if b64_log:
                content = base64.b64decode(b64_log)
                acc_path.parent.mkdir(parents=True, exist_ok=True)
                acc_path.write_bytes(content)
                return FileResponse(
                    path=acc_path,
                    filename="TRANSFER.LOG",
                    media_type="application/octet-stream",
                )
        except Exception as dec_err:
            logger.warning("Error recovering TRANSFER.LOG from JSON for %s: %s", document_id, dec_err)

    # If still not found, try generating on-the-fly
    try:
        from invoice_core.historical_matcher import process_invoice_and_create_accounting_package
        raw_info = json.loads(record.ocr_result_json) if record.ocr_result_json else record.to_dict()
        bundle = process_invoice_and_create_accounting_package(raw_info)
        if bundle and bundle.get("transfer_log_bytes"):
            acc_path.parent.mkdir(parents=True, exist_ok=True)
            acc_path.write_bytes(bundle["transfer_log_bytes"])
            if bundle.get("transfer_ldb_bytes"):
                (acc_path.parent / "TRANSFER.ldb").write_bytes(bundle["transfer_ldb_bytes"])
            return FileResponse(
                path=acc_path,
                filename="TRANSFER.LOG",
                media_type="application/octet-stream",
            )
    except Exception as gen_err:
        logger.warning("Error generating TRANSFER.LOG on the fly for %s: %s", document_id, gen_err)

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="TRANSFER.LOG file not found or could not be generated for this document",
    )


@router.get(
    "/api/v1/accounting/transfer-ldb/{document_id}",
    summary="Download Microinvest Delta Pro TRANSFER.ldb lock file",
    tags=["Accounting & Delta Pro"],
)
async def download_transfer_ldb(document_id: str, db: Session = Depends(get_db)):
    """Download companion Microinvest Delta Pro lock file TRANSFER.ldb (64 bytes)."""
    acc_path = Path(f".stored_documents/accounting/{document_id}/TRANSFER.ldb")
    if acc_path.exists():
        return FileResponse(
            path=acc_path,
            filename="TRANSFER.ldb",
            media_type="application/octet-stream",
        )

    record = db.query(DocumentRecord).filter(DocumentRecord.id == document_id).first()
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    if record.ocr_result_json:
        try:
            ocr_data = json.loads(record.ocr_result_json)
            b64_ldb = (
                ocr_data.get("accounting_bundle", {}).get("transfer_files", {}).get("transfer_ldb_b64")
                or ocr_data.get("custom_metadata", {}).get("transfer_files", {}).get("transfer_ldb_b64")
            )
            if b64_ldb:
                content = base64.b64decode(b64_ldb)
                acc_path.parent.mkdir(parents=True, exist_ok=True)
                acc_path.write_bytes(content)
                return FileResponse(
                    path=acc_path,
                    filename="TRANSFER.ldb",
                    media_type="application/octet-stream",
                )
        except Exception as dec_err:
            logger.warning("Error recovering TRANSFER.ldb from JSON for %s: %s", document_id, dec_err)

    # Generate companion ldb
    from invoice_core.delta_pro_generator import DELTA_PRO_LDB_TEMPLATE
    acc_path.parent.mkdir(parents=True, exist_ok=True)
    acc_path.write_bytes(DELTA_PRO_LDB_TEMPLATE)
    return FileResponse(
        path=acc_path,
        filename="TRANSFER.ldb",
        media_type="application/octet-stream",
    )


@router.get(
    "/api/v1/accounting/package/{document_id}",
    summary="Download complete Microinvest Delta Pro import ZIP package",
    tags=["Accounting & Delta Pro"],
)
async def download_delta_pro_package(document_id: str, db: Session = Depends(get_db)):
    """Download ZIP archive containing TRANSFER.LOG, TRANSFER.ldb, and accounting metadata."""
    acc_path_log = Path(f".stored_documents/accounting/{document_id}/TRANSFER.LOG")
    acc_path_ldb = Path(f".stored_documents/accounting/{document_id}/TRANSFER.ldb")
    acc_path_json = Path(f".stored_documents/accounting/{document_id}/accounting_operation.json")

    # Generate if not present
    if not acc_path_log.exists():
        record = db.query(DocumentRecord).filter(DocumentRecord.id == document_id).first()
        if not record:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        try:
            from invoice_core.historical_matcher import process_invoice_and_create_accounting_package
            raw_info = json.loads(record.ocr_result_json) if record.ocr_result_json else record.to_dict()
            bundle = process_invoice_and_create_accounting_package(raw_info)
            if bundle and bundle.get("transfer_log_bytes"):
                acc_path_log.parent.mkdir(parents=True, exist_ok=True)
                acc_path_log.write_bytes(bundle["transfer_log_bytes"])
                if bundle.get("transfer_ldb_bytes"):
                    acc_path_ldb.write_bytes(bundle["transfer_ldb_bytes"])
                (acc_path_log.parent / "accounting_operation.json").write_text(
                    json.dumps(bundle, default=str, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception as e:
            logger.warning("Failed on-the-fly bundle generation for %s: %s", document_id, e)

    if not acc_path_log.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delta Pro package could not be generated")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("TRANSFER.LOG", acc_path_log.read_bytes())
        if acc_path_ldb.exists():
            zf.writestr("TRANSFER.ldb", acc_path_ldb.read_bytes())
        else:
            from invoice_core.delta_pro_generator import DELTA_PRO_LDB_TEMPLATE
            zf.writestr("TRANSFER.ldb", DELTA_PRO_LDB_TEMPLATE)
        if acc_path_json.exists():
            zf.writestr("accounting_operation.json", acc_path_json.read_bytes())

    zip_buf.seek(0)
    return Response(
        content=zip_buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="Delta_Pro_Import_{document_id[:8]}.zip"'
        },
    )


@router.post(
    "/api/v1/accounting/batch-transfer-log",
    summary="Generate Consolidated Microinvest Delta Pro TRANSFER.LOG for Multiple Invoices",
    tags=["Accounting & Delta Pro"],
)
async def generate_batch_transfer_log(
    req: BatchTransferLogRequest,
    db: Session = Depends(get_db),
):
    """Generate consolidated Jet 2.0 binary TRANSFER.LOG & TRANSFER.ldb for multiple invoices."""
    from invoice_core.delta_pro_generator import generate_multi_delta_pro_transfer_log

    docs_payload = []
    # 1. Fetch requested or candidate documents
    if req.document_ids:
        records = db.query(DocumentRecord).filter(DocumentRecord.id.in_(req.document_ids)).all()
    else:
        records = db.query(DocumentRecord).filter(DocumentRecord.is_valid.is_(True)).limit(150).all()

    for rec in records:
        info = json.loads(rec.ocr_result_json) if rec.ocr_result_json else rec.to_dict()
        op = info.get("accounting_operation") or {}
        p = info.get("parties") or {}
        f = info.get("financials") or {}
        m = info.get("document_metadata") or {}

        inv_num = getattr(rec, "invoice_number", None) or m.get("invoice_number") or op.get("document_number") or "0000000000"
        doc_date = getattr(rec, "date_issued", None) or getattr(rec, "issue_date", None) or m.get("date_issued") or op.get("document_date") or ""
        is_cn = bool(getattr(rec, "is_credit_note", False) or m.get("is_credit_note") or op.get("is_credit_note"))

        supp_name = rec.supplier_name or p.get("counterpart_name") or op.get("contractor_name") or "МАГНЕЗИЯ ЕООД"
        supp_eik = rec.supplier_eik or p.get("counterpart_eik") or op.get("contractor_eik") or "114631464"
        supp_vat = f"BG{supp_eik}" if supp_eik else None

        base = float(rec.tax_base or f.get("tax_base") or op.get("tax_base") or 0.0)
        vat = float(rec.vat_amount or f.get("vat_amount") or op.get("vat_amount") or 0.0)
        total = float(rec.total_amount or f.get("total_amount") or op.get("total_amount") or 0.0)
        curr = rec.currency or f.get("currency") or op.get("currency") or "EUR"

        direction = (
            p.get("direction")
            or op.get("direction")
            or getattr(rec, "direction", None)
            or ("SALES" if getattr(rec, "doc_type", None) == "SALES" or p.get("counterpart_role") == "CLIENT" else "PURCHASE")
        )
        exp_acc = str(op.get("expense_account") or op.get("nominal_account") or ("601" if direction == "PURCHASE" else "702"))
        vat_acc = str(op.get("vat_account") or ("4531" if direction == "PURCHASE" else "4532"))
        cred_acc = str(op.get("counterpart_account") or ("401" if direction == "PURCHASE" else "411"))
        reason = str(op.get("reason") or "м-ли")

        distributions = op.get("distributions") or info.get("distributions")
        if not distributions and info.get("items") and len(info.get("items")) > 1:
            try:
                from invoice_core.account_mapping import split_invoice_postings
                distributions = split_invoice_postings(info, prefer_subaccounts=False)
            except Exception:
                distributions = None

        docs_payload.append({
            "document_metadata": {
                "invoice_number": inv_num,
                "date_issued": doc_date,
                "is_credit_note": is_cn,
            },
            "parties": {
                "direction": direction,
                "counterpart_name": supp_name,
                "counterpart_eik": supp_eik,
                "counterpart_vat": supp_vat,
            },
            "financials": {
                "tax_base": base,
                "vat_amount": vat,
                "total_amount": total,
                "currency": curr,
            },
            "accounting_operation": {
                "direction": direction,
                "expense_account": exp_acc,
                "nominal_account": exp_acc,
                "vat_account": vat_acc,
                "counterpart_account": cred_acc,
                "reason": reason,
                "distributions": distributions,
            },
            "items": info.get("items"),
            "distributions": distributions,
        })

    # If no DB records were found, fallback to cached Building 11 analysis if present
    if not docs_payload:
        cache_path = Path("scratch_kingston_analysis.json")
        if cache_path.exists():
            try:
                cached_docs = json.loads(cache_path.read_text("utf-8"))
                for c in cached_docs:
                    if c.get("document_metadata") and c.get("financials"):
                        docs_payload.append(c)
            except Exception as ce:
                logger.warning("Error reading scratch analysis fallback: %s", ce)

    if not docs_payload:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No valid invoice documents found to build batch TRANSFER.LOG",
        )

    # 2. Generate multi-document binary TRANSFER.LOG
    log_bytes, ldb_bytes = generate_multi_delta_pro_transfer_log(
        documents=docs_payload,
        client_company_name=req.client_company_name,
    )

    batch_id = uuid.uuid4().hex[:12]
    batch_dir = Path(f".stored_documents/accounting/batches/{batch_id}")
    batch_dir.mkdir(parents=True, exist_ok=True)

    log_path = batch_dir / "TRANSFER.LOG"
    ldb_path = batch_dir / "TRANSFER.ldb"
    summary_path = batch_dir / "batch_summary.json"

    log_path.write_bytes(log_bytes)
    ldb_path.write_bytes(ldb_bytes)

    tot_base = sum(d["financials"]["tax_base"] for d in docs_payload)
    tot_vat = sum(d["financials"]["vat_amount"] for d in docs_payload)
    tot_gross = sum(d["financials"]["total_amount"] for d in docs_payload)

    summary_data = {
        "batch_id": batch_id,
        "client_company": req.client_company_name,
        "client_eik": req.client_eik,
        "total_documents": len(docs_payload),
        "total_tax_base": round(tot_base, 2),
        "total_vat": round(tot_vat, 2),
        "total_gross": round(tot_gross, 2),
        "currency": docs_payload[0]["financials"].get("currency", "EUR") if docs_payload else "EUR",
        "generated_at": datetime.now().isoformat(),
        "transfer_log_size": len(log_bytes),
        "transfer_ldb_size": len(ldb_bytes),
        "invoices": [d["document_metadata"]["invoice_number"] for d in docs_payload],
    }
    summary_path.write_text(json.dumps(summary_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # 3. Auto-drop to designated folders
    drop_candidates = []
    if req.target_drop_dir:
        drop_candidates.append(Path(req.target_drop_dir))
    env_drop = os.environ.get("MICROINVEST_IMPORT_DIR")
    if env_drop:
        drop_candidates.append(Path(env_drop))
    # Check NO NAME usb flash drive if present
    usb_drop = Path("/Volumes/NO NAME/Building_11")
    if usb_drop.parent.exists():
        drop_candidates.append(usb_drop)
    # Check local project Building_11 folder
    local_drop = Path("Building_11")
    drop_candidates.append(local_drop)

    synced_locations = []
    for dpath in drop_candidates:
        try:
            dpath.mkdir(parents=True, exist_ok=True)
            (dpath / "TRANSFER.LOG").write_bytes(log_bytes)
            (dpath / "TRANSFER.ldb").write_bytes(ldb_bytes)
            synced_locations.append(str(dpath.resolve()))
        except Exception as se:
            logger.debug("Could not sync batch to %s: %s", dpath, se)

    # 4. Optional Obsidian dossier generation
    obsidian_file = None
    if req.sync_to_obsidian:
        try:
            from invoice_core.obsidian_sync import generate_obsidian_client_dossier
            period_label = req.period or datetime.now().strftime("%Y-%m")
            obs_path = generate_obsidian_client_dossier(
                client_eik=req.client_eik,
                client_name=req.client_company_name,
                invoices=docs_payload,
                period=period_label,
            )
            obsidian_file = str(obs_path)
        except Exception as oe:
            logger.warning("Obsidian auto-sync warning: %s", oe)

    # 5. Format responses
    if req.response_format == "log":
        return FileResponse(path=log_path, filename="TRANSFER.LOG", media_type="application/octet-stream")
    elif req.response_format == "zip":
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("TRANSFER.LOG", log_bytes)
            zf.writestr("TRANSFER.ldb", ldb_bytes)
            zf.writestr("batch_summary.json", summary_path.read_bytes())
        zip_buf.seek(0)
        return Response(
            content=zip_buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="Delta_Pro_Batch_{batch_id}.zip"'},
        )

    return {
        "ok": True,
        "batch_id": batch_id,
        "client_company": req.client_company_name,
        "client_eik": req.client_eik,
        "total_documents": len(docs_payload),
        "total_tax_base": round(tot_base, 2),
        "total_vat": round(tot_vat, 2),
        "total_gross": round(tot_gross, 2),
        "download_log_url": f"/api/v1/accounting/batches/{batch_id}/transfer-log",
        "download_ldb_url": f"/api/v1/accounting/batches/{batch_id}/transfer-ldb",
        "download_zip_url": f"/api/v1/accounting/batches/{batch_id}/package",
        "synced_drop_locations": synced_locations,
        "obsidian_dossier_path": obsidian_file,
    }


@router.get(
    "/api/v1/accounting/batches/{batch_id}/transfer-log",
    summary="Download Batch Microinvest Delta Pro TRANSFER.LOG file",
    tags=["Accounting & Delta Pro"],
)
async def download_batch_transfer_log(batch_id: str):
    p = Path(f".stored_documents/accounting/batches/{batch_id}/TRANSFER.LOG")
    if not p.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch TRANSFER.LOG not found")
    return FileResponse(path=p, filename="TRANSFER.LOG", media_type="application/octet-stream")


@router.get(
    "/api/v1/accounting/batches/{batch_id}/transfer-ldb",
    summary="Download Batch Microinvest Delta Pro TRANSFER.ldb file",
    tags=["Accounting & Delta Pro"],
)
async def download_batch_transfer_ldb(batch_id: str):
    p = Path(f".stored_documents/accounting/batches/{batch_id}/TRANSFER.ldb")
    if not p.exists():
        from invoice_core.delta_pro_generator import DELTA_PRO_LDB_TEMPLATE
        return Response(content=DELTA_PRO_LDB_TEMPLATE, media_type="application/octet-stream", headers={"Content-Disposition": 'attachment; filename="TRANSFER.ldb"'})
    return FileResponse(path=p, filename="TRANSFER.ldb", media_type="application/octet-stream")


@router.get(
    "/api/v1/accounting/batches/{batch_id}/package",
    summary="Download Batch Delta Pro ZIP Package",
    tags=["Accounting & Delta Pro"],
)
async def download_batch_package(batch_id: str):
    batch_dir = Path(f".stored_documents/accounting/batches/{batch_id}")
    log_p = batch_dir / "TRANSFER.LOG"
    ldb_p = batch_dir / "TRANSFER.ldb"
    sum_p = batch_dir / "batch_summary.json"

    if not log_p.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch package not found")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("TRANSFER.LOG", log_p.read_bytes())
        if ldb_p.exists():
            zf.writestr("TRANSFER.ldb", ldb_p.read_bytes())
        else:
            from invoice_core.delta_pro_generator import DELTA_PRO_LDB_TEMPLATE
            zf.writestr("TRANSFER.ldb", DELTA_PRO_LDB_TEMPLATE)
        if sum_p.exists():
            zf.writestr("batch_summary.json", sum_p.read_bytes())

    zip_buf.seek(0)
    return Response(
        content=zip_buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="Delta_Pro_Batch_{batch_id}.zip"'},
    )


@router.post(
    "/api/v1/accounting/sync-to-drop-folder",
    summary="Sync TRANSFER.LOG and TRANSFER.ldb to designated import folder / USB",
    tags=["Accounting & Delta Pro"],
)
async def sync_to_drop_folder(req: DropSyncRequest):
    target_p = Path(req.target_dir)
    try:
        target_p.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as ex:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot create or access target folder '{req.target_dir}': {ex}. Please verify permissions or mount path."
        )

    src_log = None
    src_ldb = None

    if req.batch_id:
        src_log = Path(f".stored_documents/accounting/batches/{req.batch_id}/TRANSFER.LOG")
        src_ldb = Path(f".stored_documents/accounting/batches/{req.batch_id}/TRANSFER.ldb")
    elif req.document_id:
        src_log = Path(f".stored_documents/accounting/{req.document_id}/TRANSFER.LOG")
        src_ldb = Path(f".stored_documents/accounting/{req.document_id}/TRANSFER.ldb")
    else:
        # Check canonical Building 11 or comparison_export
        for c in [Path("Building_11/TRANSFER.LOG"), Path("comparison_export/TRANSFER.LOG")]:
            if c.exists():
                src_log = c
                src_ldb = c.parent / "TRANSFER.ldb"
                break

    if not src_log or not src_log.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source TRANSFER.LOG file not found")

    dest_log = target_p / "TRANSFER.LOG"
    dest_ldb = target_p / "TRANSFER.ldb"

    try:
        dest_log.write_bytes(src_log.read_bytes())
        if src_ldb and src_ldb.exists():
            dest_ldb.write_bytes(src_ldb.read_bytes())
        else:
            from invoice_core.delta_pro_generator import DELTA_PRO_LDB_TEMPLATE
            dest_ldb.write_bytes(DELTA_PRO_LDB_TEMPLATE)
    except (PermissionError, OSError) as ex:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed writing TRANSFER files into '{req.target_dir}': {ex}"
        )

    return {
        "ok": True,
        "target_dir": str(target_p.resolve()),
        "synced_files": ["TRANSFER.LOG", "TRANSFER.ldb"],
        "transfer_log_size": dest_log.stat().st_size,
        "transfer_ldb_size": dest_ldb.stat().st_size,
    }


@router.post(
    "/api/v1/accounting/obsidian-sync",
    summary="Generate or Update Obsidian Accounting Dossier",
    tags=["Accounting & Delta Pro"],
)
async def sync_obsidian_dossier(req: ObsidianSyncRequest, db: Session = Depends(get_db)):
    from invoice_core.obsidian_sync import generate_obsidian_client_dossier

    docs_payload = []
    if req.document_ids:
        records = db.query(DocumentRecord).filter(DocumentRecord.id.in_(req.document_ids)).all()
    else:
        records = db.query(DocumentRecord).filter(DocumentRecord.is_valid.is_(True)).limit(150).all()

    for rec in records:
        info = json.loads(rec.ocr_result_json) if rec.ocr_result_json else rec.to_dict()
        docs_payload.append(info)

    if not docs_payload:
        cache_path = Path("scratch_kingston_analysis.json")
        if cache_path.exists():
            docs_payload = json.loads(cache_path.read_text("utf-8"))

    if not docs_payload:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No documents available to generate Obsidian note")

    out_file = generate_obsidian_client_dossier(
        client_eik=req.client_eik,
        client_name=req.client_company_name,
        invoices=docs_payload,
        period=req.period,
    )

    return {
        "ok": True,
        "obsidian_file": str(out_file),
        "size_bytes": out_file.stat().st_size,
        "client_company": req.client_company_name,
        "period": req.period,
    }


@router.post(
    "/api/v1/accounting/utm-bridge",
    summary="UTM Windows XP VM Direct Bridge & USB Controller",
    tags=["Accounting & Delta Pro"],
)
async def utm_vm_bridge_endpoint(req: UtmBridgeRequest):
    from invoice_core.utm_bridge import (
        DEFAULT_TRANSFER_ROOT,
        connect_usb_to_vm,
        disconnect_usb_from_vm,
        get_vm_status,
        list_usb_devices,
        stage_firm_transfer_files,
    )

    if req.action == "status":
        return get_vm_status(req.vm_name)
    elif req.action == "usb-list":
        return {"ok": True, "devices": list_usb_devices()}
    elif req.action == "usb-connect":
        if not req.device_id:
            raise HTTPException(status_code=400, detail="device_id is required for usb-connect")
        return connect_usb_to_vm(req.device_id, req.vm_name)
    elif req.action == "usb-disconnect":
        if not req.device_id:
            raise HTTPException(status_code=400, detail="device_id is required for usb-disconnect")
        return disconnect_usb_from_vm(req.device_id, req.vm_name)
    elif req.action == "stage":
        target = Path(req.drop_dir) if req.drop_dir else DEFAULT_TRANSFER_ROOT
        src = Path("Building_11")
        if not src.exists():
            src = Path(".stored_documents/accounting")
        return stage_firm_transfer_files(
            source_dir=src,
            target_drop_dir=target,
            firm_slug=req.firm_slug,
            firm_eik=req.firm_eik,
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {req.action}")


@router.post(
    "/api/v1/accounting/delta-pro/dispatch-to-vm",
    summary="Dispatch Microinvest Delta Pro TRANSFER.LOG to Windows VM Hot-Folder",
    tags=["Accounting & Delta Pro"],
)
async def dispatch_delta_pro_to_vm(
    req: DeltaProVmDispatchRequest,
    db: Session = Depends(get_db),
):
    """Dispatch generated Jet 2.0 binary TRANSFER.LOG to a Windows VM hot-folder with SHA-256 hashing and audit trail logging."""
    from invoice_core.delta_pro_generator import DELTA_PRO_LDB_TEMPLATE

    log_bytes: Optional[bytes] = None
    ldb_bytes: bytes = DELTA_PRO_LDB_TEMPLATE

    # 1. Resolve source TRANSFER.LOG content
    if req.transfer_log_bytes_b64:
        try:
            log_bytes = base64.b64decode(req.transfer_log_bytes_b64)
        except Exception as ex:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid base64 payload: {ex}")
    elif req.batch_id:
        batch_dir = Path(f".stored_documents/accounting/batches/{req.batch_id}")
        batch_log = batch_dir / "TRANSFER.LOG"
        batch_ldb = batch_dir / "TRANSFER.ldb"
        if batch_log.exists():
            log_bytes = batch_log.read_bytes()
            if batch_ldb.exists():
                ldb_bytes = batch_ldb.read_bytes()
    elif req.document_id:
        doc_dir = Path(f".stored_documents/accounting/{req.document_id}")
        doc_log = doc_dir / "TRANSFER.LOG"
        doc_ldb = doc_dir / "TRANSFER.ldb"
        if doc_log.exists():
            log_bytes = doc_log.read_bytes()
            if doc_ldb.exists():
                ldb_bytes = doc_ldb.read_bytes()
        else:
            record = db.query(DocumentRecord).filter(DocumentRecord.id == req.document_id).first()
            if record and record.ocr_result_json:
                try:
                    from invoice_core.historical_matcher import process_invoice_and_create_accounting_package
                    bundle = process_invoice_and_create_accounting_package(json.loads(record.ocr_result_json))
                    if bundle and bundle.get("transfer_log_bytes"):
                        log_bytes = bundle["transfer_log_bytes"]
                        if bundle.get("transfer_ldb_bytes"):
                            ldb_bytes = bundle["transfer_ldb_bytes"]
                except Exception as gen_err:
                    logger.warning("Could not generate transfer log for %s: %s", req.document_id, gen_err)

    # Fallback to firm folders or comparison export
    if not log_bytes:
        candidate_paths = [
            Path(f"{req.firm_slug}/TRANSFER.LOG"),
            Path(f"{req.firm_eik}_{req.firm_slug}/TRANSFER.LOG"),
            Path("Building_11/TRANSFER.LOG"),
            Path("comparison_export/TRANSFER.LOG"),
            Path(".stored_documents/accounting/TRANSFER.LOG"),
        ]
        for cp in candidate_paths:
            if cp.exists():
                log_bytes = cp.read_bytes()
                c_ldb = cp.parent / "TRANSFER.ldb"
                if c_ldb.exists():
                    ldb_bytes = c_ldb.read_bytes()
                break

    if not log_bytes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No source TRANSFER.LOG file found to dispatch. Please generate one first or provide a batch_id / document_id / base64 payload."
        )

    # 2. Compute SHA-256 hash
    sha256_hash = hashlib.sha256(log_bytes).hexdigest()

    # 3. Determine target hot-folder path
    if req.target_hot_folder:
        hot_folder = Path(req.target_hot_folder)
    else:
        env_target = os.environ.get("DELTA_PRO_VM_HOTFOLDER") or os.environ.get("DELTA_PRO_HOTFOLDER")
        if env_target:
            hot_folder = Path(env_target)
        else:
            candidate_vm_dirs = [
                Path(f"/Users/diokarabaz/teamwork_projects/microinvest_vm_validation/wine_prefix/drive_c/MICRO/{req.firm_slug}"),
                Path(f"/Volumes/VM_SHARED/TRANSFER_IN/{req.firm_slug}"),
                Path(f"/tmp/microinvest_vm_hotfolder/{req.firm_slug}"),
            ]
            hot_folder = candidate_vm_dirs[-1]
            for cvd in candidate_vm_dirs:
                if cvd.parent.exists():
                    hot_folder = cvd
                    break

    try:
        hot_folder.mkdir(parents=True, exist_ok=True)
        dest_log = hot_folder / "TRANSFER.LOG"
        dest_ldb = hot_folder / "TRANSFER.ldb"
        dest_log.write_bytes(log_bytes)
        dest_ldb.write_bytes(ldb_bytes)
    except Exception as write_err:
        logger.error("Failed writing TRANSFER files to VM hot-folder: %s", write_err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed writing TRANSFER files to VM hot-folder: {write_err}"
        )

    # 4. Record Audit Trail in Database
    doc_record = None
    if req.document_id:
        doc_record = db.query(DocumentRecord).filter(DocumentRecord.id == req.document_id).first()
    if not doc_record and req.firm_eik:
        doc_record = db.query(DocumentRecord).filter(DocumentRecord.recipient_eik == req.firm_eik).first()
    if not doc_record:
        doc_record = db.query(DocumentRecord).first()

    audit_id: Optional[int] = None
    if doc_record:
        try:
            audit_entry = AuditTrailRecord(
                document_id=doc_record.id,
                actor=req.actor,
                action="delta_pro_vm_dispatch",
                field_name="TRANSFER.LOG",
                old_value=None,
                new_value=sha256_hash,
                details_json=json.dumps({
                    "vm_name": req.vm_name,
                    "destination_path": str(dest_log.resolve()),
                    "destination_hot_folder": str(hot_folder.resolve()),
                    "file_name": "TRANSFER.LOG",
                    "file_size": len(log_bytes),
                    "sha256": sha256_hash,
                    "firm_eik": req.firm_eik,
                    "firm_slug": req.firm_slug,
                    "batch_id": req.batch_id,
                    "dispatched_at": datetime.now().isoformat(),
                }, ensure_ascii=False),
            )
            db.add(audit_entry)
            db.commit()
            db.refresh(audit_entry)
            audit_id = audit_entry.id
        except Exception as audit_err:
            logger.warning("Could not persist audit record: %s", audit_err)
            db.rollback()

    return {
        "ok": True,
        "status": "dispatched",
        "file_name": "TRANSFER.LOG",
        "file_size": len(log_bytes),
        "sha256": sha256_hash,
        "destination_path": str(dest_log.resolve()),
        "destination_hot_folder": str(hot_folder.resolve()),
        "companion_ldb_dispatched": dest_ldb.exists(),
        "vm_name": req.vm_name,
        "firm_eik": req.firm_eik,
        "firm_slug": req.firm_slug,
        "dispatched_at": datetime.now().isoformat(),
        "audit_trail_id": audit_id,
        "document_id": doc_record.id if doc_record else req.document_id,
    }


@router.get(
    "/api/v1/accounting/operation/{document_id}",
    summary="Get Statutory Accounting Operation & Historical Comparison Details",
    tags=["Accounting & Delta Pro"],
)
async def get_accounting_operation_details(document_id: str, db: Session = Depends(get_db)):
    record = db.query(DocumentRecord).filter(DocumentRecord.id == document_id).first()
    acc_json_path = Path(f".stored_documents/accounting/{document_id}/accounting_operation.json")
    if not record and acc_json_path.exists():
        try:
            saved_bundle = json.loads(acc_json_path.read_text(encoding="utf-8"))
            log_path = Path(f".stored_documents/accounting/{document_id}/TRANSFER.LOG")
            ldb_path = Path(f".stored_documents/accounting/{document_id}/TRANSFER.ldb")
            return {
                "document_id": document_id,
                "file_name": saved_bundle.get("file_name", document_id),
                "status": "completed",
                "accounting_operation": saved_bundle.get("accounting_operation"),
                "historical_match_report": saved_bundle.get("match_report"),
                "transfer_files": {
                    "has_transfer_log": log_path.exists(),
                    "has_transfer_ldb": ldb_path.exists(),
                    "download_log_url": f"/api/v1/accounting/transfer-log/{document_id}",
                    "download_ldb_url": f"/api/v1/accounting/transfer-ldb/{document_id}",
                },
            }
        except Exception:
            pass

    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    ocr_data = {}
    if record.ocr_result_json:
        try:
            ocr_data = json.loads(record.ocr_result_json)
        except Exception:
            pass

    acc_op = ocr_data.get("accounting_operation")
    match_rep = ocr_data.get("historical_match_report")
    bundle = ocr_data.get("accounting_bundle")

    if not acc_op and bundle:
        acc_op = bundle.get("accounting_operation")
    if not match_rep and bundle:
        match_rep = bundle.get("match_report")

    if not acc_op:
        try:
            from invoice_core.historical_matcher import process_invoice_and_create_accounting_package
            new_bundle = process_invoice_and_create_accounting_package(ocr_data or record.to_dict())
            if new_bundle:
                acc_op = new_bundle.get("accounting_operation")
                match_rep = new_bundle.get("match_report")
        except Exception as gen_err:
            logger.warning("Could not generate accounting operation for %s: %s", document_id, gen_err)

    if not acc_op:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Accounting operation not available for this document (e.g. non-commercial document or critical contractor mismatch)",
        )

    log_path = Path(f".stored_documents/accounting/{document_id}/TRANSFER.LOG")
    ldb_path = Path(f".stored_documents/accounting/{document_id}/TRANSFER.ldb")

    return {
        "document_id": document_id,
        "file_name": record.file_name,
        "status": record.status,
        "accounting_operation": acc_op,
        "historical_match_report": match_rep,
        "transfer_files": {
            "has_transfer_log": log_path.exists(),
            "has_transfer_ldb": ldb_path.exists(),
            "download_log_url": f"/api/v1/accounting/transfer-log/{document_id}",
            "download_ldb_url": f"/api/v1/accounting/transfer-ldb/{document_id}",
        },
    }

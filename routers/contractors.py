"""FastAPI Router for Contractor Verification, Business Registry Search, and Vendor Profiles."""

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import DocumentRecord, get_db

logger = logging.getLogger("invoice_ocr_api")
router = APIRouter()


class ContractorVerifyRequest(BaseModel):
    identifier: str = Field(..., description="EIK, Bulstat, or foreign VAT number", json_schema_extra={"example": "121644736"})
    country_code: Optional[str] = Field(default=None, description="Country code (e.g. BG, IE, DE, LU)", json_schema_extra={"example": "BG"})
    date_tax_event: Optional[str] = Field(default=None, description="Tax event transaction date (YYYY-MM-DD)", json_schema_extra={"example": "2026-08-16"})
    bypass_cache: bool = Field(default=False, description="Bypass local cache and query live registry")


@router.post(
    "/api/v1/verify/contractor",
    summary="Real-Time Contractor Verification (TR, NRA, VIES)",
    tags=["Verification"],
)
@router.post(
    "/api/v1/contractors/verify",
    summary="Real-Time Contractor Verification (TR, NRA, VIES) (Alias)",
    tags=["Verification"],
)
async def verify_contractor_endpoint(request: ContractorVerifyRequest):
    """Verify company status in Commercial Register, NRA VAT register (Art. 94), or EU VIES."""
    from contractor_verification import verify_contractor_async
    try:
        res = await verify_contractor_async(
            identifier=request.identifier,
            country_code=request.country_code,
            date_tax_event=request.date_tax_event,
            bypass_cache=request.bypass_cache,
        )
        return JSONResponse(content=res.to_dict())
    except Exception as exc:
        logger.error("Contractor verification error: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Verification failed: {exc}",
        )


@router.get(
    "/api/businesses/search",
    tags=["Business Search (Local)"],
    summary="Local Business & EIK Search (Replaces CompanyBook API)",
)
@router.post(
    "/api/businesses/search",
    tags=["Business Search (Local)"],
    summary="Local Business & EIK Search (Replaces CompanyBook API)",
)
async def search_business_local(
    q: str = Query(default=""),
    enrich: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    """Local company registry and EIK search using SQLite cache, vendor profiles, and Modulo 11 check."""
    from invoice_core.vendor_profiles import get_vendor_profile, list_known_profiles
    from invoice_core.normalizers import validate_eik

    clean_q = q.strip()
    if not clean_q:
        return {"success": True, "results": []}

    results = []
    seen_eiks = set()

    # 1. Search in YAML vendor profiles
    vp = get_vendor_profile(clean_q)
    if vp:
        eik = vp.get("eik") or clean_q
        seen_eiks.add(eik)
        results.append({
            "name": vp.get("name"),
            "eik": eik,
            "vat_number": vp.get("vat_number") or f"BG{eik}",
            "address": vp.get("address"),
            "source": "local_vendor_profile",
        })

    for p in list_known_profiles():
        if p.get("eik") and p["eik"] not in seen_eiks:
            if clean_q.lower() in p.get("name", "").lower() or clean_q in p.get("eik", ""):
                seen_eiks.add(p["eik"])
                results.append({
                    "name": p.get("name"),
                    "eik": p.get("eik"),
                    "vat_number": p.get("vat_number") or f"BG{p['eik']}",
                    "address": p.get("address"),
                    "source": "local_vendor_profile",
                })

    # 2. Search in local database records
    db_records = (
        db.query(DocumentRecord)
        .filter(
            (DocumentRecord.supplier_eik == clean_q)
            | (DocumentRecord.supplier_name.ilike(f"%{clean_q}%"))
        )
        .limit(10)
        .all()
    )
    for r in db_records:
        if r.supplier_eik and r.supplier_eik not in seen_eiks:
            seen_eiks.add(r.supplier_eik)
            results.append({
                "name": r.supplier_name,
                "eik": r.supplier_eik,
                "vat_number": r.supplier_vat or f"BG{r.supplier_eik}",
                "address": r.supplier_address,
                "source": "local_database_cache",
            })

    # 3. If query is a valid Bulgarian EIK checksum, provide verified synthetic candidate
    digits_only = re.sub(r"\D", "", clean_q)
    if digits_only and digits_only not in seen_eiks:
        if validate_eik(digits_only):
            results.append({
                "name": f"Търговец с ЕИК {digits_only}",
                "eik": digits_only,
                "vat_number": f"BG{digits_only}",
                "address": None,
                "source": "local_modulo11_verified",
            })

    return {"success": True, "results": results}


@router.get(
    "/api/v1/vendors",
    tags=["Vendor Profiles"],
    summary="List all loaded vendor profiles and configuration status",
)
async def list_vendor_profiles_endpoint():
    """Retrieve all active vendor profiles loaded from YAML configuration files."""
    from invoice_core.vendor_profiles import get_vendor_profile_loader

    loader = get_vendor_profile_loader()
    profiles = loader.get_profiles()
    diag = loader.get_diagnostics()

    vendors = [p.to_dict() for p in profiles.values()]
    return {
        "success": True,
        "total": len(vendors),
        "vendors": vendors,
        "last_reloaded": diag.get("last_reloaded"),
        "errors": diag.get("errors", []),
        "config_dirs": diag.get("config_dirs", []),
    }


@router.get(
    "/api/v1/vendors/{vendor_id}",
    tags=["Vendor Profiles"],
    summary="Get details of a specific vendor profile",
)
async def get_vendor_profile_endpoint(vendor_id: str):
    """Retrieve details for a specific vendor profile by ID, EIK, or keyword."""
    from invoice_core.vendor_profiles import get_vendor_profile_loader

    loader = get_vendor_profile_loader()
    prof = loader.get_profile(vendor_id)
    if not prof:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Vendor profile '{vendor_id}' not found",
        )
    return {
        "success": True,
        "vendor": prof.to_dict(),
    }


@router.post(
    "/api/v1/vendors/reload",
    tags=["Vendor Profiles"],
    summary="Dynamic hot-reload of vendor profiles from YAML files without server restart",
)
async def reload_vendor_profiles_endpoint():
    """Hot-reload vendor profiles from YAML files on disk without stopping the service."""
    from invoice_core.vendor_profiles import get_vendor_profile_loader

    loader = get_vendor_profile_loader()
    result = loader.reload()
    return {
        "success": True,
        "message": f"Successfully reloaded {result['loaded_count']} vendor profiles",
        "reloaded_count": result["loaded_count"],
        "vendors": result["profiles"],
        "errors": result.get("errors", []),
        "timestamp": result["reloaded_at"],
    }

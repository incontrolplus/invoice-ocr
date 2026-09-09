"""Reinforcement Learning from Human Feedback (RLHF) & Vendor Layout Learning Engine.

Learns continuously from manual accountant corrections in the HITL dashboard:
1. Derives invoice number series patterns (prefixes, lengths, regexes).
2. Deduces dot-matrix homoglyph character mappings (e.g. n->0, e->6, a->9, c->6).
3. Learns 2D spatial bounding box priors for key fields (invoice_number, totals, EIK).
4. Auto-creates and updates vendor profile YAML configurations under config/vendors/.
5. Hot-reloads memory caches instantly with zero downtime.
6. Maintains immutable audit exemplars in the database for tracking learning progress.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
from typing import Any, Optional
import uuid

import yaml
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from .constants import PROJECT_ROOT
from .vendor_profiles import (
    CONFIG_VENDORS_DIR,
    VendorProfile,
    get_vendor_profile_loader,
    get_vendor_profiles,
    reset_vendor_profiles_cache,
)

logger = logging.getLogger("invoice_ocr.feedback_learning")


# ---------------------------------------------------------------------------
# Utility Functions: Coordinate Normalization & Feature Deducers
# ---------------------------------------------------------------------------

def normalize_bbox(
    bbox: Any,
    page_width: Optional[float] = None,
    page_height: Optional[float] = None,
) -> Optional[list[float]]:
    """Normalize pixel bounding box [left, top, width, height] to relative [0.0 - 1.0]."""
    if not bbox or not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return None

    try:
        left, top, width, height = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
    except (ValueError, TypeError):
        return None

    # If coordinates are already normalized between 0.0 and 1.0
    if 0.0 <= left <= 1.0 and 0.0 <= top <= 1.0 and width <= 1.0 and height <= 1.0:
        return [round(left, 4), round(top, 4), round(width, 4), round(height, 4)]

    # Use provided page dimensions or realistic A4 scanning dimensions (2480x3508)
    pw = float(page_width) if page_width and page_width > 0 else 2480.0
    ph = float(page_height) if page_height and page_height > 0 else 3508.0

    rel_x = max(0.0, min(1.0, round(left / pw, 4)))
    rel_y = max(0.0, min(1.0, round(top / ph, 4)))
    rel_w = max(0.0, min(1.0, round(width / pw, 4)))
    rel_h = max(0.0, min(1.0, round(height / ph, 4)))

    return [rel_x, rel_y, rel_w, rel_h]


def deduce_invoice_number_rules(
    corrected_value: str,
    raw_token_text: Optional[str] = None,
) -> dict[str, Any]:
    """Derive prefix, length, regex pattern, and homoglyphs from a corrected invoice number."""
    clean = re.sub(r"\D", "", str(corrected_value).strip())
    if not clean:
        return {}

    num_len = len(clean)
    rules: dict[str, Any] = {
        "length": num_len,
        "sample": clean,
    }

    # Detect leading zero prefix (e.g. 0000006960 -> prefix '000000' or '0')
    zero_match = re.match(r"^(0+)", clean)
    if zero_match and len(zero_match.group(1)) >= 2:
        prefix = zero_match.group(1)
        rules["prefix"] = prefix
        rules["regex"] = f"^{prefix}\\d{{{num_len - len(prefix)}}}$"
    elif clean.startswith("0"):
        rules["prefix"] = "0"
        rules["regex"] = f"^0\\d{{{num_len - 1}}}$"
    elif num_len == 10:
        # Check standard 4-digit or 6-digit series prefix (e.g. 100099, 0703)
        prefix = clean[:4]
        rules["prefix"] = prefix
        rules["regex"] = f"^{prefix}\\d{{{num_len - len(prefix)}}}$"
    else:
        rules["prefix"] = clean[:2]
        rules["regex"] = f"^\\d{{{num_len}}}$"

    # Detect homoglyph mappings if raw token text was non-numeric
    homoglyphs: dict[str, str] = {}
    is_dot_matrix = False

    if raw_token_text:
        raw_clean = re.sub(r"\s+", "", str(raw_token_text))
        if not raw_clean.isdigit() and len(raw_clean) == num_len:
            for r_ch, c_ch in zip(raw_clean, clean):
                if r_ch != c_ch and not r_ch.isdigit() and c_ch.isdigit():
                    homoglyphs[r_ch] = c_ch
                    if r_ch.islower():
                        homoglyphs[r_ch.upper()] = c_ch
                    elif r_ch.isupper():
                        homoglyphs[r_ch.lower()] = c_ch
            if homoglyphs:
                is_dot_matrix = True
        elif any(c in raw_clean.lower() for c in ["n", "e", "a", "c"]):
            is_dot_matrix = True

    if homoglyphs:
        rules["homoglyphs"] = homoglyphs
    if is_dot_matrix:
        rules["dot_matrix"] = True

    return rules


def extract_company_keywords(name: str) -> list[str]:
    """Generate clean vendor keywords from company name."""
    if not name:
        return []

    # Strip legal form words (ЕООД, ООД, АД, ЕАД, ДЗЗД, etc.)
    cleaned = re.sub(r"(?i)\b(еоод|оод|еад|ад|дззд|сд|ет|кд|чуждестранно\s*лице)\b", "", name)
    cleaned = re.sub(r'[^\w\s\-]', " ", cleaned)
    tokens = [t.strip().lower() for t in cleaned.split() if len(t.strip()) >= 3]

    results: list[str] = []
    if name.strip():
        results.append(name.strip().lower())
    for t in tokens:
        if t not in results:
            results.append(t)
    return results[:5]


# ---------------------------------------------------------------------------
# Vendor Profile Synthesis & Dynamic Persistence
# ---------------------------------------------------------------------------

def update_or_create_vendor_profile(
    supplier_eik: str,
    supplier_name: Optional[str] = None,
    supplier_vat: Optional[str] = None,
    supplier_address: Optional[str] = None,
    series_rules: Optional[dict[str, Any]] = None,
    spatial_prior: Optional[dict[str, Any]] = None,
    homoglyphs: Optional[dict[str, str]] = None,
    dot_matrix: Optional[bool] = None,
) -> tuple[str, Path, bool]:
    """Create or update a vendor profile YAML file dynamically and reload cache.

    Returns:
        (profile_id, file_path, is_new_profile)
    """
    clean_eik = re.sub(r"\D", "", str(supplier_eik).strip())
    if not clean_eik:
        raise ValueError("Cannot learn vendor profile without valid supplier EIK")

    CONFIG_VENDORS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Discover existing profile by EIK
    existing_loader = get_vendor_profile_loader()
    existing_profile = existing_loader.get_profile(clean_eik)

    target_file: Optional[Path] = None
    target_id: str = f"learned_{clean_eik}"
    is_new = True

    if existing_profile:
        target_id = existing_profile.id
        # Search for corresponding YAML file in CONFIG_VENDORS_DIR
        for p in CONFIG_VENDORS_DIR.glob("*.yaml"):
            if p.stem == target_id:
                target_file = p
                is_new = False
                break
        if not target_file:
            # Check by filename slug
            cand = CONFIG_VENDORS_DIR / f"{target_id}.yaml"
            if cand.exists():
                target_file = cand
                is_new = False

    if not target_file:
        target_file = CONFIG_VENDORS_DIR / f"learned_{clean_eik}.yaml"
        is_new = True

    # 2. Load existing YAML content or initialize default dict
    profile_data: dict[str, Any] = {}
    if target_file.exists():
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, dict):
                    profile_data = loaded
        except Exception as exc:
            logger.warning("Failed to parse existing YAML %s: %s", target_file, exc)

    if not profile_data:
        profile_data = {
            "id": target_id,
            "name": supplier_name or f"Доставчик ЕИК {clean_eik}",
            "eik": clean_eik,
            "vat": supplier_vat or f"BG{clean_eik}",
            "address": supplier_address or "",
            "keywords": extract_company_keywords(supplier_name or ""),
            "invoice_number_series": {},
            "spatial_priors": {},
            "dot_matrix": False,
            "ocr": {},
        }

    # 3. Merge newly learned data
    if supplier_name and (not profile_data.get("name") or "Доставчик ЕИК" in profile_data["name"]):
        profile_data["name"] = supplier_name
        kws = profile_data.setdefault("keywords", [])
        for kw in extract_company_keywords(supplier_name):
            if kw not in kws:
                kws.append(kw)

    if supplier_vat:
        profile_data["vat"] = supplier_vat
    if supplier_address and not profile_data.get("address"):
        profile_data["address"] = supplier_address

    # Merge invoice number series
    if series_rules:
        curr_series = profile_data.setdefault("invoice_number_series", {})
        if "prefix" in series_rules:
            curr_series["prefix"] = series_rules["prefix"]
        if "length" in series_rules:
            curr_series["length"] = series_rules["length"]
        if "regex" in series_rules:
            curr_series["regex"] = series_rules["regex"]
        if "sample" in series_rules:
            curr_series["sample"] = series_rules["sample"]

    # Merge spatial priors
    if spatial_prior:
        field_name = spatial_prior.get("field")
        bbox = spatial_prior.get("bbox")
        if field_name and bbox:
            curr_priors = profile_data.setdefault("spatial_priors", {})
            curr_priors[field_name] = {
                "bbox": bbox,
                "page": spatial_prior.get("page", 1),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

    # Merge dot-matrix & homoglyphs
    if dot_matrix is not None and dot_matrix:
        profile_data["dot_matrix"] = True
        ocr_section = profile_data.setdefault("ocr", {})
        ocr_section["dot_matrix"] = True

    if homoglyphs:
        ocr_section = profile_data.setdefault("ocr", {})
        existing_homo = ocr_section.setdefault("homoglyphs", {})
        existing_homo.update(homoglyphs)
        profile_data["dot_matrix"] = True

    # 4. Save YAML atomically
    tmp_file = target_file.with_suffix(".tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            profile_data,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
    tmp_file.replace(target_file)
    logger.info("Saved learned vendor profile to %s (id: %s)", target_file, target_id)

    # 5. Hot-reload cache immediately
    reset_vendor_profiles_cache()

    return target_id, target_file, is_new


# ---------------------------------------------------------------------------
# Core Learning Pipeline: Human Feedback Ingestion
# ---------------------------------------------------------------------------

def process_human_feedback(
    db: Session,
    record: Any,
    diffs: list[dict[str, Any]],
    corrections: dict[str, Any],
    actor: str = "accountant",
    feedback_metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Process manual accountant corrections and update the learning models."""
    from database import InvoiceFeedbackRecord

    if not diffs and not corrections:
        return {"status": "no_changes"}

    meta = feedback_metadata or {}
    raw_token_text = meta.get("raw_token_text")
    token_bbox = meta.get("token_bbox")
    target_field = meta.get("field_name")
    page_w = meta.get("page_width")
    page_h = meta.get("page_height")

    # Supplier identity
    supp_eik = corrections.get("supplier_eik") or getattr(record, "supplier_eik", None)
    clean_eik = re.sub(r"\D", "", str(supp_eik or ""))
    supp_name = corrections.get("supplier_name") or getattr(record, "supplier_name", None)
    supp_vat = corrections.get("supplier_vat") or getattr(record, "supplier_vat", None)
    supp_addr = corrections.get("supplier_address") or getattr(record, "supplier_address", None)

    rules_learned: list[str] = []
    series_rules: Optional[dict[str, Any]] = None
    spatial_prior: Optional[dict[str, Any]] = None
    homoglyphs: Optional[dict[str, str]] = None
    dot_matrix_flag: bool = False

    # 1. Process each changed field
    for diff in diffs:
        field = diff["field"]
        old_val = str(diff.get("old", ""))
        new_val = str(diff.get("new", ""))

        rule_type = "field_value_override"
        details: dict[str, Any] = {"old": old_val, "new": new_val}

        # Normalize bbox if this field was explicitly clicked
        norm_bbox = None
        if token_bbox and (not target_field or target_field == field):
            norm_bbox = normalize_bbox(token_bbox, page_width=page_w, page_height=page_h)
            if norm_bbox:
                spatial_prior = {"field": field, "bbox": norm_bbox, "page": 1}
                rule_type = "spatial_prior_and_value"
                rules_learned.append(f"spatial_prior:{field}")

        if field == "invoice_number":
            inv_rules = deduce_invoice_number_rules(new_val, raw_token_text=raw_token_text)
            if inv_rules:
                series_rules = inv_rules
                rule_type = "invoice_number_series"
                rules_learned.append(f"series_prefix:{inv_rules.get('prefix')}")
                if "homoglyphs" in inv_rules:
                    homoglyphs = inv_rules["homoglyphs"]
                    dot_matrix_flag = True
                    rules_learned.append(f"homoglyphs:{','.join(homoglyphs.keys())}")
                if inv_rules.get("dot_matrix"):
                    dot_matrix_flag = True
                    rules_learned.append("dot_matrix_profile")
                details["series_rules"] = inv_rules

        elif field in ("supplier_name", "supplier_eik", "supplier_vat"):
            rule_type = "vendor_identity"
            rules_learned.append(f"vendor_{field}")

        elif field in ("tax_base", "vat_amount", "total_amount"):
            rule_type = "financial_rule"
            rules_learned.append(f"financial_{field}")

        # Persist feedback exemplar record in database
        feedback_rec = InvoiceFeedbackRecord(
            document_id=record.id,
            supplier_eik=clean_eik or None,
            supplier_name=supp_name or None,
            field_name=field,
            original_value=old_val,
            corrected_value=new_val,
            raw_token_text=raw_token_text if (not target_field or target_field == field) else None,
            token_bbox_json=json.dumps(norm_bbox) if norm_bbox else None,
            learned_rule_type=rule_type,
            reward_score=1.0,
            actor=actor,
            details_json=json.dumps(details, ensure_ascii=False),
        )
        db.add(feedback_rec)

    # 2. Update or create vendor profile if supplier EIK is known
    profile_id = None
    file_path = None
    is_new_profile = False

    if clean_eik:
        try:
            profile_id, file_path, is_new_profile = update_or_create_vendor_profile(
                supplier_eik=clean_eik,
                supplier_name=supp_name,
                supplier_vat=supp_vat,
                supplier_address=supp_addr,
                series_rules=series_rules,
                spatial_prior=spatial_prior,
                homoglyphs=homoglyphs,
                dot_matrix=dot_matrix_flag,
            )
        except Exception as exc:
            logger.warning("Could not persist vendor profile for %s: %s", clean_eik, exc)

    db.commit()

    return {
        "status": "learned" if rules_learned else "recorded",
        "supplier_eik": clean_eik or None,
        "vendor_id": profile_id,
        "profile_file": str(file_path) if file_path else None,
        "is_new_vendor": is_new_profile,
        "rules_updated": rules_learned,
        "feedback_count": len(diffs),
    }


# ---------------------------------------------------------------------------
# RLHF Analytics & Diagnostics Endpoint Helpers
# ---------------------------------------------------------------------------

def get_feedback_statistics(db: Session) -> dict[str, Any]:
    """Aggregate statistics for RLHF monitoring."""
    from database import InvoiceFeedbackRecord

    total_records = db.query(func.count(InvoiceFeedbackRecord.id)).scalar() or 0
    distinct_vendors = db.query(func.count(func.distinct(InvoiceFeedbackRecord.supplier_eik))).filter(
        InvoiceFeedbackRecord.supplier_eik.isnot(None)
    ).scalar() or 0

    # Counts by field
    field_counts_query = db.query(
        InvoiceFeedbackRecord.field_name,
        func.count(InvoiceFeedbackRecord.id)
    ).group_by(InvoiceFeedbackRecord.field_name).all()
    by_field = {row[0]: row[1] for row in field_counts_query}

    # Recent exemplars
    recent = db.query(InvoiceFeedbackRecord).order_by(
        desc(InvoiceFeedbackRecord.created_at)
    ).limit(10).all()

    all_profiles = get_vendor_profiles()
    learned_profiles = [p.id for p in all_profiles.values() if p.id.startswith("learned_")]

    return {
        "total_corrections_learned": total_records,
        "distinct_vendors_refined": distinct_vendors,
        "corrections_by_field": by_field,
        "active_learned_vendor_profiles": learned_profiles,
        "total_active_profiles": len(all_profiles),
        "recent_feedback_exemplars": [r.to_dict() for r in recent],
    }

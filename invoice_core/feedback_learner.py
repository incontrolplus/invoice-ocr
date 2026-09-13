"""Continuous Learning Engine: Delta Pro 'СЛЕД ПРЕГЛЕД' Feedback Analyzer.

Extracts human accounting corrections made in Microinvest Delta Pro
and automatically updates contractor profiles, default expense accounts,
invoice numbering rules, and the Supabase accounting knowledge base.
"""
from __future__ import annotations

from decimal import Decimal
import json
import logging
from pathlib import Path
import re
from typing import Any, Optional, Sequence
import yaml

logger = logging.getLogger("feedback_learner")


def normalize_doc_number(num: str) -> str:
    """Strip leading zeros and non-digit noise for robust matching."""
    cleaned = re.sub(r"\D", "", str(num or ""))
    return cleaned.lstrip("0") or "0"


def match_operation(
    orig: dict[str, Any],
    reviewed_list: Sequence[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Find corresponding reviewed operation by EIK, total amount, or normalized number."""
    orig_total = Decimal(str(orig.get("total_amount") or "0.00"))
    orig_eik = str(orig.get("contractor_eik") or orig.get("supplier_eik") or "").strip()
    orig_num_clean = normalize_doc_number(orig.get("document_number") or orig.get("invoice_number") or "")

    # 1. Exact match on EIK and exact total amount
    for rev in reviewed_list:
        rev_total = Decimal(str(rev.get("total_amount") or "0.00"))
        rev_eik = str(rev.get("contractor_eik") or rev.get("supplier_eik") or "").strip()
        if orig_eik and rev_eik == orig_eik and abs(orig_total - rev_total) < Decimal("0.01"):
            return rev

    # 2. Match on normalized invoice number and total amount
    for rev in reviewed_list:
        rev_total = Decimal(str(rev.get("total_amount") or "0.00"))
        rev_num_clean = normalize_doc_number(rev.get("document_number") or rev.get("invoice_number") or "")
        if orig_num_clean and rev_num_clean == orig_num_clean and abs(orig_total - rev_total) < Decimal("0.01"):
            return rev

    # 3. Match on total amount and issue date
    orig_date = str(orig.get("document_date") or orig.get("issue_date") or "")
    for rev in reviewed_list:
        rev_total = Decimal(str(rev.get("total_amount") or "0.00"))
        rev_date = str(rev.get("document_date") or rev.get("issue_date") or "")
        if abs(orig_total - rev_total) < Decimal("0.01") and (orig_date and rev_date and orig_date == rev_date):
            return rev

    return None


def extract_corrections(
    original_operations: Sequence[dict[str, Any]],
    reviewed_operations: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Compare generated operations vs reviewed operations and identify differences."""
    report = {
        "total_compared": len(original_operations),
        "exact_matches": 0,
        "corrected_count": 0,
        "corrections": [],
        "contractor_rules_to_learn": {},
    }

    for orig in original_operations:
        match = match_operation(orig, reviewed_operations)
        if not match:
            continue

        orig_inv_num = str(orig.get("document_number") or orig.get("invoice_number") or "").strip()
        rev_inv_num = str(match.get("document_number") or match.get("invoice_number") or "").strip()

        orig_exp_acc = str(orig.get("expense_account") or "601").strip()
        rev_exp_acc = str(match.get("expense_account") or "601").strip()

        orig_reason = str(orig.get("reason") or "").strip()
        rev_reason = str(match.get("reason") or "").strip()

        eik = str(match.get("contractor_eik") or orig.get("contractor_eik") or "").strip()
        name = str(match.get("contractor_name") or orig.get("contractor_name") or "").strip()

        diffs = {}
        if orig_inv_num != rev_inv_num:
            diffs["invoice_number"] = {"before": orig_inv_num, "after": rev_inv_num}

        if orig_exp_acc != rev_exp_acc:
            diffs["expense_account"] = {"before": orig_exp_acc, "after": rev_exp_acc}

        if orig_reason and rev_reason and orig_reason != rev_reason:
            diffs["reason"] = {"before": orig_reason, "after": rev_reason}

        if diffs:
            report["corrected_count"] += 1
            item = {
                "contractor_eik": eik,
                "contractor_name": name,
                "total_amount": float(orig.get("total_amount") or 0),
                "differences": diffs,
            }
            report["corrections"].append(item)

            # Record learned contractor prior
            if eik:
                if eik not in report["contractor_rules_to_learn"]:
                    report["contractor_rules_to_learn"][eik] = {
                        "eik": eik,
                        "name": name,
                        "preferred_expense_account": rev_exp_acc,
                        "preferred_reason": rev_reason or "м-ли",
                        "corrections_observed": 1,
                    }
                else:
                    report["contractor_rules_to_learn"][eik]["corrections_observed"] += 1
        else:
            report["exact_matches"] += 1

    return report


def persist_learned_vendor_rules(
    contractor_rules: dict[str, dict[str, Any]],
    vendors_dir: Path,
) -> list[str]:
    """Update or create learned vendor YAML configurations in config/vendors/."""
    vendors_dir.mkdir(parents=True, exist_ok=True)
    updated_files = []

    for eik, rule in contractor_rules.items():
        if not eik or len(eik) < 9:
            continue

        filename = f"learned_{eik}.yaml"
        file_path = vendors_dir / filename

        existing_data = {}
        if file_path.exists():
            try:
                existing_data = yaml.safe_load(file_path.read_text("utf-8")) or {}
            except Exception as e:
                logger.warning(f"Could not read existing vendor file {file_path}: {e}")

        # Merge new learned priors
        existing_data["id"] = f"learned_{eik}"
        existing_data["name"] = rule.get("name") or existing_data.get("name") or ""
        existing_data["eik"] = eik
        existing_data["vat"] = f"BG{eik}" if not existing_data.get("vat") else existing_data.get("vat")
        existing_data["accounting_defaults"] = {
            "default_expense_account": rule["preferred_expense_account"],
            "default_reason": rule["preferred_reason"],
            "confidence": 1.0,
            "source": "delta_pro_human_review_feedback",
        }

        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(existing_data, f, allow_unicode=True, default_flow_style=False)

        updated_files.append(str(file_path))
        logger.info(f"Updated learned vendor profile: {file_path}")

    return updated_files

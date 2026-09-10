"""Partner & Contractor Verification against accounting.partners (Supabase).

Verifies both parties in a commercial document:
- КУПУВАЧ (Buyer / Recipient)
- ПРОДАВАЧ (Seller / Supplier)

Checks:
1. Presence of both EIK numbers in the central database table `accounting.partners`.
2. Name & Identity correspondence:
   - Compares recognized company name against canonical `legal_name`, `transliteration`,
     `trade_name`, and `ocr_aliases`.
   - Recognizes exact matches, normalized matches (without legal form), transliterated
     variants, and fuzzy OCR typo variations.
   - Flags CRITICAL / DIVERGENT mismatches where recognized name is completely different
     from the company owning the EIK in the registry.
3. Enforces HITL (Human-in-the-Loop) routing:
   - When a critical divergence is detected, the document is NOT classified / auto-routed,
     and is instead marked for manual review (`status: needs_review`, `routing_action: hitl_review`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import difflib
import logging
import re
from typing import Any, Optional

from invoice_core.models import Party, ValidationIssue

logger = logging.getLogger("partner_verification")

# Legal form regex patterns (Bulgarian Cyrillic & Latin variants)
LEGAL_FORMS = [
    r"\bеоод\s+енд\s+ко\b",
    r"\beood\s*&\s*co\b",
    r"\beood\s+and\s+co\b",
    r"\bеоод\b",
    r"\bоод\b",
    r"\bеад\b",
    r"\bад\b",
    r"\bет\b",
    r"\bсд\b",
    r"\bкд\b",
    r"\bкда\b",
    r"\bдззд\b",
    r"\beood\b",
    r"\blood\b",
    r"\bead\b",
    r"\bad\b",
    r"\bltd\b",
    r"\bllc\b",
    r"\bgmbh\b",
    r"\bcorp\b",
    r"\binc\b",
    r"\bag\b",
    r"\bsa\b",
    r"\bbv\b",
]

# Generic geographic and organization stop words that cannot be the sole basis for a name match
GENERIC_STOP_WORDS = {
    "българия", "bulgaria", "софия", "sofia", "варна", "varna", "пловдив", "plovdiv",
    "бургас", "burgas", "европа", "europe", "интернешънъл", "international",
    "група", "group", "инвест", "invest", "трейдинг", "trading", "комерс", "commerce",
    "холдинг", "holding", "системс", "systems", "сървисиз", "services", "къмпани", "company",
    "глобал", "global", "експрес", "express", "транс", "trans", "логистик", "logistic",
    "логистикс", "logistics", "център", "center", "centre", "магазин", "shop", "маркет", "market",
}

# Transliteration mappings (Bulgarian Law on Transliteration)
BG_TO_EN_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht", "ъ": "a", "ь": "y",
    "ю": "yu", "я": "ya",
}

EN_TO_BG_MULTI = [
    ("sht", "щ"), ("dzh", "дж"), ("zh", "ж"), ("ch", "ч"), ("sh", "ш"),
    ("ts", "ц"), ("yu", "ю"), ("ya", "я"), ("ge", "дже"), ("gi", "джи"),
]
EN_TO_BG_SINGLE = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "й", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
    "v": "в", "w": "в", "x": "кс", "y": "й", "z": "з",
}


def normalize_company_name(name: Optional[str]) -> str:
    """Normalize company name by stripping legal forms, punctuation, quotes and extra spaces."""
    if not name:
        return ""
    s = name.lower()
    # Remove common quotation marks, braces, dashes, slashes
    s = re.sub(r'[\"\'\`«»„“\.,\(\)\-\/\\\:\;\!\?\*\#]', " ", s)
    # Strip legal forms
    for lf in LEGAL_FORMS:
        s = re.sub(lf, " ", s)
    return " ".join(s.split())


def transliterate_bg_to_en(text: str) -> str:
    """Transliterate Bulgarian text to Latin."""
    out = []
    for ch in text.lower():
        out.append(BG_TO_EN_MAP.get(ch, ch))
    return "".join(out)


def transliterate_en_to_bg(text: str) -> str:
    """Phonetic approximation of Latin text to Cyrillic."""
    s = text.lower()
    for en, bg in EN_TO_BG_MULTI:
        s = s.replace(en, bg)
    return "".join(EN_TO_BG_SINGLE.get(ch, ch) for ch in s)


def get_distinctive_tokens(norm_name: str) -> set[str]:
    """Extract distinctive tokens from normalized company name excluding generic stop words."""
    tokens = set(norm_name.split())
    return {t for t in tokens if len(t) > 2 and t not in GENERIC_STOP_WORDS}


def compute_company_name_similarity(
    recognized_name: Optional[str],
    candidate_names: list[Optional[str]],
) -> tuple[float, str, str]:
    """Compute highest similarity between recognized name and candidate names from registry.

    Returns:
        (best_score, match_status, matched_candidate)
        match_status in ("EXACT", "SIMILAR", "DIVERGENT", "EMPTY")
    """
    if not recognized_name or not recognized_name.strip():
        return 0.0, "EMPTY", ""

    rec_norm = normalize_company_name(recognized_name)
    rec_dist_tokens = get_distinctive_tokens(rec_norm)

    best_score = 0.0
    best_cand = ""
    best_status = "DIVERGENT"

    valid_candidates = [c for c in candidate_names if c and c.strip()]
    if not valid_candidates:
        return 0.0, "DIVERGENT", ""

    for cand in valid_candidates:
        cand_norm = normalize_company_name(cand)
        cand_dist_tokens = get_distinctive_tokens(cand_norm)

        # 1. Exact normalized match (full strings)
        if rec_norm and cand_norm and rec_norm == cand_norm:
            return 1.0, "EXACT", cand

        # 2. Transliterated exact match
        rec_trans = transliterate_bg_to_en(rec_norm)
        cand_trans = transliterate_bg_to_en(cand_norm)
        if rec_trans and cand_trans and rec_trans == cand_trans:
            return 1.0, "EXACT", cand

        rec_bg = transliterate_en_to_bg(rec_norm)
        cand_bg = transliterate_en_to_bg(cand_norm)
        if rec_bg and cand_bg and rec_bg == cand_bg:
            return 1.0, "EXACT", cand

        # 3. Distinctive Token Evaluation
        if rec_dist_tokens and cand_dist_tokens:
            common = rec_dist_tokens & cand_dist_tokens
            if common:
                overlap_ratio = len(common) / max(len(rec_dist_tokens), 1)
                token_score = 0.75 + (0.25 * overlap_ratio)
                if token_score > best_score:
                    best_score = token_score
                    best_cand = cand
                    best_status = "EXACT" if token_score >= 0.95 else "SIMILAR"
                continue

            # Check transliterated distinctive token overlap
            t1_trans = {transliterate_en_to_bg(t) for t in rec_dist_tokens}
            t2_trans = {transliterate_en_to_bg(t) for t in cand_dist_tokens}
            common_trans = t1_trans & t2_trans
            if common_trans:
                overlap_ratio = len(common_trans) / max(len(t1_trans), 1)
                trans_score = 0.75 + (0.25 * overlap_ratio)
                if trans_score > best_score:
                    best_score = trans_score
                    best_cand = cand
                    best_status = "EXACT" if trans_score >= 0.95 else "SIMILAR"
                continue

            # Fuzzy matching on distinctive strings
            d1_str = " ".join(sorted(rec_dist_tokens))
            d2_str = " ".join(sorted(cand_dist_tokens))
            dist_ratio = difflib.SequenceMatcher(None, d1_str, d2_str).ratio()

            bg_d1 = transliterate_en_to_bg(d1_str)
            bg_d2 = transliterate_en_to_bg(d2_str)
            trans_dist_ratio = difflib.SequenceMatcher(None, bg_d1, bg_d2).ratio()

            local_best_dist = max(dist_ratio, trans_dist_ratio)
            if local_best_dist > best_score:
                best_score = local_best_dist
                best_cand = cand
                if local_best_dist >= 0.70:
                    best_status = "SIMILAR"
            # When distinctive tokens exist on both sides, DO NOT fallback to full-string match
            # which could be polluted by generic words like "България"!
            continue

        # 4. If one or both sides lack distinctive tokens, fallback to full string matching
        seq_ratio = difflib.SequenceMatcher(None, rec_norm, cand_norm).ratio()
        trans_ratio = difflib.SequenceMatcher(None, rec_bg, cand_bg).ratio()
        local_best = max(seq_ratio, trans_ratio)
        if local_best > best_score:
            best_score = local_best
            best_cand = cand
            if local_best >= 0.75:
                best_status = "SIMILAR"

    # Final decision on verdict
    if best_score >= 0.95:
        best_status = "EXACT"
    elif best_score >= 0.60:
        best_status = "SIMILAR"
    else:
        best_status = "DIVERGENT"

    return round(best_score, 4), best_status, best_cand


@dataclass
class PartyCheckResult:
    """Verification outcome for a single invoice party."""
    role: str  # "supplier" or "recipient"
    role_bg: str  # "Продавач" or "Купувач"
    eik: Optional[str]
    recognized_name: Optional[str]
    db_partner_found: bool = False
    db_canonical_name: Optional[str] = None
    db_seat_address: Optional[str] = None
    db_mol_name: Optional[str] = None
    db_vat_status: Optional[str] = None
    match_status: str = "PENDING"  # "EXACT", "SIMILAR", "DIVERGENT", "NOT_IN_DB", "MISSING_EIK"
    similarity_score: float = 0.0
    matched_candidate: str = ""
    is_critical_mismatch: bool = False
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PartiesVerificationReport:
    """Comprehensive validation report for both parties in a document."""
    supplier_check: PartyCheckResult
    recipient_check: PartyCheckResult
    both_eiks_in_db: bool
    has_critical_mismatch: bool
    requires_hitl: bool
    hitl_reasons: list[str] = field(default_factory=list)
    validation_issues: list[ValidationIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "supplier": self.supplier_check.to_dict(),
            "recipient": self.recipient_check.to_dict(),
            "both_eiks_in_db": self.both_eiks_in_db,
            "has_critical_mismatch": self.has_critical_mismatch,
            "requires_hitl": self.requires_hitl,
            "hitl_reasons": self.hitl_reasons,
            "validation_issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "severity": issue.severity,
                    "field": issue.field,
                    "detected_value": issue.detected_value,
                    "expected_value": issue.expected_value,
                }
                for issue in self.validation_issues
            ],
        }


def verify_party_against_partner(
    party: Optional[Party],
    role: str = "supplier",
    verifier: Any = None,
) -> PartyCheckResult:
    """Verify a single party against accounting.partners and Commercial Register."""
    role_bg = "Продавач" if role == "supplier" else "Купувач"

    if not party:
        return PartyCheckResult(
            role=role,
            role_bg=role_bg,
            eik=None,
            recognized_name=None,
            db_partner_found=False,
            match_status="MISSING_EIK",
            is_critical_mismatch=False,
            details=f"Липсват данни за {role_bg.lower()} в документа",
        )

    raw_eik = party.eik or party.vat_number
    clean_eik = re.sub(r"[^0-9A-Za-z]", "", raw_eik or "")
    if clean_eik.upper().startswith("BG"):
        clean_eik = clean_eik[2:]

    rec_name = party.name

    if not clean_eik:
        return PartyCheckResult(
            role=role,
            role_bg=role_bg,
            eik=None,
            recognized_name=rec_name,
            db_partner_found=False,
            match_status="MISSING_EIK",
            is_critical_mismatch=False,
            details=f"Не е разпознат ЕИК на {role_bg.lower()} във фактурата",
        )

    # Perform lookup via contractor_verification
    from contractor_verification import default_verifier
    active_verifier = verifier or default_verifier

    c_res = None
    try:
        c_res = active_verifier.verify_sync(clean_eik)
    except Exception as exc:
        logger.warning("Error looking up contractor %s: %s", clean_eik, exc)

    if not c_res or not c_res.company_name:
        return PartyCheckResult(
            role=role,
            role_bg=role_bg,
            eik=clean_eik,
            recognized_name=rec_name,
            db_partner_found=False,
            match_status="NOT_IN_DB",
            is_critical_mismatch=False,
            details=f"ЕИК {clean_eik} не е открит в accounting.partners или Търговския регистър",
        )

    db_canonical_name = c_res.company_name
    db_seat = c_res.seat_address or c_res.address
    db_mol = c_res.mol_name
    vat_st = getattr(c_res.vat_status, "value", str(c_res.vat_status))

    # Collect all known candidate aliases from raw_data or contractor result
    candidate_names: list[Optional[str]] = [db_canonical_name]
    if c_res.raw_data:
        raw_d = c_res.raw_data
        if raw_d.get("transliteration"):
            candidate_names.append(raw_d.get("transliteration"))
        if raw_d.get("trade_name"):
            candidate_names.append(raw_d.get("trade_name"))
        if raw_d.get("ocr_aliases") and isinstance(raw_d.get("ocr_aliases"), list):
            candidate_names.extend(raw_d.get("ocr_aliases"))

    # Compute similarity
    best_score, match_status, matched_cand = compute_company_name_similarity(
        rec_name,
        candidate_names,
    )

    is_mismatch = False
    details_msg = ""

    if match_status == "DIVERGENT":
        is_mismatch = True
        details_msg = (
            f"Критично разминаване за {role_bg.lower()}: разпознатото в документа име "
            f"'{rec_name}' е напълно различно от официалното име на фирмата по ЕИК {clean_eik} "
            f"в accounting.partners ('{db_canonical_name}')."
        )
    elif match_status == "EXACT":
        details_msg = f"Точно съответствие с официалните данни на {role_bg.lower()} ('{db_canonical_name}')."
    elif match_status == "SIMILAR":
        details_msg = (
            f"Сходно наименование ({best_score:.0%}) с официалните данни "
            f"('{db_canonical_name}'). Прието като валиден търговски синоним / малка грешка."
        )
    elif match_status == "EMPTY":
        details_msg = f"Разпознат е ЕИК {clean_eik} ({db_canonical_name}), но липсва текстово име във фактурата."

    return PartyCheckResult(
        role=role,
        role_bg=role_bg,
        eik=clean_eik,
        recognized_name=rec_name,
        db_partner_found=True,
        db_canonical_name=db_canonical_name,
        db_seat_address=db_seat,
        db_mol_name=db_mol,
        db_vat_status=vat_st,
        match_status=match_status,
        similarity_score=best_score,
        matched_candidate=matched_cand,
        is_critical_mismatch=is_mismatch,
        details=details_msg,
    )


def verify_invoice_parties(
    supplier: Optional[Party],
    recipient: Optional[Party],
    verifier: Any = None,
) -> PartiesVerificationReport:
    """Verify both parties (Seller and Buyer) and generate consolidated HITL report."""
    sup_check = verify_party_against_partner(supplier, role="supplier", verifier=verifier)
    rec_check = verify_party_against_partner(recipient, role="recipient", verifier=verifier)

    both_in_db = sup_check.db_partner_found and rec_check.db_partner_found
    has_critical = sup_check.is_critical_mismatch or rec_check.is_critical_mismatch

    hitl_reasons: list[str] = []
    validation_issues: list[ValidationIssue] = []

    if sup_check.is_critical_mismatch:
        hitl_reasons.append(sup_check.details)
        validation_issues.append(ValidationIssue(
            code="CRITICAL_SUPPLIER_NAME_MISMATCH",
            message=sup_check.details,
            severity="error",
            field="supplier.name",
            detected_value=sup_check.recognized_name,
            expected_value=sup_check.db_canonical_name,
        ))

    if rec_check.is_critical_mismatch:
        hitl_reasons.append(rec_check.details)
        validation_issues.append(ValidationIssue(
            code="CRITICAL_RECIPIENT_NAME_MISMATCH",
            message=rec_check.details,
            severity="error",
            field="recipient.name",
            detected_value=rec_check.recognized_name,
            expected_value=rec_check.db_canonical_name,
        ))

    # Note if an EIK is not found in the partners table
    if sup_check.eik and not sup_check.db_partner_found:
        validation_issues.append(ValidationIssue(
            code="SUPPLIER_NOT_IN_ACCOUNTING_PARTNERS",
            message=f"Продавач с ЕИК {sup_check.eik} не е открит в accounting.partners",
            severity="warning",
            field="supplier.eik",
            detected_value=sup_check.eik,
        ))

    if rec_check.eik and not rec_check.db_partner_found:
        validation_issues.append(ValidationIssue(
            code="RECIPIENT_NOT_IN_ACCOUNTING_PARTNERS",
            message=f"Купувач с ЕИК {rec_check.eik} не е открит в accounting.partners",
            severity="warning",
            field="recipient.eik",
            detected_value=rec_check.eik,
        ))

    requires_hitl = has_critical or len([i for i in validation_issues if i.severity == "error"]) > 0

    return PartiesVerificationReport(
        supplier_check=sup_check,
        recipient_check=rec_check,
        both_eiks_in_db=both_in_db,
        has_critical_mismatch=has_critical,
        requires_hitl=requires_hitl,
        hitl_reasons=hitl_reasons,
        validation_issues=validation_issues,
    )

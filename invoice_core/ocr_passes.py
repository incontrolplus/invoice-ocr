"""Multi-pass Tesseract OCR execution, scoring, and fusion."""
from __future__ import annotations

from collections import defaultdict
import logging
import re
from typing import Any

import numpy as np
import pytesseract
from pytesseract import Output

from .constants import BULGARIAN_KEYWORDS, DEFAULT_OCR_LANG, MIN_CONFIDENCE
from .layout import _match_column_synonym
from .models import OcrToken, PageTransform
from .normalizers import clean_ocr_artifacts
from .tesseract_env import (
    TesseractLanguageMissingError,
    get_installed_ocr_languages,
    resolve_effective_ocr_lang,
    setup_tessdata_prefix,
)

logger = logging.getLogger("invoice_ocr")

def _parse_ocr_dict_to_tokens(data: dict[str, list[Any]]) -> list[OcrToken]:
    """Convert pytesseract dict output to a list of OcrToken."""
    tokens: list[OcrToken] = []
    n = len(data.get('text', []))
    for i in range(n):
        text = str(data['text'][i]).strip()
        conf = float(data['conf'][i])
        if conf == -1 or not text:
            continue
        tokens.append(OcrToken(
            text=text,
            conf=conf,
            bbox=(
                int(data['left'][i]),
                int(data['top'][i]),
                int(data['width'][i]),
                int(data['height'][i]),
            ),
            block_num=int(data.get('block_num', [0] * n)[i]),
            par_num=int(data.get('par_num', [0] * n)[i]),
            line_num=int(data.get('line_num', [0] * n)[i]),
            word_num=int(data.get('word_num', [0] * n)[i]),
        ))
    return tokens


def execute_ocr_pass(
    img: np.ndarray,
    psm: int = 3,
    lang: str = DEFAULT_OCR_LANG,
    tessdata_dir: Path | str | None = None,
) -> list[OcrToken]:
    """Execute a single Tesseract OCR pass and parse into OcrToken list."""
    setup_tessdata_prefix(tessdata_dir)
    config = f"--psm {psm}"
    try:
        data = pytesseract.image_to_data(
            img, lang=lang, config=config, output_type=Output.DICT,
        )
    except pytesseract.TesseractError as exc:
        err_msg = str(exc)
        if "Failed loading language" in err_msg or "Error opening data file" in err_msg:
            req = [l.strip() for l in lang.split("+") if l.strip()]
            available = get_installed_ocr_languages()
            missing = [l for l in req if l not in available]
            if missing:
                raise TesseractLanguageMissingError(missing_langs=missing, available_langs=available) from exc
        raise
    return _parse_ocr_dict_to_tokens(data)


def compute_box_metrics(
    b1: tuple[int, int, int, int],
    b2: tuple[int, int, int, int],
) -> tuple[float, float]:
    """Compute IoU (Intersection over Union) and IoMin (Intersection over Min Area)."""
    l1, t1, w1, h1 = b1
    r1, b1_ = l1 + w1, t1 + h1
    l2, t2, w2, h2 = b2
    r2, b2_ = l2 + w2, t2 + h2

    inter_l = max(l1, l2)
    inter_t = max(t1, t2)
    inter_r = min(r1, r2)
    inter_b = min(b1_, b2_)

    if inter_r <= inter_l or inter_b <= inter_t:
        return 0.0, 0.0

    inter_area = float((inter_r - inter_l) * (inter_b - inter_t))
    area1 = float(w1 * h1)
    area2 = float(w2 * h2)
    union_area = area1 + area2 - inter_area

    iou = inter_area / union_area if union_area > 0 else 0.0
    min_area = min(area1, area2)
    iomin = inter_area / min_area if min_area > 0 else 0.0
    return iou, iomin


def is_line_noise_token(t: OcrToken) -> bool:
    """Filter out spurious table border and line noise tokens."""
    if not t.text or not t.text.strip():
        return True
    # Table border and divider character sequences (e.g. ----, ____, ====, ------, |)
    if re.fullmatch(r"[-_=~+|—\s]+", t.text) and len(t.text) >= 2:
        return True
    # Repetitive character string (e.g. OOOOOOOO, --------, ________)
    # Never discard statutory 10-digit invoice number tokens (e.g. 0000006960, 0000000001)
    if len(t.text) >= 10 and len(set(t.text.lower())) <= 3 and not (len(t.text) == 10 and t.text.isdigit()):
        return True

    w, h = t.width, t.height
    if w > 0 and h > 0:
        aspect = w / h
        # Extreme aspect ratio horizontal or vertical
        if aspect > 12 and h <= 6:
            return True
        if aspect < 0.08 and w <= 6:
            return True
        # Non-alphanumeric noise of tiny size or low confidence
        if not re.search(r'[0-9a-zA-Zа-яА-Я]', t.text):
            if len(t.text) <= 2 and (w <= 8 or h <= 8):
                return True
            if t.conf < 30:
                return True
    elif not re.search(r'[0-9a-zA-Zа-яА-Я]', t.text):
        return True

    return False


def score_token_quality(t: OcrToken) -> float:
    """Score individual token quality for multi-pass OCR competition."""
    if is_line_noise_token(t):
        return 0.0

    score = float(t.conf)
    clean_txt = t.text.strip().lower()
    if not clean_txt:
        return -100.0

    # Heavily penalize tokens lacking any alphanumeric characters
    if not re.search(r'[0-9a-zA-Zа-яА-Я]', t.text):
        score -= 50.0

    # Length bonus: longer complete words preferred over fragments
    score += min(len(clean_txt), 12) * 1.5

    # Check valid characters ratio
    valid_chars = sum(1 for c in t.text if c.isalnum() or c in '.,-/%()')
    ratio = valid_chars / len(t.text) if t.text else 0.0
    if ratio < 0.8:
        score -= 25.0

    # Bulgarian statutory / domain keywords
    if any(kw in clean_txt for kw in BULGARIAN_KEYWORDS):
        score += 30.0

    # Valid date pattern (supports both DD.MM.YYYY and YYYY-MM-DD)
    if re.search(r'^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$|^\d{4}[./-]\d{1,2}[./-]\d{1,2}$', t.text):
        score += 25.0

    # Valid EIK or VAT ID pattern
    is_eik_or_vat = bool(re.search(r'^\d{9}$|^\d{13}$|^BG\d{9,13}$', t.text, re.IGNORECASE))
    if is_eik_or_vat:
        score += 35.0

    # Valid IBAN pattern
    if re.search(r'^BG\d{2}[A-Z]{4}\d{14}$', t.text, re.IGNORECASE):
        score += 25.0

    # Valid monetary amount or invoice number pattern
    if re.search(r'^\d+[.,]\d{2}$', t.text):
        score += 25.0
    elif re.fullmatch(r'\d{10}', t.text) and not is_eik_or_vat:
        score += 35.0  # Bulgarian statutory 10-digit invoice number
    elif re.search(r'^\d{1,5}$', t.text):
        score += 10.0
    elif re.fullmatch(r'\d{6,9}', t.text) and not is_eik_or_vat:
        score += 10.0
    elif re.fullmatch(r'\d{14,}', t.text) and not is_eik_or_vat:
        score -= 20.0  # Long barcode noise

    # Spurious quote / edge junk penalty
    if t.text.startswith(('„', '“', '"', "'", '`', '|')):
        score -= 10.0
    if t.text.endswith(('|', '`')):
        score -= 10.0

    # Repetitive character string penalty (do not penalize statutory 10-digit invoice numbers)
    if len(t.text) > 10 and len(set(t.text.lower())) <= 3 and not (len(t.text) == 10 and t.text.isdigit()):
        score -= 50.0

    return score


def fuse_ocr_passes(
    pass1_tokens: list[OcrToken],
    pass2_tokens: list[OcrToken],
    iou_threshold: float = 0.40,
    iomin_threshold: float = 0.65,
) -> list[OcrToken]:
    """Fuse Pass 1 (PSM 3) and Pass 2 (PSM 11) tokens via spatial alignment and scoring.

    Rules:
    1. Filter out isolated line noise tokens (table dividers, borders).
    2. Overlapping tokens (IoU >= 0.40 or IoMin >= 0.65) compete via multi-factor quality scoring.
    3. The higher-scoring candidate wins.
    4. Non-overlapping tokens from Pass 1 and valid sparse tokens from Pass 2 are preserved.
    5. All output tokens have is_low_confidence set to True if conf < 60.0.
    6. ZERO-DISCARD CONTRACT: No valid tokens are dropped.
    """
    def _suppress_internal_fragments(tokens: list[OcrToken]) -> list[OcrToken]:
        suppressed: set[int] = set()
        for i, ti in enumerate(tokens):
            if is_line_noise_token(ti):
                continue
            for j, tj in enumerate(tokens):
                if i != j and j not in suppressed and not is_line_noise_token(tj):
                    iou, iomin = compute_box_metrics(ti.bbox, tj.bbox)
                    if iomin >= 0.75 and len(ti.text) < len(tj.text):
                        if ti.text.lower() in tj.text.lower():
                            suppressed.add(i)
                            break
        return [t for i, t in enumerate(tokens) if i not in suppressed]

    clean_p1 = _suppress_internal_fragments([t for t in pass1_tokens if not is_line_noise_token(t)])
    clean_p2 = _suppress_internal_fragments([t for t in pass2_tokens if not is_line_noise_token(t)])

    fused: list[OcrToken] = []
    matched_p2_idx: set[int] = set()
    already_added_p2_winners: set[int] = set()

    for t1 in clean_p1:
        overlaps: list[tuple[int, OcrToken, float, float]] = []
        for idx2, t2 in enumerate(clean_p2):
            iou, iomin = compute_box_metrics(t1.bbox, t2.bbox)
            if iou >= iou_threshold or iomin >= iomin_threshold:
                overlaps.append((idx2, t2, iou, iomin))

        if not overlaps:
            t1.is_low_confidence = (t1.conf < MIN_CONFIDENCE)
            fused.append(t1)
        else:
            # Score candidate Pass 2 tokens, prioritizing quality score over raw IoU
            best_idx2, best_t2, best_iou, _ = max(
                overlaps,
                key=lambda x: (score_token_quality(x[1]), x[2]),
            )
            s1 = score_token_quality(t1)
            s2 = score_token_quality(best_t2)

            if best_idx2 in already_added_p2_winners:
                iou_win, _ = compute_box_metrics(t1.bbox, best_t2.bbox)
                if iou_win >= 0.50 or (t1.text.lower() in best_t2.text.lower() and len(t1.text) < len(best_t2.text)):
                    matched_p2_idx.add(best_idx2)
                    continue

            if s2 > s1:
                if best_idx2 not in already_added_p2_winners:
                    best_t2.is_low_confidence = (best_t2.conf < MIN_CONFIDENCE)
                    fused.append(best_t2)
                    already_added_p2_winners.add(best_idx2)
                matched_p2_idx.add(best_idx2)
                for idx2, t2_other, _, _ in overlaps:
                    if idx2 != best_idx2:
                        iou_other, _ = compute_box_metrics(best_t2.bbox, t2_other.bbox)
                        s_other = score_token_quality(t2_other)
                        if iou_other >= 0.30 and (s_other < 50.0 or t2_other.conf < 70.0):
                            matched_p2_idx.add(idx2)
            else:
                t1.is_low_confidence = (t1.conf < MIN_CONFIDENCE)
                fused.append(t1)
                matched_p2_idx.add(best_idx2)
                for idx2, t2_other, _, _ in overlaps:
                    if idx2 != best_idx2:
                        iou_other, _ = compute_box_metrics(t1.bbox, t2_other.bbox)
                        s_other = score_token_quality(t2_other)
                        # Don't swallow high-confidence Pass 2 keywords/tokens
                        if iou_other >= 0.40 and (s_other < 50.0 or t2_other.conf < 70.0):
                            matched_p2_idx.add(idx2)

    # Admit qualified Pass 2 orphans
    for idx2, t2 in enumerate(clean_p2):
        if idx2 not in matched_p2_idx:
            s2 = score_token_quality(t2)
            is_valid = (
                any(kw in t2.text.lower() for kw in BULGARIAN_KEYWORDS)
                or _match_column_synonym(t2.text) is not None
                or bool(re.search(r'\d{2,}', t2.text))
                or (t2.conf >= 55.0 and len(t2.text) >= 2)
            )
            if is_valid and s2 >= 35.0:
                t2.is_low_confidence = (t2.conf < MIN_CONFIDENCE)
                fused.append(t2)

    # Sort geometrically: top-to-bottom (grouped in ~15px bands), then left-to-right
    fused.sort(key=lambda t: (t.top // 15, t.left))
    return fused


def _score_ocr_result(tokens: list[OcrToken]) -> float:
    """Score an OCR result for quality selection.

    Weighted combination of:
        - Mean confidence of tokens (40 %)
        - Percentage of high-confidence tokens (30 %)
        - Total recognised character count (20 %)
        - Structural indicators: date patterns, digit sequences (10 %)
    """
    if not tokens:
        return 0.0

    confs = [t.conf for t in tokens if t.conf > 0]
    if not confs:
        return 0.0

    mean_conf = sum(confs) / len(confs)
    high_pct = len([c for c in confs if c >= 80]) / len(confs)
    total_chars = sum(len(t.text) for t in tokens)

    full_text = " ".join(t.text for t in tokens).lower()
    has_date = bool(re.search(r'\d{1,2}[./-]\d{1,2}[./-]\d{2,4}', full_text))
    has_numbers = bool(re.search(r'\d{3,}', full_text))
    has_keywords = any(kw in full_text for kw in [
        "фактура", "invoice", "доставчик", "получател",
        "данъчна основа", "ддс", "общо",
    ])

    score = (
        mean_conf * 0.4
        + high_pct * 100 * 0.3
        + min(total_chars / 10.0, 50) * 0.2  # cap character bonus
    )
    if has_date:
        score += 3
    if has_numbers:
        score += 2
    if has_keywords:
        score += 5

    return score


def run_multiple_ocr_passes(
    variants: list[tuple[str, np.ndarray]],
    lang: str = DEFAULT_OCR_LANG,
    tessdata_dir: Path | str | None = None,
) -> list[OcrToken]:
    """Execute multi-pass OCR (PSM 3 and PSM 11) and fuse results via spatial scoring.

    Pass 1 executes PSM 3 (automatic layout analysis) for paragraph & column structure.
    Pass 2 executes PSM 11 (sparse text) for isolated numbers, codes, and stamps.
    Tokens are fused via fuse_ocr_passes() with multi-factor scoring.
    """
    if not variants:
        return []

    setup_tessdata_prefix(tessdata_dir)
    effective_lang = resolve_effective_ocr_lang(lang)

    # Select target image for multi-pass OCR: prefer clahe_gray or standard
    target_img = variants[0][1]
    for name, img in variants:
        if name in ("clahe_gray", "standard"):
            target_img = img
            break

    try:
        p1_tokens = execute_ocr_pass(target_img, psm=3, lang=effective_lang, tessdata_dir=tessdata_dir)
    except Exception as exc:
        logger.warning("Pass 1 (PSM 3) failed: %s", exc)
        p1_tokens = []

    try:
        p2_tokens = execute_ocr_pass(target_img, psm=11, lang=effective_lang, tessdata_dir=tessdata_dir)
    except Exception as exc:
        logger.warning("Pass 2 (PSM 11) failed: %s", exc)
        p2_tokens = []

    if not p1_tokens and not p2_tokens:
        return []
    if not p1_tokens:
        for t in p2_tokens:
            t.is_low_confidence = (t.conf < MIN_CONFIDENCE)
        return p2_tokens
    if not p2_tokens:
        for t in p1_tokens:
            t.is_low_confidence = (t.conf < MIN_CONFIDENCE)
        return p1_tokens

    fused = fuse_ocr_passes(p1_tokens, p2_tokens)

    # Adaptive dot-matrix / degraded scan recovery pass
    dm_img = None
    for name, img in variants:
        if name == "dotmatrix_bridged":
            dm_img = img
            break

    if dm_img is not None and fused:
        mean_conf = sum(t.conf for t in fused) / len(fused)
        low_conf_ratio = sum(1 for t in fused if t.conf < MIN_CONFIDENCE) / len(fused)
        has_inv_num = any(re.fullmatch(r'\d{10}', t.text) for t in fused)

        # Trigger if overall OCR confidence is poor (< 75.0), high noise ratio (> 0.25),
        # or key statutory 10-digit invoice number is missing
        if mean_conf < 75.0 or low_conf_ratio > 0.25 or not has_inv_num:
            logger.info(
                "Triggering adaptive dot-matrix recovery pass (mean_conf=%.1f, low_conf_ratio=%.2f, has_inv_num=%s)",
                mean_conf, low_conf_ratio, has_inv_num,
            )
            try:
                dm_p1 = execute_ocr_pass(dm_img, psm=3, lang=effective_lang, tessdata_dir=tessdata_dir)
            except Exception as exc:
                logger.warning("Dot-matrix Pass 1 (PSM 3) failed: %s", exc)
                dm_p1 = []
            try:
                dm_p2 = execute_ocr_pass(dm_img, psm=11, lang=effective_lang, tessdata_dir=tessdata_dir)
            except Exception as exc:
                logger.warning("Dot-matrix Pass 2 (PSM 11) failed: %s", exc)
                dm_p2 = []
            try:
                dm_p6 = execute_ocr_pass(dm_img, psm=6, lang=effective_lang, tessdata_dir=tessdata_dir)
            except Exception as exc:
                logger.warning("Dot-matrix Pass 6 (PSM 6) failed: %s", exc)
                dm_p6 = []

            dm_list = [p for p in (dm_p1, dm_p2, dm_p6) if p]
            if dm_list:
                dm_fused = dm_list[0]
                for extra in dm_list[1:]:
                    dm_fused = fuse_ocr_passes(dm_fused, extra)
                fused = fuse_ocr_passes(fused, dm_fused)


    return fused


def build_raw_ocr_evidence(
    pages: list[PageImage],
    tokens: list[OcrToken],
) -> dict[str, Any]:
    """Build Layer 1 raw OCR evidence serialization dictionary.

    Follows the ZERO-DISCARD CONTRACT: retains all recognized tokens,
    including low-confidence tokens (conf < 60.0).
    """
    page_map: dict[int, list[dict[str, Any]]] = {}
    for p in pages:
        page_map[p.page_number] = []

    low_conf_count = 0
    conf_sum = 0.0
    for t in tokens:
        is_low = (t.conf < MIN_CONFIDENCE)
        t.is_low_confidence = is_low
        if is_low:
            low_conf_count += 1
        conf_sum += t.conf
        page_tokens = page_map.setdefault(t.page_number, [])
        page_tokens.append({
            "text": t.text,
            "conf": round(float(t.conf), 2),
            "bbox": [t.left, t.top, t.width, t.height],
            "page_number": t.page_number,
            "is_low_confidence": is_low,
        })

    mean_conf = round(conf_sum / len(tokens), 2) if tokens else 0.0
    page_records = []
    for p in pages:
        toks = page_map.get(p.page_number, [])
        page_records.append({
            "page_number": p.page_number,
            "width": p.width,
            "height": p.height,
            "token_count": len(toks),
            "tokens": toks,
        })

    return {
        "total_pages": len(pages),
        "total_tokens": len(tokens),
        "mean_confidence": mean_conf,
        "low_confidence_count": low_conf_count,
        "pages": page_records,
    }


# ===================================================================
# LAYER 3 — TOKEN NORMALIZATION
# ===================================================================

def normalize_ocr_tokens(tokens: list[OcrToken]) -> list[OcrToken]:
    """Clean OCR artifacts from each token's text.

    Preserves tokens with non-empty text after cleaning.
    Does NOT remove financial punctuation.
    """
    result: list[OcrToken] = []
    for t in tokens:
        # Filter out anomalous giant smear/line artifacts (e.g. 2-4 chars spanning >400px with conf < 50)
        if len(t.text.strip()) <= 4 and t.width > 400 and t.conf < 50:
            continue
        # Filter out tall non-alphanumeric punctuation/boundary artifacts (e.g. '!' or '|' > 100px)
        if len(t.text.strip()) <= 3 and t.height > 100 and not any(c.isalnum() for c in t.text):
            continue
        cleaned = clean_ocr_artifacts(t.text)
        if cleaned:
            t.text = cleaned
            result.append(t)
    return result


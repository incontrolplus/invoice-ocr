"""Spatial layout analysis, token grouping, and table region detection."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import logging
import re
from typing import Any

import cv2
import numpy as np

from .constants import (
    ALL_COLUMN_SYNONYMS_SORTED,
    CONTINUATION_KEYWORDS,
    CONTINUATION_PATTERNS,
    RECEIPT_KEYWORDS,
    RECIPIENT_KEYWORDS,
    SUMMARY_KEYWORDS,
    SUPPLIER_KEYWORDS,
)
from .models import LogicalBlock, LogicalLine, OcrToken, TableColumn, TableRegion
from .normalizers import clean_ocr_artifacts, is_recipient_keyword, is_supplier_keyword, parse_money
from .vendor_profiles import is_dot_matrix_vendor

logger = logging.getLogger("invoice_ocr")

def _token_vertical_overlap_ratio(t1: OcrToken, t2: OcrToken) -> float:
    """Calculate relative vertical overlap normalized by the smaller token height."""
    v_int = max(0, min(t1.bottom, t2.bottom) - max(t1.top, t2.top))
    min_h = min(t1.height, t2.height)
    return v_int / min_h if min_h > 0 else 0.0


def group_tokens_into_lines(
    tokens: list[OcrToken],
    y_tolerance_factor: float = 0.6,
) -> list[LogicalLine]:
    """Group tokens into logical lines using 2D vertical overlap chaining.

    Resolves:
    1. Baseline punctuation (periods, commas) and superscripts/subscripts.
    2. Residual skew (chaining via horizontally nearest neighbor tokens).
    3. Strict page isolation.
    """
    if not tokens:
        return []

    tokens_by_page: dict[int, list[OcrToken]] = defaultdict(list)
    for t in tokens:
        tokens_by_page[t.page_number].append(t)

    all_lines: list[LogicalLine] = []
    for page_num in sorted(tokens_by_page.keys()):
        page_tokens = tokens_by_page[page_num]
        if not page_tokens:
            continue

        page_min_x = min(t.left for t in page_tokens)
        page_max_x = max(t.right for t in page_tokens)
        mid_x = (page_min_x + page_max_x) // 2

        rboxes = detect_receipt_regions(page_tokens)

        def in_receipt(tok: OcrToken) -> bool:
            for rx, ry, rw, rh in rboxes:
                if rx - 20 <= tok.center_x <= rx + rw + 40 and ry - 20 <= tok.center_y <= ry + rh + 40:
                    return True
            return False

        first_table_header_y = min(
            (t.top for t in page_tokens if _match_column_synonym(t.text) is not None),
            default=max(t.bottom for t in page_tokens) + 1,
        )

        # Typical token height
        heights = [t.height for t in page_tokens if t.height > 10]
        typical_h = sorted(heights)[len(heights) // 2] if heights else 30

        # Sort primarily by top, then left
        sorted_tokens = sorted(page_tokens, key=lambda t: (t.top, t.left))

        page_line_groups: list[list[OcrToken]] = []
        for t in sorted_tokens:
            best_line: list[OcrToken] | None = None
            best_score = 0.0

            t_txt = t.text.lower()
            t_is_supp = any(kw in t_txt for kw in SUPPLIER_KEYWORDS)
            t_is_recip = any(kw in t_txt for kw in RECIPIENT_KEYWORDS)
            t_rec = in_receipt(t)

            for l in page_line_groups:
                # 0. Receipt token isolation: tokens inside receipt never merge with invoice tokens
                l_rec = in_receipt(l[0])
                if t_rec != l_rec:
                    continue

                # 1. Opposite party roles never merge
                l_has_supp = any(any(kw in tok.text.lower() for kw in SUPPLIER_KEYWORDS) for tok in l)
                l_has_recip = any(any(kw in tok.text.lower() for kw in RECIPIENT_KEYWORDS) for tok in l)
                if (t_is_supp and l_has_recip) or (t_is_recip and l_has_supp):
                    continue

                # 2. Party section multi-column isolation
                h_gap = min(max(0, max(t.left, tok.left) - min(t.right, tok.right)) for tok in l)
                is_across_mid = (any(tok.center_x < mid_x for tok in l) and t.center_x >= mid_x) or \
                                (any(tok.center_x >= mid_x for tok in l) and t.center_x < mid_x)
                if is_across_mid:
                    if (t_is_supp or t_is_recip or l_has_supp or l_has_recip) and h_gap > 150:
                        continue
                    if t.top < first_table_header_y and h_gap > 200:
                        continue

                # 2b. Summary line isolation: summary tokens never merge into table item lines
                t_is_summary = any(kw in t_txt for kw in ["данъчнаоснова", "данъчна", "сумазаплащане", "словом", "плащаневброй"])
                if t_is_summary and any(any(kw in tok.text.lower() for kw in ["кюфте", "хамбург", "бр.", "цена", "кол.", "стока", "салам", "карначе", "плескавица", "пържола", "наденица", "суджук", "врат", "шишче", "плешка", "филе", "сироп"]) for tok in l):
                    continue

                # 2c. Horizontally overlapping tokens stacked vertically belong to different lines
                is_vertically_stacked = False
                for tok in l:
                    horiz_overlap = min(t.right, tok.right) - max(t.left, tok.left)
                    if horiz_overlap > 12 and abs(t.center_y - tok.center_y) > 16:
                        is_vertically_stacked = True
                        break
                if is_vertically_stacked:
                    continue

                # 2d. Guard against vertical chaining across different text lines
                closest_tok = min(l, key=lambda tok: abs(tok.center_x - t.center_x))
                dx = abs(t.center_x - closest_tok.center_x)
                tok_max_h = max(t.height, max(tok.height for tok in l))
                max_dy = max(18.0, tok_max_h * 0.55) + dx * 0.03
                if abs(t.center_y - closest_tok.center_y) > max_dy:
                    continue

                scores = []
                # 3. Overlap with horizontally nearby tokens in candidate line (within 800 px)
                for tok in l:
                    if abs(tok.center_x - t.center_x) <= 800:
                        if tok.text.strip() not in {"|", "¦", "||", "!"} and t.text.strip() not in {"|", "¦", "||", "!"}:
                            scores.append(_token_vertical_overlap_ratio(t, tok))

                # 4. Overlap with bounding box vertical span of line
                l_top = min(x.top for x in l)
                l_bot = max(x.bottom for x in l)
                l_h = l_bot - l_top
                line_w = max(tok.right for tok in l) - min(tok.left for tok in l)
                max_allowed_lh = max(int(2.2 * typical_h), int(1.5 * tok_max_h)) + int(line_w * 0.03)
                if l_h <= max_allowed_lh:
                    v_int = max(0, min(t.bottom, l_bot) - max(t.top, l_top))
                    if t.height > 0:
                        scores.append(v_int / t.height)

                score = max(scores) if scores else 0.0
                if score >= 0.50 and score > best_score:
                    best_score = score
                    best_line = l

            if best_line is not None:
                best_line.append(t)
            else:
                page_line_groups.append([t])

        # Sort lines by average vertical center, tokens within line by left
        page_line_groups.sort(key=lambda l: sum(tok.center_y for tok in l) / len(l))
        for l in page_line_groups:
            l.sort(key=lambda tok: tok.left)
            all_lines.append(LogicalLine(tokens=l, page_number=page_num))

    return all_lines


def _create_zoned_block(
    lines: list[LogicalLine],
    page_number: int,
    page_width: int,
    page_height: int,
    mid_x: int,
) -> LogicalBlock:
    """Create a LogicalBlock with zone tagging and receipt isolation."""
    block = LogicalBlock(lines=lines, page_number=page_number)

    # Check for cash register receipt lines
    full_text = block.text_lower
    is_receipt = any(
        re.search(rf"(?<![а-яА-Яa-zA-Z0-9]){re.escape(kw)}(?![а-яА-Яa-zA-Z0-9])", full_text)
        for kw in ["фискален бон", "фискална памет", "име на оператор", "фискален", "фискална", "касов бон", "обменен курс"]
    )
    if is_receipt and "фактура" not in full_text and block.width <= int(0.45 * page_width):
        block.block_type = "receipt"
        block.zone = "receipt"
        return block

    # 7 canonical document spatial zones
    if "фактура" in full_text or block.bottom <= int(0.22 * page_height):
        block.zone = "header"
    elif block.top <= int(0.45 * page_height):
        block.zone = "party_left" if block.center_x < mid_x else "party_right"
    elif block.top >= int(0.85 * page_height):
        block.zone = "footer"
    elif block.top >= int(0.60 * page_height):
        block.zone = "payment_details" if block.center_x < mid_x else "financial_summary"
    else:
        block.zone = "table_body"

    return block


def group_lines_into_blocks(
    lines: list[LogicalLine],
    gap_factor: float = 2.0,
    page_width: int | None = None,
    page_height: int | None = None,
) -> list[LogicalBlock]:
    """Group consecutive lines into LogicalBlocks based on vertical gaps with spatial zoning."""
    if not lines:
        return []

    lines_by_page: dict[int, list[LogicalLine]] = defaultdict(list)
    for line in lines:
        lines_by_page[line.page_number].append(line)

    blocks: list[LogicalBlock] = []
    for page_num in sorted(lines_by_page.keys()):
        page_lines = lines_by_page[page_num]
        if not page_lines:
            continue

        p_w = page_width or max((l.right for l in page_lines), default=2480)
        p_h = page_height or max((l.bottom for l in page_lines), default=3508)
        mid_x = p_w // 2

        # Separate receipt lines from regular invoice lines
        page_tokens = [t for l in page_lines for t in l.tokens]
        rboxes = detect_receipt_regions(page_tokens)

        receipt_lines: list[LogicalLine] = []
        regular_lines: list[LogicalLine] = []

        for l in page_lines:
            is_l_receipt = any(
                rx - 20 <= l.center_x <= rx + rw + 40 and ry - 20 <= l.center_y <= ry + rh + 40
                for (rx, ry, rw, rh) in rboxes
            ) or (
                l.center_x > mid_x
                and l.width <= int(0.40 * p_w)
                and any(re.search(rf"(?<![а-яА-Яa-zA-Z0-9]){re.escape(kw)}(?![а-яА-Яa-zA-Z0-9])", l.text_lower) for kw in RECEIPT_KEYWORDS)
            )
            if is_l_receipt and "фактура" not in l.text_lower:
                receipt_lines.append(l)
            else:
                regular_lines.append(l)

        if receipt_lines:
            r_block = LogicalBlock(lines=receipt_lines, page_number=page_num)
            r_block.block_type = "receipt"
            r_block.zone = "receipt"
            blocks.append(r_block)

        if not regular_lines:
            continue

        line_heights = [line.height for line in regular_lines if line.tokens]
        median_lh = sorted(line_heights)[len(line_heights) // 2] if line_heights else 20
        gap_threshold = max(int(gap_factor * median_lh), 15)

        current_lines: list[LogicalLine] = [regular_lines[0]]

        for i in range(1, len(regular_lines)):
            prev_bottom = regular_lines[i - 1].bottom
            curr_top = regular_lines[i].top
            gap = curr_top - prev_bottom
            if gap > gap_threshold:
                blocks.append(_create_zoned_block(current_lines, page_num, p_w, p_h, mid_x))
                current_lines = [regular_lines[i]]
            else:
                current_lines.append(regular_lines[i])

        if current_lines:
            blocks.append(_create_zoned_block(current_lines, page_num, p_w, p_h, mid_x))

    return blocks


def _match_column_synonym(text: str) -> str | None:
    """Return the semantic column type if *text* matches any column synonym."""
    if not text:
        return None
    text_lower = text.lower().strip()
    if any(ex in text_lower for ex in ["описание на сделката", "място на сделката", "сделката"]):
        return None
    clean = re.sub(r"^[^\w#№%]+|[^\w#№%]+$", "", text_lower)
    if not clean:
        return None

    # VAT rate column header prefix in Bulgarian dot-matrix invoices (ддс%, ддс90, ддсзе, ддс30, etc.)
    if clean.startswith("ддс"):
        return "vat_rate"

    # Exact token match first (highest priority)
    for s, ctype in ALL_COLUMN_SYNONYMS_SORTED:
        if clean == s:
            return ctype

    # Word-boundary regex match for multi-word or compound synonyms
    for s, ctype in ALL_COLUMN_SYNONYMS_SORTED:
        pattern = rf"(?<![а-яА-Яa-zA-Z0-9]){re.escape(s)}(?![а-яА-Яa-zA-Z0-9])"
        if re.search(pattern, text_lower):
            return ctype

    return None


def _is_summary_line(line: LogicalLine) -> bool:
    """Return True if the line text contains summary keywords."""
    text = line.text_lower.strip()
    if any(kw in text for kw in SUMMARY_KEYWORDS):
        return True
    if ("дан" in text or "данъчн" in text) and "основ" in text:
        return True
    if ("сума" in text or "общ" in text or "всичко" in text) and ("плащан" in text or "дължим" in text):
        return True
    if re.search(r'\b(?:ддс|ставка)\b.*?\b(?:20%?|9%?|0%?|2096)\b', text):
        return True
    if re.search(r'\b(?:20|9|0|2096)\s*(?:%|96)?\s*(?:ддс|ставка)', text):
        return True
    if re.search(r'^\s*(?:20|9|0)\s*(?:%|96|%96)\s*(?:ддс|ставка|:)?', text):
        return True
    if re.search(r'\b(?:общо|всичко)\s*(?:нето|ддс|с\s+ддс|за\s+плащане|словом|:|=)\b', text):
        return True
    if re.search(r'^\s*(?:общо|всичко)\s*[:=]?\s*\d', text):
        return True
    return False


def is_transfer_or_header_line(line: LogicalLine) -> bool:
    """Return True if line is a multi-page subtotal transfer line or repeated header."""
    txt = line.text_lower.strip()
    return any(p.search(txt) for p in CONTINUATION_PATTERNS)


def resolve_party_orientation(
    lines: list[LogicalLine],
    page_width: int,
    page_height: int,
) -> tuple[str, str]:
    """Resolve Left column vs Right column party orientation in party band (0.05H..0.45H).

    Returns (left_role, right_role) where role is 'supplier' or 'recipient'.
    """
    party_lines = [
        l for l in lines
        if 0.02 * page_height <= l.top <= 0.45 * page_height
    ]
    mid_x = page_width // 2

    left_supp = 0
    left_recip = 0
    right_supp = 0
    right_recip = 0

    for l in party_lines:
        if "мобилен клиент" in l.text_lower:
            continue
        txt_low = l.text_lower
        if is_dot_matrix_vendor(txt_low):
            if l.center_x >= mid_x:
                right_supp += 5
            else:
                left_supp += 5
        if "каскада" in txt_low or "208380135" in txt_low:
            if l.center_x < mid_x:
                left_recip += 5
            else:
                right_recip += 5

        for t in l.tokens:
            txt = t.text.lower()
            is_supp = is_supplier_keyword(txt)
            is_recip = is_recipient_keyword(txt)
            if t.center_x < mid_x:
                if is_supp:
                    left_supp += 1
                if is_recip:
                    left_recip += 1
            else:
                if is_supp:
                    right_supp += 1
                if is_recip:
                    right_recip += 1

    if left_recip > left_supp or right_supp > right_recip:
        return ("recipient", "supplier")
    return ("supplier", "recipient")



def detect_receipt_regions(
    tokens: list[OcrToken],
) -> list[tuple[int, int, int, int]]:
    """Detect bounding boxes of thermal fiscal receipts stapled onto invoices."""
    receipt_toks = [
        t for t in tokens
        if any(re.search(rf"(?<![а-яА-Яa-zA-Z0-9]){re.escape(kw)}(?![а-яА-Яa-zA-Z0-9])", t.text.lower()) for kw in [
            "фискален бон", "фискална памет", "име на оператор", "фискален", "фискална",
            "касов бон", "оператор", "обменен курс", "курс евро", "в брой евро"
        ])
    ]
    if len(receipt_toks) < 2:
        return []

    min_l = min(t.left for t in receipt_toks)
    min_t = min(t.top for t in receipt_toks)
    max_r = max(t.right for t in receipt_toks)
    max_b = max(t.bottom for t in receipt_toks)
    return [(max(0, min_l - 15), max(0, min_t - 15), max_r - min_l + 30, max_b - min_t + 30)]


def clean_table_crop(img_gray: np.ndarray) -> np.ndarray:
    """Remove table grid lines morphologically so borderless text is recognized cleanly.

    Applies morphological opening with orthogonal rectangular kernels to isolate
    and erase horizontal and vertical grid borders. This resolves line-item OCR
    occlusions caused by thick dot-matrix or thermal grid lines interfering with Tesseract.
    """
    if img_gray is None or not isinstance(img_gray, np.ndarray) or img_gray.size == 0:
        return img_gray
    if img_gray.shape[0] < 20 or img_gray.shape[1] < 40:
        return img_gray

    try:
        thresh = cv2.threshold(img_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        h_len = max(20, min(40, img_gray.shape[1] // 10))
        v_len = max(20, min(40, img_gray.shape[0] // 10))
        h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (h_len, 1))
        h_lines = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, h_kernel)
        v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_len))
        v_lines = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, v_kernel)
        table_lines = cv2.add(h_lines, v_lines)
        clean = img_gray.copy()
        clean[table_lines > 0] = 255
        return clean
    except Exception as exc:
        logger.debug("clean_table_crop failed: %s", exc)
        return img_gray


def build_x_projection_profile(
    tokens: list[OcrToken],
    page_width: int = 2500,
    smoothing_window: int = 25,
    min_density: float = 0.1,
) -> list[TableColumn]:
    """Calculate 1D occupancy histogram along X-coordinates in table region to detect columns.

    Deterministically identifies column gutters and spans from text density peaks,
    facilitating semantic column extraction even in borderless tables.
    """
    valid_tokens = [t for t in tokens if t.text.strip() and t.text.strip() not in {"|", "¦", "||", "/", "\\"}]
    if not valid_tokens:
        return []

    actual_width = max((t.right for t in valid_tokens), default=page_width)
    eff_width = max(page_width, actual_width + 50)
    hist = np.zeros(eff_width, dtype=np.float32)
    for t in valid_tokens:
        l = max(0, min(eff_width - 1, t.left))
        r = max(0, min(eff_width - 1, t.right))
        hist[l:r + 1] += 1.0

    kernel = np.ones(smoothing_window) / float(smoothing_window)
    smoothed = np.convolve(hist, kernel, mode="same")

    active = smoothed > min_density
    diff = np.diff(active.astype(int))
    starts = np.where(diff == 1)[0] + 1
    ends = np.where(diff == -1)[0]
    if active[0]:
        starts = np.r_[0, starts]
    if active[-1]:
        ends = np.r_[ends, len(active) - 1]

    raw_spans = []
    for s, e in zip(starts, ends):
        col_toks = [t for t in valid_tokens if s <= t.center_x <= e]
        if col_toks:
            raw_spans.append((int(s), int(e), col_toks))

    merged_spans = []
    for s, e, toks in raw_spans:
        if merged_spans and s - merged_spans[-1][1] < 20:
            prev_s, prev_e, prev_toks = merged_spans.pop()
            merged_spans.append((prev_s, e, prev_toks + toks))
        else:
            merged_spans.append((s, e, toks))

    columns: list[TableColumn] = []
    for s, e, toks in merged_spans:
        alpha_count = sum(1 for t in toks if re.search(r"[а-яА-Яa-zA-Z]", t.text))
        num_count = sum(1 for t in toks if parse_money(t.text) is not None)
        has_vat = any("%" in t.text or t.text.strip() in {"20.00", "20", "9.00", "9"} for t in toks)

        sem_type = "column"
        if has_vat and (s >= 1800 or e >= 1900):
            sem_type = "vat_rate"
        elif alpha_count > num_count:
            if any(t.text.lower() in {"бр", "бр.", "кг", "кг.", "л", "л."} for t in toks) and (e - s < 250):
                sem_type = "unit_of_measure"
            else:
                sem_type = "description"
        elif num_count > 0:
            if s >= 2100:
                sem_type = "total_price"
            elif s >= 1700:
                sem_type = "unit_price"
            elif s >= 1300:
                sem_type = "quantity"
            elif s < 450:
                sem_type = "article_code"

        columns.append(TableColumn(
            header_text=f"{sem_type}_{s}_{e}",
            semantic_type=sem_type,
            x_left=s,
            x_right=e,
            x_center=(s + e) // 2,
        ))
    return columns


def detect_table_regions(
    lines: list[LogicalLine],
    tokens: list[OcrToken],
) -> list[TableRegion]:
    """Detect line-items tables using multi-line header matching and asymmetric X-projections.

    Supports:
    1. Multi-line headers (split across 1 to 3 adjacent visual lines).
    2. Asymmetric column boundaries (description column extended to unit/quantity anchor).
    3. Multi-page tables (detects table regions on all pages without breaking early).
    """
    if not lines:
        return []

    lines_by_page: dict[int, list[LogicalLine]] = defaultdict(list)
    for line in lines:
        lines_by_page[line.page_number].append(line)

    tables: list[TableRegion] = []
    primary_columns: list[TableColumn] | None = None

    for page_num in sorted(lines_by_page.keys()):
        page_lines = lines_by_page[page_num]
        if not page_lines:
            continue

        page_lh = [l.height for l in page_lines if l.tokens]
        median_lh = sorted(page_lh)[len(page_lh) // 2] if page_lh else 25

        header_found = False
        n_lines = len(page_lines)

        p_h = max(max((l.bottom for l in page_lines), default=3500), 2000)

        for i in range(n_lines):
            for window_size in (1, 2, 3):
                if i + window_size > n_lines:
                    continue
                window_lines = page_lines[i : i + window_size]

                # Window must be located in plausible table header zone
                if window_lines[0].top < int(0.08 * p_h) or window_lines[-1].bottom > int(0.70 * p_h):
                    continue
                if any("сделката" in w_line.text_lower for w_line in window_lines):
                    continue
                if any(any(kw in w_line.text_lower for kw in ["клиент", "телефон", "фактура", "дата дан", "продавач", "получател"]) for w_line in window_lines):
                    continue
                if any(any(kw in w_line.text_lower for kw in ["плащане", "в брой", "по сметка", "банкова сметка"]) for w_line in window_lines):
                    continue

                if window_size > 1:
                    max_interline_gap = max(
                        window_lines[k + 1].top - window_lines[k].bottom
                        for k in range(window_size - 1)
                    )
                    if max_interline_gap > int(2.5 * median_lh):
                        continue

                matched_cols: list[tuple[str, OcrToken]] = []
                for w_line in window_lines:
                    for token in w_line.tokens:
                        ctype = _match_column_synonym(token.text)
                        if ctype:
                            matched_cols.append((ctype, token))

                    w_text = w_line.text_lower
                    for syn, ctype in ALL_COLUMN_SYNONYMS_SORTED:
                        if " " in syn and syn in w_text:
                            if ctype not in [mc[0] for mc in matched_cols]:
                                for token in w_line.tokens:
                                    if token.text.lower() in syn:
                                        matched_cols.append((ctype, token))
                                        break

                unique_types = {mc[0] for mc in matched_cols}
                if len(unique_types) >= 3:
                    logger.info(
                        "Table header zone detected on p.%d at lines %d..%d: columns=%s",
                        page_num, i, i + window_size - 1, ", ".join(sorted(unique_types)),
                    )

                    seen_types: set[str] = set()
                    columns: list[TableColumn] = []
                    for ctype, token in matched_cols:
                        if ctype not in seen_types:
                            seen_types.add(ctype)
                            columns.append(TableColumn(
                                header_text=token.text,
                                semantic_type=ctype,
                                x_center=token.center_x,
                                x_left=token.left,
                                x_right=token.right,
                            ))

                    columns.sort(key=lambda c: c.x_center)

                    for idx_c, col in enumerate(columns):
                        if idx_c == 0:
                            col.x_left = 0
                        else:
                            prev_col = columns[idx_c - 1]
                            if prev_col.semantic_type == "description":
                                split_x = max(prev_col.x_right + 10, col.x_left - 35)
                                prev_col.x_right = split_x
                                col.x_left = split_x
                            else:
                                mid = (prev_col.x_center + col.x_center) // 2
                                prev_col.x_right = mid
                                col.x_left = mid

                    if columns:
                        page_tokens = [t for t in tokens if t.page_number == page_num]
                        columns[-1].x_right = max(
                            (t.right for t in page_tokens), default=columns[-1].x_center + 300,
                        )

                    data_lines: list[LogicalLine] = []
                    start_data_idx = i + window_size
                    for d_line in page_lines[start_data_idx:]:
                        if _is_summary_line(d_line):
                            break
                        if is_transfer_or_header_line(d_line):
                            continue
                        if any(kw in d_line.text_lower for kw in RECEIPT_KEYWORDS):
                            continue
                        data_lines.append(d_line)

                    columns = _cross_validate_table_columns(columns, data_lines)
                    primary_columns = columns

                    if len(data_lines) > 0:
                        tables.append(TableRegion(
                            columns=columns,
                            header_line=window_lines[0],
                            data_lines=data_lines,
                            page_number=page_num,
                        ))
                        header_found = True
                        break

            if header_found:
                break

        if not header_found and primary_columns is not None and len(tables) > 0:
            # Continuation table can only be on consecutive pages following a previous table page
            if page_num != tables[-1].page_number + 1:
                continue

            # A page that contains fiscal receipt blocks or buyer/seller settlement info without a table header
            # is a settlement/closing page, not a continuation table page!
            page_text_all = " ".join(l.text_lower for l in page_lines)
            if any(kw in page_text_all for kw in ["фискален бон", "касов бон", "касова бележка", "касиер", "фп номер", "фискална памет", "регистрация в нап"]):
                continue
            if any(kw in page_text_all for kw in ["получател", "пояучател", "купувач", "продавач"]) and any(kw in page_text_all for kw in ["подпис", "съставил", "клиент", "бон"]):
                continue

            data_lines = []
            for d_line in page_lines:
                if _is_summary_line(d_line):
                    break
                if is_transfer_or_header_line(d_line):
                    continue
                if any(kw in d_line.text_lower for kw in RECEIPT_KEYWORDS):
                    continue
                if any(kw in d_line.text_lower for kw in ["продавач", "получател", "купувач", "доставчик", "телефон", "клиент", "фактура", "дата дан", "еик", "ейк", "ин по ддс", "шосе", "каса", "касиер", "бон", "памет", "фискален"]):
                    continue
                max_bot = max((l.bottom for l in page_lines), default=3000)
                if d_line.top < int(0.12 * max_bot):
                    continue
                data_lines.append(d_line)

            valid_table_rows = 0
            for dl in data_lines:
                cv = _assign_line_to_columns(dl, primary_columns)
                has_desc = bool(cv.get("description"))
                has_num = bool(parse_money(cv.get("quantity", ""))) or \
                          bool(parse_money(cv.get("unit_price", ""))) or \
                          bool(parse_money(cv.get("total_price", "")))
                if has_desc and has_num:
                    valid_table_rows += 1

            if valid_table_rows >= 2 and data_lines:
                logger.info("Continuation table projected on p.%d with %d lines", page_num, len(data_lines))
                tables.append(TableRegion(
                    columns=primary_columns,
                    header_line=page_lines[0],
                    data_lines=data_lines,
                    page_number=page_num,
                ))

    return tables


def _assign_token_to_column(
    token: OcrToken,
    columns: list[TableColumn],
) -> str | None:
    """Assign a token to the best column by bounds containment or horizontal overlap."""
    if not columns:
        return None

    for col in columns:
        if col.x_left <= token.center_x <= col.x_right:
            return col.semantic_type

    best_col: TableColumn | None = None
    best_overlap = 0
    for col in columns:
        h_int = max(0, min(token.right, col.x_right) - max(token.left, col.x_left))
        if h_int > best_overlap:
            best_overlap = h_int
            best_col = col

    if best_col is not None and best_overlap > 0:
        return best_col.semantic_type

    best_col = min(columns, key=lambda c: abs(c.x_center - token.center_x))
    return best_col.semantic_type


def _assign_line_to_columns(
    line: LogicalLine,
    columns: list[TableColumn],
) -> dict[str, str]:
    """Assign all tokens in a line to columns, concatenating text per column."""
    # We store tuples of (text, center_x) to detect overlapping duplicates
    col_texts: dict[str, list[tuple[str, int]]] = {c.semantic_type: [] for c in columns}
    for token in line.tokens:
        col_type = _assign_token_to_column(token, columns)
        if col_type and col_type in col_texts:
            # Deduplicate OCR vs PDF duplicate tokens at the same location
            is_dup = False
            for ex_txt, ex_x in col_texts[col_type]:
                if abs(ex_x - token.center_x) < 40:
                    if abs(ex_x - token.center_x) < 15 or token.text == ex_txt or (len(token.text) > 3 and token.text in ex_txt) or (len(ex_txt) > 3 and ex_txt in token.text):
                        is_dup = True
                        break
            if not is_dup:
                col_texts[col_type].append((token.text, token.center_x))
    return {k: " ".join([t[0] for t in v]) for k, v in col_texts.items() if v}


def _cross_validate_table_columns(
    columns: list[TableColumn],
    data_lines: list[LogicalLine],
) -> list[TableColumn]:
    """Verify and adjust column semantic types using cross-mathematical validation:

    Checks candidate numeric columns against the relationship:
        quantity * unit_price ≈ total_price
    The column satisfying this condition for the majority of rows is definitively
    designated as 'quantity' (rather than article code, index, or packaging).
    """
    if len(columns) < 3 or not data_lines:
        return columns

    total_cols = [c for c in columns if c.semantic_type == "total_price"]
    if not total_cols:
        return columns
    total_col = total_cols[0]

    # Candidate quantity columns: currently quantity, packaging, or numeric candidates
    candidate_qty_cols = [
        c for c in columns
        if c.semantic_type in {"quantity", "packaging"}
        or any(w in c.header_text.lower() for w in ["кол", "к-во", "съд", "разф", "мее", "нек", "бр"])
    ]
    # Candidate price columns: currently unit_price or header mentions "цена"
    candidate_price_cols = [
        c for c in columns
        if c.semantic_type == "unit_price"
        or any(w in c.header_text.lower() for w in ["цена", "ед", "price"])
    ]

    if not candidate_qty_cols or not candidate_price_cols:
        return columns

    best_qty_col: TableColumn | None = None
    best_price_col: TableColumn | None = None
    max_matches = 0

    for q_col in candidate_qty_cols:
        for p_col in candidate_price_cols:
            if q_col == p_col or q_col == total_col or p_col == total_col:
                continue
            matches = 0
            for dl in data_lines:
                cv = _assign_line_to_columns(dl, columns)
                q_txt = cv.get(q_col.semantic_type, "")
                p_txt = cv.get(p_col.semantic_type, "")
                t_txt = cv.get(total_col.semantic_type, "")

                # Reject 5-8 digit integers (SKUs / barcodes / LOTs) from being quantity
                q_clean = re.sub(r'[^\d.,]', '', q_txt)
                if re.fullmatch(r'\d{5,14}', q_clean):
                    continue

                q_val = parse_money(q_txt)
                p_val = parse_money(p_txt)
                t_val = parse_money(t_txt)

                if q_val and p_val and t_val and q_val > Decimal("0") and p_val > Decimal("0") and t_val > Decimal("0"):
                    calc = q_val * p_val
                    # Match if within 0.05 or within 2% or accounting for VAT (1.20)
                    if (
                        abs(calc - t_val) <= Decimal("0.05")
                        or abs(calc * Decimal("1.20") - t_val) <= Decimal("0.05")
                        or abs(calc - t_val) <= max(Decimal("0.05"), t_val * Decimal("0.02"))
                    ):
                        matches += 1

            if matches > max_matches:
                max_matches = matches
                best_qty_col = q_col
                best_price_col = p_col

    if best_qty_col is not None and max_matches >= 2:
        current_qty_cols = [c for c in columns if c.semantic_type == "quantity"]
        if current_qty_cols and current_qty_cols[0] != best_qty_col:
            old_qty_col = current_qty_cols[0]
            if any(w in old_qty_col.header_text.lower() for w in ["съд", "разф", "опак"]):
                old_qty_col.semantic_type = "packaging"
            else:
                old_qty_col.semantic_type = "index"
            best_qty_col.semantic_type = "quantity"
            logger.info(
                "Cross-mathematical validation designated column '%s' as quantity (matches=%d)",
                best_qty_col.header_text, max_matches,
            )
        elif not current_qty_cols:
            best_qty_col.semantic_type = "quantity"
            logger.info(
                "Cross-mathematical validation assigned column '%s' as quantity (matches=%d)",
                best_qty_col.header_text, max_matches,
            )

    return columns


